"""Parity suite: every engine-test-data case translated and run against
Snowflake, compared to flag_engine.is_context_in_segment.

Skipped if SNOWFLAKE_* env vars aren't set. CI sets the creds via secrets
and runs this suite as part of `make test`.

The Snowflake round-trips happen in `conftest.py`'s session-scoped
fixtures: one INSERT for all 102 cases, one SELECT mega-query for all
510 pair evaluations. The per-test function below is just a dict lookup
against the pre-computed `parity_results` and a comparison against the
engine's in-memory result.
"""

from __future__ import annotations

import pytest
from flag_engine.segments.evaluator import is_context_in_segment

from tests.conftest import load_test_cases


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
def test_translate_segment__engine_test_data_case__matches_engine(
    case_idx: int,
    seg_key: str,
    loaded_cases: list[dict],
    parity_results: dict[tuple[int, str], bool | None],
) -> None:
    # Given a (case, segment) pair from engine-test-data and the pre-computed
    # batched parity_results dict mapping each pair to its SQL-evaluated bool
    case = loaded_cases[case_idx]
    eval_ctx = case["context"]
    segment = eval_ctx["segments"][seg_key]
    sql_match = parity_results[(case_idx, seg_key)]
    if sql_match is None:
        pytest.skip(f"segment uses untranslatable operator (case {case_idx} seg {seg_key})")

    # When the engine evaluates the same (context, segment) in-memory
    engine_match = is_context_in_segment(eval_ctx, segment)

    # Then the SQL-evaluated and engine-evaluated booleans agree
    assert engine_match == sql_match, (
        f"engine={engine_match} sql={sql_match} for case={case_idx} seg={seg_key}"
    )
