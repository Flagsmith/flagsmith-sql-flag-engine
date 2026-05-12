"""ClickHouse dialect: SQL fragments tailored to ClickHouse's function set.

## Expected schema

The translator emits predicates against a single `IDENTITIES` table —
four typed columns `environment_id`, `id`, `identifier`, `identity_key`,
plus one `Nullable(String)` column `traits` holding the identity's full
trait map as a JSON-encoded string. Trait keys are JSON keys, not schema
columns.

`String` was chosen over the experimental `JSON` type because:

  - It works on every supported ClickHouse version, including LTS.
  - `JSONExtract*` is the supported way to read JSON values out of a
    `String` column; the functions are vectorised and unwind to a
    columnar plan when the same key is referenced across rows.

## Notable choices

  - `JSONExtractRaw` returns the empty string when the key is missing;
    `trait_path` wraps that in `nullIf(..., '')` so the translator's
    `IS NULL` / `IS NOT NULL` checks behave like Snowflake's VARIANT.
    A trait whose value is the JSON string `""` round-trips as `'""'`
    (with quotes) so the disambiguation is safe.

  - `trait_eq` and `trait_in` dispatch on `JSONType(traits, key)` —
    ClickHouse's native discriminator. JSON numeric values may surface
    as `'Int64'`, `'UInt64'`, or `'Double'` depending on the value's
    shape, so the numeric branches accept all three.

  - Anchored regex uses `match(value, '^(...)')` — ClickHouse's `match`
    is RE2 and unanchored, so we prepend `^` to mirror Python's
    `re.match` (start-anchored, prefix-allowed, not full-match).

  - n-th digit run uses `extractAll(value, '\\d+')[n]`; ClickHouse's
    array subscript is 1-indexed and returns `''` for out-of-bounds, so
    we `nullIf(..., '')` to keep the engine's "no n-th run" → NULL
    contract.

  - Hex-to-int parsing uses `reinterpretAsUInt32(reverse(unhex(...)))`.
    `unhex` returns the 4-byte buffer for an 8-char hex slice;
    `reinterpretAsUInt32` reads little-endian, so we `reverse` first
    to get the big-endian value the engine expects.
"""

from flagsmith_sql_flag_engine.utils import re2_safe, string_literal

