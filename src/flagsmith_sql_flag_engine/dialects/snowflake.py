"""Snowflake dialect: SQL fragments tailored to Snowflake's function set.

## Expected schema

The translator emits predicates against a single `IDENTITIES` table —
four typed columns `environment_id`, `id`, `identifier`, `identity_key`,
plus one `VARIANT` column `traits` holding the identity's full trait
map. Trait keys are *data* in the VARIANT, not schema columns.

VARIANT was chosen over column-per-trait wide-form because:

  - Snowflake caps tables at ~3,000 columns; large trait vocabularies
    cross that.
  - VARIANT path-extraction is columnar, not a JSON parse per row;
    perf is within ~30% of typed columns for simple key lookups.

## Notable choices

  - `MD5_HEX` returns the 32-char hex digest directly.
  - Hex-to-int parsing uses `TO_NUMBER(SUBSTR(hex, n, 8), 'XXXXXXXX')`,
    producing a non-negative number that fits Snowflake's 38-digit NUMBER.
  - Anchored regex uses `REGEXP_INSTR(value, pattern) = 1`, equivalent
    to Python's `re.match` — start-anchored, prefix-allowed, not full-
    match.
  - n-th digit run uses `REGEXP_SUBSTR(value, '\\\\d+', 1, n)`;
    Snowflake's occurrence parameter is 1-indexed.
"""

from __future__ import annotations

from flagsmith_sql_flag_engine.utils import re2_safe, string_literal

# Canonical IDENTITIES schema the translator emits against.
SCHEMA_DDL = """\
CREATE TABLE IF NOT EXISTS IDENTITIES (
    -- environment.key from EnvironmentContext; used as the env partition
    environment_id STRING NOT NULL,

    -- stable per-identity row id
    id NUMBER NOT NULL,

    -- the identity's external identifier, exposed as $.identity.identifier
    identifier STRING NOT NULL,

    -- the composite identity key, exposed as $.identity.key
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

    def trait_eq(self, alias: str, trait_key: str, value: object, negate: bool) -> str:
        path = self.trait_path(alias, trait_key)
        str_path = self.cast_string(path)
        str_value = str(value)
        str_lit = string_literal(str_value)
        # Engine bool cast: `lambda v: v not in ("False", "false")`. We compare
        # against the variant's `::STRING` form 'true'/'false' rather than
        # invoke `(...)::BOOLEAN` directly — Snowflake's optimiser eagerly
        # evaluates the BOOLEAN cast even when the IS_BOOLEAN guard would
        # have short-circuited, and a non-bool variant blows up the query
        # with `100037: Boolean value 'red' is not recognized`.
        bool_str_lit = "'false'" if str_value in ("False", "false") else "'true'"
        # Engine int/float cast: int(v) / float(v); ValueError → no match.
        try:
            int_lit: str | None = str(int(str_value))
        except (ValueError, TypeError):
            int_lit = None
        try:
            float_lit: str | None = repr(float(str_value))
        except (ValueError, TypeError):
            float_lit = None

        if not negate:
            # Fast string compare always present — handles VARCHAR traits and
            # canonically-stringified INTEGER traits in one cheap branch.
            clauses = [f"{str_path} = {str_lit}"]
            clauses.append(f"(IS_BOOLEAN({path}) AND {str_path} = {bool_str_lit})")
            if float_lit is not None:
                # Variant float `1.23` stringifies to `'1.230000000000000e+00'`-ish
                # in Snowflake — direct string compare misses it, so a typed
                # branch is needed. TRY_TO_DOUBLE on the string form sidesteps
                # the same eager-eval trap as the bool branch.
                clauses.append(
                    f"((IS_DECIMAL({path}) OR IS_DOUBLE({path}))"
                    f" AND TRY_TO_DOUBLE({str_path}) = {float_lit})"
                )
            return "(" + " OR ".join(clauses) + ")"

        # NOT_EQUAL: per-type dispatch — engine returns True only when the
        # cast succeeded *and* values differ, which an OR-of-positives
        # can't express without over-matching.
        no_match = "FALSE"  # engine returns False on cast failure
        bool_branch = f"{str_path} <> {bool_str_lit}"
        int_branch = f"({path})::NUMBER <> {int_lit}" if int_lit is not None else no_match
        float_branch = f"({path})::FLOAT <> {float_lit}" if float_lit is not None else no_match
        return (
            f"((TYPEOF({path}) = 'BOOLEAN' AND {bool_branch})"
            f" OR (TYPEOF({path}) = 'INTEGER' AND {int_branch})"
            f" OR (TYPEOF({path}) IN ('DECIMAL', 'DOUBLE') AND {float_branch})"
            f" OR (TYPEOF({path}) NOT IN ('BOOLEAN', 'INTEGER', 'DECIMAL', 'DOUBLE')"
            f" AND {str_path} <> {str_lit}))"
        )

    def trait_in(self, alias: str, trait_key: str, items: list[str]) -> str:
        # Collapsed to a single `TYPEOF` gate around one string IN compare —
        # Snowflake stringifies INTEGER variants without decimals, so the same
        # `(path)::STRING IN (...)` works for both VARCHAR and INTEGER. Bool /
        # float / array traits never match per engine semantics, so they fall
        # outside the gate.
        path = self.trait_path(alias, trait_key)
        str_path = self.cast_string(path)
        item_lits = ",".join(string_literal(v) for v in items)
        return f"(TYPEOF({path}) IN ('VARCHAR', 'INTEGER') AND {str_path} IN ({item_lits}))"

    # ----- string operations -----

    def position(self, needle_lit: str, haystack_expr: str) -> str:
        return f"POSITION({needle_lit}, {haystack_expr}) > 0"

    def lpad(self, expr: str, width: int, pad_lit: str) -> str:
        return f"LPAD({expr}, {width}, {pad_lit})"

    def coalesce(self, *exprs: str) -> str:
        return f"COALESCE({', '.join(exprs)})"

    # ----- regex -----

    def regex_supports(self, pattern: str) -> bool:
        # Snowflake's regex engine is RE2.
        return re2_safe(pattern)

    @staticmethod
    def _regex_literal(pattern: str) -> str:
        # Snowflake's regex flavour is POSIX-style: a single backslash in the
        # SQL literal is treated as a literal backslash by both the SQL string
        # parser AND the regex engine, so `'\d'` matches the character `d`,
        # not a digit. To get a regex metachar like `\d`, `\s` or `\w`, we
        # double the backslash so the engine sees `\\d`. SQL single quotes
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
        # TRY_TO_DOUBLE / TRY_TO_NUMBER instead of TRY_CAST: they accept
        # VARIANT directly, and a non-numeric variant value yields NULL
        # instead of erroring out the whole query. Engine behaviour for
        # type-mismatched comparisons is "doesn't match", which NULL
        # propagation through the predicate gives us.
        return f"TRY_TO_DOUBLE(({expr})::STRING)"

    def cast_number(self, expr: str) -> str:
        return f"TRY_TO_NUMBER(({expr})::STRING)"

    # ----- composition -----

    def mod(self, dividend: str, divisor: str) -> str:
        return f"MOD({dividend}, {divisor})"
