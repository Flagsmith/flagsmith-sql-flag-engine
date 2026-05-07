"""Unit tests for the SQL-injection guards.

Sanitiser is the single seam for value-derived strings becoming SQL. These
tests document the contract the rest of the translator depends on.
"""

from __future__ import annotations

from flagsmith_sql_flag_engine.sanitiser import Sanitiser


def test_escape_string__plain_text__passes_through_unchanged() -> None:
    # Given a value with no quoting-significant characters
    value = "growth"

    # When escaped
    out = Sanitiser.escape_string(value)

    # Then the value passes through unchanged
    assert out == "growth"


def test_escape_string__embedded_single_quote__doubles_quote() -> None:
    # Given a value containing a single quote
    value = "o'brien"

    # When escaped
    out = Sanitiser.escape_string(value)

    # Then the quote is doubled (SQL standard)
    assert out == "o''brien"


def test_escape_string__injection_payload__escapes_terminator() -> None:
    # Given a SQL-injection-shaped value with a single quote
    value = "x' OR '1'='1"

    # When escaped
    out = Sanitiser.escape_string(value)

    # Then every single quote is doubled, neutralising the literal-terminator
    assert out == "x'' OR ''1''=''1"


def test_string_literal__plain_value__wraps_in_single_quotes() -> None:
    # Given a value with no quoting-significant characters
    value = "growth"

    # When wrapped as a SQL string literal
    out = Sanitiser.string_literal(value)

    # Then the result is the value wrapped in single quotes
    assert out == "'growth'"


def test_string_literal__injection_payload__neutralises_terminator() -> None:
    # Given a SQL-injection-shaped value
    value = "x'); DROP TABLE IDENTITIES; --"

    # When wrapped as a SQL string literal
    out = Sanitiser.string_literal(value)

    # Then the embedded quote is doubled and the wrapper holds together
    assert out == "'x''); DROP TABLE IDENTITIES; --'"


def test_variant_path_key__plain_key__wraps_in_double_quotes() -> None:
    # Given a trait key with no quoting-significant characters
    key = "plan"

    # When wrapped as a VARIANT path key
    out = Sanitiser.variant_path_key(key)

    # Then the result is the key wrapped in double quotes
    assert out == '"plan"'


def test_variant_path_key__embedded_double_quote__doubles_quote() -> None:
    # Given a trait key containing a double quote
    key = 'has"quote'

    # When wrapped
    out = Sanitiser.variant_path_key(key)

    # Then the embedded double quote is doubled (SQL standard for identifiers)
    assert out == '"has""quote"'


def test_variant_path_key__hyphenated_key__round_trips() -> None:
    # Given a trait key with characters illegal in unquoted SQL identifiers
    key = "user-name"

    # When wrapped
    out = Sanitiser.variant_path_key(key)

    # Then double-quoting accepts it as an identifier
    assert out == '"user-name"'


def test_numeric_literal__numeric_string__returns_canonical_float_string() -> None:
    # Given a string that parses cleanly as a number
    value = "30"

    # When sanitised
    out = Sanitiser.numeric_literal(value)

    # Then the result is the canonical float string form
    assert out == "30.0"


def test_numeric_literal__numeric_value__returns_canonical_float_string() -> None:
    # Given an int or float value
    # When sanitised
    # Then both produce canonical float string forms
    assert Sanitiser.numeric_literal(30) == "30.0"
    assert Sanitiser.numeric_literal(1.5) == "1.5"


def test_numeric_literal__non_numeric_string__returns_none() -> None:
    # Given a non-numeric string
    value = "abc"

    # When sanitised
    out = Sanitiser.numeric_literal(value)

    # Then the sanitiser declines (signals untranslatable to caller)
    assert out is None


def test_numeric_literal__injection_payload__returns_none() -> None:
    # Given a SQL-injection-shaped value
    value = "100; DROP TABLE IDENTITIES; --"

    # When sanitised
    out = Sanitiser.numeric_literal(value)

    # Then the sanitiser declines — float() raises before any SQL is built
    assert out is None


def test_numeric_literal__none_value__returns_none() -> None:
    # Given a None value
    # When sanitised
    # Then the sanitiser declines (TypeError caught)
    assert Sanitiser.numeric_literal(None) is None


def test_numeric_literal__bool_value__returns_none() -> None:
    # Given a Python bool (which float() would happily coerce to 1.0/0.0)
    # When sanitised
    # Then the sanitiser declines explicitly — engine treats bool segment
    # values as strings via type-coercion, so a numeric interpretation
    # would diverge from engine behaviour
    assert Sanitiser.numeric_literal(True) is None
    assert Sanitiser.numeric_literal(False) is None


def test_modulo_literal__well_formed_pair__returns_canonical_floats() -> None:
    # Given a well-formed `divisor|remainder` operand
    value = "5|0"

    # When sanitised
    out = Sanitiser.modulo_literal(value)

    # Then both sides come back as canonical float strings
    assert out == ("5.0", "0.0")


def test_modulo_literal__missing_separator__returns_none() -> None:
    # Given a value lacking the `|` separator
    value = "5"

    # When sanitised
    # Then the sanitiser declines (ValueError on unpack)
    assert Sanitiser.modulo_literal(value) is None


def test_modulo_literal__injection_in_divisor__returns_none() -> None:
    # Given an injection payload in the divisor side
    value = "5; DROP TABLE IDENTITIES; --|0"

    # When sanitised
    out = Sanitiser.modulo_literal(value)

    # Then the sanitiser declines (float() on the divisor raises)
    assert out is None


def test_modulo_literal__injection_in_remainder__returns_none() -> None:
    # Given an injection payload in the remainder side
    value = "5|0; DROP TABLE IDENTITIES; --"

    # When sanitised
    out = Sanitiser.modulo_literal(value)

    # Then the sanitiser declines
    assert out is None


def test_modulo_literal__non_string_value__returns_none() -> None:
    # Given a non-string value (e.g. None or a number)
    # When sanitised
    # Then the sanitiser declines — split() raises AttributeError on None,
    # produces no `|` for a number that stringifies cleanly
    assert Sanitiser.modulo_literal(None) is None
    assert Sanitiser.modulo_literal(5) is None
