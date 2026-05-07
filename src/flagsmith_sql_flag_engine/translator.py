"""Translate `SegmentContext` predicate trees into SQL `WHERE` expressions.

The translator emits a predicate that goes against an `IDENTITIES` table
holding a `traits` VARIANT column (per `SnowflakeDialect.SCHEMA_DDL`).
Trait-bound conditions become VARIANT path-extractions
(`i.traits:"<key>"`); `PERCENTAGE_SPLIT` and `:semver`-marked comparators
compile to inline pure-SQL using the active `Dialect`.

Output shape::

    SELECT ... FROM IDENTITIES i
    WHERE i.environment_id = '<env-key>'
      AND <returned expression>

The caller is responsible for the surrounding query. The translator only
produces the predicate.

`environment_id` in the `IDENTITIES` table is a string column holding
`EnvironmentContext.key` — the same identifier the engine uses. There is
no separate integer PK.

Returns `None` if any condition uses an untranslatable operator —
specifically REGEX patterns containing backreferences or lookarounds
(RE2 doesn't support them). Callers should fall back to the Python
flag_engine for those segments, or surface an error at segment-edit time.
"""

from __future__ import annotations

import re

from flag_engine.context.types import (
    EnvironmentContext,
    SegmentCondition,
    SegmentContext,
    SegmentRule,
)

from flagsmith_sql_flag_engine.dialect import Dialect
from flagsmith_sql_flag_engine.dialects.snowflake import SnowflakeDialect
from flagsmith_sql_flag_engine.sanitiser import Sanitiser

TRANSLATABLE_OPERATORS: frozenset[str] = frozenset(
    {
        "EQUAL",
        "NOT_EQUAL",
        "IN",
        "IS_SET",
        "IS_NOT_SET",
        "CONTAINS",
        "NOT_CONTAINS",
        "GREATER_THAN",
        "LESS_THAN",
        "GREATER_THAN_INCLUSIVE",
        "LESS_THAN_INCLUSIVE",
        "MODULO",
        "PERCENTAGE_SPLIT",
        "REGEX",
    }
)


# Conservative check for Python-re features RE2 doesn't support.
_RE2_UNSAFE = re.compile(
    r"\\\d"  # backreference like \1 .. \9
    r"|\(\?[=!<]"  # lookahead / lookbehind / negative variants
)


def _regex_safe_for_re2(pattern: str) -> bool:
    return _RE2_UNSAFE.search(pattern) is None


# Constants for chunked MD5-mod-9999 hash. The engine computes
# `int(md5_hex, 16) % 9999`; we split the 32-hex digest into four 8-hex
# chunks, parse each as a 32-bit int, and combine via modular arithmetic.
# Constants are (16^24, 16^16, 16^8) mod 9999, precomputed.
_HASH_CONST_HIGH = 7291  # 16^24 mod 9999
_HASH_CONST_MID = 1897  # 16^16 mod 9999
_HASH_CONST_LOW = 6835  # 16^8 mod 9999


# ---------------------------------------------------------------------------
# Context: shape information the translator needs to produce correct refs.
# ---------------------------------------------------------------------------


class TranslateContext:
    """Inputs the translator needs to produce a query for a specific shape.

    `environment`     — flag_engine `EnvironmentContext` TypedDict
                        (`{"key": str, "name": str}`). `key` is used as
                        `environment_id` in the `IDENTITIES` table; `name`
                        is referenced by `$.environment.name` JSONPath
                        properties.
    `dialect`         — implementation of the `Dialect` protocol.
    `identities_alias` — table alias for `IDENTITIES` in the surrounding
                        query (default `i`).
    `identifier_col`  — column on the alias for `$.identity.identifier`.
    `identity_key_col` — column on the alias for `$.identity.key`.
    `traits_col`      — VARIANT column holding the identity's trait map.
    `segment_key`     — salts `PERCENTAGE_SPLIT`. Auto-injected from the
                        segment's `key` field by `translate_segment`.
    """

    def __init__(
        self,
        environment: EnvironmentContext,
        dialect: Dialect | None = None,
        identities_alias: str = "i",
        identifier_col: str = "i.identifier",
        identity_key_col: str = "i.identity_key",
        traits_col: str = "i.traits",
        segment_key: str | None = None,
    ) -> None:
        self.environment = environment
        self.dialect: Dialect = dialect if dialect is not None else SnowflakeDialect()
        self.identities_alias = identities_alias
        self.identifier_col = identifier_col
        self.identity_key_col = identity_key_col
        self.traits_col = traits_col
        self.segment_key = segment_key

    @property
    def env_id_lit(self) -> str:
        """SQL string literal for the environment id (= EnvironmentContext.key)."""
        return Sanitiser.string_literal(self.environment["key"])

    def trait_path(self, trait_key: str) -> str:
        """VARIANT path-extraction for a trait: `i.traits:"<key>"`."""
        return f"{self.traits_col}:{Sanitiser.variant_path_key(trait_key)}"

    def jsonpath_expr(self, prop: str) -> str | None:
        if prop == "$.identity.identifier":
            return self.identifier_col
        if prop == "$.identity.key":
            return self.identity_key_col
        if prop == "$.environment.name":
            return Sanitiser.string_literal(self.environment["name"])
        if prop == "$.environment.key":
            return self.env_id_lit
        return None

    def with_segment_key(self, key: str) -> TranslateContext:
        return TranslateContext(
            environment=self.environment,
            dialect=self.dialect,
            identities_alias=self.identities_alias,
            identifier_col=self.identifier_col,
            identity_key_col=self.identity_key_col,
            traits_col=self.traits_col,
            segment_key=key,
        )


