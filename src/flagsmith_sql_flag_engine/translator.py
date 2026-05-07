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

import json
import re

import jsonpath_rfc9535
from flag_engine.context.types import (
    EvaluationContext,
    SegmentCondition,
    SegmentContext,
    SegmentRule,
)
from flag_engine.segments.evaluator import is_context_in_segment

from flagsmith_sql_flag_engine.dialect import Dialect
from flagsmith_sql_flag_engine.utils import (
    escape_string,
    modulo_literal,
    numeric_literal,
    string_literal,
)

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

    `evaluation_context` — flag_engine `EvaluationContext` TypedDict.
                        `context.identity` ignored since identity values come from
                        each `IDENTITIES` row at SQL execution time.
    `dialect`         — required implementation of the `Dialect` protocol.
                        The dialect owns the IDENTITIES schema, so column
                        references are derived via dialect methods rather
                        than configured here.
    `identities_alias` — table alias for `IDENTITIES` in the surrounding
                        query (default `i`).
    `segment_key`     — salts `PERCENTAGE_SPLIT`. Auto-injected from the
                        segment's `key` field by `translate_segment`.
    """

    def __init__(
        self,
        evaluation_context: EvaluationContext,
        dialect: Dialect,
        identities_alias: str = "i",
        segment_key: str | None = None,
    ) -> None:
        self.evaluation_context = evaluation_context
        self.dialect = dialect
        self.identities_alias = identities_alias
        self.segment_key = segment_key

    @property
    def identity_key_expr(self) -> str:
        return self.dialect.identity_key_expr(self.identities_alias)

    def trait_path(self, trait_key: str) -> str:
        """Dialect-specific path-extraction for a trait value."""
        return self.dialect.trait_path(self.identities_alias, trait_key)

    def jsonpath_expr(self, prop: str) -> str | None:
        # Identity properties are bound to the IDENTITIES row at execution
        # time, so they translate to column references rather than literals.
        if prop == "$.identity.identifier":
            return self.dialect.identifier_expr(self.identities_alias)
        if prop == "$.identity.key":
            return self.dialect.identity_key_expr(self.identities_alias)
        # Everything else is resolved against the eval context now and
        # baked into the generated SQL as a literal.
        try:
            compiled = jsonpath_rfc9535.compile(prop)
        except jsonpath_rfc9535.JSONPathSyntaxError:
            return None
        result = compiled.find_one(dict(self.evaluation_context))
        if result is None or result.value is None:
            return None
        value = result.value
        if not isinstance(value, (str, int, float, bool)):
            return None
        return string_literal(str(value))

    def with_segment_key(self, key: str) -> TranslateContext:
        return TranslateContext(
            evaluation_context=self.evaluation_context,
            dialect=self.dialect,
            identities_alias=self.identities_alias,
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
    seg_lit = string_literal(seg_key)
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


def _engine_static_verdict(ctx: TranslateContext, cond: SegmentCondition) -> str:
    """Run a single condition through `is_context_in_segment` against the
    eval context and emit `'TRUE'`/`'FALSE'`. Used for JSONPath conditions
    that don't reference row-bound state — the verdict is the same for
    every row in the resulting query, so we collapse it now."""
    fake_segment: SegmentContext = {
        "key": ctx.segment_key or "_static",
        "name": "_static",
        "rules": [{"type": "ALL", "conditions": [cond]}],
    }
    try:
        matches = is_context_in_segment(ctx.evaluation_context, fake_segment)
    except Exception:
        # Engine catches almost everything internally; if anything escapes
        # (mismatched type, unknown operator), default to "no match" so
        # the surrounding rule composition still produces sensible SQL.
        return "FALSE"
    return "TRUE" if matches else "FALSE"


def _engine_in_values(value: object) -> list[str] | None:
    """Mirror `flag_engine.segments.evaluator._get_in_values`: parse a segment
    value into a list of candidate strings. Returns None for inputs the
    engine doesn't accept (non-string, non-list)."""
    if isinstance(value, list):
        return [v if isinstance(v, str) else str(v) for v in value]
    if not isinstance(value, str):
        return None
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return value.split(",")
        if isinstance(parsed, list):
            return [v if isinstance(v, str) else str(v) for v in parsed]
    return value.split(",")


