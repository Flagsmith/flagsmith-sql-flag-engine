"""Sanitisation of segment-value-derived strings before they cross into SQL.

The translator emits SQL by string composition rather than via a query-builder
library. That means every value that comes from a `SegmentCondition` (operand,
trait key, segment key, env constants) needs to be escaped or validated
before it lands in a SQL fragment. This module is the single home for that
escape / validation logic.

**For future contributors**: if you find yourself f-string-interpolating a
value that originated in a segment definition or evaluation context, route
it through a `Sanitiser` method. Bypassing this layer is how SQL injection
happens — the audit trail is `Sanitiser.<method>` call sites.

Threat model: segment definitions come from Flagsmith users with
`MANAGE_SEGMENTS` permission on a project. Trusted-but-not-fully-trusted —
a malicious operand value should not be able to escalate to arbitrary SQL
execution against the analytical store.
"""

from __future__ import annotations


class Sanitiser:
    """Static-method namespace for SQL escape / validation primitives."""

    @staticmethod
    def escape_string(value: str) -> str:
        """Double single quotes for inclusion inside a SQL string literal.

        Use when the caller is composing a larger literal (e.g. CSV-style
        `IN ('a','b','c')`) and wants the un-wrapped escape. For the common
        case of a single value, prefer `string_literal`.
        """
        return value.replace("'", "''")

    @staticmethod
    def string_literal(value: str) -> str:
        """Wrap a value as a single-quoted SQL string literal."""
        return "'" + Sanitiser.escape_string(value) + "'"

    @staticmethod
    def variant_path_key(key: str) -> str:
        """Double-quoted Snowflake VARIANT path key.

        Snowflake's `traits:"key"` syntax accepts arbitrary Unicode keys when
        double-quoted; embedded double quotes are doubled per the SQL standard.
        """
        return '"' + key.replace('"', '""') + '"'

    @staticmethod
    def numeric_literal(value: object) -> str | None:
        """Validate `value` is numeric and return its canonical-float string form.

        Returns `None` if `value` is not parseable as a float — the caller
        should propagate that as "untranslatable", giving the segment-edit
        UI a clean failure mode instead of injecting unparseable SQL.

        Booleans are rejected explicitly: `float(True) == 1.0` in Python,
        but the engine treats segment-value booleans as strings via its
        type-coercion path, so a numeric interpretation here would diverge.
        """
        if isinstance(value, bool):
            return None
        try:
            return str(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def modulo_literal(value: object) -> tuple[str, str] | None:
        """Parse a `divisor|remainder` MODULO operand pair.

        Returns `(divisor, remainder)` as canonical-float string forms, or
        `None` if either side fails to parse.
        """
        try:
            divisor_str, remainder_str = str(value).split("|")
            return str(float(divisor_str)), str(float(remainder_str))
        except (ValueError, AttributeError):
            return None
