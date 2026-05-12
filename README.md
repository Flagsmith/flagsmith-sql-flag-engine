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
from flagsmith_sql_flag_engine.dialects import SnowflakeDialect

eval_context: EvaluationContext = {
    "environment": {"key": "n9fbf9...3ngWhb", "name": "Production"},
}
ctx = TranslateContext(evaluation_context=eval_context, dialect=SnowflakeDialect())

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
    traits Nullable(String)
)
ENGINE = MergeTree()
ORDER BY (environment_id, id);
```

The Snowflake table stores traits in a single `VARIANT` column; the
ClickHouse table stores the JSON-encoded trait map in a `Nullable(String)`
column read via `JSONExtract*`. Either way, trait keys are *data* — new
keys appear without schema changes — and the translator only sees the
abstract path extraction.

Programmatic access:

```python
from flagsmith_sql_flag_engine.dialects.snowflake import SCHEMA_DDL as SNOWFLAKE_DDL
from flagsmith_sql_flag_engine.dialects.clickhouse import SCHEMA_DDL as CLICKHOUSE_DDL
```

## Engine parity

Validated against [Flagsmith/engine-test-data](https://github.com/Flagsmith/engine-test-data),
the test suite all engine implementations are tested against. The
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

# ClickHouse (bring up a local container first)
docker run -d --rm --name clickhouse-parity \
    -p 18123:8123 \
    -e CLICKHOUSE_SKIP_USER_SETUP=1 \
    clickhouse/clickhouse-server:latest
export CLICKHOUSE_HOST=localhost
export CLICKHOUSE_PORT=18123

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

### Snowflake vs ClickHouse

Both dialects pass the engine-parity suite with the same two xfails
(prerelease semver sort and `$.`-prefixed trait names — translator-level
divergences shared by every dialect). Operator coverage is identical.

The shape of the two implementations differs because the engines do:

| Concern              | Snowflake                                | ClickHouse                                                  |
| -------------------- | ---------------------------------------- | ----------------------------------------------------------- |
| Trait storage        | `VARIANT` (columnar JSON)                | `Nullable(String)` JSON read via `JSONExtract*`             |
| Trait path           | `i.traits:"key"` returns VARIANT         | `if(JSONType=Null, NULL, ...)` returning canonical string   |
| Type discrimination  | `TYPEOF`, `IS_BOOLEAN`, `IS_DECIMAL`     | `JSONType` (Enum8) — exact type names                       |
| Hex chunk parse      | `TO_NUMBER(SUBSTR(...), 'XXXXXXXX')`     | `reinterpretAsUInt32(reverse(unhex(substring(...))))`       |
| Anchored regex       | `REGEXP_INSTR(value, pat) = 1`           | `match(value, '^(pat)')` — `match` is unanchored            |
| Numeric coalesce     | Lossless string fast path via VARIANT::STRING | Per-type dispatch — no analogous canonical stringify   |
| Nullable propagation | `(VARIANT NULL)::STRING → NULL`          | `Nullable(String)` is explicit; regex funcs reject it       |

Practical implications for callers:

- ClickHouse's `match`, `extractAll` reject `Nullable(String)` input
  because the implied result types are unrepresentable. The dialect
  wraps regex value expressions in `ifNull(..., '')`; trait paths are
  always guarded by `IS NOT NULL` upstream, so the default is
  unreachable at runtime.
- Snowflake's VARIANT path collapses both missing keys and JSON null to
  SQL NULL "by accident" (cast propagation). The ClickHouse dialect
  collapses both explicitly via `JSONType = 'Null'`. Caller-visible
  behaviour is the same.
- ClickHouse's batched `EXISTS`-equivalent uses windowed `count() > 0`
  inside a `UNION ALL` — `EXISTS (SELECT 1 FROM ...)` isn't a top-level
  expression in ClickHouse the way it is in Snowflake.

## Operator coverage

| Operator                                     | Translatable | Notes                                                          |
| -------------------------------------------- | :----------: | -------------------------------------------------------------- |
| `EQUAL`, `NOT_EQUAL`, `IN`                   |     yes      |                                                                |
| `IS_SET`, `IS_NOT_SET`                       |     yes      | `traits:"<key>" IS NOT NULL` / `IS NULL`                       |
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
