"""Snowflake harness for the engine-parity suite.

Owns the Snowpark session, the per-run `IDENTITIES_ENGINE_PARITY_<uuid>`
TEMPORARY table (auto-drops at session close, no teardown), and the
batched `INSERT ... SELECT UNION ALL` and `SELECT ... UNION ALL` shapes
that round-trip through Snowflake in two queries per session.
"""

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from snowflake.snowpark import Session

from flagsmith_sql_flag_engine.dialect import Dialect
from flagsmith_sql_flag_engine.dialects.snowflake import SnowflakeDialect
from flagsmith_sql_flag_engine.utils import escape_string
from tests.harnesses._base import EvaluationCase, IdentityRow

# Cases the SQL translator can't match the engine on under Snowflake;
# xfail keeps the divergence visible without masking a regression
# elsewhere. Entries are file stems (matching `EngineTestCase.name`);
# add the why inline.
_XFAIL_CASE_NAMES: set[str] = {
    # Engine sorts semver prereleases (1.0.0-rc.2 < 1.0.0-rc.3); the SQL
    # semver-sort-key collapses to major.minor.patch only.
    "test_semver_greater_than_prerelease__should_match",
    "test_semver_less_than_prerelease__should_match",
    # Engine does trait-first dispatch: a row with a trait literally named
    # `$.identity` shadows the JSONPath lookup. Replicating per-row trait
    # fallback in SQL roughly doubles the cost of every wrapped JSONPath
    # condition (Snowflake evaluates both IFF arms), so we accept the
    # divergence on this niche shape (`$.`-prefixed trait names) and let
    # callers fall back to the engine.
    "test_jsonpath_like_trait__existing_jsonpath__should_match_trait",
}


def _q(s: str) -> str:
    """Quote a value for inclusion in a single-quoted Snowflake string
    literal. Snowflake string literals process `\\` as an escape, so JSON
    traits with `\\uXXXX` or `\\"` would lose their backslash before
    reaching PARSE_JSON; double the backslashes here. The single-quote
    doubling is the SQL-standard escape that `escape_string` already
    handles."""
    return escape_string(s.replace("\\", "\\\\"))


class SnowflakeHarness:
    name: str = "snowflake"
    dialect: Dialect = SnowflakeDialect()
    xfail_case_names: set[str] = _XFAIL_CASE_NAMES

    @contextmanager
    def session(self) -> Iterator[Session]:
        config: dict[str, str] = {
            "account": os.environ["SNOWFLAKE_ACCOUNT"],
            "user": os.environ["SNOWFLAKE_USER"],
            "role": os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
            "database": os.environ.get("SNOWFLAKE_DATABASE", "FS_TEST"),
            "schema": os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC"),
            "private_key_file": os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"],
        }
        sess = Session.builder.configs(config).create()
        try:
            yield sess
        finally:
            sess.close()

    def setup_identities(self, session: Session, rows: list[IdentityRow]) -> str:
        suffix = uuid.uuid4().hex[:8]
        db = os.environ.get("SNOWFLAKE_DATABASE", "FS_TEST")
        schema = os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC")
        table = f"{db}.{schema}.IDENTITIES_ENGINE_PARITY_{suffix}"
        # TEMPORARY so the table auto-drops at session close — no teardown.
        session.sql(
            f"""
            CREATE TEMPORARY TABLE {table} (
                environment_id STRING NOT NULL,
                id NUMBER NOT NULL,
                identifier STRING NOT NULL,
                identity_key STRING NOT NULL,
                traits VARIANT
            )
            """
        ).collect()

        if not rows:
            return table

        selects = [
            f"SELECT '{_q(r.environment_id)}', {r.id}, "
            f"'{_q(r.identifier)}', '{_q(r.identity_key)}', "
            + (f"PARSE_JSON('{_q(r.traits_json)}')" if r.traits_json else "NULL")
            for r in rows
        ]
        session.sql(
            f"INSERT INTO {table} "
            "(environment_id, id, identifier, identity_key, traits) "
            + "\nUNION ALL\n".join(selects)
        ).collect()
        return table

    def evaluate(
        self,
        session: Session,
        identity_table: str,
        cases: list[EvaluationCase],
    ) -> dict[str, bool]:
        select_clauses = [
            f"SELECT '{_q(c.pair_id)}' AS pair_id, "
            f"EXISTS (SELECT 1 FROM {identity_table} i "
            f"WHERE i.environment_id = '{_q(c.environment_key)}' "
            f"AND ({c.predicate_sql})) AS m"
            for c in cases
        ]
        rows = session.sql("\nUNION ALL\n".join(select_clauses)).collect()
        return {row["PAIR_ID"]: bool(row["M"]) for row in rows}
