"""SQL translator for Flagsmith segment predicates.

Public API:
    translate_segment(segment, ctx) -> Optional[str]
    TranslateContext

See README.md for usage. The translator is dialect-aware via the `Dialect`
protocol — `flagsmith_sql_flag_engine.dialects.snowflake.SnowflakeDialect`
is the only implementation today.
"""

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
    "Dialect",
    "TranslateContext",
    "translate_condition",
    "translate_rule",
    "translate_segment",
]
