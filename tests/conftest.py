"""Pytest fixtures.

Unit tests run anywhere; the parity suite needs a Snowflake account. The
parity fixtures skip automatically if `SNOWFLAKE_ACCOUNT` isn't set, so
running `pytest` with no env vars only runs the unit tests.

Each parity-test session creates a per-run transient `IDENTITIES_PARITY_<uuid>`
table so concurrent CI runs don't step on each other. Schema mirrors
`SnowflakeDialect.SCHEMA_DDL`: 4 typed columns + a `traits` VARIANT.
Table is dropped on teardown.

The parity fixtures are batched to keep Snowflake round-trips down:

  - All test-case identities go into the scratch table in a single
    `INSERT INTO ... VALUES (...), (...), ...` statement.
  - All test-case (segment, identity) pairs are evaluated in a single
    `SELECT ... UNION ALL ...` mega-query, returning a `(case_idx, seg_key)
    -> bool` dict. Per-test parametrised tests then do an in-memory dict
    lookup. Two Snowflake round-trips for the whole suite.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypedDict

import pytest
from flag_engine.context.types import EvaluationContext
from flag_engine.result.types import EvaluationResult

from flagsmith_sql_flag_engine import TranslateContext, translate_segment
from flagsmith_sql_flag_engine.dialects.snowflake import SnowflakeDialect


class EngineTestCase(TypedDict):
    """An engine-test-data fixture file. The `result` field (engine-evaluated
    flag values) is carried through but unused by the parity suite."""

    context: EvaluationContext
    result: EvaluationResult


REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_TEST_DATA = REPO_ROOT / "engine-test-data" / "test_cases"


def _snowflake_creds_present() -> bool:
    return bool(os.environ.get("SNOWFLAKE_ACCOUNT")) and bool(os.environ.get("SNOWFLAKE_USER"))


@pytest.fixture(scope="session")
def snowflake_session() -> Iterator[Any]:
    """Snowpark session keyed off SNOWFLAKE_* env vars. Session-scoped."""
    if not _snowflake_creds_present():
        pytest.skip("SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER not set")
    from snowflake.snowpark import Session

    config: dict[str, str] = {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "role": os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        "database": os.environ.get("SNOWFLAKE_DATABASE", "FS_TEST"),
        "schema": os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC"),
    }
    if pk_path := os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH"):
        config["private_key_file"] = pk_path
    elif password := os.environ.get("SNOWFLAKE_PASSWORD"):
        config["password"] = password
    else:
        pytest.skip("no SNOWFLAKE_PRIVATE_KEY_PATH or SNOWFLAKE_PASSWORD")

    sess = Session.builder.configs(config).create()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture(scope="session")
def parity_table(snowflake_session: Any) -> Iterator[str]:
    """Create a per-run scratch IDENTITIES table mirroring
    `SnowflakeDialect.SCHEMA_DDL`. Returns the fully-qualified name."""
    suffix = uuid.uuid4().hex[:8]
    db = os.environ.get("SNOWFLAKE_DATABASE", "FS_TEST")
    schema = os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC")
    table = f"{db}.{schema}.IDENTITIES_PARITY_{suffix}"
    snowflake_session.sql(
        f"""
        CREATE TRANSIENT TABLE {table} (
            environment_id STRING NOT NULL,
            id NUMBER NOT NULL,
            identifier STRING NOT NULL,
            identity_key STRING NOT NULL,
            traits VARIANT
        )
        """
    ).collect()
    try:
        yield table
    finally:
        snowflake_session.sql(f"DROP TABLE IF EXISTS {table}").collect()


def load_test_cases() -> list[EngineTestCase]:
    """Load all engine-test-data cases. Returns empty list if the submodule
    hasn't been initialised — the parity tests will skip in that case."""
    if not ENGINE_TEST_DATA.exists():
        return []
    cases: list[EngineTestCase] = []
    for path in sorted(ENGINE_TEST_DATA.glob("*.json")):
        with open(path) as f:
            cases.append(json.load(f))
    return cases


