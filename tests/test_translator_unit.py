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
    eval_ctx: EvaluationContext = {
        "environment": {"key": env_key, "name": env_name},
        # Identity carries every trait keyed by representative segments below;
        # PERCENTAGE_SPLIT bails when the prop isn't a known trait of the
        # context identity, so make sure it is.
        "identity": {
            "identifier": "u",
            "key": "k",
            "traits": {"plan": "growth", "country": "GB", "uuid_attr": "abc"},
        },
    }
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
    assert "(i.traits:\"plan\")::STRING = 'growth'" in sql


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

    # Then the predicate collapses to a SQL constant — the env name is fixed
    # for every row in the resulting query, so the translator pre-computes
    # the engine's verdict via `is_context_in_segment`.
    assert sql == "((TRUE))"


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


def test_translate_segment__numeric_comparator_with_injection__compiles_to_false() -> None:
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

        # When translated
        sql = translate_segment(seg, _ctx())

        # Then the bad operand collapses to FALSE (Python float() raises before
        # any SQL is built; engine returns False on the same input). Zero
        # injection-payload bytes ever reach the SQL output.
        assert sql == "((FALSE))", op


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


def test_translate_segment__modulo_with_injection_in_divisor__compiles_to_false() -> None:
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

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the bad operand collapses to FALSE (matching engine behaviour:
    # float() on the divisor raises and the engine returns False), with
    # zero injection-payload bytes ever reaching the SQL output
    assert sql == "((FALSE))"


# --- coverage: untranslatable operator and condition shapes ---


def test_translate_segment__unknown_operator__returns_none() -> None:
    # Given a condition with an operator the translator doesn't support
    seg = {
        "key": "u1",
        "name": "s",
        "rules": [{"type": "ALL", "conditions": [{"operator": "WHATEVER", "property": "x"}]}],
    }

    # When / Then the translator declines
    assert translate_segment(seg, _ctx()) is None


def test_translate_segment__condition_without_property__compiles_to_false() -> None:
    # Given a non-PERCENTAGE_SPLIT condition with no `property`
    seg = {
        "key": "u2",
        "name": "s",
        "rules": [
            {"type": "ALL", "conditions": [{"operator": "EQUAL", "property": "", "value": "x"}]}
        ],
    }

    # When / Then the predicate collapses to FALSE (engine looks up nothing,
    # the comparator's cast fails, returns False)
    assert translate_segment(seg, _ctx()) == "((FALSE))"


# --- coverage: PERCENTAGE_SPLIT short-circuits ---


def test_translate_segment__percentage_split_unparseable_threshold__compiles_to_false() -> None:
    # Given a PERCENTAGE_SPLIT whose value can't parse as a number
    seg = {
        "key": "ps1",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "PERCENTAGE_SPLIT", "property": "", "value": "abc"}],
            }
        ],
    }

    # When / Then the predicate collapses to FALSE (engine: float() on the
    # threshold raises and the comparator returns False)
    assert translate_segment(seg, _ctx()) == "((FALSE))"


def _ctx_no_identity() -> TranslateContext:
    return TranslateContext(
        evaluation_context={"environment": {"key": "k", "name": "n"}},
        dialect=SnowflakeDialect(),
    )


def _ctx_identity_without(field: str) -> TranslateContext:
    identity: dict = {"identifier": "u", "key": "k", "traits": {}}
    identity.pop(field)
    return TranslateContext(
        evaluation_context={"environment": {"key": "k", "name": "n"}, "identity": identity},
        dialect=SnowflakeDialect(),
    )


def test_translate_segment__percentage_split_implicit_key_no_identity__compiles_to_false() -> None:
    # Given a PERCENTAGE_SPLIT with no property (implicit `$.identity.key`) and an eval
    # context with no identity
    seg = {
        "key": "ps2",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "PERCENTAGE_SPLIT", "property": "", "value": "50"}],
            }
        ],
    }

    # When / Then the predicate collapses to FALSE — engine returns False without identity
    assert translate_segment(seg, _ctx_no_identity()) == "((FALSE))"


def test_translate_segment__percentage_split_identity_key_missing__compiles_to_false() -> None:
    # Given the eval context's identity has no `key`
    seg = {
        "key": "ps3",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "PERCENTAGE_SPLIT", "property": "$.identity.key", "value": "50"}
                ],
            }
        ],
    }

    # When / Then the predicate collapses to FALSE
    assert translate_segment(seg, _ctx_identity_without("key")) == "((FALSE))"


