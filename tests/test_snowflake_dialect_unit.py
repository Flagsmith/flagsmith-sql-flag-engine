"""Unit tests for `SnowflakeDialect` SQL output shapes.

The dialect-agnostic translator behaviour is exercised in
`test_translator_unit.py` (against the ClickHouse dialect). These tests
pin the Snowflake-specific SQL fragments — VARIANT path syntax, `::STRING`
casts, `POSITION(needle, haystack)` argument order, `MD5_HEX` /
`TO_NUMBER` hashing — that the parity suite can't fully cover.
"""

from flag_engine.context.types import EvaluationContext, SegmentContext

from flagsmith_sql_flag_engine import TranslateContext, translate_segment
from flagsmith_sql_flag_engine.dialects.snowflake import SnowflakeDialect


def _ctx() -> TranslateContext:
    eval_ctx: EvaluationContext = {
        "environment": {"key": "test-env-key", "name": "Test"},
        "identity": {
            "identifier": "u",
            "key": "k",
            "traits": {"plan": "growth", "country": "GB", "uuid_attr": "abc"},
        },
    }
    return TranslateContext(evaluation_context=eval_ctx, dialect=SnowflakeDialect())


def test_translate_segment__equal_on_string_trait__emits_variant_path() -> None:
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

    # Then the predicate uses VARIANT path-extraction with quoted key, cast to STRING
    assert sql is not None
    assert 'i.traits:"plan"' in sql
    assert "(i.traits:\"plan\")::STRING = 'growth'" in sql


def test_translate_segment__in_with_csv_value__emits_variant_in_clause() -> None:
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

    # When translated
    sql = translate_segment(seg, _ctx())

    # Then each item is split into a SQL IN list against the VARIANT path
    assert sql is not None
    assert 'i.traits:"country"' in sql
    assert "IN ('GB','US','DE')" in sql


def test_translate_segment__is_set__emits_is_not_null_on_variant_path() -> None:
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

    # Then the predicate is a VARIANT path-nullness check
    assert sql is not None
    assert 'i.traits:"beta_cohort" IS NOT NULL' in sql


def test_translate_segment__is_not_set__emits_is_null_on_variant_path() -> None:
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

    # Then the predicate is `IS NULL` on the VARIANT path
    assert sql is not None
    assert 'i.traits:"x" IS NULL' in sql


def test_translate_segment__percentage_split_no_property__uses_md5_hex_and_to_number() -> None:
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

    # Then the SQL contains inline MD5_HEX / TO_NUMBER arithmetic and the threshold literal
    assert sql is not None
    assert "MD5_HEX" in sql
    assert "TO_NUMBER" in sql
    assert "<= 50.0" in sql


def test_translate_segment__percentage_split_on_trait__hashes_variant_path() -> None:
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

    # Then the hash subject pulls from the trait's VARIANT path
    assert sql is not None
    assert 'i.traits:"uuid_attr"' in sql
    assert "MD5_HEX" in sql


def test_translate_segment__trait_key_with_hyphens__double_quotes_variant_key() -> None:
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

    # Then the trait key is double-quoted inside the VARIANT path
    assert sql is not None
    assert 'i.traits:"user-name"' in sql


def test_translate_segment__contains_on_identity_identifier__uses_position_needle_first() -> None:
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

    # Then the predicate uses Snowflake's `POSITION(needle, haystack)`
    assert sql is not None
    assert "POSITION('@', i.identifier) > 0" in sql


def test_translate_segment__contains_on_trait__uses_position_over_variant_string_cast() -> None:
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

    # Then the predicate uses POSITION on the cast-to-string VARIANT value
    assert sql is not None
    assert "POSITION('G', (i.traits:\"country\")::STRING) > 0" in sql


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

    # Then the hash subject is the identifier column ref via MD5_HEX
    assert sql is not None
    assert "i.identifier" in sql
    assert "MD5_HEX" in sql
