"""Dialect protocol — primitives the translator needs to emit dialect-correct SQL.

The translator's structure (rule walker, condition routing, EXISTS composition)
is dialect-agnostic. The dialect-specific bits are the small SQL fragments that
differ across engines: how to compute MD5 → hex digest, how to parse 8 hex
chars to a 32-bit integer, what the syntax for prefix-anchored regex match is,
and so on.
"""

from __future__ import annotations

from typing import Protocol


class Dialect(Protocol):
    """Per-dialect SQL fragments. All methods return SQL string fragments
    that get composed into the final WHERE clause.

    The methods that take expressions take them as already-formatted SQL
    strings (e.g. column references, string literals); the dialect only
    chooses the right SQL syntax for the operation.
    """

    name: str  # human-readable, used in test ids and error messages

    # --- IDENTITIES schema access ---
    #
    # The dialect owns the canonical IDENTITIES schema (see `schema_ddl`),
    # so it also owns the SQL expression for each logical column. The
    # translator just hands over an alias.

    def identifier_expr(self, alias: str) -> str:
        """SQL expression for `$.identity.identifier`."""
        ...

    def identity_key_expr(self, alias: str) -> str:
        """SQL expression for `$.identity.key`."""
        ...

    def trait_path(self, alias: str, trait_key: str) -> str:
        """Path-extract a trait value from the IDENTITIES traits container.

        The path syntax varies by SQL engine.
        """
        ...

    # --- string operations ---

    def position(self, needle_lit: str, haystack_expr: str) -> str:
        """Boolean: does `needle_lit` (a SQL string literal) appear in
        `haystack_expr`? Used for CONTAINS / NOT_CONTAINS."""
        ...

    def lpad(self, expr: str, width: int, pad_lit: str) -> str:
        """Left-pad `expr` to `width` using `pad_lit`."""
        ...

    def coalesce(self, *exprs: str) -> str:
        """COALESCE/NVL-style: first non-null."""
        ...

    # --- regex ---

    def regexp_anchored_match(self, value_expr: str, pattern: str) -> str:
        """Boolean: equivalent to Python `re.match(pattern, value)` —
        anchored at position 0, may be a prefix of the value (not full-match).

        `pattern` is the raw Python regex string; the dialect handles its
        own escaping into a SQL literal (regex flavours differ in how
        backslashes are treated)."""
        ...

    def regexp_nth_digit_run(self, value_expr: str, n: int) -> str:
        """Extract the n-th sequence of digits from `value_expr`. Returns NULL
        if there are fewer than n digit runs. Used for semver."""
        ...

    # --- hashing primitives for PERCENTAGE_SPLIT ---

    def md5_hex(self, expr: str) -> str:
        """SQL fragment producing the lowercase 32-char hex MD5 digest."""
        ...

    def parse_hex_chunk(self, hex_expr: str, start: int, length: int = 8) -> str:
        """Parse `length` hex characters of `hex_expr` starting at 1-indexed
        `start` into a non-negative integer."""
        ...

    # --- type casts ---

    def cast_string(self, expr: str) -> str:
        """Cast `expr` to STRING / VARCHAR."""
        ...

    def cast_float(self, expr: str) -> str:
        """Cast `expr` to a 64-bit float / DOUBLE."""
        ...

    def cast_number(self, expr: str) -> str:
        """Cast `expr` to a NUMBER / BIGINT (engine-side numeric type for
        modulo arithmetic)."""
        ...

    # --- composition ---

    def mod(self, dividend: str, divisor: str) -> str:
        """`dividend MOD divisor` returning a numeric value."""
        ...
