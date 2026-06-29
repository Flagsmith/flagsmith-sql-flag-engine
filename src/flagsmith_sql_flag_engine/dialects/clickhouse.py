"""ClickHouse dialect: SQL fragments tailored to ClickHouse's function set.

## Expected schema

The translator emits predicates against a single `IDENTITIES` table —
four typed columns `environment_id`, `id`, `identifier`, `identity_key`,
plus one `JSON` column `traits` holding the identity's full trait map
in ClickHouse's native columnar JSON layout. Trait keys are JSON paths
on the column, not schema columns.

The `JSON` type was chosen over `Nullable(String)` + `JSONExtract*`
because:

  - It stores each path as a typed subcolumn, so trait reads are a
    direct columnar scan — no per-row JSON parse. Empirically: at 870M
    rows on a Cloud trial, simple/multi predicates dropped from 14-20×
    slower than Snowflake VARIANT to within 2.5-4×. The wide-String
    variant scales linearly with row count where Snowflake / `JSON`
    stay near-flat.
  - Schema evolution is implicit: new trait keys appear as new
    subcolumns at INSERT time, no DDL change.
  - It matches Snowflake `VARIANT`'s semantic model — same NULL-on-miss
    behaviour, same type discrimination, same path syntax cost shape.

The trade-off is that ClickHouse caps `max_dynamic_paths` per JSON
column (default 1024). Above that, additional paths spill into a
`Dynamic` catch-all and lose the columnar fast path. This is fine for
typical Flagsmith trait vocabularies; we should monitor.

## Notable choices

  - Subcolumn access uses backtick-quoted identifiers: ``i.traits.`key` ``.
    Backticks are doubled to escape; arbitrary trait keys including
    spaces and dots are supported. CH's `getSubcolumn(json, 'key')`
    function works but doesn't compose with the typed-variant `.:Type`
    accessor, so we standardise on backtick form everywhere.

  - `trait_path` returns the trait's canonical string form via
    `toString(<sub>)`, with a leading `IS NULL` guard so missing keys
    and JSON null surface as SQL NULL. Mirrors Snowflake's `::STRING`
    semantics — downstream regex / position / compare paths get
    unquoted strings, decimal digits for numerics, and `'true'` /
    `'false'` for bools.

  - `trait_eq` (positive) leads with a `toString(<sub>) = <lit>` fast
    path — covers String + canonical-stringified Int / UInt / Float +
    lowercase Bool in one subcolumn read. A typed-variant Bool branch
    (``<sub>.:Bool = <target>``) picks up Python-bool-repr "True" /
    "False" coercions, and a `toFloat64OrNull(toString(<sub>))` branch
    catches floats whose canonical toString integer-trims (1.0 → '1').
    Mirrors Snowflake's `v::STRING` fast path. `NOT_EQUAL` still does
    explicit per-type dispatch via typed-variant subcolumns
    (``.:String``, ``.:Int64``, ``.:UInt64``, ``.:Float64``, ``.:Bool``);
    each accessor is NULL when the JSON value is the wrong type, which
    matches the engine's "cast failed → False" semantics.

  - Anchored regex uses `match(value, '^(...)')` — ClickHouse's `match`
    is RE2 and unanchored, so we prepend `^` to mirror Python's
    `re.match` (start-anchored, prefix-allowed, not full-match).

  - n-th digit run uses `extractAll(value, '\\d+')[n]`; ClickHouse's
    array subscript is 1-indexed and returns `''` for out-of-bounds, so
    we `nullIf(..., '')` to keep the engine's "no n-th run" → NULL
    contract.

  - PERCENTAGE_SPLIT reinterprets the raw 16-byte `MD5` digest directly
    into a native `UInt128` and takes `% 9999`, rather than parsing hex
    chunks and recombining them with modular arithmetic. `MD5(expr)`
    returns a big-endian `FixedString(16)`; `reinterpretAsUInt128` reads
    little-endian, so `reverse` first to make the value equal
    `int(md5_hex, 16)`. One hash, one reinterpret, one modulo — a
    consistent speedup on `% Split`-heavy predicates.

## Setup

`JSON` type DDL requires `SET allow_experimental_json_type = 1` on
ClickHouse Cloud as of 25.12 (no longer experimental on OSS 25.x).
Callers should apply this setting at session creation."""

