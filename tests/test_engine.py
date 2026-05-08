"""Parity suite: every engine-test-data segment translated and run against
Snowflake, compared to `flag_engine.is_context_in_segment`.

Needs Snowflake creds — the `conftest` fixtures read `SNOWFLAKE_*` env
vars; CI provides them via secrets. The fixtures batch all rows into a
single INSERT and all predicate evaluations into a single SELECT, so
the per-test function here is just a dict lookup plus a bool compare.
"""

from __future__ import annotations

import pytest
from flag_engine.segments.evaluator import is_context_in_segment

from tests.conftest import (
    SEGMENT_TEST_CASES,
    SegmentEngineTestCase,
    SegmentTestResult,
)

# Cases the SQL translator can't match the engine on; xfail keeps the
# divergence visible without masking a regression elsewhere. Entries are
# file stems (matching `EngineTestCase.name`); add the why inline.
XFAIL_CASE_NAMES = {
    # Engine sorts semver prereleases (1.0.0-rc.2 < 1.0.0-rc.3); the SQL
    # semver-sort-key collapses to major.minor.patch only.
    "test_semver_greater_than_prerelease__should_match",
    "test_semver_less_than_prerelease__should_match",
    # Engine does trait-first dispatch: a row with a trait literally named
    # `$.identity` shadows the JSONPath lookup. Replicating per-row trait
    # fallback in SQL roughly doubles the cost of every wrapped JSONPath
    # condition (Snowflake evaluates both IFF arms), so we accept the
    # divergence on this niche shape (`$.`-prefixed trait names) and let
    # callers fall back to the engine.
    "test_jsonpath_like_trait__existing_jsonpath__should_match_trait",
}


@pytest.mark.parity
@pytest.mark.parametrize(
    "segment_test_case",
    SEGMENT_TEST_CASES,
    ids=[
        f"{segment_test_case['name']}-{segment_test_case['segment_key']}"
        for segment_test_case in SEGMENT_TEST_CASES
    ],
)
def test_translate_segment__engine_test_data_case__matches_engine(
    segment_test_case: SegmentEngineTestCase,
    snowflake_results: dict[tuple[str, str], SegmentTestResult],
) -> None:
    # Given a (case, segment) pair from engine-test-data and the pre-computed
    # SQL match for that pair
    if (case_name := segment_test_case["name"]) in XFAIL_CASE_NAMES:
        pytest.xfail(f"known divergence: {case_name}")

    segment_key = segment_test_case["segment_key"]
    sql_match = snowflake_results[(case_name, segment_key)]["is_match"]

    # When the engine evaluates the same (context, segment) in-memory
    engine_match = is_context_in_segment(
        segment_test_case["context"], segment_test_case["segment_context"]
    )

    # Then the SQL-evaluated and engine-evaluated booleans agree
    assert engine_match == sql_match, (
        f"engine={engine_match} sql={sql_match} for case={case_name} seg={segment_key}"
    )
