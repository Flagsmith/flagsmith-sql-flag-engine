"""Translate `SegmentContext` predicate trees into SQL `WHERE` expressions.

The translator emits a predicate that goes against an `IDENTITIES` table
(aliased `i`) directly. Trait-bound conditions become `EXISTS` subqueries
against a `TRAITS` table; `PERCENTAGE_SPLIT` and `:semver`-marked
comparators compile to inline pure-SQL using the active `Dialect`.

Output shape::

    SELECT ... FROM IDENTITIES i
    WHERE i.environment_id = '<env-key>'
      AND <returned expression>

The caller is responsible for the surrounding query. The translator only
produces the predicate.

`environment_id` in the `IDENTITIES` and `TRAITS` tables is a string column
holding `EnvironmentContext.key` — the same identifier the engine uses.
There is no separate integer PK.

Returns `None` if any condition uses an untranslatable operator —
specifically REGEX patterns containing backreferences or lookarounds
(RE2 doesn't support them). Callers should fall back to the Python
flag_engine for those segments, or surface an error at segment-edit time.
"""

from __future__ import annotations

import re
from typing import Optional

from flag_engine.context.types import EnvironmentContext, SegmentContext

from flagsmith_sql_flag_engine.dialect import Dialect
from flagsmith_sql_flag_engine.dialects.snowflake import SnowflakeDialect

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
    r"\\\d"        # backreference like \1 .. \9
    r"|\(\?[=!<]"  # lookahead / lookbehind / negative variants
)


def _regex_safe_for_re2(pattern: str) -> bool:
    return _RE2_UNSAFE.search(pattern) is None


def _q(s: str) -> str:
    """SQL-escape a single-quoted string literal."""
    return s.replace("'", "''")


# Constants for chunked MD5-mod-9999 hash. The engine computes
# `int(md5_hex, 16) % 9999`; we split the 32-hex digest into four 8-hex
# chunks, parse each as a 32-bit int, and combine via modular arithmetic.
# Constants are (16^24, 16^16, 16^8) mod 9999, precomputed.
_HASH_CONST_HIGH = 7291   # 16^24 mod 9999
_HASH_CONST_MID = 1897    # 16^16 mod 9999
_HASH_CONST_LOW = 6835    # 16^8 mod 9999


# ---------------------------------------------------------------------------
# Context: shape information the translator needs to produce correct refs.
# ---------------------------------------------------------------------------


