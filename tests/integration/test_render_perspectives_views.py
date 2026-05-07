"""
Regression tests for ``render_perspectives``'s ``views`` parameter
handling (B3 from ``docs/openscad_mcp_bug_report.md``).

Bug B3 was reported as: passing ``views=["front","top","isometric"]``
through Claude Desktop's ``tools/call`` surface failed with::

    1 validation error for call[render_perspectives]
    views
      Input should be a valid list [type=list_type,
        input_value='["front","top","isometric"]', input_type=str]

The earlier diagnosis (recorded in commit ``cc9d0e8``) attributed B3
to mcp-remote. **That attribution was wrong.** Investigation 2026-05-07
read mcp-remote v0.1.38 end-to-end and confirmed:

- ``CallToolRequestParamsSchema`` declares ``arguments: record(string,
  unknown)`` — zod's ``unknown()`` neither validates nor transforms.
- ``mcpProxy`` forwarder passes ``request.params`` through verbatim
  (``return request``) without touching ``arguments``.
- Wire serialization is whole-message ``JSON.stringify(message)`` at
  three sites; nested arrays survive intact.

The shape originates **upstream** of mcp-remote — in a client-side
tool-call argument encoder (Claude Desktop on Windows is the observed
reproduction path; the encoder itself has not been directly inspected).

Fix (this commit): server-side defensive coercion via
``coerce_jsonish_list`` applied to ``render_perspectives.views`` via
Pydantic ``Annotated[..., BeforeValidator(...)]``. Native lists pass
through unchanged; JSON-stringified strings get re-parsed before the
list-type validator runs.

Tests below pin three paths:
- in-memory ``Client(mcp)`` with native list (always worked, codified).
- in-memory ``Client(mcp)`` with JSON-stringified string (post-fix only).
- live SSE with both shapes.

DoS hardening for the post-fix coercion path is in
``tests/unit/test_render_perspectives_views_cap.py``.
"""
from __future__ import annotations

import os
import shutil

import pytest


pytestmark = [
    pytest.mark.skipif(
        shutil.which("openscad") is None,
        reason="openscad binary not installed; B3 regression tests cannot render",
    ),
    pytest.mark.integration,
    pytest.mark.slow,
]


@pytest.fixture(autouse=True)
def _ensure_xvfb_display(monkeypatch):
    """OpenSCAD on Linux needs DISPLAY (Qt's QApplication is linked
    unconditionally). Production daemon runs Xvfb on :99; mirror that
    here so tests are reproducible without manual plumbing."""
    if "DISPLAY" not in os.environ:
        monkeypatch.setenv("DISPLAY", ":99")


# =============================================================================
# In-memory probe — proves the tool layer accepts native lists
# =============================================================================


class TestRenderPerspectivesInMemory:
    """``Client(mcp)`` is the same code path the existing test suite
    uses. If these fail, the bug is in openscad-mcp's tool layer and a
    fix in this repo would be required."""

    async def test_default_views_renders_all_seven(self):
        """Omitting ``views`` falls through to the default seven
        (``front``, ``back``, ``left``, ``right``, ``top``, ``bottom``,
        ``isometric``). Codifies the smoke-harness's working baseline."""
        from fastmcp import Client
        from openscad_mcp.server import mcp

        async with Client(mcp) as c:
            result = await c.call_tool(
                "render_perspectives",
                {
                    "scad_content": "$fn=16; cube([5,5,5]);",
                    "output_format": "base64",
                },
            )
            d = result.data if hasattr(result, "data") else result
            assert d.get("success") is True, d
            views = d.get("views", {})
            assert len(views) == 7, sorted(views)

    async def test_explicit_views_native_list_renders_subset(self):
        """The bug-B3 case via in-memory: passing ``views`` as a native
        Python list produces exactly that subset of perspectives. This
        proves the openscad-mcp tool layer + Pydantic validation accept
        a real list correctly."""
        from fastmcp import Client
        from openscad_mcp.server import mcp

        async with Client(mcp) as c:
            result = await c.call_tool(
                "render_perspectives",
                {
                    "scad_content": "$fn=16; cube([5,5,5]);",
                    "views": ["front", "top", "isometric"],
                    "output_format": "base64",
                },
            )
            d = result.data if hasattr(result, "data") else result
            assert d.get("success") is True, d
            views = d.get("views", {})
            assert sorted(views) == ["front", "isometric", "top"], sorted(views)

    async def test_explicit_views_jsonish_string_in_memory(self):
        """**Post-fix only.** Passing ``views`` as a JSON-stringified
        string — the exact shape Claude Desktop's encoder produces —
        must be coerced back to a list by ``coerce_jsonish_list`` and
        render normally. Pre-fix this errored with the Pydantic
        ``list_type`` complaint (input_value='[\"front\",\"top\",\"isometric\"]',
        input_type=str)."""
        from fastmcp import Client
        from openscad_mcp.server import mcp

        async with Client(mcp) as c:
            result = await c.call_tool(
                "render_perspectives",
                {
                    "scad_content": "$fn=16; cube([5,5,5]);",
                    # CRUCIAL: a string, not a list. The BeforeValidator
                    # at server.py runs coerce_jsonish_list on this and
                    # parses it back to ["front","top","isometric"].
                    "views": '["front","top","isometric"]',
                    "output_format": "base64",
                },
            )
            d = result.data if hasattr(result, "data") else result
            assert d.get("success") is True, d
            views = d.get("views", {})
            assert sorted(views) == ["front", "isometric", "top"], sorted(views)