from flagsmith_sql_flag_engine.utils import re2_safe, string_literal

SCHEMA_DDL = """\
CREATE TABLE IF NOT EXISTS IDENTITIES (
    -- environment.key from EnvironmentContext; used as the env partition
    environment_id String,

    -- stable per-identity row id
    id UInt64,

    -- the identity's external identifier, exposed as $.identity.identifier
    identifier String,

    -- the composite identity key, exposed as $.identity.key
    identity_key String,

    -- the identity's full trait map. ClickHouse's `JSON` type stores each
    -- path as a typed subcolumn so trait lookups are columnar reads, not
    -- per-row JSON parses. SQL NULL for an identity with no traits.
    traits JSON
)
ENGINE = MergeTree()
ORDER BY (environment_id, id);
"""


def _backtick(trait_key: str) -> str:
    """Escape a trait key for use as a backtick-quoted JSON subcolumn name.
    Doubles embedded backticks per CH's identifier escape rule."""
    return "`" + trait_key.replace("`", "``") + "`"


def _non_null(expr: str) -> str:
    """Coerce a possibly-`Nullable(String)` expression down to non-nullable
    `String`. ClickHouse rejects regex functions (`match`, `extractAll`)
    over `Nullable(String)` because the inferred result types
    `Nullable(UInt8)` / `Nullable(Array(String))` aren't representable.
    The translator always guards these calls with `IS NOT NULL`, so the
    coalesce default is unreachable at runtime."""
    return f"ifNull({expr}, '')"


