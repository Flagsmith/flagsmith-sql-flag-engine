import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from clickhouse_connect.driver import Client
from flag_engine.context.types import EvaluationContext, SegmentContext

from flagsmith_sql_flag_engine import (
    Binder,
    ClickHouseServerParamStyle,
    TranslateContext,
    translate_segment,
)
from flagsmith_sql_flag_engine.dialects.clickhouse import ClickHouseDialect
from tests.harnesses.clickhouse import ClickHouseHarness


@pytest.fixture
def clickhouse_session() -> Iterator[Client]:
    with ClickHouseHarness().session() as session:
        yield session


@pytest.fixture
def scratch_identities_table(clickhouse_session: Client) -> Iterator[str]:
    table = f"TEST_BINDER_{uuid.uuid4().hex[:8]}"
    clickhouse_session.command(
        f"CREATE TABLE {table} (environment_id String, id UInt64, identifier String,"
        f" identity_key String, traits JSON) ENGINE = Memory"
    )
    try:
        yield table
    finally:
        clickhouse_session.command(f"DROP TABLE IF EXISTS {table}")


def test_translate_segment__bound_percent_regex__matches_in_clickhouse(
    clickhouse_session: Any,
    scratch_identities_table: str,
) -> None:
    # Given
    table = scratch_identities_table
    clickhouse_session.command(
        f"INSERT INTO {table} (environment_id, id, identifier, identity_key, traits) VALUES"
        f" ('e', 1, 'match', 'k1', '{{\"email\": \"ada%lovelace@example.com\"}}'::JSON),"
        f" ('e', 2, 'no-match', 'k2', '{{\"email\": \"123@example.com\"}}'::JSON)"
    )
    seg: SegmentContext = {
        "key": "1",
        "name": "s",
        "rules": [
            {
                "type": "ALL",
                "conditions": [
                    {
                        "operator": "REGEX",
                        "property": "email",
                        "value": r"[a-z%]+@example\.com",
                    }
                ],
            }
        ],
    }
    binder = Binder(ClickHouseServerParamStyle())
    eval_ctx: EvaluationContext = {"environment": {"key": "e", "name": "Test"}}
    predicate = translate_segment(
        seg, TranslateContext(eval_ctx, ClickHouseDialect(), binder=binder)
    )
    assert predicate is not None

    # When
    rows = clickhouse_session.query(
        f"SELECT i.identifier FROM {table} i WHERE ({predicate}) ORDER BY i.identifier",
        parameters=binder.params,
    ).result_rows

    # Then
    assert [row[0] for row in rows] == ["match"]
