"""Snowflake dialect: SQL fragments tailored to Snowflake's function set.

Notable choices:
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


class SnowflakeDialect:
    name = "snowflake"

    # ----- string operations -----

    def position(self, needle_lit: str, haystack_expr: str) -> str:
        return f"POSITION({needle_lit}, {haystack_expr}) > 0"

    def lpad(self, expr: str, width: int, pad_lit: str) -> str:
        return f"LPAD({expr}, {width}, {pad_lit})"

    def coalesce(self, *exprs: str) -> str:
        return f"COALESCE({', '.join(exprs)})"

    # ----- regex -----

    def regexp_anchored_match(self, value_expr: str, pattern_lit: str) -> str:
        # REGEXP_INSTR returns 1-indexed position of first match; = 1 means
        # the match starts at the beginning. Equivalent to re.match.
        return f"REGEXP_INSTR({value_expr}, {pattern_lit}) = 1"

    def regexp_nth_digit_run(self, value_expr: str, n: int) -> str:
        # `\d+` finds runs of digits; 4th arg is 1-indexed occurrence number.
        return f"REGEXP_SUBSTR({value_expr}, '\\\\d+', 1, {n})"

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
        return f"({expr})::FLOAT"

    def cast_number(self, expr: str) -> str:
        return f"({expr})::NUMBER"

    # ----- composition -----

    def mod(self, dividend: str, divisor: str) -> str:
        return f"MOD({dividend}, {divisor})"
