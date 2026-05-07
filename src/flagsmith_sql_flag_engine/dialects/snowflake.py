"""Snowflake dialect: SQL fragments tailored to Snowflake's function set.

## Expected schema

The translator emits predicates against a single `IDENTITIES` table with
a fixed shape: 4 typed columns (`environment_id`, `id`, `identifier`,
`identity_key`) plus one `VARIANT` column `traits` holding the
identity's full trait map as a JSON-shaped object. Trait keys are
*data* in the VARIANT, not schema columns — adding new trait keys never
requires DDL.

This was chosen over column-per-trait wide-form because:
  - Snowflake caps tables at ~3,000 columns; SaaS-aggregated trait
    vocabularies cross that.
  - `ALTER TABLE ADD COLUMN` on the write path needs elevated grants and
    coordination with the CDC pipeline. VARIANT side-steps both.
  - Snowflake's VARIANT path-extraction is columnar, not a JSON parse
    per row; perf is within ~30% of typed columns for simple key
    lookups based on Snowflake's published benchmarks.

The *slow* PoC shape that VARIANT might be confused with was
`OBJECT_AGG` at query time over a long-form `TRAITS` table (the CTE
joined IDENTITIES + TRAITS and aggregated the trait map per row, every
query — 245s at 100M). Here the VARIANT is *pre-materialised* by the
CDC pipeline; query-time it's a single columnar read with subkey
extraction. Different operation, different perf.

## Notable choices

  - `MD5_HEX` returns the 32-char hex digest directly.
  - Hex-to-int parsing uses `TO_NUMBER(SUBSTR(hex, n, 8), 'XXXXXXXX')`,
    producing a non-negative number that fits Snowflake's 38-digit NUMBER.
  - Anchored regex uses `REGEXP_INSTR(value, pattern) = 1`, which is
    equivalent to Python's `re.match(pattern, value)` (start-anchored,
    prefix-allowed, not full-match).
  - n-th digit run uses `REGEXP_SUBSTR(value, '\\\\d+', 1, n)` — Snowflake's
    occurrence parameter is 1-indexed.
"""

from __future__ import annotations

# Canonical schema the translator expects. Run this once when standing up
# a Snowflake-backed Flagsmith installation; the CDC pipeline that
# materialises IDENTITIES from the source-of-truth feed populates the
# `traits` VARIANT with the identity's trait map. New trait keys appear
# as new keys inside the VARIANT — no DDL required.
SCHEMA_DDL = """\
CREATE TABLE IF NOT EXISTS IDENTITIES (
    -- environment.key from EnvironmentContext; used as the env partition
    environment_id STRING NOT NULL,

    -- stable per-identity row id
    id NUMBER NOT NULL,

    -- the identity's external identifier, exposed as $.identity.identifier
    identifier STRING NOT NULL,

    -- the SDK-side composite identity key, exposed as $.identity.key
    identity_key STRING NOT NULL,

    -- the identity's full trait map: {"plan": "growth", "country": "GB", ...}.
    -- Trait keys are object keys; Snowflake stores VARIANT as columnar-encoded
    -- JSON-ish so subkey lookups are vectorised and fast. NULL when the
    -- identity has no traits.
    traits VARIANT,

    PRIMARY KEY (environment_id, id)
)
CLUSTER BY (environment_id, id);
"""


class SnowflakeDialect:
    name = "snowflake"
    schema_ddl = SCHEMA_DDL

    # ----- IDENTITIES schema access -----

    def identifier_expr(self, alias: str) -> str:
        return f"{alias}.identifier"

    def identity_key_expr(self, alias: str) -> str:
        return f"{alias}.identity_key"

    def trait_path(self, alias: str, trait_key: str) -> str:
        # Snowflake VARIANT path syntax: `i.traits:"key"`. The key is
        # double-quoted and any embedded double quotes are doubled per
        # the SQL standard.
        escaped = trait_key.replace('"', '""')
        return f'{alias}.traits:"{escaped}"'

    # ----- string operations -----

    def position(self, needle_lit: str, haystack_expr: str) -> str:
        return f"POSITION({needle_lit}, {haystack_expr}) > 0"

    def lpad(self, expr: str, width: int, pad_lit: str) -> str:
        return f"LPAD({expr}, {width}, {pad_lit})"

    def coalesce(self, *exprs: str) -> str:
        return f"COALESCE({', '.join(exprs)})"

    # ----- regex -----

    @staticmethod
    def _regex_literal(pattern: str) -> str:
        # Snowflake's regex flavour is POSIX-style: a single backslash in the
        # SQL literal is treated as a literal backslash by both the SQL string
        # parser AND the regex engine, so `'\d'` matches the character `d`,
        # not a digit. To get a regex metachar (`\d`, `\s`, `\w`...) we need
        # to double the backslash so the engine sees `\\d`. SQL single quotes
        # are escaped by doubling per the SQL standard.
        doubled = pattern.replace("\\", "\\\\").replace("'", "''")
        return f"'{doubled}'"

    def regexp_anchored_match(self, value_expr: str, pattern: str) -> str:
        # REGEXP_INSTR returns 1-indexed position of first match; = 1 means
        # the match starts at the beginning. Equivalent to re.match.
        return f"REGEXP_INSTR({value_expr}, {self._regex_literal(pattern)}) = 1"

    def regexp_nth_digit_run(self, value_expr: str, n: int) -> str:
        # `\d+` finds runs of digits; 4th arg is 1-indexed occurrence number.
        digit_run = self._regex_literal("\\d+")
        return f"REGEXP_SUBSTR({value_expr}, {digit_run}, 1, {n})"

    # ----- hashing -----

    def md5_hex(self, expr: str) -> str:
        return f"MD5_HEX({expr})"

    def parse_hex_chunk(self, hex_expr: str, start: int, length: int = 8) -> str:
        format_str = "X" * length
        return f"TO_NUMBER(SUBSTR({hex_expr}, {start}, {length}), '{format_str}')"

    # ----- casts -----

    def cast_string(self, expr: str) -> str:
        return f"({expr})::STRING"

    def cast_float(self, expr: str) -> str:
        # TRY_TO_DOUBLE/TRY_TO_NUMBER (rather than TRY_CAST) because
        # they accept VARIANT directly, and a non-numeric variant value
        # (e.g. a string trait used in a numeric comparison) yields NULL
        # instead of erroring out the whole query. Engine behaviour for
        # type-mismatched comparisons is "doesn't match", which NULL
        # propagation through the predicate gives us.
        return f"TRY_TO_DOUBLE(({expr})::STRING)"

    def cast_number(self, expr: str) -> str:
        return f"TRY_TO_NUMBER(({expr})::STRING)"

    # ----- composition -----

    def mod(self, dividend: str, divisor: str) -> str:
        return f"MOD({dividend}, {divisor})"
