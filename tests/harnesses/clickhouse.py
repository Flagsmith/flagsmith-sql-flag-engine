"""ClickHouse harness for the engine-parity suite.

Owns the `clickhouse-connect` HTTP client, the per-run
`IDENTITIES_ENGINE_PARITY_<uuid>` Memory-engine table, and the batched
`INSERT` + `UNION ALL` `SELECT` shapes that round-trip through ClickHouse
in two queries per session.

Reads connection settings from the `CLICKHOUSE_*` environment variables;
all default to the values served by the standard
`clickhouse/clickhouse-server` Docker image on `localhost:8123`.
"""

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver import Client

from flagsmith_sql_flag_engine.dialect import Dialect
from flagsmith_sql_flag_engine.dialects.clickhouse import ClickHouseDialect
from flagsmith_sql_flag_engine.utils import escape_string
from tests.harnesses._base import EvaluationCase, IdentityRow

# Cases the SQL translator can't match the engine on under ClickHouse;
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
    # condition, so we accept the divergence on this niche shape
    # (`$.`-prefixed trait names) and let callers fall back to the engine.
    "test_jsonpath_like_trait__existing_jsonpath__should_match_trait",
    # Engine returns False for a PERCENTAGE_SPLIT when the eval context has
    # no identity key. The SQL engine intentionally diverges: it runs over
    # IDENTITIES rows where the identity key always exists, so it hashes the
    # row's key rather than folding to FALSE — the behaviour row-oriented
    # callers (segment-membership counts/members) need.
    "test_percentage_split__no_identity_key__should_match",
}


def _q(s: str) -> str:
    """Escape a value for inclusion in a single-quoted ClickHouse string
    literal. ClickHouse string literals process `\\` as an escape, so JSON
    traits with `\\uXXXX` or `\\"` would lose their backslash before
    reaching `JSONExtract*`; double the backslashes here. Single quotes
    are escaped by doubling per the SQL standard."""
    return escape_string(s.replace("\\", "\\\\"))


class ClickHouseHarness:
    name: str = "clickhouse"
    dialect: Dialect = ClickHouseDialect()
    xfail_case_names: set[str] = _XFAIL_CASE_NAMES

    @contextmanager
    def session(self) -> Iterator[Client]:
        client = clickhouse_connect.get_client(
            host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
            database=os.environ.get("CLICKHOUSE_DATABASE", "default"),
            # The parity suite stacks ~600 (case, segment) pairs into one
            # `UNION ALL` SELECT. The resulting query text comfortably
            # exceeds the 256 KB `max_query_size` default, and the parsed
            # AST exceeds the 50,000-element `max_ast_elements` default.
            # The harness is the only caller; production segments stay
            # well below both limits.
            settings={
                "max_query_size": 16 * 1024 * 1024,
                "max_ast_elements": 1_000_000,
                "max_expanded_ast_elements": 1_000_000,
                # Required for `JSON`-column DDL on ClickHouse Cloud as of
                # 25.12. No-op on OSS builds where the type is already GA.
                "allow_experimental_json_type": 1,
            },
        )
        try:
            yield client
        finally:
            client.close()

    def setup_identities(self, session: Client, rows: list[IdentityRow]) -> str:
        suffix = uuid.uuid4().hex[:8]
        database = os.environ.get("CLICKHOUSE_DATABASE", "default")
        table = f"{database}.IDENTITIES_ENGINE_PARITY_{suffix}"
        # Memory engine keeps the table in RAM — fast on inserts, no on-disk
        # cleanup. Caller is responsible for the DROP in the `evaluate`
        # path's finally block; the client disconnect also wipes Memory
        # tables, so a crashed test still self-cleans.
        # `JSON` column for traits — matches the dialect's `schema_ddl`
        # shape. Memory engine for the scratch table; no on-disk cleanup.
        session.command(
            f"""
            CREATE TABLE {table} (
                environment_id String,
                id UInt64,
                identifier String,
                identity_key String,
                traits JSON
            )
            ENGINE = Memory
            """
        )
        if rows:
            # clickhouse-connect's bulk `insert` doesn't accept JSON strings
            # for `JSON` columns directly — it expects Python dicts. Use a
            # raw `INSERT ... VALUES` with `::JSON` casts on the literal so
            # CH parses each trait map server-side.
            values = [
                f"('{_q(r.environment_id)}', {r.id},"
                f" '{_q(r.identifier)}', '{_q(r.identity_key)}',"
                + (f" '{_q(r.traits_json)}'::JSON)" if r.traits_json else " NULL)")
                for r in rows
            ]
            session.command(
                f"INSERT INTO {table}"
                f" (environment_id, id, identifier, identity_key, traits)"
                f" VALUES {','.join(values)}"
            )
        return table

    def evaluate(
        self,
        session: Client,
        identity_table: str,
        cases: list[EvaluationCase],
    ) -> dict[str, bool]:
        # ClickHouse doesn't have correlated `EXISTS (SELECT 1 FROM ...)`
        # the way Snowflake does, but a windowed `count() > 0` over a
        # WHERE-filtered scan gives the same boolean and lets us stack
        # every case into one `UNION ALL`.
        select_clauses = [
            f"SELECT '{_q(c.pair_id)}' AS pair_id,"
            f" toUInt8(count() > 0) AS m FROM {identity_table} i"
            f" WHERE i.environment_id = '{_q(c.environment_key)}'"
            f" AND ({c.predicate_sql})"
            for c in cases
        ]
        result = session.query("\nUNION ALL\n".join(select_clauses))
        rows: list[tuple[Any, ...]] = result.result_rows
        return {str(row[0]): bool(row[1]) for row in rows}
