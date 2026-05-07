"""Parity suite: every engine-test-data case translated and run against
Snowflake, compared to flag_engine.is_context_in_segment.

Skipped if SNOWFLAKE_* env vars aren't set, so `pytest` without creds
runs the unit suite only. CI sets the creds via secrets and runs this
suite as a separate job.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

import pytest
from flag_engine.context.types import EnvironmentContext
from flag_engine.segments.evaluator import is_context_in_segment

from flagsmith_sql_flag_engine import TranslateContext, translate_segment
from tests.conftest import load_test_cases, parity_env_key


@pytest.fixture(scope="session")
def loaded_cases(snowflake_session: Any, parity_table: str) -> Iterator[list[dict]]:
    """Load every test case's identity into the scratch IDENTITIES table
    with traits as a VARIANT (PARSE_JSON-encoded). Returns the list of
    cases (with overridden `environment.key` so engine and SQL agree on
    what env to filter by).
    """
    cases = load_test_cases()
    if not cases:
        pytest.skip("engine-test-data submodule not initialised")
    out: list[dict] = []
    for i, case in enumerate(cases):
        case = copy.deepcopy(case)
        ctx = case["context"]
        env = ctx.get("environment") or {}
        original_key = env.get("key", "")
        env["key"] = parity_env_key(i, original_key)
        env_id = env["key"].replace("'", "''")

        ident = ctx.get("identity") or {}
        identifier = (ident.get("identifier") or "").replace("'", "''")
        identity_key = (ident.get("key") or "").replace("'", "''")
        identity_id = i + 1
        traits = ident.get("traits") or {}
        if traits:
            traits_json = json.dumps(traits).replace("'", "''")
            traits_lit = f"PARSE_JSON('{traits_json}')"
        else:
            traits_lit = "NULL"
        snowflake_session.sql(
            f"""
            INSERT INTO {parity_table} (environment_id, id, identifier, identity_key, traits)
            SELECT '{env_id}', {identity_id}, '{identifier}', '{identity_key}', {traits_lit}
            """
        ).collect()
        out.append(case)
    yield out


def _all_case_segments() -> list[tuple[int, str]]:
    """Flatten cases × segments for parametrisation. Read at collection time."""
    cases = load_test_cases()
    out: list[tuple[int, str]] = []
    for i, case in enumerate(cases):
        for seg_key in case.get("context", {}).get("segments") or {}:
            out.append((i, seg_key))
    return out


@pytest.mark.parity
@pytest.mark.parametrize(
    "case_idx, seg_key",
    _all_case_segments(),
    ids=[f"case{i}-seg{k}" for i, k in _all_case_segments()],
)
def test_segment_matches_engine(
    case_idx: int,
    seg_key: str,
    snowflake_session: Any,
    loaded_cases: list[dict],
    parity_table: str,
) -> None:
    case = loaded_cases[case_idx]
    eval_ctx = case["context"]
    env: EnvironmentContext = eval_ctx["environment"]
    segment = eval_ctx["segments"][seg_key]

    engine_match = is_context_in_segment(eval_ctx, segment)

    tr_ctx = TranslateContext(environment=env)
    sql = translate_segment(segment, tr_ctx)
    if sql is None:
        pytest.skip(f"segment uses untranslatable operator (case {case_idx} seg {seg_key})")
    env_id_lit = env["key"].replace("'", "''")
    rows = snowflake_session.sql(
        f"""
        SELECT EXISTS(
          SELECT 1 FROM {parity_table} i
          WHERE i.environment_id = '{env_id_lit}' AND ({sql})
        ) AS m
        """
    ).collect()
    sql_match = bool(rows[0]["M"])
    assert engine_match == sql_match, (
        f"engine={engine_match} sql={sql_match} for case={case_idx} seg={seg_key}"
    )
