"""Dialect implementations. Today only Snowflake is supported."""

from flagsmith_sql_flag_engine.dialects.snowflake import SnowflakeDialect

__all__ = ["SnowflakeDialect"]
