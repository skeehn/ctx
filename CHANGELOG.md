# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
- Initial release of ctx-vault with core functionality:
  - .ctx file format specification
  - Parser for extracting headers, body, and chunks
  - Indexer service with file system watching
  - FastAPI service for search and retrieval
  - Benchmark showing 30.58× latency improvement over Markdown
- Production deployment guide
- Open source release preparation (README, LICENSE, CONTRIBUTING, CODE_OF_CONDUCT)

## [0.1.0] - 2026-08-17
### Added
- Initial commit with basic .ctx format and parser
- Basic indexer and API implementation
- First benchmark results showing performance improvement

### Changed
- N/A

### Deprecated
- N/A

### Removed
- N/A

### Fixed
- N/A

### Security
- N/A