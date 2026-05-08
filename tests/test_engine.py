"""Engine-parity suite: every engine-test-data segment translated and run
through each registered `DialectTestHarness`, compared to
`flag_engine.is_context_in_segment`.

Each harness needs its own SQL engine reachable; the fixtures in
`conftest` raise on missing creds rather than silently skip. Per-harness
xfails live on the harness itself (see `xfail_case_names`).
"""

from __future__ import annotations

import pytest
from flag_engine.segments.evaluator import is_context_in_segment

from tests.conftest import (
    SEGMENT_TEST_CASES,
    SegmentEngineTestCase,
    SegmentTestResult,
)
from tests.harnesses import DialectTestHarness


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
    harness: DialectTestHarness,
    harness_results: dict[tuple[str, str], SegmentTestResult],
) -> None:
    # Given a (case, segment) pair from engine-test-data and the pre-computed
    # SQL match for that pair on the active harness
    case_name = segment_test_case["name"]
    if case_name in harness.xfail_case_names:
        pytest.xfail(f"known {harness.name} divergence: {case_name}")

    segment_key = segment_test_case["segment_key"]
    sql_match = harness_results[(case_name, segment_key)]["is_match"]

    # When the engine evaluates the same (context, segment) in-memory
    engine_match = is_context_in_segment(
        segment_test_case["context"], segment_test_case["segment_context"]
    )

    # Then the SQL-evaluated and engine-evaluated booleans agree
    assert engine_match == sql_match, (
        f"engine={engine_match} sql={sql_match} for "
        f"harness={harness.name} case={case_name} seg={segment_key}"
    )