class ClickHouseDialect:
    name = "clickhouse"
    schema_ddl = SCHEMA_DDL

    # ----- IDENTITIES schema access -----

    def identifier_expr(self, alias: str) -> str:
        return f"{alias}.identifier"

    def identity_key_expr(self, alias: str) -> str:
        return f"{alias}.identity_key"

    def _sub(self, alias: str, trait_key: str) -> str:
        """The raw JSON subcolumn reference for a trait key.
        ``alias.traits.`key` `` — Dynamic-typed, NULL for missing keys
        and explicit JSON null."""
        return f"{alias}.traits.{_backtick(trait_key)}"

    def trait_path(self, alias: str, trait_key: str) -> str:
        # Return the trait's canonical string form, mirroring Snowflake's
        # `i.traits:"key"::STRING`:
        #
        #   - missing key       → NULL
        #   - JSON null value   → NULL
        #   - JSON string "x"   → 'x' (quotes stripped)
        #   - JSON int / float  → '42' / '3.14'
        #   - JSON true / false → 'true' / 'false'
        #
        # `toString` over a JSON subcolumn does the right canonicalisation
        # natively. The `IS NULL` guard distinguishes missing from a
        # JSON empty string (`""` round-trips as `''` through toString,
        # the same value `toString(NULL)` produces) — the translator's
        # `IS NULL` / `IS NOT NULL` checks rely on this distinction.
        sub = self._sub(alias, trait_key)
        return f"if({sub} IS NULL, NULL, toString({sub}))"

    def trait_eq(self, alias: str, trait_key: str, value: object, negate: bool) -> str:
        sub = self._sub(alias, trait_key)
        str_value = str(value)
        str_lit = string_literal(str_value)
        # Engine bool cast: `v not in ("False", "false")`. A JSON true matches
        # every segment value except literal "False" / "false"; those two coerce
        # to False and match a JSON false.
        bool_target = "true" if str_value not in ("False", "false") else "false"
        # Engine int / float cast: ValueError → no match for that branch.
        try:
            int_lit: str | None = str(int(str_value))
        except (ValueError, TypeError):
            int_lit = None
        try:
            float_lit: str | None = repr(float(str_value))
        except (ValueError, TypeError):
            float_lit = None

        # `toString(<sub>)` returns the JSON value's canonical string form
        # in a single subcolumn read — 'x' for String, '42' for Int / UInt,
        # '3.14' for Float, 'true' / 'false' for Bool. Mirrors Snowflake's
        # `v::STRING` and lets us collapse the typical match path to one
        # comparison instead of an OR across five typed-variant subcolumns.
        str_path = f"toString({sub})"
        bool_sub = f"{sub}.:Bool"

        if not negate:
            # Fast path: covers String + canonical-stringified Int / UInt /
            # Float + lowercase Bool ('true' / 'false') in one branch.
            clauses = [f"({str_path} = {str_lit})"]
            # Bool branch: engine treats any segment value except "False" /
            # "false" as bool True, so a JSON true trait must match e.g.
            # `EQUAL("flag", "growth")`. The fast path catches the
            # lowercase case; this branch picks up Python-bool-repr "True"
            # / "False" and any other coercion that doesn't string-match
            # 'true' / 'false' directly.
            clauses.append(f"({bool_sub} = {bool_target})")
            # Float branch: floats whose `toString` integer-trims (1.0 →
            # '1') miss the fast path against a `'1.0'` segment value.
            # `toFloat64OrNull(str_path)` covers Int / UInt / Float
            # uniformly; non-numeric traits stringify to something
            # `toFloat64OrNull` rejects → NULL → no match.
            if float_lit is not None and float_lit != str_value:
                clauses.append(f"(toFloat64OrNull({str_path}) = {float_lit})")
            return "(" + " OR ".join(clauses) + ")"

        # NOT_EQUAL: per-type dispatch. Engine returns True only when the
        # cast succeeded *and* values differ. `.:Type IS NOT NULL AND .:Type
        # <> lit` encodes that directly; types where the segment value can't
        # cast contribute FALSE.
        no_match = "FALSE"
        str_sub = f"{sub}.:String"
        int_sub = f"{sub}.:Int64"
        uint_sub = f"{sub}.:UInt64"
        float_sub = f"{sub}.:Float64"
        bool_branch = f"({bool_sub} IS NOT NULL AND {bool_sub} <> {bool_target})"
        if int_lit is not None or float_lit is not None:
            num_lit = int_lit if int_lit is not None else float_lit
            num_branch = (
                f"(({int_sub} IS NOT NULL AND {int_sub} <> {num_lit})"
                f" OR ({uint_sub} IS NOT NULL AND {uint_sub} <> {num_lit})"
                f" OR ({float_sub} IS NOT NULL AND {float_sub} <> {num_lit}))"
            )
        else:
            num_branch = no_match
        return (
            f"(({str_sub} IS NOT NULL AND {str_sub} <> {str_lit}) OR {bool_branch} OR {num_branch})"
        )

    def trait_in(self, alias: str, trait_key: str, items: list[str]) -> str:
        # `toString(<sub>)` returns the canonical string form for any JSON
        # value type in a single subcolumn read. Engine semantics only
        # match String and integer trait types — bool / float / array
        # traits never match — so we gate the toString-based IN compare on
        # `.:Bool IS NULL AND .:Float64 IS NULL`. Int / UInt traits pass
        # because their stringified form ('42') matches the item literals;
        # missing keys propagate NULL through toString and fail the IN.
        sub = self._sub(alias, trait_key)
        bool_sub = f"{sub}.:Bool"
        float_sub = f"{sub}.:Float64"
        str_path = f"toString({sub})"
        item_lits = ",".join(string_literal(v) for v in items)
        return f"({bool_sub} IS NULL AND {float_sub} IS NULL AND {str_path} IN ({item_lits}))"

    # ----- string operations -----

    def position(self, needle_lit: str, haystack_expr: str) -> str:
        # ClickHouse's argument order is (haystack, needle), opposite of
        # Snowflake's POSITION(needle, haystack). Returns 1-indexed
        # position, 0 for not-found.
        return f"position({haystack_expr}, {needle_lit}) > 0"

    def lpad(self, expr: str, width: int, pad_lit: str) -> str:
        return f"leftPad({expr}, {width}, {pad_lit})"

    def coalesce(self, *exprs: str) -> str:
        return f"coalesce({', '.join(exprs)})"

    # ----- regex -----

    def regex_supports(self, pattern: str) -> bool:
        # ClickHouse's regex engine is RE2 (`match`, `extractAll`).
        return re2_safe(pattern)

    @staticmethod
    def _regex_literal(pattern: str) -> str:
        # ClickHouse string literals process `\` as an escape, so a SQL
        # `'\d'` reaches the regex engine as `d`. Double the backslashes so
        # the engine sees `\d`; SQL single quotes are escaped by doubling
        # per the SQL standard.
        doubled = pattern.replace("\\", "\\\\").replace("'", "''")
        return f"'{doubled}'"

    def regexp_anchored_match(self, value_expr: str, pattern: str) -> str:
        # `match` is RE2 but unanchored — equivalent to `re.search`. Prepend
        # `^` to get `re.match` semantics (start-anchored, prefix-allowed).
        # Wrapping in `(...)` keeps the user's top-level alternation from
        # binding tighter than the anchor.
        anchored = "^(" + pattern + ")"
        return f"match({_non_null(value_expr)}, {self._regex_literal(anchored)})"

    def regexp_nth_digit_run(self, value_expr: str, n: int) -> str:
        # `extractAll` returns the matches array; subscript is 1-indexed
        # and yields `''` past the end. `nullIf` collapses that to NULL so
        # `COALESCE` upstream can fall back to `'0'`. `ifNull` coerces a
        # `Nullable(String)` input down to `String` — ClickHouse refuses
        # `extractAll` on `Nullable(String)` because the inferred result
        # type `Nullable(Array(String))` is unrepresentable.
        digit_run = self._regex_literal("\\d+")
        return f"nullIf(extractAll({_non_null(value_expr)}, {digit_run})[{n}], '')"

    # ----- hashing -----

    def hashed_percentage_mod_9999(self, subject_sql: str) -> str:
        # The engine computes `int(md5_hex, 16) % 9999`. ClickHouse has a
        # native 128-bit integer type, so reinterpret the raw 16-byte `MD5`
        # digest straight into a `UInt128` and take the modulo — one hash,
        # one reinterpret, one modulo. This avoids both the `hex`/`unhex`
        # round-trip and the four-chunk modular-arithmetic recombination the
        # 64-bit fallback needs.
        #
        # `MD5(...)` returns a big-endian `FixedString(16)`;
        # `reinterpretAsUInt128` reads little-endian, so `reverse` the bytes
        # first to make the integer equal `int(md5_hex, 16)`.
        return f"modulo(reinterpretAsUInt128(reverse(MD5({subject_sql}))), 9999)"

    # ----- casts -----

    def cast_string(self, expr: str) -> str:
        return f"toString({expr})"

    def cast_float(self, expr: str) -> str:
        # `toFloat64OrNull` over the string form sidesteps `toFloat64`'s
        # exception on a non-numeric input — engine behaviour on a cast
        # failure is "doesn't match", which NULL propagation through the
        # surrounding predicate gives us.
        return f"toFloat64OrNull(toString({expr}))"

    def cast_number(self, expr: str) -> str:
        return f"toInt64OrNull(toString({expr}))"

    # ----- composition -----

    def mod(self, dividend: str, divisor: str) -> str:
        return f"modulo({dividend}, {divisor})"
