# flagsmith-sql-flag-engine

SQL translator for Flagsmith segment predicates.

Where the Python and Rust engines evaluate `is_context_in_segment` directly,
this engine takes a `SegmentContext` and emits a SQL `WHERE` expression. The
SQL goes against an `IDENTITIES` table (one row per identity) plus a `TRAITS`
table (long-form, one row per identity-trait pair); trait-bound conditions
become `EXISTS` subqueries. `PERCENTAGE_SPLIT` and `:semver`-marked
comparators compile to inline pure-SQL — no UDF call required at runtime.

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

`environment_id` in the `IDENTITIES` and `TRAITS` tables is a string column
holding `EnvironmentContext.key` directly — the same identifier the engine
uses, no separate integer PK.

`translate_segment` returns `None` if the segment uses an operator the
translator can't handle today (REGEX with backreferences or lookarounds —
RE2 doesn't support them). Callers should fall back to the Python engine for
those segments, or surface an error at segment-edit time.

## Engine parity

Validated against [Flagsmith/engine-test-data](https://github.com/Flagsmith/engine-test-data),
the test suite all engine implementations are tested against. The parity
suite loads each test case's identity + traits into a Snowflake scratch
schema, translates the case's segments, runs the generated SQL, and
compares to `flag_engine.is_context_in_segment`.

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
| `IS_SET`, `IS_NOT_SET`                       |     yes      | `EXISTS`/`NOT EXISTS` on `TRAITS`                     |
| `CONTAINS`, `NOT_CONTAINS`                   |     yes      |                                                       |
| `GREATER_THAN`, `LESS_THAN` (+ `_INCLUSIVE`) |     yes      |                                                       |
| `MODULO`                                     |     yes      |                                                       |
| `PERCENTAGE_SPLIT`                           |     yes      | inlined MD5-mod-9999; ~0.005% diverge on hash==9998   |
| `REGEX`                                      |   partial    | RE2 syntax only; backref/lookaround → caller fallback |
| `:semver`-marked comparators                 |     yes      | major.minor.patch only; ignores prerelease            |

## Development

```bash
uv sync                       # install dev deps (auto-includes dev group)
uv run pytest -m "not parity" # unit tests, no Snowflake required
uv run pytest -m parity       # parity suite, Snowflake creds required
uv run ruff check
uv run mypy
```
