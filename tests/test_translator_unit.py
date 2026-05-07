"""Unit tests for the translator. No Snowflake required.

Asserts SQL string shapes for representative segments. Useful as a fast
sanity check before running the parity suite. The parity suite is the
authoritative correctness check — these are about catching regressions
in the translator's SQL generation, not engine equivalence.
"""

from __future__ import annotations

from flag_engine.context.types import EnvironmentContext

from flagsmith_sql_flag_engine import TranslateContext, translate_segment


def _ctx(env_key: str = "test-env-key", env_name: str = "Test") -> TranslateContext:
    env: EnvironmentContext = {"key": env_key, "name": env_name}
    return TranslateContext(environment=env)


def test_simple_equal_emits_variant_path() -> None:
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
    sql = translate_segment(seg, _ctx())
    assert sql is not None
    # VARIANT path-extraction with quoted key, cast to STRING for comparison.
    assert 'i.traits:"plan"' in sql
    assert "::STRING = 'growth'" in sql


def test_in_operator_translates_csv_value() -> None:
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
    sql = translate_segment(seg, _ctx())
    assert sql is not None
    assert 'i.traits:"country"' in sql
    assert "IN ('GB','US','DE')" in sql


def test_is_set_emits_is_not_null_on_path() -> None:
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
    sql = translate_segment(seg, _ctx())
    assert sql is not None
    assert 'i.traits:"beta_cohort" IS NOT NULL' in sql
    assert "EXISTS" not in sql  # no subquery, just path nullness


def test_is_not_set_emits_is_null_on_path() -> None:
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
    sql = translate_segment(seg, _ctx())
    assert sql is not None
    assert 'i.traits:"x" IS NULL' in sql


def test_percentage_split_inlines_md5_arithmetic() -> None:
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
    sql = translate_segment(seg, _ctx())
    assert sql is not None
    assert "MD5_HEX" in sql
    assert "TO_NUMBER" in sql
    assert "<= 50.0" in sql


def test_percentage_split_on_trait_uses_variant_path() -> None:
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
    sql = translate_segment(seg, _ctx())
    assert sql is not None
    assert 'i.traits:"uuid_attr"' in sql
    assert "MD5_HEX" in sql


def test_jsonpath_identity_identifier_uses_column_directly() -> None:
    seg = {
        "key": "5",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {
                        "operator": "EQUAL",
                        "property": "$.identity.identifier",
                        "value": "x",
                    }
                ],
            }
        ],
    }
    sql = translate_segment(seg, _ctx())
    assert sql is not None
    assert "i.identifier" in sql
    assert "traits" not in sql  # no traits lookup needed


def test_jsonpath_environment_name_uses_context_value() -> None:
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
    sql = translate_segment(seg, _ctx(env_name="Production"))
    assert sql is not None
    assert "'Production' = 'Production'" in sql


def test_regex_with_backreference_returns_none() -> None:
    seg = {
        "key": "6",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {
                        "operator": "REGEX",
                        "property": "x",
                        "value": r"(foo)\1",
                    }
                ],
            }
        ],
    }
    assert translate_segment(seg, _ctx()) is None


def test_regex_with_lookahead_returns_none() -> None:
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
    assert translate_segment(seg, _ctx()) is None


def test_none_rule_emits_negation() -> None:
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
    sql = translate_segment(seg, _ctx())
    assert sql is not None
    assert sql.startswith("(NOT (")


def test_empty_rules_returns_false() -> None:
    seg = {"key": "9", "name": "s", "rules": []}
    assert translate_segment(seg, _ctx()) == "FALSE"


def test_trait_with_special_characters_in_key_quoted() -> None:
    """Trait keys can contain hyphens, dots, etc.; VARIANT path quotes them."""
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
    sql = translate_segment(seg, _ctx())
    assert sql is not None
    assert 'i.traits:"user-name"' in sql
