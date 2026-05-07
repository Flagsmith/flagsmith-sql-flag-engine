"""Pytest fixtures.

Unit tests run anywhere; the parity suite needs a Snowflake account. The
parity fixture skips automatically if `SNOWFLAKE_ACCOUNT` isn't set, so
running `pytest` with no env vars only runs the unit tests.

Each parity-test session creates a per-run pair of transient tables
(`IDENTITIES_PARITY_<uuid>`, `TRAITS_PARITY_<uuid>`) so concurrent CI runs
don't step on each other. Tables are dropped on teardown.

`environment_id` columns are STRING — they hold `EnvironmentContext.key`
values directly, matching the engine's vocabulary.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_TEST_DATA = REPO_ROOT / "engine-test-data" / "test_cases"


def _snowflake_creds_present() -> bool:
    return bool(os.environ.get("SNOWFLAKE_ACCOUNT")) and bool(
        os.environ.get("SNOWFLAKE_USER")
    )


@pytest.fixture(scope="session")
def snowflake_session() -> Iterator[Any]:
    """Snowpark session keyed off SNOWFLAKE_* env vars. Session-scoped."""
    if not _snowflake_creds_present():
        pytest.skip("SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER not set")
    from snowflake.snowpark import Session

    config: dict[str, str] = {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "role": os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        "database": os.environ.get("SNOWFLAKE_DATABASE", "FS_TEST"),
        "schema": os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC"),
    }
    if pk_path := os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH"):
        config["private_key_file"] = pk_path
    elif password := os.environ.get("SNOWFLAKE_PASSWORD"):
        config["password"] = password
    else:
        pytest.skip("no SNOWFLAKE_PRIVATE_KEY_PATH or SNOWFLAKE_PASSWORD")

    sess = Session.builder.configs(config).create()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture(scope="session")
def parity_tables(snowflake_session: Any) -> Iterator[tuple[str, str]]:
    """Create a per-run pair of scratch tables matching the production schema
    shape (string `environment_id`). Returns `(identities_table, traits_table)`
    fully-qualified names.
    """
    suffix = uuid.uuid4().hex[:8]
    db = os.environ.get("SNOWFLAKE_DATABASE", "FS_TEST")
    schema = os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC")
    identities = f"{db}.{schema}.IDENTITIES_PARITY_{suffix}"
    traits = f"{db}.{schema}.TRAITS_PARITY_{suffix}"
    snowflake_session.sql(
        f"""
        CREATE TRANSIENT TABLE {identities} (
            environment_id STRING,
            id NUMBER,
            identifier STRING,
            identity_key STRING
        )
        """
    ).collect()
    snowflake_session.sql(
        f"""
        CREATE TRANSIENT TABLE {traits} (
            environment_id STRING,
            identity_id NUMBER,
            trait_key STRING,
            string_value STRING,
            integer_value NUMBER,
            float_value FLOAT,
            boolean_value BOOLEAN
        )
        """
    ).collect()
    try:
        yield identities, traits
    finally:
        snowflake_session.sql(f"DROP TABLE IF EXISTS {identities}").collect()
        snowflake_session.sql(f"DROP TABLE IF EXISTS {traits}").collect()


def load_test_cases() -> list[dict]:
    """Load all engine-test-data cases. Returns empty list if the submodule
    hasn't been initialised — the parity tests will skip in that case."""
    if not ENGINE_TEST_DATA.exists():
        return []
    cases: list[dict] = []
    for path in sorted(ENGINE_TEST_DATA.glob("*.json")):
        with open(path) as f:
            cases.append(json.load(f))
    return cases


def parity_env_key(case_idx: int, original: str) -> str:
    """Per-case unique env key. Prefixed with `parity-{idx}` so concurrent
    cases (or cases that share an `environment.key` in the source data)
    don't see each other's identities."""
    return f"parity-{case_idx}-{original}"
