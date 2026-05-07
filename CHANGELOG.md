# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — 2026-05-07 — Smoke-test findings (B1 + B2 + B3)

Three bugs surfaced by live SSE smoke testing against an
``openscad-mcp`` SSE daemon at ``:9300``. Full bug brief in the
consumer repo at ``docs/openscad_mcp_bug_report.md``.

### Added
- ``scripts/smoke-test-sse.py`` — 15-tool end-to-end smoke harness
  against a running SSE daemon. Manual pre-deploy operational gate;
  not pytest-discovered (no ``test_`` prefix).
- ``tests/integration/`` package: ``test_validate_scad.py`` (B1),
  ``test_render_quality_presets.py`` (B2), and
  ``test_render_perspectives_views.py`` (B3 baseline).
- ``tests/unit/test_variable_validation.py`` — 34 tests for the new
  ``_validate_variable_names`` helper (positive cases, regression
  pins, OpenSCAD reserved-word rejection, value-bound DoS guards).

### Fixed
- **B1 — ``validate_scad`` always returned ``valid:false``.** The tool
  used ``-o /dev/null``, which OpenSCAD 2021.01 rejects with "Either
  add a valid suffix or specify one using the ``--export-format``
  option" before the parser runs. stderr was always empty and
  ``is_valid = (returncode == 0 and ...)`` was permanently False for
  every input. Fix: route output to a ``.png`` discard file under
  ``config.cache.directory`` (so ``MCP_CACHE_SIZE_MB`` covers it) and
  move cleanup into ``try/finally`` so it runs on
  ``subprocess.TimeoutExpired`` and any future exception path
  (mirrors the ``analyze_model`` idiom further down in
  ``server.py``).
- **B2 — ``quality:"draft"`` rejected by variable-name validator.**
  The preset is ``{"$fn": 8, "$fa": 12, "$fs": 2}``; the validator's
  Python-identifier regex ``^[a-zA-Z_][a-zA-Z0-9_]*$`` rejected
  ``$fn`` with ``Invalid variable name '$fn': must match ...``,
  breaking ``compare_renders``'s default-quality path. Fix:
  consolidate the four duplicated inline regex sites into a single
  ``_validate_variable_names`` helper near the top of ``server.py``;
  widen the regex to ``^\$?[a-zA-Z_][a-zA-Z0-9_]*$`` to accept
  OpenSCAD's special variables (``$fn``, ``$fa``, ``$fs``, ``$t``,
  ``$preview``, ``$vpr``, etc.). Also explicitly reject OpenSCAD
  reserved words (``module``, ``function``, ``if``, …) since
  ``-D`` for those silently shadows keywords.

### Changed
- Variable validation is now centralised in
  ``_validate_variable_names`` at ``server.py:70-115``. Replaces
  4 duplicated inline regex blocks at lines 298, 1368, 2003, 2202.
  Refactor preserves the ``ValueError`` shape callers expect.

### Security
- **DoS hardening (Security review F2)**: widening the variable-name
  regex would have opened a window for ``{"$fn": 999999}`` to spin
  OpenSCAD's CGAL kernel for the full ``MCP_RENDER_TIMEOUT`` (default
  120 s) on a 5-slot semaphore, since ``MCP_RATE_LIMIT`` is declared
  upstream but not yet enforced in the request path. The new helper
  bounds known special variables: ``$fn ≤ 256``, ``$fa ≥ 0.01``,
  ``$fs ≥ 0.001``, ``$t ∈ [0, 1]``. Type-checked too (``bool`` /
  ``str`` raise a numeric-type error rather than sneaking through).
- ``validate_discard_*.png`` files now live under
  ``config.cache.directory`` (covered by ``_evict_cache_if_needed``)
  rather than ``config.temp_dir`` (uncapped). Closes Security review
  F4. Hex suffix widened to 16 chars for collision margin under
  high concurrency (Security review F7).
