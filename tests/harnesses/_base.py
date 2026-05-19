"""Engine-parity test harness protocol.

Each `DialectTestHarness` adapts the engine-parity suite to one SQL
engine. The conftest fixtures are parametrised over the registered
harnesses; a test does an in-memory dict lookup against the harness's
results.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from flagsmith_sql_flag_engine.dialect import Dialect


@dataclass(frozen=True)
class IdentityRow:
    """One row to seed into the harness's scratch IDENTITIES table.

    Mirrors the canonical IDENTITIES schema: 4 typed columns plus a
    `traits` payload pre-serialised to JSON (None when the source case
    has no traits)."""

    environment_id: str
    id: int
    identifier: str
    identity_key: str
    traits_json: str | None


@dataclass(frozen=True)
class EvaluationCase:
    """One (case, segment) predicate to run against the scratch table.
    `pair_id` is an opaque round-trip key the harness echoes back in its
    result dict — the conftest uses it to recover the (case_name,
    segment_key) tuple."""

    pair_id: str
    environment_key: str
    predicate_sql: str


class DialectTestHarness(Protocol):
    """Adapter for running the engine-parity suite against one SQL engine.

    Concrete harnesses own session/connection setup, scratch-table DDL,
    INSERT batching, and the (case, segment) mega-SELECT shape — all the
    bits that vary by SQL engine.
    """

    name: str
    dialect: Dialect
    xfail_case_names: set[str]

    def session(self) -> AbstractContextManager[Any]:
        """Open a session/connection. Caller manages lifecycle via ctx-mgr."""

    def setup_identities(self, session: Any, rows: list[IdentityRow]) -> str:
        """Create scratch IDENTITIES table on `session`, batch-INSERT all
        `rows`, return the fully-qualified table name."""

    def evaluate(
        self,
        session: Any,
        identity_table: str,
        cases: list[EvaluationCase],
    ) -> dict[str, bool]:
        """Run all `cases` as one batched query. Each case translates to
        `EXISTS (SELECT 1 FROM identity_table i WHERE
        i.environment_id = case.environment_key AND (case.predicate_sql))`
        (or the dialect's equivalent). Returns `pair_id -> is_match`."""