# ---------------------------------------------------------------------------
# Inline SQL builders for hash-based and version-based predicates.
# ---------------------------------------------------------------------------


def _percentage_split_expr(
    ctx: TranslateContext, seg_key: str, ctx_value_sql: str, threshold: float
) -> str:
    """Boolean SQL fragment: hash(seg_key + "," + value) <= threshold.

    Mirrors `flag_engine.utils.hashing.get_hashed_percentage_for_object_ids`
    via four 8-hex-char chunks combined modulo 9999. Diverges from the engine
    on the ~1/9999 inputs where the bare hash mod 9999 == 9998 (engine
    recurses with doubled input; we skip).
    """
    d = ctx.dialect
    seg_lit = Sanitiser.string_literal(seg_key)
    hash_subject = f"{seg_lit} || ',' || ({ctx_value_sql})"
    h = d.md5_hex(hash_subject)
    s1 = d.parse_hex_chunk(h, 1)
    s2 = d.parse_hex_chunk(h, 9)
    s3 = d.parse_hex_chunk(h, 17)
    s4 = d.parse_hex_chunk(h, 25)
    weighted = (
        f"{s1} * {_HASH_CONST_HIGH} + {s2} * {_HASH_CONST_MID} + {s3} * {_HASH_CONST_LOW} + {s4}"
    )
    return f"({d.mod(weighted, '9999')} / 9998.0 * 100.0 <= {float(threshold)})"


def _semver_sort_key_expr(ctx: TranslateContext, value_sql: str) -> str:
    """Sortable padded major.minor.patch key. String-comparing two outputs of
    this gives the engine's GT/GTE/LT/LTE/EQ/NE result for the
    major.minor.patch portion. Prerelease is ignored."""
    d = ctx.dialect
    parts = [
        d.lpad(d.coalesce(d.regexp_nth_digit_run(value_sql, n), "'0'"), 10, "'0'")
        for n in (1, 2, 3)
    ]
    return f"({parts[0]} || '.' || {parts[1]} || '.' || {parts[2]})"


# ---------------------------------------------------------------------------
# Trait-bound and direct comparisons. Both go against IDENTITIES alias `i`
# directly: trait conditions read `i."<trait>"`, JSONPath conditions read
# the appropriate identity column or env literal.
# ---------------------------------------------------------------------------


def _comparison(
    ctx: TranslateContext,
    op: str,
    expr: str,
    value: object,
    is_jsonpath: bool = False,
) -> str | None:
    """Emit a SQL fragment comparing `expr` against `value` per `op`.

    Used for both trait columns (VARIANT, cast as needed) and JSONPath
    references (already-typed columns or string literals).
    """
    if value is None:
        return None
    d = ctx.dialect
    lit = Sanitiser.string_literal(str(value))
    str_expr = expr if is_jsonpath else d.cast_string(expr)
    if op == "EQUAL":
        return f"{str_expr} = {lit}"
    if op == "NOT_EQUAL":
        return f"{str_expr} <> {lit}"
    if op == "IN":
        items = "','".join(Sanitiser.escape_string(v.strip()) for v in str(value).split(","))
        return f"{str_expr} IN ('{items}')"
    if op == "CONTAINS":
        return d.position(lit, str_expr)
    if op == "NOT_CONTAINS":
        return f"({expr} IS NOT NULL AND NOT ({d.position(lit, str_expr)}))"
    if op in {"GREATER_THAN", "LESS_THAN", "GREATER_THAN_INCLUSIVE", "LESS_THAN_INCLUSIVE"}:
        numeric_lit = Sanitiser.numeric_literal(value)
        if numeric_lit is None:
            return None
        sql_op = {
            "GREATER_THAN": ">",
            "LESS_THAN": "<",
            "GREATER_THAN_INCLUSIVE": ">=",
            "LESS_THAN_INCLUSIVE": "<=",
        }[op]
        return f"({expr} IS NOT NULL AND {d.cast_float(expr)} {sql_op} {numeric_lit})"
    if op == "MODULO":
        parsed = Sanitiser.modulo_literal(value)
        if parsed is None:
            return None
        divisor_lit, remainder_lit = parsed
        mod_expr = d.mod(d.cast_number(expr), divisor_lit)
        return f"({expr} IS NOT NULL AND ({mod_expr}) = {remainder_lit})"
    if op == "REGEX":
        if not _regex_safe_for_re2(str(value)):
            return None
        return f"({expr} IS NOT NULL AND {d.regexp_anchored_match(str_expr, lit)})"
    return None


