# flagsmith-sql-flag-engine

SQL translator for Flagsmith segment predicates.

Where the Python and Rust `flag_engine` implementations evaluate
`is_context_in_segment` against an in-memory `EvaluationContext`, this
package takes a `SegmentContext` and emits a SQL `WHERE` expression that
evaluates the segment against an entire `IDENTITIES` table — one row per
identity, with the identity's full trait map held in a single column
the translator path-extracts at query time. `PERCENTAGE_SPLIT` and
`:semver`-marked comparators compile to inline pure-SQL.

## Quickstart

```python
from flag_engine.context.types import EvaluationContext, SegmentContext

from flagsmith_sql_flag_engine import TranslateContext, translate_segment
from flagsmith_sql_flag_engine.dialects import ClickHouseDialect

eval_context: EvaluationContext = {
    "environment": {"key": "n9fbf9...3ngWhb", "name": "Production"},
}
ctx = TranslateContext(evaluation_context=eval_context, dialect=ClickHouseDialect())

segment: SegmentContext = {
    "key": "growth-cohort",
    "name": "Growth cohort",
    "rules": [
        {
            "type": "ALL",
            "conditions": [
                {"operator": "EQUAL", "property": "plan", "value": "growth"},
            ],
        },
    ],
}
where_expr = translate_segment(segment, ctx)
# where_expr is a SQL string. Drop into:
#   SELECT COUNT(*) FROM IDENTITIES i
#   WHERE i.environment_id = 'n9fbf9...3ngWhb' AND ({where_expr})
```

`environment_id` in the `IDENTITIES` table is a string column holding
`EnvironmentContext.key` directly — the same identifier the engine uses,
no separate integer PK.

`translate_segment` returns `None` if the segment uses an operator the
translator can't handle — typically a REGEX pattern the active dialect's
regex flavour can't compile. Callers should fall back to
`flag_engine.is_context_in_segment` for those segments.

## Schema

Each dialect publishes the table layout it expects via a `schema_ddl`
constant. For Snowflake:

```sql
CREATE TABLE IF NOT EXISTS IDENTITIES (
    environment_id STRING NOT NULL,
    id NUMBER NOT NULL,
    identifier STRING NOT NULL,
    identity_key STRING NOT NULL,
    traits VARIANT,
    PRIMARY KEY (environment_id, id)
)
CLUSTER BY (environment_id, id);
```

For ClickHouse:

```sql
CREATE TABLE IF NOT EXISTS IDENTITIES (
    environment_id String,
    id UInt64,
    identifier String,
    identity_key String,
    traits JSON
)
ENGINE = MergeTree()
ORDER BY (environment_id, id);
```

Both engines store traits in a single columnar-JSON column —
Snowflake's `VARIANT` and ClickHouse's `JSON` (24+, GA in 25.x). Each
key is stored as a typed subcolumn, so trait reads are direct columnar
scans rather than per-row JSON parses. Trait keys are *data* — new keys
appear without schema changes — and the translator only sees the
abstract path extraction.

ClickHouse Cloud requires `SET allow_experimental_json_type = 1` when
creating a `JSON`-column table (the type is GA on OSS 25.x); the test
harness applies this setting automatically.

Programmatic access:

```python
from flagsmith_sql_flag_engine.dialects.snowflake import SCHEMA_DDL as SNOWFLAKE_DDL
from flagsmith_sql_flag_engine.dialects.clickhouse import SCHEMA_DDL as CLICKHOUSE_DDL
```

## Engine parity

Validated against [Flagsmith/engine-test-data](https://github.com/Flagsmith/engine-test-data),
the test suite every engine implementation is checked against. The
engine-parity suite loads each test case's identity into a per-dialect
scratch table, translates the case's segments, runs the generated SQL,
and compares to `flag_engine.is_context_in_segment`.

To run the engine-parity suite locally:

```bash
git submodule update --init                 # pull engine-test-data

# Snowflake
export SNOWFLAKE_ACCOUNT=...
export SNOWFLAKE_USER=...
export SNOWFLAKE_PRIVATE_KEY_PATH=...

# ClickHouse — bring up the local container the CI workflow also uses
docker compose up --detach --wait clickhouse

uv run pytest tests/test_engine.py
```

Each harness's environment variables are only read at session-create
time; to run a single dialect's parity, pass e.g. `-k snowflake` or
`-k clickhouse` and only export that dialect's credentials.

Adding a new dialect's parity coverage is one harness module — see
`tests/harnesses/` for the shape.

## Dialects

The translator is dialect-aware: a `Dialect` protocol abstracts the
SQL fragments that differ across SQL engines — MD5 hex, hex-to-int
parsing, prefix-anchored regex, padded-version comparison, type-aware
trait predicates, regex flavour. Today `SnowflakeDialect` and
`ClickHouseDialect` are implemented; adding another engine such as
DuckDB or Postgres means writing one class.

## Operator coverage

| Operator                                     | Translatable | Notes                                                          |
| -------------------------------------------- | :----------: | -------------------------------------------------------------- |
| `EQUAL`, `NOT_EQUAL`, `IN`                   |     yes      |                                                                |
| `IS_SET`, `IS_NOT_SET`                       |     yes      | trait subcolumn `IS NOT NULL` / `IS NULL`                      |
| `CONTAINS`, `NOT_CONTAINS`                   |     yes      |                                                                |
| `GREATER_THAN`, `LESS_THAN` plus `_INCLUSIVE`|     yes      |                                                                |
| `MODULO`                                     |     yes      |                                                                |
| `PERCENTAGE_SPLIT`                           |     yes      | inlined MD5-mod-9999; ~0.005% diverge on hash==9998            |
| `REGEX`                                      |   partial    | dialect-flavour gated; unsupported patterns → caller fallback  |
| `:semver`-marked comparators                 |     yes      | major.minor.patch only; ignores prerelease                     |

## Development

```bash
make install                  # uv sync + pre-commit install
make lint                     # run pre-commit hooks across the tree
make typecheck                # mypy
make test                     # unit tests
```

Ruff (lint + format) runs as a pre-commit hook on every commit. Mypy
runs as a `make typecheck` hook on staged Python files.
