"""Pytest fixtures.

Unit tests run anywhere. The engine-parity suite drives a
`DialectTestHarness` (see `tests/harnesses`) — one harness per SQL
engine, registered in `HARNESSES`. Each harness owns its own session,
scratch-table DDL, and batched INSERT / SELECT shapes.

Per harness session, the fixtures:

  - load every case-with-identity into the harness's scratch table in
    one batched INSERT (`harness_identity_table`);
  - translate each (case, segment) pair against the harness's dialect
    and ask the harness to evaluate them all in one batched SELECT
    (`harness_results`).

Parametrised tests do an in-memory dict lookup against that result.
"""

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypedDict, cast

import json5
import pytest
from flag_engine.context.types import EvaluationContext, IdentityContext, SegmentContext
from flag_engine.result.types import EvaluationResult

from flagsmith_sql_flag_engine import TranslateContext, translate_segment
from tests.harnesses import (
    HARNESSES,
    DialectTestHarness,
    EvaluationCase,
    IdentityRow,
)


class EngineTestCase(TypedDict):
    """An engine-test-data fixture file. The `result` field (engine-evaluated
    flag values) is carried through but unused by the engine-parity suite."""

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


# ASCII Unit Separator. Used to pack `(case_name, segment_key)` into a
# single string column on the engine-parity SELECT and split it back at
# row-iteration time. Picked over `:` (or any printable character) so a
# future case_name or segment_key containing punctuation can't collide.
_PAIR_SEP = "\x1f"


@pytest.fixture(scope="session", params=HARNESSES, ids=lambda h: h.name)
def harness(request: pytest.FixtureRequest) -> DialectTestHarness:
    return cast(DialectTestHarness, request.param)


@pytest.fixture(scope="session")
def harness_session(harness: DialectTestHarness) -> Iterator[Any]:
    with harness.session() as sess:
        yield sess


@pytest.fixture(scope="session")
def harness_identities() -> list[EngineTestCase]:
    """Deep-copied cases with `environment.key` suffixed for cross-case
    uniqueness. Same shape across harnesses (the suffixing is dialect-
    agnostic), so the fixture is computed once per pytest session."""
    overridden: list[EngineTestCase] = []
    for identity_id, case in enumerate(TEST_CASES, start=1):
        case = copy.deepcopy(case)
        case["context"]["environment"]["key"] += str(identity_id)
        overridden.append(case)
    return overridden


@pytest.fixture(scope="session")
def harness_identity_table(
    harness: DialectTestHarness,
    harness_session: Any,
    harness_identities: list[EngineTestCase],
) -> str:
    """Per-harness scratch IDENTITIES table loaded with one row per case-
    with-identity. Cases without an identity get no row — their segments
    compile to row-independent SQL, so the empty result still gives the
    right answer."""
    rows: list[IdentityRow] = []
    for identity_id, case in enumerate(harness_identities, start=1):
        ctx = case["context"]
        identity: IdentityContext | None = ctx.get("identity")
        if not identity:
            continue
        traits = identity.get("traits")
        rows.append(
            IdentityRow(
                environment_id=ctx["environment"]["key"],
                id=identity_id,
                identifier=identity.get("identifier") or "",
                identity_key=identity.get("key") or "",
                traits_json=json.dumps(traits) if traits else None,
            )
        )
    return harness.setup_identities(harness_session, rows)


@pytest.fixture(scope="session")
def harness_results(
    harness: DialectTestHarness,
    harness_session: Any,
    harness_identity_table: str,
    harness_identities: list[EngineTestCase],
) -> dict[tuple[str, str], SegmentTestResult]:
    """Run every (case, segment) pair's translated SQL through the harness
    in one batched query.

    Returns a `(case_name, segment_key) -> SegmentTestResult` dict. Every
    case in the dataset compiles today; cases that need to fall back to
    the engine are listed in the harness's `xfail_case_names` rather than
    carrying a third state on the way through.
    """
    cases: list[EvaluationCase] = []
    for case in harness_identities:
        ctx = case["context"]
        env_key = ctx["environment"]["key"]
        for segment_key, segment in (ctx.get("segments") or {}).items():
            translate_ctx = TranslateContext(
                evaluation_context=ctx,
                dialect=harness.dialect,
            )
            sql = translate_segment(segment, translate_ctx)
            assert sql is not None, (
                f"case {case['name']} seg {segment_key} unsupported on {harness.name} — "
                "either fix the translator or add the case name to the harness's "
                "xfail_case_names"
            )
            cases.append(
                EvaluationCase(
                    pair_id=case["name"] + _PAIR_SEP + segment_key,
                    environment_key=env_key,
                    predicate_sql=sql,
                )
            )

    raw = harness.evaluate(harness_session, harness_identity_table, cases)
    results: dict[tuple[str, str], SegmentTestResult] = {}
    for pair_id, is_match in raw.items():
        case_name, segment_key = pair_id.split(_PAIR_SEP, 1)
        results[(case_name, segment_key)] = SegmentTestResult(
            test_case_name=case_name,
            segment_key=segment_key,
            is_match=is_match,
        )
    return results
