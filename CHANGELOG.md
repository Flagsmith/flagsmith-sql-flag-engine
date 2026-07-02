# Changelog

## [0.2.0](https://github.com/Flagsmith/flagsmith-sql-flag-engine/compare/v0.1.3...v0.2.0) (2026-07-02)


### Features

* Bind segment values as query parameters ([#20](https://github.com/Flagsmith/flagsmith-sql-flag-engine/issues/20)) ([dc5ae0d](https://github.com/Flagsmith/flagsmith-sql-flag-engine/commit/dc5ae0d3c291ab3fbed16dd433ea9e142812cf54))

## [0.1.3](https://github.com/Flagsmith/flagsmith-sql-flag-engine/compare/v0.1.2...v0.1.3) (2026-07-01)


### Bug Fixes

* **translator:** % Split over a trait folds to FALSE without an identity context ([#18](https://github.com/Flagsmith/flagsmith-sql-flag-engine/issues/18)) ([31a4958](https://github.com/Flagsmith/flagsmith-sql-flag-engine/commit/31a4958810f9f0d4d33e5cdfb6dcc848e2208314))

## [0.1.2](https://github.com/Flagsmith/flagsmith-sql-flag-engine/compare/v0.1.1...v0.1.2) (2026-06-30)


### Bug Fixes

* **translator:** % Split without explicitly provided identity context translate to `FALSE` ([#16](https://github.com/Flagsmith/flagsmith-sql-flag-engine/issues/16)) ([78cbd1a](https://github.com/Flagsmith/flagsmith-sql-flag-engine/commit/78cbd1a36cc28f8e4c1bdf0b136112c6a257c489))

## [0.1.1](https://github.com/Flagsmith/flagsmith-sql-flag-engine/compare/v0.1.0...v0.1.1) (2026-05-27)


### Bug Fixes

* Segment rule with no conditions or nested rules crashes translation ([#14](https://github.com/Flagsmith/flagsmith-sql-flag-engine/issues/14)) ([f909d67](https://github.com/Flagsmith/flagsmith-sql-flag-engine/commit/f909d67e1e20be825dad3921bcaad3c9c100f69f))

## 0.1.0 (2026-05-20)


### Features

* SQL translator with ClickHouse dialect ([#1](https://github.com/Flagsmith/flagsmith-sql-flag-engine/issues/1)) ([3a6a175](https://github.com/Flagsmith/flagsmith-sql-flag-engine/commit/3a6a175c99ba284afa2da3a9804c8b130d23e67c))


### CI

* add PyPI publishing workflow and release-please config ([#10](https://github.com/Flagsmith/flagsmith-sql-flag-engine/issues/10)) ([695717a](https://github.com/Flagsmith/flagsmith-sql-flag-engine/commit/695717abca640cd32d0240af6c8ea02c511b2b0c))


### Other

* Clear `.release-please-manifest.json` content ([#12](https://github.com/Flagsmith/flagsmith-sql-flag-engine/issues/12)) ([c14267a](https://github.com/Flagsmith/flagsmith-sql-flag-engine/commit/c14267a8bf7243f1d41062bf6a3dc4659551642c))