def test_translate_segment__percentage_split_identity_identifier_missing__compiles_to_false() -> (
    None
):
    # Given the eval context's identity has no `identifier`
    seg = {
        "key": "ps4",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {
                        "operator": "PERCENTAGE_SPLIT",
                        "property": "$.identity.identifier",
                        "value": "50",
                    }
                ],
            }
        ],
    }

    # When / Then the predicate collapses to FALSE
    assert translate_segment(seg, _ctx_identity_without("identifier")) == "((FALSE))"


def test_translate_segment__percentage_split_unknown_jsonpath__returns_none() -> None:
    # Given a PERCENTAGE_SPLIT on a JSONPath that resolves to nothing in the eval context
    seg = {
        "key": "ps5",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "PERCENTAGE_SPLIT", "property": "$.nope.nope", "value": "50"}
                ],
            }
        ],
    }

    # When / Then the translator declines (no value to hash)
    assert translate_segment(seg, _ctx()) is None


def test_translate_segment__percentage_split_trait_not_in_context__compiles_to_false() -> None:
    # Given a PERCENTAGE_SPLIT on a trait the eval context's identity doesn't carry
    seg = {
        "key": "ps6",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "PERCENTAGE_SPLIT", "property": "missing_trait", "value": "50"}
                ],
            }
        ],
    }

    # When / Then the predicate collapses to FALSE
    assert translate_segment(seg, _ctx()) == "((FALSE))"


# --- coverage: identity-bound JSONPath comparators (column refs, not pre-eval) ---


def test_translate_segment__is_set_on_identity_identifier__emits_true() -> None:
    # Given an IS_SET on $.identity.identifier — every IDENTITIES row IS an
    # identity, so the predicate is unconditionally true
    seg = {
        "key": "j1",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "IS_SET", "property": "$.identity.identifier", "value": ""}
                ],
            }
        ],
    }

    # When / Then the predicate collapses to TRUE
    assert translate_segment(seg, _ctx()) == "((TRUE))"


def test_translate_segment__is_not_set_on_identity_key__emits_false() -> None:
    # Given an IS_NOT_SET on $.identity.key — same as above, inverted
    seg = {
        "key": "j2",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "IS_NOT_SET", "property": "$.identity.key", "value": ""}
                ],
            }
        ],
    }

    # When / Then the predicate collapses to FALSE
    assert translate_segment(seg, _ctx()) == "((FALSE))"


def test_translate_segment__not_equal_on_identity_identifier__emits_inequality() -> None:
    # Given a NOT_EQUAL against the identifier column
    seg = {
        "key": "j3",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "NOT_EQUAL", "property": "$.identity.identifier", "value": "ada"}
                ],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the predicate is a direct column inequality compare
    assert sql is not None
    assert "i.identifier <> 'ada'" in sql


def test_translate_segment__contains_on_identity_identifier__uses_position() -> None:
    # Given a CONTAINS on the identifier column
    seg = {
        "key": "j4",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "CONTAINS", "property": "$.identity.identifier", "value": "@"}
                ],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the predicate uses POSITION on the identifier column
    assert sql is not None
    assert "POSITION('@', i.identifier) > 0" in sql


def test_translate_segment__not_contains_on_identity_identifier__inverts_position() -> None:
    # Given a NOT_CONTAINS on the identifier column
    seg = {
        "key": "j5",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "NOT_CONTAINS", "property": "$.identity.identifier", "value": "@"}
                ],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the predicate is the inverse of POSITION, with a not-null guard
    assert sql is not None
    assert "IS NOT NULL AND NOT" in sql
    assert "POSITION('@', i.identifier) > 0" in sql


def test_translate_segment__comparison_with_none_value__compiles_to_false() -> None:
    # Given an EQUAL on a JSONPath identity column with no value field
    seg = {
        "key": "j6",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "EQUAL", "property": "$.identity.identifier"}],
            }
        ],
    }

    # When / Then the predicate collapses to FALSE (engine treats null value
    # as a cast failure → returns False)
    assert translate_segment(seg, _ctx()) == "((FALSE))"


# --- coverage: rule composition ---


def test_translate_segment__rule_with_untranslatable_nested_rule__returns_none() -> None:
    # Given a nested rule containing an untranslatable operator
    seg = {
        "key": "r2",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [],
                "rules": [
                    {
                        "type": "ALL",
                        "conditions": [{"operator": "REGEX", "property": "x", "value": r"(foo)\1"}],
                    }
                ],
            }
        ],
    }

    # When / Then untranslatability propagates up through translate_rule's
    # recursion, surfacing None at the top level so the caller can fall back
    assert translate_segment(seg, _ctx()) is None