def parity_env_key(case_idx: int, original: str) -> str:
    """Per-case unique env key. Prefixed with `parity-{idx}` so concurrent
    cases (or cases that share an `environment.key` in the source data)
    don't see each other's identities."""
    return f"parity-{case_idx}-{original}"


def _q(s: str) -> str:
    return s.replace("'", "''")


@pytest.fixture(scope="session")
def loaded_cases(snowflake_session: Any, parity_table: str) -> Iterator[list[EngineTestCase]]:
    """Load every test case's identity into the scratch IDENTITIES table
    in a single multi-row INSERT. Returns the list of cases (with
    overridden `environment.key` so engine and SQL agree on what env to
    filter by). One Snowflake round-trip for all 102 cases.
    """
    cases = load_test_cases()
    if not cases:
        pytest.skip("engine-test-data submodule not initialised")
    overridden: list[EngineTestCase] = []
    selects: list[str] = []
    for i, case in enumerate(cases):
        case = copy.deepcopy(case)
        ctx = case["context"]
        env = ctx["environment"]
        env["key"] = parity_env_key(i, env.get("key", ""))
        env_id = _q(env["key"])

        ident = ctx.get("identity") or {}
        identifier = _q(ident.get("identifier") or "")
        identity_key = _q(ident.get("key") or "")
        identity_id = i + 1
        traits = ident.get("traits") or {}
        if traits:
            traits_lit = f"PARSE_JSON('{_q(json.dumps(traits))}')"
        else:
            traits_lit = "NULL"
        selects.append(
            f"SELECT '{env_id}', {identity_id}, '{identifier}', '{identity_key}', {traits_lit}"
        )
        overridden.append(case)

    snowflake_session.sql(
        f"INSERT INTO {parity_table} (environment_id, id, identifier, identity_key, traits) "
        + "\nUNION ALL\n".join(selects)
    ).collect()
    yield overridden


@pytest.fixture(scope="session")
def parity_results(
    snowflake_session: Any,
    parity_table: str,
    loaded_cases: list[EngineTestCase],
) -> dict[tuple[int, str], bool | None]:
    """Run every (case, segment) pair's translated SQL in one mega-query
    and return a `(case_idx, seg_key) -> bool | None` dict. `None` means
    the segment uses an operator the translator can't handle (test will
    pytest.skip on that key).

    One Snowflake round-trip for all 510 pairs.
    """
    pairs: list[tuple[int, str, str | None, str]] = []
    select_clauses: list[str] = []
    for case_idx, case in enumerate(loaded_cases):
        eval_ctx = case["context"]
        env_key = eval_ctx["environment"]["key"]
        for seg_key, segment in (eval_ctx.get("segments") or {}).items():
            tr_ctx = TranslateContext(evaluation_context=eval_ctx, dialect=SnowflakeDialect())
            sql = translate_segment(segment, tr_ctx)
            pairs.append((case_idx, seg_key, sql, env_key))

    for i, (_case_idx, _seg_key, sql, env_key) in enumerate(pairs):
        if sql is None:
            continue
        env_lit = _q(env_key)
        select_clauses.append(
            f"SELECT {i} AS pair_id, "
            f"EXISTS (SELECT 1 FROM {parity_table} i "
            f"WHERE i.environment_id = '{env_lit}' AND ({sql})) AS m"
        )

    results: dict[tuple[int, str], bool | None] = {}
    if select_clauses:
        rows = snowflake_session.sql("\nUNION ALL\n".join(select_clauses)).collect()
        for row in rows:
            i = int(row["PAIR_ID"])
            case_idx, seg_key, _sql, _env = pairs[i]
            results[(case_idx, seg_key)] = bool(row["M"])
    # Fill in untranslatable pairs as None so test parametrisation can skip.
    for case_idx, seg_key, sql, _env in pairs:
        if sql is None:
            results[(case_idx, seg_key)] = None
    return results
