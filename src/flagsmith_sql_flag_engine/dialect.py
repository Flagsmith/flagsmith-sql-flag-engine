"""Per-dialect SQL fragments — MD5 hex, hex-to-int parsing, prefix-anchored
regex, padded-version comparison, type-aware trait predicates, regex flavour."""

from typing import Protocol

from flagsmith_sql_flag_engine.binder import Binder


class Dialect(Protocol):
    """Per-dialect SQL fragments.

    Methods return SQL string fragments. Inputs are already-formatted SQL
    strings (column refs, string literals); the dialect only chooses the
    right syntax for the operation.

    Methods that embed a segment- or context-derived value take an
    optional `binder`: when provided, the value is emitted as a bound
    query parameter rather than an inline literal.
    """

    name: str  # human-readable, used in test ids and error messages

    # --- IDENTITIES schema access ---
    #
    # The dialect owns the canonical IDENTITIES schema, see `schema_ddl`,
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

    def trait_eq(
        self,
        alias: str,
        trait_key: str,
        value: object,
        negate: bool,
        binder: Binder | None = None,
    ) -> str:
        """Type-aware EQUAL / NOT_EQUAL predicate on a trait, mirroring
        `flag_engine`'s per-type coercion: the segment value is cast to
        the trait's runtime type before compare, and a cast failure
        means no match for both ops. Implementation is dialect-specific
        because trait-type discrimination and runtime type-coercion
        casts both vary by engine.
        """
        ...

    def trait_in(
        self,
        alias: str,
        trait_key: str,
        items: list[str],
        binder: Binder | None = None,
    ) -> str:
        """Type-aware IN predicate on a trait, mirroring engine semantics:
        string trait does direct lookup; integer trait stringifies and
        looks up; other trait types never match. `items` is the parsed
        candidate list per `flag_engine`'s `_get_in_values`.
        """
        ...

    # --- string operations ---

    def position(self, needle_lit: str, haystack_expr: str) -> str:
        """Boolean: does the string literal `needle_lit` appear in
        `haystack_expr`? Used for CONTAINS / NOT_CONTAINS."""
        ...

    def lpad(self, expr: str, width: int, pad_lit: str) -> str:
        """Left-pad `expr` to `width` using `pad_lit`."""
        ...

    def coalesce(self, *exprs: str) -> str:
        """COALESCE/NVL-style: first non-null."""
        ...

    # --- regex ---

    def regex_supports(self, pattern: str) -> bool:
        """Return True if this dialect's regex engine can compile
        `pattern`. The translator falls back to `None` for any REGEX
        condition where this returns False, letting the caller defer
        to `flag_engine`."""
        ...

    def regexp_anchored_match(
        self,
        value_expr: str,
        pattern: str,
        binder: Binder | None = None,
    ) -> str:
        """Boolean: equivalent to Python `re.match(pattern, value)` —
        anchored at position 0, may be a prefix of the value, not a
        full-match.

        `pattern` is the raw Python regex string. With no `binder`, the
        dialect handles its own escaping into a SQL literal, since regex
        flavours differ in how backslashes are treated."""
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
        """Cast `expr` to a NUMBER / BIGINT — the engine-side numeric
        type used for modulo arithmetic."""
        ...

    # --- composition ---

    def mod(self, dividend: str, divisor: str) -> str:
        """`dividend MOD divisor` returning a numeric value."""
        ...
