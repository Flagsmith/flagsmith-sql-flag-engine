"""Unit tests for the translator. No Snowflake required.

Asserts SQL string shapes for representative segments. Useful as a fast
sanity check before running the parity suite. The parity suite is the
authoritative correctness check — these are about catching regressions
in the translator's SQL generation, not engine equivalence.
"""

from __future__ import annotations

from flag_engine.context.types import EvaluationContext

from flagsmith_sql_flag_engine import TranslateContext, translate_segment
from flagsmith_sql_flag_engine.dialects.snowflake import SnowflakeDialect


def _ctx(env_key: str = "test-env-key", env_name: str = "Test") -> TranslateContext:
    eval_ctx: EvaluationContext = {"environment": {"key": env_key, "name": env_name}}
    return TranslateContext(evaluation_context=eval_ctx, dialect=SnowflakeDialect())


def test_translate_segment__equal_on_string_trait__emits_variant_path() -> None:
    # Given a segment with a single EQUAL on a string trait
    seg = {
        "key": "1",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "EQUAL", "property": "plan", "value": "growth"}],
            }
        ],
    }

    # When the segment is translated
    sql = translate_segment(seg, _ctx())

    # Then the predicate uses VARIANT path-extraction with quoted key, cast to STRING
    assert sql is not None
    assert 'i.traits:"plan"' in sql
    assert "::STRING = 'growth'" in sql


def test_translate_segment__in_with_csv_value__translates_to_in_clause() -> None:
    # Given a segment using IN with a comma-separated value
    seg = {
        "key": "2",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "IN", "property": "country", "value": "GB,US,DE"}],
            }
        ],
    }

    # When the segment is translated
    sql = translate_segment(seg, _ctx())

    # Then each item is split into a SQL IN list
    assert sql is not None
    assert 'i.traits:"country"' in sql
    assert "IN ('GB','US','DE')" in sql


def test_translate_segment__is_set__emits_is_not_null_on_path() -> None:
    # Given an IS_SET condition on a trait key
    seg = {
        "key": "3",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "IS_SET", "property": "beta_cohort", "value": ""}],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the predicate is a path-nullness check, no subquery
    assert sql is not None
    assert 'i.traits:"beta_cohort" IS NOT NULL' in sql
    assert "EXISTS" not in sql


def test_translate_segment__is_not_set__emits_is_null_on_path() -> None:
    # Given an IS_NOT_SET condition on a trait key
    seg = {
        "key": "4",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "IS_NOT_SET", "property": "x", "value": ""}],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the predicate is `IS NULL` on the VARIANT path
    assert sql is not None
    assert 'i.traits:"x" IS NULL' in sql


def test_translate_segment__percentage_split_no_property__inlines_md5_arithmetic() -> None:
    # Given a PERCENTAGE_SPLIT with no property (engine hashes the identity key)
    seg = {
        "key": "100",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "PERCENTAGE_SPLIT", "property": "", "value": "50"}],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the SQL contains inline MD5/TO_NUMBER arithmetic and the threshold literal
    assert sql is not None
    assert "MD5_HEX" in sql
    assert "TO_NUMBER" in sql
    assert "<= 50.0" in sql


def test_translate_segment__percentage_split_on_trait__uses_variant_path() -> None:
    # Given a PERCENTAGE_SPLIT keyed on a trait value
    seg = {
        "key": "101",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "PERCENTAGE_SPLIT", "property": "uuid_attr", "value": "30"}
                ],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the hash subject pulls from the trait's VARIANT path
    assert sql is not None
    assert 'i.traits:"uuid_attr"' in sql
    assert "MD5_HEX" in sql


def test_translate_segment__jsonpath_identity_identifier__uses_column_directly() -> None:
    # Given a condition referencing $.identity.identifier
    seg = {
        "key": "5",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "EQUAL", "property": "$.identity.identifier", "value": "x"}
                ],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the predicate references the identifier column, not a trait path
    assert sql is not None
    assert "i.identifier" in sql
    assert "traits" not in sql


