"""Unit tests for `ClickHouseDialect` SQL fragments not exercised by the
engine-parity suite. No ClickHouse required.

The engine-test-data dataset has no trait-bound `CONTAINS` cases, so the
dialect's `position` is the one method the parity run can't reach.
"""

from flag_engine.context.types import EvaluationContext, SegmentContext

from flagsmith_sql_flag_engine import TranslateContext, translate_segment
from flagsmith_sql_flag_engine.dialects.clickhouse import ClickHouseDialect


def test_translate_segment__contains_on_trait__emits_clickhouse_position() -> None:
    # Given a CONTAINS condition on a trait, with the ClickHouse dialect
    seg: SegmentContext = {
        "key": "ch1",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "CONTAINS", "property": "plan", "value": "growth"}],
            }
        ],
    }
    eval_ctx: EvaluationContext = {
        "environment": {"key": "e", "name": "Test"},
    }
    ctx = TranslateContext(evaluation_context=eval_ctx, dialect=ClickHouseDialect())

    # When we translate the segment
    sql = translate_segment(seg, ctx)

    # Then the predicate uses ClickHouse's `position(haystack, needle)` —
    # note the argument order is the inverse of Snowflake's
    # `POSITION(needle, haystack)`.
    assert sql is not None
    assert "position(toString(" in sql
    assert ", 'growth') > 0" in sql


def test_translate_segment__regex_inline__emits_anchored_match_with_escaped_literal() -> None:
    # Given an RE2-safe REGEX on a trait and no binder (the default inline path)
    seg: SegmentContext = {
        "key": "ch2",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "REGEX", "property": "email", "value": r"[a-z]+@example\.com"}
                ],
            }
        ],
    }
    eval_ctx: EvaluationContext = {"environment": {"key": "e", "name": "Test"}}
    ctx = TranslateContext(evaluation_context=eval_ctx, dialect=ClickHouseDialect())

    # When we translate the segment
    sql = translate_segment(seg, ctx)

    # Then the pattern is inlined as an anchored, backslash-doubled `match`
    # literal — the branch a binder would replace with a bound parameter
    assert sql is not None
    assert r"match(ifNull(toString(" in sql
    assert r"'^([a-z]+@example\\.com)')" in sql
