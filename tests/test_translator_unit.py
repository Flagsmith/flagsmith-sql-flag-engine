"""Unit tests for the translator. No ClickHouse required.

Asserts SQL string shapes for representative segments, driving the
dialect-agnostic structure of the translator. Per-dialect SQL-output
shapes live in `tests/test_<dialect>_dialect_unit.py`.
"""

from typing import cast

from flag_engine.context.types import EvaluationContext, IdentityContext, SegmentContext

from flagsmith_sql_flag_engine import TranslateContext, translate_segment
from flagsmith_sql_flag_engine.dialects.clickhouse import ClickHouseDialect


def _ctx(env_key: str = "test-env-key", env_name: str = "Test") -> TranslateContext:
    eval_ctx: EvaluationContext = {
        "environment": {"key": env_key, "name": env_name},
        # PERCENTAGE_SPLIT short-circuits to FALSE when the prop isn't in
        # the eval context's traits, so seed the traits referenced below.
        "identity": {
            "identifier": "u",
            "key": "k",
            "traits": {"plan": "growth", "country": "GB", "uuid_attr": "abc"},
        },
    }
    return TranslateContext(evaluation_context=eval_ctx, dialect=ClickHouseDialect())


def test_translate_segment__equal_on_string_trait__emits_trait_subcolumn() -> None:
    # Given a segment with a single EQUAL on a string trait
    seg: SegmentContext = {
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

    # Then the predicate references the JSON subcolumn and compares its
    # canonical string form to the segment value
    assert sql is not None
    assert "i.traits.`plan`" in sql
    assert "toString(i.traits.`plan`) = 'growth'" in sql


def test_translate_segment__in_with_csv_value__translates_to_in_clause() -> None:
    # Given a segment using IN with a comma-separated value
    seg: SegmentContext = {
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
    assert "i.traits.`country`" in sql
    assert "IN ('GB','US','DE')" in sql


def test_translate_segment__is_set__emits_is_not_null_on_path() -> None:
    # Given an IS_SET condition on a trait key
    seg: SegmentContext = {
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
    assert "i.traits.`beta_cohort`" in sql
    assert "IS NOT NULL" in sql
    assert "EXISTS" not in sql


def test_translate_segment__is_not_set__emits_is_null_on_path() -> None:
    # Given an IS_NOT_SET condition on a trait key
    seg: SegmentContext = {
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

    # Then the predicate is `IS NULL` on the trait subcolumn path
    assert sql is not None
    assert "i.traits.`x`" in sql
    assert "IS NULL" in sql


def test_translate_segment__percentage_split_no_property__inlines_md5_arithmetic() -> None:
    # Given a PERCENTAGE_SPLIT with no property (engine hashes the identity key)
    seg: SegmentContext = {
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

    # Then the SQL contains inline MD5/hex-chunk arithmetic and the threshold literal
    assert sql is not None
    assert "MD5(" in sql
    assert "reinterpretAsUInt32" in sql
    assert "<= 50.0" in sql


def test_translate_segment__percentage_split_on_trait__uses_trait_subcolumn() -> None:
    # Given a PERCENTAGE_SPLIT keyed on a trait value
    seg: SegmentContext = {
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

    # Then the hash subject pulls from the trait's JSON subcolumn
    assert sql is not None
    assert "i.traits.`uuid_attr`" in sql
    assert "MD5(" in sql


def test_translate_segment__jsonpath_identity_identifier__uses_column_directly() -> None:
    # Given a condition referencing $.identity.identifier
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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
    seg: SegmentContext = {"key": "9", "name": "s", "rules": []}

    # When translated
    # Then the predicate is the literal FALSE (no identity matches an empty segment)
    assert translate_segment(seg, _ctx()) == "FALSE"


def test_translate_segment__rule_with_no_conditions_or_nested_rules__matches_all() -> None:
    # Given
    seg: SegmentContext = {
        "key": "10",
        "name": "s",
        "rules": [{"type": "ALL", "conditions": [], "rules": []}],
    }

    # When / Then
    assert translate_segment(seg, _ctx()) == "(TRUE)"


def test_translate_segment__rule_with_conditions_and_nested_rule__ands_the_two_groups() -> None:
    # Given a rule carrying both a condition and a (translatable) nested rule
    seg: SegmentContext = {
        "key": "11",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [{"operator": "EQUAL", "property": "plan", "value": "growth"}],
                "rules": [
                    {
                        "type": "ALL",
                        "conditions": [{"operator": "EQUAL", "property": "country", "value": "GB"}],
                    }
                ],
            }
        ],
    }

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then the condition group and the nested-rule group are AND-ed together
    # (mirroring the engine: matches_conditions AND matches_rules)
    assert sql == (
        "(((("
        "if(i.traits.`plan` IS NULL, NULL, toString(i.traits.`plan`)) IS NOT NULL"
        " AND ((toString(i.traits.`plan`) = 'growth') OR (i.traits.`plan`.:Bool = true))"
        "))) AND (((("
        "if(i.traits.`country` IS NULL, NULL, toString(i.traits.`country`)) IS NOT NULL"
        " AND ((toString(i.traits.`country`) = 'GB') OR (i.traits.`country`.:Bool = true))"
        ")))))"
    )


def test_translate_segment__any_rule_with_conditions_and_nested_rule__ors_within_ands_across() -> (
    None
):
    # Given an ANY rule with two conditions and a nested rule
    seg: SegmentContext = {
        "key": "12",
        "name": "s",
        "rules": [
            {
                "type": "ANY",
                "conditions": [
                    {"operator": "EQUAL", "property": "plan", "value": "growth"},
                    {"operator": "EQUAL", "property": "plan", "value": "scale"},
                ],
                "rules": [
                    {
                        "type": "ALL",
                        "conditions": [{"operator": "EQUAL", "property": "country", "value": "GB"}],
                    }
                ],
            }
        ],
    }

    # When
    sql = translate_segment(seg, _ctx())

    # Then
    # interpreted as `any(conditions) AND any(rules)`
    assert sql == (
        "(((("
        "if(i.traits.`plan` IS NULL, NULL, toString(i.traits.`plan`)) IS NOT NULL"
        " AND ((toString(i.traits.`plan`) = 'growth') OR (i.traits.`plan`.:Bool = true))"
        ")) OR (("
        "if(i.traits.`plan` IS NULL, NULL, toString(i.traits.`plan`)) IS NOT NULL"
        " AND ((toString(i.traits.`plan`) = 'scale') OR (i.traits.`plan`.:Bool = true))"
        "))) AND (((("
        "if(i.traits.`country` IS NULL, NULL, toString(i.traits.`country`)) IS NOT NULL"
        " AND ((toString(i.traits.`country`) = 'GB') OR (i.traits.`country`.:Bool = true))"
        ")))))"
    )


def test_translate_segment__trait_key_with_hyphens__quotes_subcolumn_path() -> None:
    # Given a trait key with a hyphen (illegal as an unquoted SQL identifier)
    seg: SegmentContext = {
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

    # Then the trait key is backtick-quoted in the JSON subcolumn path
    assert sql is not None
    assert "i.traits.`user-name`" in sql


def test_translate_segment__numeric_comparator_with_injection__compiles_to_false() -> None:
    # Given a comparator value that is not numeric (e.g. a SQL-injection payload)
    for op in ("GREATER_THAN", "LESS_THAN", "GREATER_THAN_INCLUSIVE", "LESS_THAN_INCLUSIVE"):
        seg: SegmentContext = {
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
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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


def test_translate_segment__unknown_operator__returns_none() -> None:
    # Given a condition with an operator the translator doesn't support
    seg = cast(
        SegmentContext,
        {
            "key": "u1",
            "name": "s",
            "rules": [{"type": "ALL", "conditions": [{"operator": "WHATEVER", "property": "x"}]}],
        },
    )

    # When / Then the translator declines
    assert translate_segment(seg, _ctx()) is None


def test_translate_segment__condition_without_property__compiles_to_false() -> None:
    # Given a non-PERCENTAGE_SPLIT condition with no `property`
    seg: SegmentContext = {
        "key": "u2",
        "name": "s",
        "rules": [
            {"type": "ALL", "conditions": [{"operator": "EQUAL", "property": "", "value": "x"}]}
        ],
    }

    # When / Then the predicate collapses to FALSE (engine looks up nothing,
    # the comparator's cast fails, returns False)
    assert translate_segment(seg, _ctx()) == "((FALSE))"


def test_translate_segment__percentage_split_unparseable_threshold__compiles_to_false() -> None:
    # Given a PERCENTAGE_SPLIT whose value can't parse as a number
    seg: SegmentContext = {
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
        dialect=ClickHouseDialect(),
    )


def _ctx_identity_without(field: str) -> TranslateContext:
    identity: dict[str, object] = {"identifier": "u", "key": "k", "traits": {}}
    identity.pop(field)
    return TranslateContext(
        evaluation_context={
            "environment": {"key": "k", "name": "n"},
            "identity": cast(IdentityContext, identity),
        },
        dialect=ClickHouseDialect(),
    )


def test_translate_segment__percentage_split_implicit_key_no_identity__compiles_to_false() -> None:
    # Given a PERCENTAGE_SPLIT with no property (implicit `$.identity.key`) and an eval
    # context with no identity
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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


def test_translate_segment__is_set_on_identity_identifier__emits_true() -> None:
    # Given an IS_SET on $.identity.identifier — every IDENTITIES row IS an
    # identity, so the predicate is unconditionally true
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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

    # Then the predicate uses ClickHouse's `position(haystack, needle)` on the
    # identifier column — argument order is inverted from Snowflake's
    # `POSITION(needle, haystack)`.
    assert sql is not None
    assert "position(i.identifier, '@') > 0" in sql


def test_translate_segment__not_contains_on_identity_identifier__inverts_position() -> None:
    # Given a NOT_CONTAINS on the identifier column
    seg: SegmentContext = {
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

    # Then the predicate is the inverse of `position`, with a not-null guard
    assert sql is not None
    assert "IS NOT NULL AND NOT" in sql
    assert "position(i.identifier, '@') > 0" in sql


def test_translate_segment__comparison_with_none_value__compiles_to_false() -> None:
    # Given an EQUAL on a JSONPath identity column with no value field
    seg = cast(
        SegmentContext,
        {
            "key": "j6",
            "name": "s",
            "rules": [
                {
                    "type": "ALL",
                    "conditions": [{"operator": "EQUAL", "property": "$.identity.identifier"}],
                }
            ],
        },
    )

    # When / Then the predicate collapses to FALSE (engine treats null value
    # as a cast failure → returns False)
    assert translate_segment(seg, _ctx()) == "((FALSE))"


def test_translate_segment__rule_with_untranslatable_nested_rule__returns_none() -> None:
    # Given a nested rule containing an untranslatable operator
    seg: SegmentContext = {
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


def test_translate_segment__in_with_non_iterable_value__compiles_to_false() -> None:
    # Given a trait IN whose value is neither a string nor a list (engine: the
    # value-set parser raises, returns False)
    seg = cast(
        SegmentContext,
        {
            "key": "tin",
            "name": "s",
            "rules": [
                {
                    "type": "ALL",
                    "conditions": [{"operator": "IN", "property": "country", "value": 123}],
                }
            ],
        },
    )

    # When / Then the predicate collapses to FALSE
    assert translate_segment(seg, _ctx()) == "((FALSE))"


def test_translate_segment__contains_on_trait__uses_position() -> None:
    # Given a CONTAINS condition on a trait
    seg: SegmentContext = {
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

    # Then the predicate uses `position` on the canonical-string form of the
    # trait value
    assert sql is not None
    assert "position(toString(" in sql
    assert ", 'G') > 0" in sql


def test_translate_segment__not_contains_on_trait__inverts_position() -> None:
    # Given a NOT_CONTAINS condition on a trait
    seg: SegmentContext = {
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

    # Then the predicate is the inverse of `position`, with a not-null guard
    assert sql is not None
    assert "IS NOT NULL AND NOT" in sql
    assert "position(toString(" in sql
    assert ", 'G') > 0" in sql


def test_translate_segment__condition_on_unmapped_identity_field__returns_none() -> None:
    # Given a condition on `$.identity.<X>` where `<X>` isn't `identifier`,
    # `key`, or `traits.<…>` — our row schema doesn't represent it
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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


def test_translate_segment__percentage_split_on_identity_whole_object__returns_none() -> None:
    # Given a PERCENTAGE_SPLIT on `$.identity` (the whole dict)
    seg: SegmentContext = {
        "key": "ps9",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {"operator": "PERCENTAGE_SPLIT", "property": "$.identity", "value": "50"}
                ],
            }
        ],
    }

    # When / Then the translator declines (engine would hash `str(dict)`,
    # which is stable but useless; not worth supporting in SQL)
    assert translate_segment(seg, _ctx()) is None


def test_translate_segment__percentage_split_on_unmapped_identity_field__returns_none() -> None:
    # Given a PERCENTAGE_SPLIT on a `$.identity.<X>` we can't represent
    seg: SegmentContext = {
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
    seg: SegmentContext = {
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
    assert "MD5(" in sql
