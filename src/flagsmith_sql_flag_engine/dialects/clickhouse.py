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

  - `trait_eq` and `trait_in` dispatch on typed-variant subcolumns
    (``i.traits.`key`.:String``, ``.:Int64``, ``.:Float64``, ``.:Bool``).
    Each accessor returns NULL when the JSON value is the wrong type,
    so OR-of-typed-compares naturally implements the engine's
    "cast succeeded AND values matched" semantics. JSON numerics may
    surface as Int64, UInt64, or Float64 depending on the literal's
    shape, so the numeric branches accept all three.

  - Anchored regex uses `match(value, '^(...)')` — ClickHouse's `match`
    is RE2 and unanchored, so we prepend `^` to mirror Python's
    `re.match` (start-anchored, prefix-allowed, not full-match).

  - n-th digit run uses `extractAll(value, '\\d+')[n]`; ClickHouse's
    array subscript is 1-indexed and returns `''` for out-of-bounds, so
    we `nullIf(..., '')` to keep the engine's "no n-th run" → NULL
    contract.

  - Hex-chunk parsing reads directly from the raw 16-byte MD5 output
    rather than round-tripping through hex. `MD5(expr)` returns a
    `FixedString(16)`; `reinterpretAsUInt32(reverse(substring(...)))`
    pulls a big-endian UInt32 out of any 4-byte slice. Skipping the
    `hex(MD5(...))` → `unhex(substring(...))` round-trip is a small but
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

        str_sub = f"{sub}.:String"
        int_sub = f"{sub}.:Int64"
        uint_sub = f"{sub}.:UInt64"
        float_sub = f"{sub}.:Float64"
        bool_sub = f"{sub}.:Bool"

        if not negate:
            # OR-of-typed-equals. Each `.:Type` accessor returns NULL when
            # the JSON value is the wrong type, so unrelated branches
            # short-circuit to NULL (false in WHERE).
            clauses = [f"({str_sub} = {str_lit})"]
            clauses.append(f"({bool_sub} = {bool_target})")
            num_lit = int_lit if int_lit is not None else float_lit
            if num_lit is not None:
                # Match across Int64 / UInt64 / Float64 — JSON numerics can
                # surface as any of the three depending on the literal shape.
                clauses.append(
                    f"({int_sub} = {num_lit} OR {uint_sub} = {num_lit} OR {float_sub} = {num_lit})"
                )
            return "(" + " OR ".join(clauses) + ")"

        # NOT_EQUAL: per-type dispatch. Engine returns True only when the
        # cast succeeded *and* values differ. `.:Type IS NOT NULL AND .:Type
        # <> lit` encodes that directly; types where the segment value can't
        # cast contribute FALSE.
        no_match = "FALSE"
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
        # String traits match items directly; integer traits stringify
        # before compare. Bool / float / array traits never match per
        # engine semantics, so they sit outside the gate.
        sub = self._sub(alias, trait_key)
        str_sub = f"{sub}.:String"
        int_sub = f"{sub}.:Int64"
        uint_sub = f"{sub}.:UInt64"
        item_lits = ",".join(string_literal(v) for v in items)
        return (
            f"(({str_sub} IS NOT NULL AND {str_sub} IN ({item_lits}))"
            f" OR ({int_sub} IS NOT NULL AND toString({int_sub}) IN ({item_lits}))"
            f" OR ({uint_sub} IS NOT NULL AND toString({uint_sub}) IN ({item_lits})))"
        )

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

    def md5_hex(self, expr: str) -> str:
        # Return the raw 16-byte MD5 digest rather than the hex string.
        # `parse_hex_chunk` below reads bytes directly via
        # `reinterpretAsUInt32(reverse(substring(...)))`, skipping the
        # `hex` → `unhex` round-trip — small but consistent win on
        # PERCENTAGE_SPLIT-heavy predicates.
        return f"MD5({expr})"

    def parse_hex_chunk(self, hex_expr: str, start: int, length: int = 8) -> str:
        # `hex_expr` is the raw `FixedString(16)` from `md5_hex` (not a hex
        # string). Map the 1-indexed hex start position to a 1-indexed byte
        # position: hex 1 → byte 1, hex 9 → byte 5, hex 17 → byte 9,
        # hex 25 → byte 13. 8 hex chars = 4 raw bytes.
        byte_start = (start - 1) // 2 + 1
        byte_length = length // 2
        slice_expr = f"substring({hex_expr}, {byte_start}, {byte_length})"
        # `reinterpretAsUInt32` reads bytes little-endian; `reverse` first
        # so the value equals `int(hex_chars, 16)` for the corresponding
        # hex slice — preserves `_HASH_CONST_*` constants from the translator.
        return f"reinterpretAsUInt32(reverse({slice_expr}))"

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