def _trait_typed_eq(ctx: TranslateContext, path: str, value: object, negate: bool) -> str:
    """Type-dispatched EQUAL/NOT_EQUAL on a VARIANT trait, mirroring engine
    semantics: segment value is cast to the trait's runtime type before
    comparison; cast failure → no match (False) for both ops.

    Each branch is gated by `TYPEOF(...) = '...'` rather than wrapped in
    a CASE statement; Snowflake's optimiser sometimes evaluates all CASE
    arms eagerly (e.g. casting a VARCHAR variant to BOOLEAN even when
    the BOOLEAN arm is unreachable for that row), and AND short-circuits
    reliably."""
    d = ctx.dialect
    sql_op = "<>" if negate else "="
    str_value = str(value)
    str_lit = string_literal(str_value)
    str_path = d.cast_string(path)
    # Engine bool cast: `lambda v: v not in ("False", "false")`. Compare via
    # the variant's lowercase string form so we never invoke `(...)::BOOLEAN`
    # on a non-bool variant.
    bool_str_lit = "'false'" if str_value in ("False", "false") else "'true'"
    bool_branch = f"({str_path}) {sql_op} {bool_str_lit}"
    # Engine int/float cast: int(v) / float(v); ValueError → no match.
    try:
        int_lit: str | None = str(int(str_value))
    except (ValueError, TypeError):
        int_lit = None
    try:
        float_lit: str | None = repr(float(str_value))
    except (ValueError, TypeError):
        float_lit = None
    no_match = "FALSE"  # engine returns False on cast failure for both EQUAL and NOT_EQUAL
    int_branch = f"({path})::NUMBER {sql_op} {int_lit}" if int_lit is not None else no_match
    float_branch = f"({path})::FLOAT {sql_op} {float_lit}" if float_lit is not None else no_match
    return (
        f"((TYPEOF({path}) = 'BOOLEAN' AND {bool_branch})"
        f" OR (TYPEOF({path}) = 'INTEGER' AND {int_branch})"
        f" OR (TYPEOF({path}) IN ('DECIMAL', 'DOUBLE') AND {float_branch})"
        f" OR (TYPEOF({path}) NOT IN ('BOOLEAN', 'INTEGER', 'DECIMAL', 'DOUBLE')"
        f" AND {str_path} {sql_op} {str_lit}))"
    )