# Canonical IDENTITIES schema the translator emits against. The translator
# checks trait presence via `IS NULL` / `IS NOT NULL`, so `traits` is
# `Nullable(String)` to match Snowflake's nullable `VARIANT`.
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

    -- the identity's full trait map JSON-encoded:
    -- {"plan": "growth", "country": "GB", ...}. NULL when the identity
    -- has no traits.
    traits Nullable(String)
)
ENGINE = MergeTree()
ORDER BY (environment_id, id);
"""

# Numeric JSON values may surface under any of these `JSONType` strings —
# `Int64` and `UInt64` for integer-shaped numbers, `Double` for fractional.
# Bool is its own branch; it gates the bool compare.
_NUMERIC_TYPES = ("Int64", "UInt64", "Double")
_NUMERIC_TYPES_SQL = "('" + "', '".join(_NUMERIC_TYPES) + "')"


def _non_null(expr: str) -> str:
    """Coerce a possibly-`Nullable(String)` expression down to non-nullable
    `String`. ClickHouse rejects regex functions (`match`, `extractAll`)
    over `Nullable(String)` because the implied result types
    `Nullable(UInt8)` / `Nullable(Array(String))` aren't representable.
    The translator always guards regex calls with `IS NOT NULL`, so the
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
        # For string traits we use `JSONExtractString` to strip the JSON
        # quotes that `JSONExtractRaw` would carry through, so downstream
        # regex / position / compare see the natural string form. For non-
        # string types `JSONExtractRaw` already yields the bare token.
        # `JSONType` returns the `'Null'` enum value for both missing keys
        # and explicit JSON null values, so a single check at the top
        # collapses both to SQL NULL — matching Snowflake's VARIANT path
        # semantics for the translator's `IS NULL` / `IS NOT NULL` guards.
        key_lit = string_literal(trait_key)
        traits = f"{alias}.traits"
        type_expr = f"JSONType({traits}, {key_lit})"
        return (
            f"if({type_expr} = 'Null', NULL,"
            f" if({type_expr} = 'String',"
            f" JSONExtractString({traits}, {key_lit}),"
            f" JSONExtractRaw({traits}, {key_lit})))"
        )

    def trait_eq(self, alias: str, trait_key: str, value: object, negate: bool) -> str:
        key_lit = string_literal(trait_key)
        traits = f"{alias}.traits"
        type_expr = f"JSONType({traits}, {key_lit})"
        str_extract = f"JSONExtractString({traits}, {key_lit})"
        str_value = str(value)
        str_lit = string_literal(str_value)
        # Engine bool cast: `lambda v: v not in ("False", "false")`. A JSON
        # `true` matches every segment value except literal "False" / "false";
        # for those two the segment coerces to False, so it matches a JSON
        # `false`.
        bool_target = 0 if str_value in ("False", "false") else 1
        bool_extract = f"JSONExtractBool({traits}, {key_lit})"
        # Engine int/float cast: int(v) / float(v); ValueError → no match.
        try:
            int_lit: str | None = str(int(str_value))
        except (ValueError, TypeError):
            int_lit = None
        try:
            float_lit: str | None = repr(float(str_value))
        except (ValueError, TypeError):
            float_lit = None
        float_extract = f"JSONExtractFloat({traits}, {key_lit})"

        if not negate:
            clauses = [f"({type_expr} = 'String' AND {str_extract} = {str_lit})"]
            clauses.append(f"({type_expr} = 'Bool' AND {bool_extract} = {bool_target})")
            if int_lit is not None:
                clauses.append(
                    f"({type_expr} IN {_NUMERIC_TYPES_SQL} AND {float_extract} = {int_lit})"
                )
            elif float_lit is not None:
                clauses.append(
                    f"({type_expr} IN {_NUMERIC_TYPES_SQL} AND {float_extract} = {float_lit})"
                )
            return "(" + " OR ".join(clauses) + ")"

        # NOT_EQUAL: per-type dispatch. Engine returns True only when the
        # cast succeeded *and* values differ. Cast-failure types fall
        # through to FALSE; that's the OR omitting them.
        no_match = "FALSE"
        bool_branch = f"{bool_extract} <> {bool_target}"
        num_branch = (
            f"{float_extract} <> {int_lit if int_lit is not None else float_lit}"
            if (int_lit is not None or float_lit is not None)
            else no_match
        )
        return (
            f"(({type_expr} = 'String' AND {str_extract} <> {str_lit})"
            f" OR ({type_expr} = 'Bool' AND {bool_branch})"
            f" OR ({type_expr} IN {_NUMERIC_TYPES_SQL} AND {num_branch}))"
        )

    def trait_in(self, alias: str, trait_key: str, items: list[str]) -> str:
        # Mirrors Snowflake's single-gate shape: string traits match items
        # directly; integer traits stringify before compare. Bool / float /
        # array traits never match per engine semantics, so they sit
        # outside the gate.
        key_lit = string_literal(trait_key)
        traits = f"{alias}.traits"
        type_expr = f"JSONType({traits}, {key_lit})"
        str_extract = f"JSONExtractString({traits}, {key_lit})"
        int_extract = f"JSONExtractInt({traits}, {key_lit})"
        item_lits = ",".join(string_literal(v) for v in items)
        return (
            f"(({type_expr} = 'String' AND {str_extract} IN ({item_lits}))"
            f" OR ({type_expr} IN ('Int64', 'UInt64')"
            f" AND toString({int_extract}) IN ({item_lits})))"
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
        # `MD5` returns a FixedString(16); `hex` formats it as uppercase.
        # `lower` to keep parity with Snowflake's `MD5_HEX` (the downstream
        # `parse_hex_chunk` is case-insensitive, but matching the
        # canonical form keeps debugging output identical across dialects).
        return f"lower(hex(MD5({expr})))"

    def parse_hex_chunk(self, hex_expr: str, start: int, length: int = 8) -> str:
        # `unhex` returns `length / 2` bytes; `reinterpretAsUInt32` reads
        # them little-endian, so `reverse` first to consume the bytes in
        # big-endian order — matching `int(hex_chunk, 16)`.
        slice_expr = f"substring({hex_expr}, {start}, {length})"
        return f"reinterpretAsUInt32(reverse(unhex({slice_expr})))"

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
