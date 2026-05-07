"""
Regression tests for ``render_perspectives``'s ``views`` parameter
handling (B3 from ``docs/openscad_mcp_bug_report.md``, 2026-05-07).

Bug B3 was reported as: passing ``views=["front","top","isometric"]`` over
SSE+``mcp-remote`` (Claude Desktop on Windows) failed with::

    1 validation error for call[render_perspectives]
    views
      Input should be a valid list [type=list_type,
        input_value='["front","top","isometric"]', input_type=str]

Stage-2 diagnosis (2026-05-07) recorded in CHANGELOG:
- **In-memory** ``Client(mcp).call_tool("render_perspectives", {"views":[...]})``
  → PASS (3 views rendered).
- **SSE** ``Client("http://127.0.0.1:9300/sse").call_tool(...)``
  → PASS (3 views rendered).

So the bug is **specifically in the** ``mcp-remote`` **stdio→SSE shim**
that Claude Desktop on Windows uses, NOT in openscad-mcp's tool layer
and NOT in FastMCP's SSE deserialization. No code change in this repo;
these tests codify the working baseline so any future regression in the
openscad-mcp/FastMCP path is caught immediately.

Acceptance criteria covered (per bug report §7):
- Item 9: diagnosis recorded in CHANGELOG (this docstring serves as the
  in-test record; full prose in CHANGELOG.md).
- Item 10: not applicable — fix landed elsewhere (mcp-remote).
- Item 11: an ``xfail`` test would only make sense in a Claude-Desktop
  test rig; we don't have one. Documenting via CHANGELOG attribution
  instead.
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

    async def test_explicit_views_over_sse(self, sse_daemon_or_skip):
        """Native list arg over SSE → 3 views back. Proves FastMCP's
        SSE wire path is NOT the bug-B3 culprit; ``mcp-remote`` is the
        surviving suspect for Claude-Desktop reproductions."""
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
