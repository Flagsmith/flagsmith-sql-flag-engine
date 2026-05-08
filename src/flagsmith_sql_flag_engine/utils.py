"""SQL escape, validation, and regex-flavour primitives, shared by
the translator and dialects.

The translator emits SQL by string composition rather than via a query-
builder. Every value originating in a `SegmentCondition` or evaluation
context must be escaped or validated before it lands in a SQL fragment;
this module is the single home for that logic.

If you find yourself f-string-interpolating a segment- or context-derived
value, route it through one of these helpers. Bypassing this layer is how
SQL injection happens; the audit trail is the call sites here.

Threat model: segment definitions come from Flagsmith users with
`MANAGE_SEGMENTS` permission on a project — trusted-but-not-fully-trusted.
A malicious operand value must not be able to escalate to arbitrary SQL
execution against the analytical store.

Functions in this module are dialect-agnostic. Anything that depends on
SQL-engine syntax — VARIANT path quoting, JSONB extraction, casts — lives
on the `Dialect` protocol instead.
"""

import re


def escape_string(value: str) -> str:
    """Double single quotes for inclusion inside a SQL string literal.

    Use when the caller is composing a larger literal — for example a
    CSV-style `IN ('a','b','c')` — and wants the un-wrapped escape. For
    a single standalone value, prefer `string_literal`.
    """
    return value.replace("'", "''")


def string_literal(value: str) -> str:
    """Wrap a value as a single-quoted SQL string literal."""
    return "'" + escape_string(value) + "'"


def numeric_literal(value: object) -> str | None:
    """Validate `value` is numeric and return its canonical-float string form.

    Returns `None` if `value` is not parseable as a float — the caller
    propagates that as "untranslatable" rather than injecting unparseable
    SQL.

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


# Conservative check for Python-re features RE2 doesn't support.
_RE2_UNSAFE = re.compile(
    r"\\\d"  # backreference like \1 .. \9
    r"|\(\?[=!<]"  # lookahead / lookbehind / negative variants
)


def re2_safe(pattern: str) -> bool:
    """Return True if `pattern` uses only features RE2 supports.

    RE2 explicitly excludes backreferences and lookarounds. Use this as
    the regex feature-detector in dialects whose SQL engine uses RE2 —
    Snowflake, BigQuery, DuckDB, ClickHouse.
    """
    return _RE2_UNSAFE.search(pattern) is None


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
