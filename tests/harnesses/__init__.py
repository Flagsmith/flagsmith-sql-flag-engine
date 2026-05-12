"""Engine-parity test harnesses — one per SQL engine.

The conftest fixtures are parametrised over `HARNESSES`; adding a new
dialect means writing one harness module and appending it here.
"""

from tests.harnesses._base import (
    DialectTestHarness,
    EvaluationCase,
    IdentityRow,
)
from tests.harnesses.clickhouse import ClickHouseHarness
from tests.harnesses.snowflake import SnowflakeHarness

HARNESSES: list[DialectTestHarness] = [SnowflakeHarness(), ClickHouseHarness()]

__all__ = [
    "DialectTestHarness",
    "EvaluationCase",
    "HARNESSES",
    "IdentityRow",
]