# =============================================================================
# SSE probe — proves FastMCP's SSE deserialization accepts native lists
# =============================================================================


class TestRenderPerspectivesOverSSE:
    """``Client("http://127.0.0.1:9300/sse")`` exercises the live SSE
    daemon. If these fail when the daemon is up, the bug is in FastMCP's
    SSE handler. If these pass, the only remaining suspect is the
    ``mcp-remote`` shim (which is exactly what Stage-2 diagnosis on
    2026-05-07 found).

    Skipped automatically when the SSE daemon is not running on
    127.0.0.1:9300 — i.e. these tests don't fail CI just because the
    operator hasn't started the daemon. Locally / on the production
    VM, the operator runs ``scripts/run-sse.sh`` first.
    """

    @pytest.fixture
    def sse_daemon_or_skip(self):
        """Skip if no listener on :9300 — we don't auto-start the
        daemon from a test (it would persist across tests in surprising
        ways)."""
        import socket

        try:
            with socket.create_connection(("127.0.0.1", 9300), timeout=1):
                pass
        except (ConnectionRefusedError, OSError):
            pytest.skip("openscad-mcp SSE daemon not running on :9300")

    async def test_explicit_views_native_list_over_sse(self, sse_daemon_or_skip):
        """Native list arg over SSE → 3 views back. Proves FastMCP's
        SSE wire path accepts native lists end-to-end (always worked,
        codified as a regression guard)."""
        from fastmcp import Client

        async with Client("http://127.0.0.1:9300/sse") as c:
            result = await c.call_tool(
                "render_perspectives",
                {
                    "scad_content": "$fn=16; cube([5,5,5]);",
                    "views": ["front", "top", "isometric"],
                    "output_format": "base64",
                },
            )
            d = result.data if hasattr(result, "data") else result
            assert d.get("success") is True, d
            views = d.get("views", {})
            assert sorted(views) == ["front", "isometric", "top"], sorted(views)

    async def test_explicit_views_jsonish_string_over_sse(self, sse_daemon_or_skip):
        """**Post-fix only.** A JSON-stringified ``views`` over SSE —
        the exact shape Claude Desktop's encoder produces — must be
        coerced back to a list by the BeforeValidator and render
        normally. Pre-fix this errored at the Pydantic list-type
        check on the daemon side."""
        from fastmcp import Client

        async with Client("http://127.0.0.1:9300/sse") as c:
            result = await c.call_tool(
                "render_perspectives",
                {
                    "scad_content": "$fn=16; cube([5,5,5]);",
                    "views": '["front","top","isometric"]',
                    "output_format": "base64",
                },
            )
            d = result.data if hasattr(result, "data") else result
            assert d.get("success") is True, d
            views = d.get("views", {})
            assert sorted(views) == ["front", "isometric", "top"], sorted(views)
