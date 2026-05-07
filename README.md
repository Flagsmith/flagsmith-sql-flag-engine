# flagsmith-sql-flag-engine

SQL translator for Flagsmith segment predicates.

Where the Python and Rust engines evaluate `is_context_in_segment` directly,
this engine takes a `SegmentContext` and emits a SQL `WHERE` expression. The
SQL goes against a single `IDENTITIES` table — one row per identity, with
the identity's full trait map held in a `traits` `VARIANT` column. Trait
keys are *data* in the VARIANT, not schema columns; new keys never require
DDL. `PERCENTAGE_SPLIT` and `:semver`-marked comparators compile to inline
pure-SQL — no UDF call required at runtime.

## Quickstart

```python
from flag_engine.context.types import EnvironmentContext, SegmentContext

from flagsmith_sql_flag_engine import TranslateContext, translate_segment
from flagsmith_sql_flag_engine.dialects import SnowflakeDialect

env: EnvironmentContext = {"key": "n9fbf9h3v4fFgH3U3ngWhb", "name": "Production"}
ctx = TranslateContext(environment=env, dialect=SnowflakeDialect())

segment: SegmentContext = ...  # your segment definition
where_expr = translate_segment(segment, ctx)
# where_expr is a SQL string. Drop into:
#   SELECT COUNT(*) FROM IDENTITIES i
#   WHERE i.environment_id = 'n9fbf9h3v4fFgH3U3ngWhb' AND ({where_expr})
```

`environment_id` in the `IDENTITIES` table is a string column holding
`EnvironmentContext.key` directly — the same identifier the engine uses,
no separate integer PK.

`translate_segment` returns `None` if the segment uses an operator the
translator can't handle today (REGEX with backreferences or lookarounds —
RE2 doesn't support them). Callers should fall back to the Python engine for
those segments, or surface an error at segment-edit time.

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

Run this once when standing up a Snowflake-backed Flagsmith installation.
The CDC pipeline that materialises `IDENTITIES` from the source-of-truth
trait stream populates the `traits` VARIANT with the identity's full trait
map: `{"plan": "growth", "country": "GB", ...}`. New trait keys appear as
new keys inside the VARIANT — no `ALTER TABLE`, no DDL on the write path.

The `traits` VARIANT is *pre-materialised* by the CDC pipeline. It's not
a long-form join + aggregate at query time (which would be slow); query
time it's a single columnar read with subkey extraction.

Programmatic access:

```python
from flagsmith_sql_flag_engine.dialects.snowflake import SCHEMA_DDL
print(SCHEMA_DDL)
```

### Performance

Measured at 870M identities, `COMPUTE_WH` (X-Small), cache disabled
(every query a fresh scan):

| scenario                | typed columns | `traits` VARIANT |
| :---------------------- | ------------: | ---------------: |
| simple (IN + IS_SET)    | 1.5s          | **2.6s**         |
| multi (4 conditions)    | 2.1s          | **3.1s**         |
| pure `%Split`           | 91.9s         | 91.9s            |

VARIANT path-extraction adds a ~50-75% overhead vs column-per-trait
typed wide-form, in exchange for schema-flexibility (no `ALTER TABLE`
on the write path, no column-count ceiling, trait keys are data). Pure
`%Split` is unaffected because it doesn't read traits.

Warehouse-scaling is roughly linear: dividing the X-Small numbers by
the warehouse size multiplier gives the latency on a larger warehouse.
870M multi-condition queries land at ~750ms on Medium and ~400ms on
Large.

## Engine parity

Validated against [Flagsmith/engine-test-data](https://github.com/Flagsmith/engine-test-data),
the test suite all engine implementations are tested against. The parity
suite loads each test case's identity into a Snowflake scratch table,
translates the case's segments, runs the generated SQL, and compares to
`flag_engine.is_context_in_segment`.

To run the parity suite locally:

```bash
git submodule update --init                 # pull engine-test-data
export SNOWFLAKE_ACCOUNT=...
export SNOWFLAKE_USER=...
export SNOWFLAKE_PRIVATE_KEY_PATH=...
uv run pytest -m parity
```

## Dialects

The translator is dialect-aware: a `Dialect` protocol abstracts the SQL
fragments that differ across SQL engines (MD5 hex, hex-to-int parsing,
prefix-anchored regex, padded-version comparison, etc.). Today only
`SnowflakeDialect` is implemented; adding another engine (e.g. DuckDB,
Postgres) means writing one class.

## Operator coverage

| Operator                                     | Translatable | Notes                                                 |
| -------------------------------------------- | :----------: | ----------------------------------------------------- |
| `EQUAL`, `NOT_EQUAL`, `IN`                   |     yes      |                                                       |
| `IS_SET`, `IS_NOT_SET`                       |     yes      | `traits:"<key>" IS NOT NULL` / `IS NULL`              |
| `CONTAINS`, `NOT_CONTAINS`                   |     yes      |                                                       |
| `GREATER_THAN`, `LESS_THAN` (+ `_INCLUSIVE`) |     yes      |                                                       |
| `MODULO`                                     |     yes      |                                                       |
| `PERCENTAGE_SPLIT`                           |     yes      | inlined MD5-mod-9999; ~0.005% diverge on hash==9998   |
| `REGEX`                                      |   partial    | RE2 syntax only; backref/lookaround → caller fallback |
| `:semver`-marked comparators                 |     yes      | major.minor.patch only; ignores prerelease            |

## Development

```bash
make install                  # uv sync + pre-commit install
make lint                     # run pre-commit hooks across the tree
make typecheck                # mypy
make test                     # unit tests, no Snowflake required
make test opts="-m parity"    # parity suite, Snowflake creds required
```

Ruff (lint + format) runs as a pre-commit hook on every commit. Mypy
runs as a `make typecheck` hook on staged Python files.