- The widened regex deliberately still rejects multi-dollar prefixes
  (``$$fn``), mid-name dollars (``f$n``), and Unicode names — pinned
  by regression tests so a future "make it more permissive" refactor
  can't open these vectors silently.

### Diagnostic note (B3 — not fixed in this repo)
- B3 (``views=[...]`` over SSE arrives as a JSON-stringified string)
  was diagnosed via two probes on 2026-05-07:
  - In-memory ``Client(mcp).call_tool("render_perspectives",
    {"views": [...]})`` → **PASS** (3 views rendered).
  - SSE ``Client("http://127.0.0.1:9300/sse").call_tool(...)`` →
    **PASS** (3 views rendered).
- Both ``fastmcp.Client`` paths handle native lists correctly. The
  bug-report's reproduction came through Claude Desktop's
  ``mcp-remote`` shim on Windows, which is the surviving suspect.
  No openscad-mcp source change required; instead
  ``tests/integration/test_render_perspectives_views.py`` codifies
  the working baseline so any future regression in the openscad-mcp
  or FastMCP wire path is caught immediately. Filing the upstream
  ``mcp-remote`` issue is operator follow-up.

## [0.1.0] - 2024-01-26

### Added
- Initial release of OpenSCAD MCP Server
- Full Model Context Protocol (MCP) implementation
- `render_single` tool for single view rendering
- `render_perspectives` tool for multiple standard views
- `check_openscad` tool for installation verification
- Support for OpenSCAD code strings and .scad files
- Customizable camera positions and targets
- Variable passing to OpenSCAD scripts
- Multiple color scheme support
- Smart response size management with automatic optimization
- Base64, file path, and compressed output formats
- Comprehensive test suite with 100+ tests
- Docker support for containerized deployment
- GitHub Actions CI/CD pipeline
- Support for Python 3.8 through 3.12
- Cross-platform compatibility (Linux, macOS, Windows)
- Environment-based configuration system
- Async/await support for non-blocking operations
- Resource caching capabilities
- Comprehensive error handling and validation
- Security features including path restrictions and dangerous function blocking
- Full documentation with examples
- MIT License

### Technical Details
- Built with FastMCP framework v2.11.3+
- Uses Pydantic for data validation
- Pillow for image processing
- PyYAML for configuration
- python-dotenv for environment management
- Async subprocess execution for OpenSCAD rendering
- Smart response compression for large images
- Configurable worker pool for parallel rendering

### Known Issues
- Animation rendering not yet supported
- STL export functionality pending implementation
- WebAssembly fallback not available in this version

## [0.0.1-alpha] - 2024-01-20

### Added
- Initial proof of concept
- Basic rendering functionality
- MCP protocol skeleton

---

## Version History

- **0.1.0** - First stable release with full MCP compliance
- **0.0.1-alpha** - Initial proof of concept

## Upgrade Guide

### From 0.0.x to 0.1.0
1. Update dependencies: `uv pip install --upgrade openscad-mcp`
2. Review new configuration options in `.env.example`
3. Update any custom integrations to use new response formats
4. Test rendering with new optimization features

## Compatibility Matrix

| OpenSCAD MCP | Python | OpenSCAD | FastMCP |
|--------------|--------|----------|---------|
| 0.1.0        | 3.8-3.12 | 2019.05+ | 2.11.3+ |
| 0.0.1-alpha  | 3.8+   | 2019.05+ | 2.0.0+  |

## Support

For questions and support, please use:
- GitHub Issues: https://github.com/yourusername/openscad-mcp-server/issues
- Discussions: https://github.com/yourusername/openscad-mcp-server/discussions

[Unreleased]: https://github.com/yourusername/openscad-mcp-server/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/yourusername/openscad-mcp-server/releases/tag/v0.1.0
[0.0.1-alpha]: https://github.com/yourusername/openscad-mcp-server/releases/tag/v0.0.1-alpha