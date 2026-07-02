"""SQL translator for Flagsmith segment predicates.

Public API:
    translate_segment(segment, ctx) -> str | None
    TranslateContext

By default the translator inlines each segment value as an escaped SQL
string literal. Pass a `Binder` on the `TranslateContext` to bind
value-bearing literals as query parameters instead — read its params off
`Binder.params` after translation. See `flagsmith_sql_flag_engine.binder`.

See README.md for usage. The translator is dialect-aware via the `Dialect`
protocol; `flagsmith_sql_flag_engine.dialects.clickhouse.ClickHouseDialect`
is the only implementation today.
"""

from flagsmith_sql_flag_engine.binder import (
    Binder,
    ClickHouseServerParamStyle,
    ParamStyle,
    PyformatParamStyle,
)
from flagsmith_sql_flag_engine.dialect import Dialect
from flagsmith_sql_flag_engine.translator import (
    TRANSLATABLE_OPERATORS,
    TranslateContext,
    translate_condition,
    translate_rule,
    translate_segment,
)

__all__ = [
    "TRANSLATABLE_OPERATORS",
    "Binder",
    "ClickHouseServerParamStyle",
    "Dialect",
    "ParamStyle",
    "PyformatParamStyle",
    "TranslateContext",
    "translate_condition",
    "translate_rule",
    "translate_segment",
]
