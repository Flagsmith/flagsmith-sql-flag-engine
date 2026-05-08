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

from tests.conftest import EngineTestCase, case_filename_at, load_test_cases

# Cases the SQL translator can't match the engine on; tagged xfail so a
# regression elsewhere doesn't get masked. If you're adding to this list,
# put the why next to the filename.
XFAIL_CASE_FILENAMES = {
    # Engine sorts semver prereleases (1.0.0-rc.2 < 1.0.0-rc.3); the SQL
    # semver-sort-key collapses to major.minor.patch only.
    "test_semver_greater_than_prerelease__should_match.jsonc",
    "test_semver_less_than_prerelease__should_match.jsonc",
    # Engine does trait-first dispatch: a row with a trait literally named
    # `$.identity` shadows the JSONPath lookup. Replicating per-row trait
    # fallback in SQL roughly doubles the cost of every wrapped JSONPath
    # condition (Snowflake evaluates both IFF arms), so we accept the
    # divergence on this niche shape (`$.`-prefixed trait names) and let
    # callers fall back to the engine.
    "test_jsonpath_like_trait__existing_jsonpath__should_match_trait.jsonc",
}


def _all_case_segments() -> list[tuple[int, str]]:
    """Flatten cases × segments for parametrisation. Read at collection time."""
    cases = load_test_cases()
    out: list[tuple[int, str]] = []
    for i, case in enumerate(cases):
        for seg_key in case["context"].get("segments") or {}:
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
    loaded_cases: list[EngineTestCase],
    parity_results: dict[tuple[int, str], bool | None],
) -> None:
    # Given a (case, segment) pair from engine-test-data and the pre-computed
    # batched parity_results dict mapping each pair to its SQL-evaluated bool
    case = loaded_cases[case_idx]
    eval_ctx = case["context"]
    segment = eval_ctx["segments"][seg_key]
    sql_match = parity_results[(case_idx, seg_key)]
    filename = case_filename_at(case_idx)
    if filename in XFAIL_CASE_FILENAMES:
        pytest.xfail(f"known divergence: {filename}")
    # `sql_match is None` means translate_segment couldn't compile the
    # segment to SQL (an unsupported operator, or a regex with backref/
    # lookaround). No engine-test-data case hits that today; if you add a
    # new case that does, list it in XFAIL_CASE_FILENAMES with a why.
    assert sql_match is not None, f"segment {seg_key} of {filename} compiled to None"

    # When the engine evaluates the same (context, segment) in-memory
    engine_match = is_context_in_segment(eval_ctx, segment)

    # Then the SQL-evaluated and engine-evaluated booleans agree
    assert engine_match == sql_match, (
        f"engine={engine_match} sql={sql_match} for case={case_idx} seg={seg_key}"
    )