def test_translate_segment__none_rule_type__inverts_predicate() -> None:
    # Given a NONE rule (matches when no condition matches)
    seg = {
        "key": "r3",
        "name": "s",
        "rules": [
            {
                "type": "NONE",
                "conditions": [{"operator": "EQUAL", "property": "plan", "value": "growth"}],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the rule is wrapped in a NOT
    assert sql is not None
    assert sql.startswith("(NOT (") or "NOT (" in sql


def test_translate_segment__unknown_rule_type__raises() -> None:
    # Given a segment with a rule type the schema doesn't allow (Flagsmith's
    # segment validation rejects this upstream — reaching the translator with
    # one is internal misuse, not a runtime input we should silently swallow)
    import pytest

    seg = {
        "key": "r4",
        "name": "s",
        "rules": [
            {
                "type": "MAYBE",
                "conditions": [{"operator": "EQUAL", "property": "plan", "value": "growth"}],
            }
        ],
    }

    # When translation is attempted
    # Then the assert fires loudly rather than returning a wrong-but-quiet answer
    with pytest.raises(AssertionError):
        translate_segment(seg, _ctx())


# --- coverage: helpers ---


def test_translate_segment__in_with_non_iterable_value__compiles_to_false() -> None:
    # Given a trait IN whose value is neither a string nor a list (engine: the
    # value-set parser raises, returns False)
    seg = {
        "key": "tin",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "IN", "property": "country", "value": 123}],
            }
        ],
    }

    # When / Then the predicate collapses to FALSE
    assert translate_segment(seg, _ctx()) == "((FALSE))"


def test_translate_segment__contains_on_trait__uses_position() -> None:
    # Given a CONTAINS condition on a trait
    seg = {
        "key": "tc1",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "CONTAINS", "property": "country", "value": "G"}],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the predicate uses POSITION on the cast-to-string trait value
    assert sql is not None
    assert "POSITION('G', (i.traits:\"country\")::STRING) > 0" in sql


def test_translate_segment__not_contains_on_trait__inverts_position() -> None:
    # Given a NOT_CONTAINS condition on a trait
    seg = {
        "key": "tc2",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "NOT_CONTAINS", "property": "country", "value": "G"}],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the predicate is the inverse of POSITION, with a not-null guard
    assert sql is not None
    assert "IS NOT NULL AND NOT" in sql
    assert "POSITION('G', (i.traits:\"country\")::STRING) > 0" in sql


def test_translate_segment__condition_on_unmapped_identity_field__returns_none() -> None:
    # Given a condition on `$.identity.<X>` where `<X>` isn't `identifier`,
    # `key`, or `traits.<…>` — our row schema doesn't represent it
    seg = {
        "key": "u3",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "EQUAL", "property": "$.identity.foo", "value": "x"}],
            }
        ],
    }

    # When / Then the translator declines (caller falls back to the engine)
    assert translate_segment(seg, _ctx()) is None


def test_translate_segment__condition_on_identity_path_with_wildcard__returns_none() -> None:
    # Given a condition on `$.identity.traits.*` (a wildcard-selector path —
    # the engine can resolve it, but we can't map it to a fixed row reference)
    seg = {
        "key": "u4",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "IS_SET", "property": "$.identity.traits.*", "value": ""}
                ],
            }
        ],
    }

    # When / Then the translator declines
    assert translate_segment(seg, _ctx()) is None


def test_translate_segment__percentage_split_on_unmapped_identity_field__returns_none() -> None:
    # Given a PERCENTAGE_SPLIT on a `$.identity.<X>` we can't represent
    seg = {
        "key": "ps8",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "PERCENTAGE_SPLIT", "property": "$.identity.foo", "value": "50"}
                ],
            }
        ],
    }

    # When / Then the translator declines (caller falls back)
    assert translate_segment(seg, _ctx()) is None


def test_translate_segment__percentage_split_on_identity_identifier__hashes_column() -> None:
    # Given a PERCENTAGE_SPLIT keyed on `$.identity.identifier`
    seg = {
        "key": "ps7",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {
                        "operator": "PERCENTAGE_SPLIT",
                        "property": "$.identity.identifier",
                        "value": "30",
                    }
                ],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the hash subject is the identifier column ref (not the eval ctx's
    # identifier value — PERCENTAGE_SPLIT is row-bound)
    assert sql is not None
    assert "i.identifier" in sql
    assert "MD5_HEX" in sql