def _trait_typed_in(ctx: TranslateContext, path: str, value: object) -> str | None:
    """Type-dispatched IN on a VARIANT trait, mirroring engine semantics:
    int trait stringifies and looks up against the parsed string set; string
    trait does direct lookup; other types never match."""
    items = _engine_in_values(value)
    if items is None:
        return None
    item_lits = ",".join(string_literal(v) for v in items)
    str_path = ctx.dialect.cast_string(path)
    return (
        f"(CASE TYPEOF({path})"
        f" WHEN 'INTEGER' THEN {str_path} IN ({item_lits})"
        f" WHEN 'VARCHAR' THEN {str_path} IN ({item_lits})"
        f" ELSE FALSE"
        f" END)"
    )


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
    lit = string_literal(str(value))
    str_expr = expr if is_jsonpath else d.cast_string(expr)
    if op == "EQUAL":
        return f"{str_expr} = {lit}"
    if op == "NOT_EQUAL":
        return f"{str_expr} <> {lit}"
    if op == "IN":
        items = "','".join(escape_string(v.strip()) for v in str(value).split(","))
        return f"{str_expr} IN ('{items}')"
    if op == "CONTAINS":
        return d.position(lit, str_expr)
    if op == "NOT_CONTAINS":
        return f"({expr} IS NOT NULL AND NOT ({d.position(lit, str_expr)}))"
    if op in {"GREATER_THAN", "LESS_THAN", "GREATER_THAN_INCLUSIVE", "LESS_THAN_INCLUSIVE"}:
        numeric_lit = numeric_literal(value)
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
        parsed = modulo_literal(value)
        if parsed is None:
            # Bad operand (`""`, `"invalid|value"`, etc.) — engine catches
            # the cast error and returns False.
            return "FALSE"
        divisor_lit, remainder_lit = parsed
        mod_expr = d.mod(d.cast_number(expr), divisor_lit)
        return f"({expr} IS NOT NULL AND ({mod_expr}) = {remainder_lit})"
    if op == "REGEX":
        pattern = str(value)
        if not _regex_safe_for_re2(pattern):
            return None
        return f"({expr} IS NOT NULL AND {d.regexp_anchored_match(str_expr, pattern)})"
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
        threshold_lit = numeric_literal(val)
        if threshold_lit is None:
            return None
        threshold = float(threshold_lit)
        identity: dict[str, object] = ctx.evaluation_context.get("identity") or {}  # type: ignore[assignment]
        if not prop:
            # Implicit `$.identity.key` — engine returns False when no
            # identity, or when the identity lacks `key` (the engine never
            # synthesises one from env+identifier).
            if not identity.get("key"):
                return "FALSE"
            value_expr = ctx.dialect.cast_string(ctx.identity_key_expr)
        elif prop.startswith("$."):
            # Identity-bound jsonpath against an absent value → engine
            # treats the looked-up value as None and PERCENTAGE_SPLIT bails.
            if prop == "$.identity.key" and not identity.get("key"):
                return "FALSE"
            if prop == "$.identity.identifier" and not identity.get("identifier"):
                return "FALSE"
            jp = ctx.jsonpath_expr(prop)
            if jp is None:
                return None
            value_expr = ctx.dialect.cast_string(jp)
        else:
            traits = identity.get("traits") or {}
            if not isinstance(traits, dict) or prop not in traits:
                return "FALSE"
            value_expr = ctx.dialect.cast_string(ctx.trait_path(prop))
        return _percentage_split_expr(ctx, ctx.segment_key, value_expr, threshold)

    if not prop:
        return None

    # JSONPath properties: row-bound identity columns stay as column refs;
    # everything else resolves against the eval context (constant for every
    # row in the query) and gets pre-computed via the engine. Properties
    # that start with `$.` but don't parse as JSONPath fall back to a trait
    # lookup with `prop` used verbatim as the key — same as the engine.
    if prop.startswith("$.") and prop not in ("$.identity.identifier", "$.identity.key"):
        try:
            jsonpath_rfc9535.compile(prop)
        except jsonpath_rfc9535.JSONPathSyntaxError:
            pass  # treat as trait key — fall through to trait branch
        else:
            return _engine_static_verdict(ctx, cond)
    elif prop.startswith("$."):
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
        bare_lit = string_literal(bare)
        col_str = ctx.dialect.cast_string(path)
        return (
            f"({path} IS NOT NULL AND "
            f"{_semver_sort_key_expr(ctx, col_str)} {sql_op} "
            f"{_semver_sort_key_expr(ctx, bare_lit)})"
        )

    # Type-aware comparators on VARIANT traits — mirror flag_engine's
    # per-type coercion of the segment value before compare.
    if op in {"EQUAL", "NOT_EQUAL"} and val is not None:
        negate = op == "NOT_EQUAL"
        return f"({path} IS NOT NULL AND {_trait_typed_eq(ctx, path, val, negate=negate)})"
    if op == "IN":
        in_pred = _trait_typed_in(ctx, path, val)
        if in_pred is None:
            return None
        return f"({path} IS NOT NULL AND {in_pred})"

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
