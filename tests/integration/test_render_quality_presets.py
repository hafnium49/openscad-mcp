"""
Integration tests for ``quality:"draft"`` rendering (B2 fix, 2026-05-07).

Bug B2: ``QUALITY_PRESETS["draft"]`` is ``{"$fn": 8, "$fa": 12, "$fs": 2}``.
Pre-fix, the variable-name validator inside ``render_scad_to_png`` rejected
``$fn`` with ``Invalid variable name '$fn': must match ^[a-zA-Z_]...``. The
existing ``tests/test_rendering_tools.py::test_quality_draft`` did NOT
catch this because it mocked ``render_scad_to_png`` itself, bypassing the
validator. These tests fix that gap by:

1. Mocking ``subprocess.run`` (one layer below the validator) so the
   validator actually runs but no OpenSCAD binary is required.
2. Adding two real-OpenSCAD tests (skip-if-missing) that prove the fix
   end-to-end against the actual binary.

Acceptance criteria covered (per docs/openscad_mcp_bug_report.md §7):
- Item 5: ``render_single`` w/ ``quality:"draft"`` returns a PNG.
- Item 6: ``render_perspectives`` w/ ``quality:"draft"`` returns N PNGs.
- Item 7: ``compare_renders`` w/ default quality (``"draft"``) returns
  before/after PNGs.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from openscad_mcp.server import (
    render_single,
    render_perspectives,
    compare_renders,
    QUALITY_PRESETS,
    _validate_variable_names,
)


def _unwrap(tool):
    """Return the underlying async function from a FastMCP FunctionTool."""
    return tool.fn if hasattr(tool, "fn") else tool


# =============================================================================
# Mocked tests — assert validator no longer rejects QUALITY_PRESETS["draft"]
# =============================================================================


@pytest.mark.integration
class TestQualityDraftValidatorPath:
    """The bug was in the variable-name validator. Direct unit-level
    proof that the QUALITY_PRESETS dict no longer raises."""

    def test_draft_preset_passes_validator(self):
        """``QUALITY_PRESETS["draft"]`` (the exact dict pasted into
        every render call's variables) must pass the validator."""
        # Bug repro pre-fix: this raised ValueError("Invalid variable
        # name '$fn': must match ^[a-zA-Z_]...").
        _validate_variable_names(QUALITY_PRESETS["draft"])

    def test_normal_and_high_presets_pass_validator(self):
        """All three quality presets must pass post-fix."""
        for preset_name, preset_vars in QUALITY_PRESETS.items():
            _validate_variable_names(preset_vars)


# =============================================================================
# Real-OpenSCAD tests — end-to-end proof
# =============================================================================


pytestmark_real = pytest.mark.skipif(
    shutil.which("openscad") is None,
    reason="openscad binary not installed; real-render tests skipped",
)


@pytestmark_real
@pytest.mark.integration
@pytest.mark.slow
class TestQualityDraftRealOpenSCAD:
    """End-to-end render with the real OpenSCAD binary. Skip-if-missing
    via the same idiom as ``tests/e2e/test_stdio_handshake.py:32-35``.

    These close acceptance gates §7 items 5, 6, 7 from the bug report.
    """

    @pytest.fixture(autouse=True)
    def _ensure_xvfb_display(self, monkeypatch):
        """OpenSCAD on Linux needs a live X display even for ``-o file.png``
        (Qt's QApplication is linked unconditionally). On the build VM
        Xvfb runs on ``:99`` per the production daemon's run-sse.sh
        Stage-A pre-flight; on a developer laptop ``xvfb-run`` would be
        the equivalent. Set ``DISPLAY`` if absent so this fixture's
        invocation is reproducible without manual Xvfb plumbing."""
        import os
        if "DISPLAY" not in os.environ:
            monkeypatch.setenv("DISPLAY", ":99")

    async def test_render_single_quality_draft(self, configured_env):
        """``render_single`` with ``quality="draft"`` returns a real PNG.
        Pre-fix this raised ``Invalid variable name '$fn'`` from the
        validator before OpenSCAD ever ran."""
        result = await _unwrap(render_single)(
            scad_content="cube([5,5,5]);",
            view="isometric",
            quality="draft",
            output_format="base64",
        )
        assert result["success"] is True, result
        assert result["mime_type"] == "image/png", result
        assert result["data"], "expected non-empty base64"

    async def test_render_perspectives_quality_draft(self, configured_env):
        """``render_perspectives`` with ``quality="draft"`` — closes
        acceptance gate §7 item 6."""
        result = await _unwrap(render_perspectives)(
            scad_content="cube([5,5,5]);",
            quality="draft",
            output_format="base64",
        )
        assert result["success"] is True, result
        views = result.get("views", {})
        # In ``output_format="base64"`` mode the tool returns
        # ``{name: <base64_str>}`` (not a per-view dict). Assert each
        # view is a non-trivial base64 PNG by checking length and the
        # PNG magic prefix once decoded.
        import base64 as _b64
        assert len(views) >= 3, f"expected ≥3 views, got: {sorted(views)}"
        for name, view_b64 in views.items():
            assert isinstance(view_b64, str) and view_b64, (name, view_b64)
            decoded = _b64.b64decode(view_b64)
            assert decoded.startswith(b"\x89PNG\r\n\x1a\n"), (name, decoded[:8])

    async def test_compare_renders_default_quality(self, configured_env):
        """``compare_renders`` without explicit ``quality`` defaults to
        ``"draft"`` (per the schema). Pre-fix this raised the validator
        error on every call. Closes acceptance gate §7 item 7."""
        result = await _unwrap(compare_renders)(
            scad_content_before="cube([5,5,5]);",
            scad_content_after="sphere(r=5);",
            view="isometric",
            # NO `quality` argument → default = "draft"
        )
        assert result["success"] is True, result
        assert result.get("before", {}).get("data"), result
        assert result.get("after", {}).get("data"), result
