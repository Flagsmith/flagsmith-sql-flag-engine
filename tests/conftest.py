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

import copy
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

import json5
import pytest
from flag_engine.context.types import EvaluationContext, IdentityContext, SegmentContext
from flag_engine.result.types import EvaluationResult
from snowflake.snowpark import Session

from flagsmith_sql_flag_engine import TranslateContext, translate_segment
from flagsmith_sql_flag_engine.dialects.snowflake import SnowflakeDialect
from flagsmith_sql_flag_engine.utils import escape_string


class EngineTestCase(TypedDict):
    """An engine-test-data fixture file. The `result` field (engine-evaluated
    flag values) is carried through but unused by the parity suite."""

    name: str
    context: EvaluationContext
    result: EvaluationResult


class SegmentEngineTestCase(EngineTestCase):
    segment_key: str
    segment_context: SegmentContext


class SegmentTestResult(TypedDict):
    """A match result for a given segment."""

    test_case_name: str
    segment_key: str
    is_match: bool


REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_TEST_DATA = REPO_ROOT / "engine-test-data" / "test_cases"
TEST_CASE_PATHS: list[Path] = sorted(
    [*ENGINE_TEST_DATA.glob("*.json"), *ENGINE_TEST_DATA.glob("*.jsonc")]
)
TEST_CASES: list[EngineTestCase] = [
    {
        "name": p.stem,
        "context": (raw := json5.loads(p.read_text()))["context"],
        "result": raw["result"],
    }
    for p in TEST_CASE_PATHS
]
SEGMENT_TEST_CASES: list[SegmentEngineTestCase] = [
    {
        **test_case,
        "segment_key": segment_key,
        "segment_context": segment_context,
    }
    for test_case in TEST_CASES
    for segment_key, segment_context in (test_case["context"].get("segments") or {}).items()
]


@pytest.fixture(scope="session")
def snowflake_session() -> Iterator[Session]:
    """Snowpark session keyed off SNOWFLAKE_* env vars. Session-scoped."""
    config: dict[str, str] = {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "role": os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        "database": os.environ.get("SNOWFLAKE_DATABASE", "FS_TEST"),
        "schema": os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC"),
        "private_key_file": os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"],
    }
    sess = Session.builder.configs(config).create()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture(scope="session")
def snowflake_identity_table(snowflake_session: Session) -> str:
    """A scratch IDENTITIES table mirroring
    `SnowflakeDialect.SCHEMA_DDL`. Returns the fully-qualified name."""
    suffix = uuid.uuid4().hex[:8]
    db = os.environ.get("SNOWFLAKE_DATABASE", "FS_TEST")
    schema = os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC")
    table = f"{db}.{schema}.IDENTITIES_PARITY_{suffix}"
    snowflake_session.sql(
        f"""
        CREATE TEMPORARY TABLE {table} (
            environment_id STRING NOT NULL,
            id NUMBER NOT NULL,
            identifier STRING NOT NULL,
            identity_key STRING NOT NULL,
            traits VARIANT
        )
        """
    ).collect()
    return table


def _q(s: str) -> str:
    # Snowflake string literals process `\` as an escape, so JSON traits with
    # `\uXXXX` or `\"` would lose their backslash before reaching PARSE_JSON.
    # Double the backslashes here; the single-quote doubling is the SQL-
    # standard escape that `escape_string` already handles.
    return escape_string(s.replace("\\", "\\\\"))


# ASCII Unit Separator. Used to pack `(case_name, segment_key)` into a single
# string column on the parity SELECT and split it back at row-iteration time.
# Picked over `:` (or any printable character) so a future case_name or
# segment_key containing punctuation can't collide with the separator.
_PAIR_SEP = "\x1f"


@pytest.fixture(scope="session")
def snowflake_identities(
    snowflake_session: Session,
    snowflake_identity_table: str,
) -> list[EngineTestCase]:
    """Load every test case's identity in a single multi-row INSERT.

    Returns the list of cases with environment keys modified for uniqueness.
    """
    overridden: list[EngineTestCase] = []
    selects: list[str] = []

    for identity_id, case in enumerate(TEST_CASES, start=1):
        case = copy.deepcopy(case)
        evaluation_context = case["context"]

        # Make every environment unique so per-case rows don't collide
        # when source cases share an `environment.key`.
        env = evaluation_context["environment"]
        env["key"] += str(identity_id)
        overridden.append(case)

        # Cases without an identity have no row in IDENTITIES; the SQL
        # we emit for their segments is row-independent (constant FALSE
        # / TRUE via `_engine_static_verdict` or the identity-object
        # fallback), so a missing row gives the right answer.
        identity_context: IdentityContext | None = evaluation_context.get("identity")
        if not identity_context:
            continue

        env_id = _q(env["key"])
        identifier = _q(identity_context.get("identifier") or "")
        identity_key = _q(identity_context.get("key") or "")

        if traits := identity_context.get("traits"):
            traits_literal = f"PARSE_JSON('{_q(json.dumps(traits))}')"
        else:
            traits_literal = "NULL"
        selects.append(
            f"SELECT '{env_id}', {identity_id}, '{identifier}', '{identity_key}', {traits_literal}"
        )

    snowflake_session.sql(
        f"INSERT INTO {snowflake_identity_table} "
        "(environment_id, id, identifier, identity_key, traits) " + "\nUNION ALL\n".join(selects)
    ).collect()

    return overridden


@pytest.fixture(scope="session")
def snowflake_results(
    snowflake_session: Session,
    snowflake_identity_table: str,
    snowflake_identities: list[EngineTestCase],
) -> dict[tuple[str, str], SegmentTestResult]:
    """Run every (case, segment) pair's translated SQL in one mega-query
    and return a `(case_idx, seg_key) -> bool` dict. Every case in the
    dataset compiles today (cases that need to fall back to the engine
    are listed in `XFAIL_CASE_FILENAMES`), so we don't carry None as a
    third state.

    One Snowflake round-trip for all 510 pairs.
    """
    select_clauses: list[str] = []
    results: dict[tuple[str, str], SegmentTestResult] = {}

    for test_case in snowflake_identities:
        evaluation_context = test_case["context"]
        environment_key = evaluation_context["environment"]["key"]

        for segment_key, segment in (evaluation_context.get("segments") or {}).items():
            translate_context = TranslateContext(
                evaluation_context=evaluation_context,
                dialect=SnowflakeDialect(),
            )
            sql = translate_segment(segment, translate_context)
            assert sql is not None, (
                f"case {test_case['name']} seg {segment_key} unsupported — "
                "either fix the translator or xfail the case by filename"
            )
            pair_id = _q(test_case["name"] + _PAIR_SEP + segment_key)
            select_clauses.append(
                f"SELECT '{pair_id}' AS test_case_name_segment_key, "
                f"EXISTS (SELECT 1 FROM {snowflake_identity_table} i "
                f"WHERE i.environment_id = '{_q(environment_key)}' AND ({sql})) AS m"
            )

    rows = snowflake_session.sql("\nUNION ALL\n".join(select_clauses)).collect()

    for row in rows:
        test_case_name, segment_key = row["TEST_CASE_NAME_SEGMENT_KEY"].split(_PAIR_SEP, 1)
        results[(test_case_name, segment_key)] = SegmentTestResult(
            test_case_name=test_case_name,
            segment_key=segment_key,
            is_match=bool(row["M"]),
        )

    return results