# ---------------------------------------------------------------------------
# Condition translation: routes the operator to the right SQL shape.
# ---------------------------------------------------------------------------


def translate_condition(cond: SegmentCondition, ctx: TranslateContext) -> str | None:
    op = cond["operator"]
    if op not in TRANSLATABLE_OPERATORS:
        return None

    prop = cond.get("property") or ""
    val = cond.get("value")

    # PERCENTAGE_SPLIT — inline pure-SQL hash, no UDF.
    if op == "PERCENTAGE_SPLIT":
        if not ctx.segment_key:
            return None
        threshold_lit = Sanitiser.numeric_literal(val)
        if threshold_lit is None:
            return None
        threshold = float(threshold_lit)
        if not prop:
            value_expr = ctx.dialect.cast_string(ctx.identity_key_col)
        elif prop.startswith("$."):
            jp = ctx.jsonpath_expr(prop)
            if jp is None:
                return None
            value_expr = ctx.dialect.cast_string(jp)
        else:
            value_expr = ctx.dialect.cast_string(ctx.trait_path(prop))
        return _percentage_split_expr(ctx, ctx.segment_key, value_expr, threshold)

    if not prop:
        return None

    # JSONPath properties → direct column / env-constant comparison.
    if prop.startswith("$."):
        path = ctx.jsonpath_expr(prop)
        if path is None:
            return None
        if op == "IS_SET":
            return "TRUE"
        if op == "IS_NOT_SET":
            return "FALSE"
        return _comparison(ctx, op, path, val, is_jsonpath=True)

    # Trait-bound predicates → VARIANT path-extraction on i.traits:"<key>".
    path = ctx.trait_path(prop)
    if op == "IS_SET":
        return f"{path} IS NOT NULL"
    if op == "IS_NOT_SET":
        return f"{path} IS NULL"

    # Semver-marked comparator (segment value ends with `:semver`).
    if isinstance(val, str) and val.endswith(":semver"):
        if op not in {
            "EQUAL",
            "NOT_EQUAL",
            "GREATER_THAN",
            "LESS_THAN",
            "GREATER_THAN_INCLUSIVE",
            "LESS_THAN_INCLUSIVE",
        }:
            return None
        bare = val[:-7]
        sql_op = {
            "EQUAL": "=",
            "NOT_EQUAL": "<>",
            "GREATER_THAN": ">",
            "LESS_THAN": "<",
            "GREATER_THAN_INCLUSIVE": ">=",
            "LESS_THAN_INCLUSIVE": "<=",
        }[op]
        bare_lit = Sanitiser.string_literal(bare)
        col_str = ctx.dialect.cast_string(path)
        return (
            f"({path} IS NOT NULL AND "
            f"{_semver_sort_key_expr(ctx, col_str)} {sql_op} "
            f"{_semver_sort_key_expr(ctx, bare_lit)})"
        )

    return _comparison(ctx, op, path, val, is_jsonpath=False)


# ---------------------------------------------------------------------------
# Rule and segment translation: Boolean composition over conditions.
# ---------------------------------------------------------------------------


def translate_rule(rule: SegmentRule, ctx: TranslateContext) -> str | None:
    children: list[str] = []
    for cond in rule.get("conditions") or []:
        sql = translate_condition(cond, ctx)
        if sql is None:
            return None
        children.append(f"({sql})")
    for nested in rule.get("rules") or []:
        sql = translate_rule(nested, ctx)
        if sql is None:
            return None
        children.append(f"({sql})")

    rule_type = rule["type"]
    if not children:
        return "TRUE"
    if rule_type == "ALL":
        return " AND ".join(children)
    if rule_type == "ANY":
        return " OR ".join(children)
    if rule_type == "NONE":
        return f"NOT ({' OR '.join(children)})"
    return None


def translate_segment(segment: SegmentContext, ctx: TranslateContext) -> str | None:
    """Return a SQL `WHERE` expression for the segment, or None if any
    condition uses an operator the translator can't handle (REGEX with
    backreferences or lookarounds).

    The expression goes after `WHERE i.environment_id = '<env-key>' AND ...`.
    The caller composes the surrounding `SELECT ... FROM IDENTITIES i`.
    """
    if ctx.segment_key is None:
        ctx = ctx.with_segment_key(segment["key"])
    rules = segment.get("rules") or []
    if not rules:
        return "FALSE"
    rule_sql: list[str] = []
    for r in rules:
        sql = translate_rule(r, ctx)
        if sql is None:
            return None
        rule_sql.append(f"({sql})")
    return " AND ".join(rule_sql)