def test_translate_segment__jsonpath_environment_name__uses_context_value() -> None:
    # Given a condition on $.environment.name and a TranslateContext with env_name="Production"
    seg = {
        "key": "10",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {
                        "operator": "EQUAL",
                        "property": "$.environment.name",
                        "value": "Production",
                    }
                ],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx(env_name="Production"))

    # Then the predicate emits a constant-vs-constant comparison from the context
    assert sql is not None
    assert "'Production' = 'Production'" in sql


def test_translate_segment__regex_with_backreference__returns_none() -> None:
    # Given a regex pattern containing a backreference (RE2-unsafe)
    seg = {
        "key": "6",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "REGEX", "property": "x", "value": r"(foo)\1"}],
            }
        ],
    }

    # When translation is attempted
    # Then the translator declines (returns None) so the caller can fall back
    assert translate_segment(seg, _ctx()) is None


def test_translate_segment__regex_with_lookahead__returns_none() -> None:
    # Given a regex pattern containing a lookahead (RE2-unsafe)
    seg = {
        "key": "7",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "REGEX", "property": "x", "value": "foo(?=bar)"}],
            }
        ],
    }

    # When translation is attempted
    # Then the translator declines
    assert translate_segment(seg, _ctx()) is None


def test_translate_segment__none_rule__emits_negation() -> None:
    # Given a NONE rule (matches when no condition is satisfied)
    seg = {
        "key": "8",
        "name": "s",
        "rules": [
            {
                "type": "NONE",
                "conditions": [{"operator": "EQUAL", "property": "p", "value": "v"}],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the predicate is wrapped in NOT(...)
    assert sql is not None
    assert sql.startswith("(NOT (")


def test_translate_segment__empty_rules__returns_false() -> None:
    # Given a segment with no rules
    seg = {"key": "9", "name": "s", "rules": []}

    # When translated
    # Then the predicate is the literal FALSE (no identity matches an empty segment)
    assert translate_segment(seg, _ctx()) == "FALSE"


def test_translate_segment__trait_key_with_hyphens__quotes_variant_path() -> None:
    # Given a trait key with a hyphen (illegal as an unquoted SQL identifier)
    seg = {
        "key": "11",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "EQUAL", "property": "user-name", "value": "alice"}],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the trait key is double-quoted in the VARIANT path
    assert sql is not None
    assert 'i.traits:"user-name"' in sql


def test_translate_segment__numeric_comparator_with_injection__returns_none() -> None:
    # Given a comparator value that is not numeric (e.g. a SQL-injection payload)
    for op in ("GREATER_THAN", "LESS_THAN", "GREATER_THAN_INCLUSIVE", "LESS_THAN_INCLUSIVE"):
        seg = {
            "key": "12",
            "name": "s",
            "rules": [
                {
                    "type": "ALL",
                    "conditions": [
                        {
                            "operator": op,
                            "property": "session_count",
                            "value": "100; DROP TABLE IDENTITIES; --",
                        }
                    ],
                }
            ],
        }

        # When translation is attempted
        # Then the translator declines (Python float() raises before any SQL is built)
        assert translate_segment(seg, _ctx()) is None, op


def test_translate_segment__numeric_comparator_with_numeric_string__interpolates_parsed_float() -> (
    None
):
    # Given a numeric comparator with a clean numeric string
    seg = {
        "key": "13",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "GREATER_THAN", "property": "session_count", "value": "30"}
                ],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the parsed float is interpolated (not the raw string)
    assert sql is not None
    assert "> 30.0" in sql or "> 30" in sql


def test_translate_segment__modulo_with_injection_in_divisor__returns_none() -> None:
    # Given a MODULO condition whose divisor contains a SQL-injection payload
    seg = {
        "key": "14",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {
                        "operator": "MODULO",
                        "property": "session_count",
                        "value": "5; DROP TABLE IDENTITIES; --|0",
                    }
                ],
            }
        ],
    }

    # When translation is attempted
    # Then the translator declines (float() on the divisor raises before SQL is built)
    assert translate_segment(seg, _ctx()) is None
