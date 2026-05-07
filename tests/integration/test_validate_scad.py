"""
Integration tests for ``validate_scad`` (B1 fix, 2026-05-07).

Bug B1: ``validate_scad`` returned ``valid=False`` with empty
``errors``/``warnings`` for ALL inputs. Root cause confirmed by direct
repro: OpenSCAD 2021.01 rejects ``-o /dev/null`` with "Either add a
valid suffix or specify one using the --export-format option" before
the parser runs, so stderr is empty and ``is_valid = (returncode == 0
and ...)`` is permanently False.

Fix: route ``-o`` at a real ``.png`` discard target under
``config.cache.directory`` (so ``MCP_CACHE_SIZE_MB`` covers it) and
move cleanup into ``try/finally`` so it runs on
``subprocess.TimeoutExpired`` too.

Tests run against the real ``openscad`` binary (skip-if-missing per
the same idiom as ``tests/e2e/test_stdio_handshake.py:32-35``).
Closes acceptance gates §7 items 1–4 from
``docs/openscad_mcp_bug_report.md``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from openscad_mcp.server import validate_scad
from openscad_mcp.utils.config import get_config


pytestmark = [
    pytest.mark.skipif(
        shutil.which("openscad") is None,
        reason="openscad binary not installed; B1 integration tests cannot render",
    ),
    pytest.mark.integration,
    pytest.mark.slow,
]


def _unwrap(tool):
    """Return the underlying async function from a FastMCP FunctionTool."""
    return tool.fn if hasattr(tool, "fn") else tool


@pytest.fixture(autouse=True)
def _ensure_xvfb_display(monkeypatch):
    """OpenSCAD on Linux links Qt's QApplication unconditionally; even
    `-o file.png` needs a live X display. Production daemon's run-sse.sh
    already runs Xvfb on :99 — set DISPLAY here so this fixture is
    reproducible without manual Xvfb plumbing."""
    if "DISPLAY" not in os.environ:
        monkeypatch.setenv("DISPLAY", ":99")


# =============================================================================
# Bug-report §7 items 1–3 — verdict correctness
# =============================================================================


class TestValidateScadVerdict:
    """The bug was a stuck `valid:false`. These tests confirm valid
    SCAD is now accepted, garbage is now flagged, and the in-between
    case (unknown module) surfaces a warning."""

    async def test_valid_returns_true(self):
        """Acceptance §7 item 1: ``cube([10,10,10]);`` → ``valid=True``,
        ``errors=[]``, AND ``warnings=[]`` (the noise-promotion guard:
        `_parse_openscad_stderr` must not mis-categorize benign render
        chatter like 'Compiling design...' as a warning)."""
        result = await _unwrap(validate_scad)(
            scad_content="cube([10,10,10]);",
        )
        assert result["success"] is True, result
        assert result["valid"] is True, result
        assert result["errors"] == [], result
        # Pin against silent regression in `_parse_openscad_stderr`'s
        # marker rules — benign stderr lines must NOT promote to warnings.
        assert result["warnings"] == [], result

    async def test_unknown_module_surfaces_warning(self):
        """Acceptance §7 item 2: ``cubex(10);`` → either ``valid=False``
        with non-empty ``errors``, OR ``valid=True`` with non-empty
        ``warnings``. OpenSCAD 2021.01 picks the warning path."""
        result = await _unwrap(validate_scad)(
            scad_content="cubex([10,10,10]);",
        )
        assert result["success"] is True, result
        # Bug report explicitly accepts either shape.
        assert (
            (result["valid"] is False and result["errors"])
            or (result["warnings"])
        ), (
            f"Expected either errors-non-empty or warnings-non-empty; "
            f"got valid={result['valid']}, errors={result['errors']}, "
            f"warnings={result['warnings']}"
        )

    async def test_garbage_surfaces_parse_error(self):
        """Acceptance §7 item 3: garbage → ``valid=False``, non-empty
        ``errors``."""
        result = await _unwrap(validate_scad)(
            scad_content="this is not valid $$$ {{{",
        )
        assert result["success"] is True, result
        assert result["valid"] is False, result
        assert result["errors"], result


# =============================================================================
# Cleanup tests — discard PNG must not leak (§7 item 4)
# =============================================================================


class TestValidateScadCleanup:
    """The discard PNG goes under ``config.cache.directory``. Cleanup
    must run on success, error, AND ``subprocess.TimeoutExpired`` paths
    (Security review F5, Test review §3 — closing the timeout-leak
    gap from the prior plan revision)."""

    def _list_discards(self) -> list[Path]:
        cache_dir = Path(get_config().cache.directory)
        if not cache_dir.exists():
            return []
        return list(cache_dir.glob("validate_discard_*.png"))

    async def test_happy_path_no_leak(self):
        """After a successful validate, no ``validate_discard_*.png``
        files survive."""
        before = set(self._list_discards())
        await _unwrap(validate_scad)(scad_content="cube([3,3,3]);")
        after = set(self._list_discards())
        leaked = after - before
        assert not leaked, f"validate_discard PNG(s) leaked: {leaked}"

    async def test_timeout_path_no_leak(self, monkeypatch):
        """Force ``subprocess.run`` to raise ``TimeoutExpired``. The
        ``try/finally`` block must still unlink the discard PNG.

        Pre-fix shape (cleanup-after-return on happy path only) leaked
        the PNG every time validation timed out — this test pins it.
        """
        before = set(self._list_discards())

        def _raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

        monkeypatch.setattr(subprocess, "run", _raise_timeout)

        # The validate_scad outer try/except converts the inner
        # RuntimeError("...timed out...") to a {"success": False,
        # "error": ...} envelope rather than re-raising. The PNG must
        # still get cleaned up either way.
        result = await _unwrap(validate_scad)(scad_content="cube([3,3,3]);")
        assert result["success"] is False, result
        assert "timed out" in result.get("error", "").lower(), result

        after = set(self._list_discards())
        leaked = after - before
        assert not leaked, (
            f"validate_discard PNG(s) leaked on TimeoutExpired path: {leaked}"
        )


# =============================================================================
# Stderr-classification regression — pin _parse_openscad_stderr behavior
# =============================================================================


class TestValidateScadStderrCategorization:
    """OpenSCAD emits non-error chatter to stderr during a real
    parse pass ("Compiling design...", "Geometries in cache...",
    "Total rendering time:"). These must NOT be promoted to
    `errors` or `warnings` by ``_parse_openscad_stderr``. The
    function's current markers (ECHO:/WARNING:/ERROR:/DEPRECATED:)
    skip them — pin that so a future refactor can't silently
    reintroduce the bug."""

    async def test_benign_stderr_routed_to_echo_only(self):
        """A SCAD body with `echo("foo")` produces both an ECHO line
        AND benign rendering chatter. Assert that:
          - `echo_output` contains the echo
          - `errors` AND `warnings` stay empty (no false positives
            from rendering progress lines)
        """
        result = await _unwrap(validate_scad)(
            scad_content='echo("hello-from-validate"); cube([3,3,3]);',
        )
        assert result["success"] is True, result
        assert result["valid"] is True, result
        # The echo statement must surface in echo_output.
        assert any(
            "hello-from-validate" in e for e in result["echo_output"]
        ), result["echo_output"]
        # No false positives from benign render chatter.
        assert result["errors"] == [], result["errors"]
        assert result["warnings"] == [], result["warnings"]