class TranslateContext:
    """Inputs the translator needs to produce a query for a specific shape.

    `environment`     — flag_engine `EnvironmentContext` TypedDict
                        (`{"key": str, "name": str}`). `key` is the identifier
                        used as `environment_id` in the `IDENTITIES` and
                        `TRAITS` tables; `name` is referenced by
                        `$.environment.name` JSONPath properties.
    `dialect`         — implementation of the `Dialect` protocol.
    `traits_table`    — `TRAITS` table identifier (optionally schema-qualified).
    `identifier_col`  — column on alias `i` for `$.identity.identifier`.
    `identity_key_col` — column on alias `i` for `$.identity.key`.
    `identity_id_col` — column on alias `i` joined to `TRAITS.identity_id`.
    `segment_key`     — salts `PERCENTAGE_SPLIT`. Auto-injected from the
                        segment's `key` field by `translate_segment`.
    """

    def __init__(
        self,
        environment: EnvironmentContext,
        dialect: Optional[Dialect] = None,
        traits_table: str = "TRAITS",
        identifier_col: str = "i.identifier",
        identity_key_col: str = "i.identity_key",
        identity_id_col: str = "i.id",
        segment_key: Optional[str] = None,
    ) -> None:
        self.environment = environment
        self.dialect: Dialect = dialect if dialect is not None else SnowflakeDialect()
        self.traits_table = traits_table
        self.identifier_col = identifier_col
        self.identity_key_col = identity_key_col
        self.identity_id_col = identity_id_col
        self.segment_key = segment_key

    @property
    def env_id_lit(self) -> str:
        """SQL string literal for the environment id (= EnvironmentContext.key)."""
        return f"'{_q(self.environment['key'])}'"

    def jsonpath_expr(self, prop: str) -> Optional[str]:
        if prop == "$.identity.identifier":
            return self.identifier_col
        if prop == "$.identity.key":
            return self.identity_key_col
        if prop == "$.environment.name":
            return f"'{_q(self.environment['name'])}'"
        if prop == "$.environment.key":
            return self.env_id_lit
        return None

    def with_segment_key(self, key: str) -> "TranslateContext":
        return TranslateContext(
            environment=self.environment,
            dialect=self.dialect,
            traits_table=self.traits_table,
            identifier_col=self.identifier_col,
            identity_key_col=self.identity_key_col,
            identity_id_col=self.identity_id_col,
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
    seg_lit = f"'{_q(seg_key)}'"
    hash_subject = f"{seg_lit} || ',' || ({ctx_value_sql})"
    h = d.md5_hex(hash_subject)
    s1 = d.parse_hex_chunk(h, 1)
    s2 = d.parse_hex_chunk(h, 9)
    s3 = d.parse_hex_chunk(h, 17)
    s4 = d.parse_hex_chunk(h, 25)
    weighted = (
        f"{s1} * {_HASH_CONST_HIGH}"
        f" + {s2} * {_HASH_CONST_MID}"
        f" + {s3} * {_HASH_CONST_LOW}"
        f" + {s4}"
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
# EXISTS-subquery builders for trait-bound predicates.
# ---------------------------------------------------------------------------


def _exists_for_trait(
    ctx: TranslateContext, trait_key: str, body: str, negate: bool = False
) -> str:
    not_kw = "NOT " if negate else ""
    return (
        f"{not_kw}EXISTS ("
        f"SELECT 1 FROM {ctx.traits_table} t "
        f"WHERE t.environment_id = {ctx.env_id_lit} "
        f"AND t.identity_id = {ctx.identity_id_col} "
        f"AND t.trait_key = '{_q(trait_key)}' "
        f"AND {body}"
        f")"
    )


def _exists_any_trait(
    ctx: TranslateContext, trait_key: str, negate: bool = False
) -> str:
    not_kw = "NOT " if negate else ""
    return (
        f"{not_kw}EXISTS ("
        f"SELECT 1 FROM {ctx.traits_table} t "
        f"WHERE t.environment_id = {ctx.env_id_lit} "
        f"AND t.identity_id = {ctx.identity_id_col} "
        f"AND t.trait_key = '{_q(trait_key)}'"
        f")"
    )


def _trait_predicate(ctx: TranslateContext, op: str, value: object) -> Optional[str]:
    """SQL fragment that filters a TRAITS row given operator + value.

    Goes inside the `AND <body>` of an EXISTS subquery. Operates on
    `string_value` / `integer_value` / `float_value` columns.
    """
    d = ctx.dialect
    if op == "EQUAL":
        return f"string_value = '{_q(str(value))}'"
    if op == "NOT_EQUAL":
        return f"string_value <> '{_q(str(value))}'"
    if op == "IN":
        items = "','".join(_q(v.strip()) for v in str(value).split(","))
        return f"string_value IN ('{items}')"
    if op == "CONTAINS":
        return d.position(f"'{_q(str(value))}'", "string_value")
    if op == "NOT_CONTAINS":
        needle_lit = f"'{_q(str(value))}'"
        return f"string_value IS NOT NULL AND NOT ({d.position(needle_lit, 'string_value')})"
    if op == "MODULO":
        try:
            divisor, remainder = str(value).split("|")
            float(divisor)
            float(remainder)
        except (ValueError, AttributeError):
            return None
        return (
            f"integer_value IS NOT NULL "
            f"AND ({d.mod('integer_value', divisor)}) = {remainder}"
        )
    if op in {
        "GREATER_THAN",
        "LESS_THAN",
        "GREATER_THAN_INCLUSIVE",
        "LESS_THAN_INCLUSIVE",
    }:
        sql_op = {
            "GREATER_THAN": ">",
            "LESS_THAN": "<",
            "GREATER_THAN_INCLUSIVE": ">=",
            "LESS_THAN_INCLUSIVE": "<=",
        }[op]
        numeric = d.coalesce("integer_value", "float_value")
        return f"{numeric} IS NOT NULL AND {numeric} {sql_op} {value}"
    if op == "REGEX":
        if not _regex_safe_for_re2(str(value)):
            return None
        pattern_lit = f"'{_q(str(value))}'"
        return (
            f"string_value IS NOT NULL "
            f"AND {d.regexp_anchored_match('string_value', pattern_lit)}"
        )
    return None


# ---------------------------------------------------------------------------
# Direct comparison: for non-trait references (JSONPath columns, env consts).
# ---------------------------------------------------------------------------


def _direct_comparison(
    ctx: TranslateContext, op: str, expr: str, value: object, is_jsonpath: bool
) -> Optional[str]:
    if value is None:
        return None
    d = ctx.dialect
    lit = f"'{_q(str(value))}'"
    str_expr = expr if is_jsonpath else d.cast_string(expr)
    if op == "EQUAL":
        return f"{str_expr} = {lit}"
    if op == "NOT_EQUAL":
        return f"{str_expr} <> {lit}"
    if op == "IN":
        items = "','".join(_q(v.strip()) for v in str(value).split(","))
        return f"{str_expr} IN ('{items}')"
    if op == "CONTAINS":
        return d.position(lit, str_expr)
    if op == "NOT_CONTAINS":
        return f"({expr} IS NOT NULL AND NOT ({d.position(lit, str_expr)}))"
    if op in {"GREATER_THAN", "LESS_THAN", "GREATER_THAN_INCLUSIVE", "LESS_THAN_INCLUSIVE"}:
        sql_op = {
            "GREATER_THAN": ">",
            "LESS_THAN": "<",
            "GREATER_THAN_INCLUSIVE": ">=",
            "LESS_THAN_INCLUSIVE": "<=",
        }[op]
        return f"({expr} IS NOT NULL AND {d.cast_float(expr)} {sql_op} {value})"
    if op == "REGEX":
        if not _regex_safe_for_re2(str(value)):
            return None
        return f"({expr} IS NOT NULL AND {d.regexp_anchored_match(str_expr, lit)})"
    return None


# ---------------------------------------------------------------------------
# Condition translation: routes the operator to the right SQL shape.
# ---------------------------------------------------------------------------


def translate_condition(
    cond: dict, ctx: TranslateContext
) -> Optional[str]:
    op = cond["operator"]
    if op not in TRANSLATABLE_OPERATORS:
        return None

    prop = cond.get("property") or ""
    val = cond.get("value")

    # PERCENTAGE_SPLIT — inline pure-SQL hash, no UDF.
    if op == "PERCENTAGE_SPLIT":
        if not ctx.segment_key:
            return None
        if not prop:
            return _percentage_split_expr(
                ctx, ctx.segment_key, ctx.dialect.cast_string(ctx.identity_key_col), float(val)
            )
        if prop.startswith("$."):
            jp = ctx.jsonpath_expr(prop)
            if jp is None:
                return None
            return _percentage_split_expr(
                ctx, ctx.segment_key, ctx.dialect.cast_string(jp), float(val)
            )
        body = _percentage_split_expr(ctx, ctx.segment_key, "string_value", float(val))
        return _exists_for_trait(ctx, prop, body)

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
        return _direct_comparison(ctx, op, path, val, is_jsonpath=True)

    # IS_SET / IS_NOT_SET → existence check on TRAITS.
    if op == "IS_SET":
        return _exists_any_trait(ctx, prop, negate=False)
    if op == "IS_NOT_SET":
        return _exists_any_trait(ctx, prop, negate=True)

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
        bare_lit = f"'{_q(bare)}'"
        body = (
            f"string_value IS NOT NULL AND "
            f"{_semver_sort_key_expr(ctx, 'string_value')} {sql_op} "
            f"{_semver_sort_key_expr(ctx, bare_lit)}"
        )
        return _exists_for_trait(ctx, prop, body)

    # Standard trait-bound predicate → EXISTS subquery.
    body_pred = _trait_predicate(ctx, op, val)
    if body_pred is None:
        return None
    return _exists_for_trait(ctx, prop, body_pred)


# ---------------------------------------------------------------------------
# Rule and segment translation: Boolean composition over conditions.
# ---------------------------------------------------------------------------


def translate_rule(rule: dict, ctx: TranslateContext) -> Optional[str]:
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


def translate_segment(
    segment: SegmentContext, ctx: TranslateContext
) -> Optional[str]:
    """Return a SQL `WHERE` expression for the segment, or None if any
    condition uses an operator the translator can't handle (REGEX with
    backreferences or lookarounds).

    The expression goes after `WHERE i.environment_id = '<env-key>' AND ...`.
    The caller composes the surrounding `SELECT ... FROM IDENTITIES i`.
    """
    if ctx.segment_key is None and "key" in segment:
        ctx = ctx.with_segment_key(str(segment["key"]))
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
