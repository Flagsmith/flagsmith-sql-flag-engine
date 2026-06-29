"""Translate `SegmentContext` predicate trees into SQL `WHERE` expressions.

Output drops into:

    SELECT ... FROM IDENTITIES i
    WHERE i.environment_id = '<env-key>' AND <translator output>

Returns `None` if any condition uses an operator the active dialect
can't translate — callers fall back to `flag_engine.is_context_in_segment`.
"""

import json
from typing import Literal, NamedTuple

import jsonpath_rfc9535
from flag_engine.context.types import (
    EvaluationContext,
    SegmentCondition,
    SegmentContext,
    SegmentRule,
)
from flag_engine.segments.evaluator import is_context_in_segment
from flag_engine.segments.types import ConditionOperator

from flagsmith_sql_flag_engine.dialect import Dialect
from flagsmith_sql_flag_engine.utils import (
    escape_string,
    modulo_literal,
    numeric_literal,
    string_literal,
)

TRANSLATABLE_OPERATORS: frozenset[ConditionOperator] = frozenset(
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

    `evaluation_context` is a flag_engine `EvaluationContext`. Its
    `identity` field is ignored since identity values come from each
    `IDENTITIES` row at SQL execution time. `dialect` is an
    implementation of the `Dialect` protocol; it owns the IDENTITIES
    schema, so column references come from dialect methods rather than
    being configured here. `identities_alias` is the table alias for
    `IDENTITIES` in the surrounding query — defaults to `i`.
    `segment_key` salts `PERCENTAGE_SPLIT` and is auto-injected from
    the segment's `key` field by `translate_segment`.
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

    def jsonpath_expr(self, prop: Literal["$.identity.identifier", "$.identity.key"]) -> str:
        # Only the row-bound identity columns need an SQL expression — every
        # other JSONPath property is resolved against the eval context up in
        # `translate_condition` via `_engine_static_verdict`.
        match prop:
            case "$.identity.identifier":
                return self.dialect.identifier_expr(self.identities_alias)
            case "$.identity.key":
                return self.dialect.identity_key_expr(self.identities_alias)

    def with_segment_key(self, key: str) -> "TranslateContext":
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
    via four 8-hex-char chunks combined modulo 9999. Diverges from the
    engine on the ~1/9999 inputs where the bare hash mod 9999 == 9998 —
    the engine recurses with doubled input; we don't.
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


JsonpathKind = Literal[
    "identifier",
    "key",
    "trait",
    "identity_object",
    "untranslatable",
    "static",
]


class JsonpathClassification(NamedTuple):
    """What a JSONPath property resolves to in the SQL setting.

    `kind` selects the shape; `trait_key` carries the trait name only when
    `kind == "trait"`.
    """

    kind: JsonpathKind
    trait_key: str | None = None


def _classify_jsonpath(prop: str) -> JsonpathClassification:
    """Classify a JSONPath property by what it resolves to in the SQL setting.

    Identity is per-row in our query model — each `IDENTITIES` row IS an
    identity — but the engine treats `$.identity.*` as a lookup against
    the eval-context identity. Most identity-bound paths therefore need
    to map to a row reference, not be statically pre-computed against
    the eval context.

    A `prop` that doesn't parse as JSONPath classifies as a trait keyed
    by the prop string itself — the engine treats unparseable `$.`-
    prefixed properties as literal trait keys, and we mirror that.
    """
    try:
        compiled = jsonpath_rfc9535.compile(prop)
    except jsonpath_rfc9535.JSONPathSyntaxError:
        return JsonpathClassification("trait", prop)
    names: list[str] = []
    for s in compiled.segments:
        if len(s.selectors) != 1:  # pragma: no cover - multi-selector segments not in dataset
            break
        name = getattr(s.selectors[0], "name", None)
        if name is None:
            break
        names.append(name)
    else:
        if names and names[0] == "identity":
            if len(names) == 1:
                # `$.identity` — the whole identity object. Every row in
                # the IDENTITIES table IS an identity by construction,
                # so we don't go through the eval context — which may or
                # may not carry an identity, depending on caller. The
                # translator encodes the row-truth directly: IS_SET →
                # TRUE, IS_NOT_SET → FALSE, scalar comparators → FALSE,
                # mirroring the engine's fail-cast on a dict.
                return JsonpathClassification("identity_object")
            if len(names) == 2 and names[1] == "identifier":
                return JsonpathClassification("identifier")
            if len(names) == 2 and names[1] == "key":
                return JsonpathClassification("key")
            if len(names) == 3 and names[1] == "traits":
                return JsonpathClassification("trait", names[2])
            return JsonpathClassification("untranslatable")
    if names and names[0] == "identity":
        # Identity path with non-name selectors — wildcards, filters,
        # etc. — we can't map those to fixed row references.
        return JsonpathClassification("untranslatable")
    return JsonpathClassification("static")


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
    matches = is_context_in_segment(ctx.evaluation_context, fake_segment)
    return "TRUE" if matches else "FALSE"


def _engine_in_values(value: object) -> list[str] | None:
    """Mirror `flag_engine.segments.evaluator._get_in_values`: parse a segment
    value into a list of candidate strings. Returns None for inputs the
    engine doesn't accept — anything that's neither a string nor a list."""
    if isinstance(value, list):
        return [v if isinstance(v, str) else str(v) for v in value]
    if not isinstance(value, str):
        return None
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return value.split(",")
        if isinstance(parsed, list):  # pragma: no branch - `[`-prefixed valid JSON parses as a list
            return [v if isinstance(v, str) else str(v) for v in parsed]
    return value.split(",")


def _comparison(
    ctx: TranslateContext,
    op: str,
    expr: str,
    value: object,
    is_jsonpath: bool = False,
) -> str | None:
    """Emit a SQL fragment comparing `expr` against `value` per `op`.

    Used for both trait references — cast via the dialect as needed —
    and JSONPath references, which arrive as already-typed columns or
    string literals.

    Returns `None` only for genuinely untranslatable inputs such as a
    REGEX pattern the active dialect's regex flavour can't compile.
    Inputs the engine evaluates to a deterministic False — missing
    value, non-numeric operand on a comparator — compile to `"FALSE"`.
    """
    if value is None:
        return "FALSE"
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
            # Engine: float() on a non-numeric operand raises → returns False.
            return "FALSE"
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
            # Bad operand — empty string, missing separator, non-numeric
            # side. Engine catches the cast error and returns False.
            return "FALSE"
        divisor_lit, remainder_lit = parsed
        mod_expr = d.mod(d.cast_number(expr), divisor_lit)
        return f"({expr} IS NOT NULL AND ({mod_expr}) = {remainder_lit})"
    if op == "REGEX":
        pattern = str(value)
        if not d.regex_supports(pattern):
            return None
        return f"({expr} IS NOT NULL AND {d.regexp_anchored_match(str_expr, pattern)})"
    raise AssertionError(  # pragma: no cover - all TRANSLATABLE_OPERATORS handled above
        f"unhandled translatable operator in _comparison: {op}"
    )


# ---------------------------------------------------------------------------
# Condition translation: routes the operator to the right SQL shape.
# ---------------------------------------------------------------------------


_SEMVER_OPS = {
    "EQUAL": "=",
    "NOT_EQUAL": "<>",
    "GREATER_THAN": ">",
    "LESS_THAN": "<",
    "GREATER_THAN_INCLUSIVE": ">=",
    "LESS_THAN_INCLUSIVE": "<=",
}


def _translate_trait_op(
    ctx: TranslateContext,
    trait_key: str,
    op: ConditionOperator,
    val: object,
) -> str | None:
    """Translate `op` on a literal trait key into SQL. Returns `None`
    for inputs the translator can't compile, such as a REGEX pattern
    the active dialect rejects."""
    path = ctx.trait_path(trait_key)
    if op == "IS_SET":
        return f"{path} IS NOT NULL"
    if op == "IS_NOT_SET":
        return f"{path} IS NULL"

    # Semver-marked comparator — the segment value ends with `:semver`.
    # Engine only invokes its semver path for the comparators below;
    # other operators treat the `:semver` suffix as ordinary string
    # content, which is what the fall-through handlers already do.
    if isinstance(val, str) and val.endswith(":semver") and op in _SEMVER_OPS:
        bare = val[:-7]
        bare_lit = string_literal(bare)
        col_str = ctx.dialect.cast_string(path)
        return (
            f"({path} IS NOT NULL AND "
            f"{_semver_sort_key_expr(ctx, col_str)} {_SEMVER_OPS[op]} "
            f"{_semver_sort_key_expr(ctx, bare_lit)})"
        )

    # Type-aware comparators on traits — delegate to the dialect. The
    # discriminator funcs like TYPEOF / IS_*, runtime type-coercion
    # casts, and short-circuit pitfalls are all engine-specific.
    if op in {"EQUAL", "NOT_EQUAL"} and val is not None:
        negate = op == "NOT_EQUAL"
        eq_pred = ctx.dialect.trait_eq(ctx.identities_alias, trait_key, val, negate=negate)
        return f"({path} IS NOT NULL AND {eq_pred})"
    if op == "IN":
        items = _engine_in_values(val)
        if items is None:
            # Bad IN value — neither a string nor a list. Engine returns
            # False.
            return "FALSE"
        in_pred = ctx.dialect.trait_in(ctx.identities_alias, trait_key, items)
        return f"({path} IS NOT NULL AND {in_pred})"

    return _comparison(ctx, op, path, val, is_jsonpath=False)


def translate_condition(cond: SegmentCondition, ctx: TranslateContext) -> str | None:
    op = cond["operator"]
    if op not in TRANSLATABLE_OPERATORS:
        return None

    prop = cond.get("property") or ""
    val = cond.get("value")

    # Classify the property up front. Identity-bound JSONPaths —
    # `$.identity.identifier`, `$.identity.key`, `$.identity.traits.<x>` —
    # map to row references; non-identity JSONPaths are eval-ctx-bound,
    # constant for every row, and get pre-computed via the engine. Bare
    # trait keys bypass the JSONPath compile — they're classified as a
    # literal trait lookup directly.
    classification = (
        _classify_jsonpath(prop) if prop.startswith("$.") else JsonpathClassification("trait", prop)
    )
    if classification.kind == "trait":
        # Trait keys carried via `$.identity.traits.<x>` arrive normalised
        # to the bare key; literal trait keys come through untouched.
        assert classification.trait_key is not None
        prop = classification.trait_key

    # PERCENTAGE_SPLIT — inline pure-SQL hash.
    if op == "PERCENTAGE_SPLIT":
        # `translate_segment` always injects `segment_key` from the segment
        # before recursing; reaching here without one means a caller invoked
        # `translate_condition` directly with a half-formed context.
        assert ctx.segment_key is not None, (
            "PERCENTAGE_SPLIT requires a segment_key as the hash salt"
        )
        threshold_lit = numeric_literal(val)
        if threshold_lit is None:
            # Engine: float() on the threshold raises → returns False.
            return "FALSE"
        threshold = float(threshold_lit)
        identity: dict[str, object] = ctx.evaluation_context.get("identity") or {}  # type: ignore[assignment]
        kind = classification.kind
        if not prop:
            # Implicit `$.identity.key`. The key is always present in the store,
            # so the split is translatable whether or not the evaluation context
            # carries an identity. This intentionally diverges from the engine's
            # "no identity → False" verdict.
            value_expr = ctx.dialect.cast_string(ctx.identity_key_expr)
        elif kind == "key":
            value_expr = ctx.dialect.cast_string(ctx.jsonpath_expr("$.identity.key"))
        elif kind == "identifier":
            value_expr = ctx.dialect.cast_string(ctx.jsonpath_expr("$.identity.identifier"))
        elif kind == "identity_object":
            # PERCENTAGE_SPLIT on `$.identity` — the whole dict. Engine
            # hashes `str(dict)`, which is a stable but useless subject;
            # nobody writes this in practice. Treat as untranslatable.
            return None
        elif kind == "untranslatable":
            # `$.identity.<X>` we don't represent in the row schema.
            return None
        elif kind == "static":
            # Non-identity JSONPath: the engine hashes the resolved value.
            # We'd need to bake it as a literal hash subject — leave for
            # future work and let the caller fall back to the engine.
            return None
        else:
            # Plain trait key, or `$.identity.traits.<X>` rewritten to
            # the bare key. Hash subject pulls from `i.traits:"<key>"`
            # per row.
            traits = identity.get("traits") or {}
            if not isinstance(traits, dict) or prop not in traits:
                return "FALSE"
            value_expr = ctx.dialect.cast_string(ctx.trait_path(prop))
        return _percentage_split_expr(ctx, ctx.segment_key, value_expr, threshold)

    if not prop:
        # Non-PERCENTAGE_SPLIT condition without a property — engine looks up
        # nothing, the comparator's cast fails, returns False.
        return "FALSE"

    if classification.kind == "trait":
        return _translate_trait_op(ctx, prop, op, val)

    # Non-trait classifications. We don't replicate the engine's per-row
    # trait-first dispatch — it would roughly double the cost of every
    # wrapped JSONPath condition. A row that happens to carry a trait
    # literally named e.g. `$.identity` would shadow our resolution.
    # Niche shape; the engine-parity suite xfails the one engine-test-
    # data case that hits it.
    if classification.kind in ("identifier", "key"):
        path = ctx.jsonpath_expr(
            "$.identity.identifier" if classification.kind == "identifier" else "$.identity.key"
        )
        if op == "IS_SET":
            return "TRUE"
        if op == "IS_NOT_SET":
            return "FALSE"
        return _comparison(ctx, op, path, val, is_jsonpath=True)
    if classification.kind == "identity_object":
        # `$.identity` — engine treats non-primitive lookups as "not
        # set" by design; no operator meaningfully takes an object. So
        # IS_SET → FALSE, IS_NOT_SET → TRUE, every scalar comparator
        # fail-casts on the dict → FALSE. The SQL answer is the same
        # for every row regardless of whether the eval context carries
        # an identity, so we encode it directly.
        return "TRUE" if op == "IS_NOT_SET" else "FALSE"
    if classification.kind == "untranslatable":
        # Identity-bound JSONPath we can't map to row state — caller falls
        # back to the engine.
        return None
    # static
    return _engine_static_verdict(ctx, cond)


# ---------------------------------------------------------------------------
# Rule and segment translation: Boolean composition over conditions.
# ---------------------------------------------------------------------------


def translate_rule(rule: SegmentRule, ctx: TranslateContext) -> str | None:
    cond_children: list[str] = []
    for cond in rule.get("conditions") or []:
        sql = translate_condition(cond, ctx)
        if sql is None:
            return None
        cond_children.append(f"({sql})")
    rule_children: list[str] = []
    for nested in rule.get("rules") or []:
        sql = translate_rule(nested, ctx)
        if sql is None:
            return None
        rule_children.append(f"({sql})")

    # Mirror the engine's `context_matches_rule`: conditions and nested rules
    # are two independent groups AND-ed together, each vacuously true when
    # empty.
    op = {"ALL": " AND ", "ANY": " OR ", "NONE": " OR "}[rule["type"]]
    groups = [
        f"NOT ({op.join(c)})" if rule["type"] == "NONE" else op.join(c)
        for c in (cond_children, rule_children)
        if c
    ]
    if not groups:
        return "TRUE"
    if len(groups) == 1:
        return groups[0]
    return " AND ".join(f"({g})" for g in groups)


def translate_segment(segment: SegmentContext, ctx: TranslateContext) -> str | None:
    """Return a SQL `WHERE` expression for the segment.

    Output shape::

        SELECT ... FROM IDENTITIES i
        WHERE i.environment_id = '<env-key>'
          AND <returned expression>

    The caller composes the surrounding query; the translator only
    produces the predicate.

    Returns `None` if any condition uses an untranslatable operator —
    currently a REGEX pattern the active dialect's regex flavour can't
    compile. Callers should fall back to
    `flag_engine.is_context_in_segment` for those segments.
    """
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
