import re
from collections.abc import Callable
from typing import cast

import pytest
from flag_engine.context.types import EvaluationContext, SegmentContext

from flagsmith_sql_flag_engine import (
    Binder,
    PyformatParamStyle,
    TranslateContext,
    translate_segment,
)
from flagsmith_sql_flag_engine.binder import (
    ClickHouseServerParamStyle,
)
from flagsmith_sql_flag_engine.dialects.clickhouse import ClickHouseDialect

MakeContextFixture = Callable[[Binder | None], TranslateContext]
MakeSegmentFixture = Callable[[str, str, object], SegmentContext]


@pytest.fixture
def make_ctx() -> MakeContextFixture:
    """Factory for a ClickHouse `TranslateContext` with the given binder."""

    def _make(binder: Binder | None) -> TranslateContext:
        eval_ctx: EvaluationContext = {"environment": {"key": "e", "name": "Test"}}
        return TranslateContext(
            evaluation_context=eval_ctx,
            dialect=ClickHouseDialect(),
            binder=binder,
        )

    return _make


@pytest.fixture
def make_segment() -> MakeSegmentFixture:
    """Factory for a single-condition segment over one `ALL` rule."""

    def _make(operator: str, prop: str, value: object) -> SegmentContext:
        return cast(
            SegmentContext,
            {
                "key": "1",
                "name": "s",
                "rules": [
                    {
                        "type": "ALL",
                        "conditions": [{"operator": operator, "property": prop, "value": value}],
                    }
                ],
            },
        )

    return _make


def test_binder__pyformat_style__mints_sequential_placeholders_and_records_values() -> None:
    # Given
    binder = Binder(PyformatParamStyle())

    # When
    first = binder.add("growth")
    second = binder.add("scale")

    # Then
    assert first == "%(p0)s"
    assert second == "%(p1)s"
    assert binder.params == {"p0": "growth", "p1": "scale"}


def test_binder__clickhouse_server_style__mints_typed_placeholders() -> None:
    # Given
    binder = Binder(ClickHouseServerParamStyle())

    # When
    placeholder = binder.add("growth")

    # Then
    assert placeholder == "{p0:String}"
    assert binder.params == {"p0": "growth"}


def test_binder__prefix__namespaces_parameter_names() -> None:
    # Given
    binder_a = Binder(PyformatParamStyle(), prefix="s13_")
    binder_b = Binder(PyformatParamStyle(), prefix="s14_")

    # When
    a = binder_a.add("x")
    b = binder_b.add("y")

    # Then
    assert a == "%(s13_p0)s"
    assert b == "%(s14_p0)s"
    assert binder_a.params.keys().isdisjoint(binder_b.params.keys())


def test_binder__value_with_percent__stored_verbatim() -> None:
    # Given
    # a value containing a `%`
    value = "[a-z%]+@example.com"
    binder = Binder(PyformatParamStyle())

    # When
    placeholder = binder.add(value)

    # Then
    assert placeholder == "%(p0)s"
    assert binder.params == {"p0": "[a-z%]+@example.com"}


def test_translate_segment__equal_with_binder__binds_operand(
    make_segment: MakeSegmentFixture,
    make_ctx: MakeContextFixture,
) -> None:
    # Given
    binder = Binder(PyformatParamStyle())

    # When
    sql = translate_segment(make_segment("EQUAL", "plan", "growth"), make_ctx(binder))

    # Then
    assert sql is not None
    assert "toString(i.traits.`plan`) = %(p0)s" in sql
    assert "'growth'" not in sql
    assert binder.params == {"p0": "growth"}


def test_translate_segment__in_with_binder__binds_each_item(
    make_segment: MakeSegmentFixture,
    make_ctx: MakeContextFixture,
) -> None:
    # Given
    binder = Binder(PyformatParamStyle())

    # When
    sql = translate_segment(make_segment("IN", "country", "GB,US,DE"), make_ctx(binder))

    # Then
    assert sql is not None
    assert "IN (%(p0)s,%(p1)s,%(p2)s)" in sql
    assert binder.params == {"p0": "GB", "p1": "US", "p2": "DE"}


def test_translate_segment__contains_with_binder__binds_needle(
    make_segment: MakeSegmentFixture,
    make_ctx: MakeContextFixture,
) -> None:
    # Given
    binder = Binder(PyformatParamStyle())

    # When
    sql = translate_segment(make_segment("CONTAINS", "country", "G"), make_ctx(binder))

    # Then
    assert sql is not None
    assert "%(p0)s) > 0" in sql
    assert binder.params == {"p0": "G"}


def test_translate_segment__not_equal_trait_with_binder__binds_string_operand(
    make_segment: MakeSegmentFixture,
    make_ctx: MakeContextFixture,
) -> None:
    # Given
    binder = Binder(PyformatParamStyle())

    # When
    sql = translate_segment(make_segment("NOT_EQUAL", "plan", "growth"), make_ctx(binder))

    # Then
    assert sql is not None
    assert "%(p0)s" in sql
    assert "'growth'" not in sql
    assert binder.params == {"p0": "growth"}


def test_translate_segment__semver_with_binder__binds_bare_version(
    make_segment: MakeSegmentFixture,
    make_ctx: MakeContextFixture,
) -> None:
    # Given
    binder = Binder(PyformatParamStyle())

    # When
    sql = translate_segment(make_segment("EQUAL", "version", "1.2.3:semver"), make_ctx(binder))

    # Then
    assert sql is not None
    assert "%(p0)s" in sql
    assert binder.params == {"p0": "1.2.3"}


def test_translate_segment__percentage_split_with_binder__binds_segment_key_salt(
    make_ctx: MakeContextFixture,
) -> None:
    # Given
    binder = Binder(PyformatParamStyle())
    seg: SegmentContext = {
        "key": "cohort-42",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "PERCENTAGE_SPLIT", "property": "", "value": "50"}],
            }
        ],
    }

    # When
    sql = translate_segment(seg, make_ctx(binder))

    # Then
    assert sql is not None
    assert "%(p0)s" in sql
    assert "<= 50.0" in sql
    assert binder.params == {"p0": "cohort-42"}


def test_translate_segment__prefix__namespaces_bound_names(
    make_segment: MakeSegmentFixture,
    make_ctx: MakeContextFixture,
) -> None:
    # Given
    binder = Binder(PyformatParamStyle(), prefix="s13_")

    # When
    sql = translate_segment(make_segment("EQUAL", "plan", "growth"), make_ctx(binder))

    # Then
    assert sql is not None
    assert "%(s13_p0)s" in sql
    assert binder.params == {"s13_p0": "growth"}


def test_translate_segment__regex_with_percent__binder_survives_pyformat_substitution(
    make_segment: MakeSegmentFixture, make_ctx: MakeContextFixture
) -> None:
    # Given
    binder = Binder(PyformatParamStyle())
    seg = make_segment("REGEX", "email", r"[a-z%]+@example\.com")

    # When
    param_sql = translate_segment(seg, make_ctx(binder))
    assert param_sql is not None

    # Then
    # no stray `%` remains in the query text
    assert re.compile(r"%\([^)]+\)s").sub("", param_sql).find("%") == -1
    assert binder.params == {"p0": r"^([a-z%]+@example\.com)"}

    # and the full `query % params` substitution succeeds
    query = f"i.environment_id IN %(env_keys)s AND ({param_sql})"
    rendered = query % {"env_keys": (1, 2), **binder.params}
    assert r"[a-z%]+@example\.com" in rendered
