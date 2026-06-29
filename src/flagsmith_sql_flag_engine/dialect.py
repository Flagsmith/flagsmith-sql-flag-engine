"""Per-dialect SQL fragments — MD5 hex, hex-to-int parsing, prefix-anchored
regex, padded-version comparison, type-aware trait predicates, regex flavour."""

from typing import Protocol


class Dialect(Protocol):
    """Per-dialect SQL fragments.

    Methods return SQL string fragments. Inputs are already-formatted SQL
    strings (column refs, string literals); the dialect only chooses the
    right syntax for the operation.
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

    def trait_eq(self, alias: str, trait_key: str, value: object, negate: bool) -> str:
        """Type-aware EQUAL / NOT_EQUAL predicate on a trait, mirroring
        `flag_engine`'s per-type coercion: the segment value is cast to
        the trait's runtime type before compare, and a cast failure
        means no match for both ops. Implementation is dialect-specific
        because trait-type discrimination and runtime type-coercion
        casts both vary by engine.
        """
        ...

    def trait_in(self, alias: str, trait_key: str, items: list[str]) -> str:
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

    def regexp_anchored_match(self, value_expr: str, pattern: str) -> str:
        """Boolean: equivalent to Python `re.match(pattern, value)` —
        anchored at position 0, may be a prefix of the value, not a
        full-match.

        `pattern` is the raw Python regex string; the dialect handles
        its own escaping into a SQL literal, since regex flavours
        differ in how backslashes are treated."""
        ...

    def regexp_nth_digit_run(self, value_expr: str, n: int) -> str:
        """Extract the n-th sequence of digits from `value_expr`. Returns NULL
        if there are fewer than n digit runs. Used for semver."""
        ...

    # --- hashing primitive for PERCENTAGE_SPLIT ---

    def hashed_percentage_mod_9999(self, subject_sql: str) -> str:
        """SQL fragment for `int(md5(subject), 16) % 9999` — the engine's
        `get_hashed_percentage_for_object_ids` modulo, before scaling.

        `subject_sql` is the already-composed hash subject (the salted,
        comma-joined string). The translator handles the threshold compare;
        the dialect only computes the integer modulo of the 128-bit digest.

        Engines with native 128-bit integers (ClickHouse, Snowflake) can
        reinterpret the raw 16-byte digest into one int and take the modulo
        directly. Engines capped at 64-bit ints can recover the same value
        without bignum support by splitting the 32-hex digest into four
        8-hex chunks and recombining modulo 9999 with the precomputed weights
        (16^24, 16^16, 16^8, 1) mod 9999 = (7291, 1897, 6835, 1)."""
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
