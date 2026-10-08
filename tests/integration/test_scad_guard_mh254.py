"""End-to-end integration for the MH-254 inline-source guard.

Runs against the real ``openscad`` binary (skip-if-missing, same idiom as the other
integration tests). Proves, through the MCP tool surface:

* a canary file whose unique token an unpatched daemon returned in ``echo_output``
  is NOT reached when referenced inline (the refusal fires first);
* ordinary programs, an installed-MCAD whole-line include, and a Japanese font NAME
  still render (no v5 user loses a legitimate capability).
"""

from __future__ import annotations

import os
import shutil
import uuid

import pytest

from openscad_mcp.scad_guard import available_mcad_files
from openscad_mcp.server import export_model, render_single

pytestmark = [
    pytest.mark.skipif(
        shutil.which("openscad") is None,
        reason="openscad binary not installed; MH-254 e2e tests cannot render",
    ),
    pytest.mark.integration,
    pytest.mark.slow,
]


def _unwrap(tool):
    return tool.fn if hasattr(tool, "fn") else tool


@pytest.fixture(autouse=True)
def _ensure_xvfb_display(monkeypatch):
    if "DISPLAY" not in os.environ:
        monkeypatch.setenv("DISPLAY", ":99")


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    import contextlib

    from openscad_mcp.utils.config import get_config

    cfg = get_config()
    with contextlib.suppress(Exception):
        cfg.cache.enabled = False


async def test_inline_include_canary_is_not_reached(tmp_path):
    """The classic read-and-report channel: an unpatched daemon echoed the token."""
    token = f"MH254CANARY{uuid.uuid4().hex}"
    canary = tmp_path / "canary.scad"
    canary.write_text(f'echo("{token}");\n')

    result = await _unwrap(render_single)(
        scad_content=f"include <{canary}>\ncube(1);",
        output_format="base64",
    )
    assert result["success"] is False
    assert "external_reference_refused" in result["error"]
    assert token not in repr(result)


async def test_inline_surface_canary_is_not_reached(tmp_path):
    token = f"MH254SURF{uuid.uuid4().hex}"
    canary = tmp_path / "canary.dat"
    canary.write_text(f"1 2 3  # {token}\n")
    result = await _unwrap(render_single)(
        scad_content=f'surface(file="{canary}");',
        output_format="base64",
    )
    assert result["success"] is False
    assert token not in repr(result)


async def test_ordinary_program_still_renders():
    result = await _unwrap(render_single)(
        scad_content="difference() { cube(20, center=true); sphere(12); }",
        view="isometric",
        quality="draft",
        output_format="base64",
    )
    assert result["success"] is True
    assert result["data"]


async def test_mcad_whole_line_include_still_renders():
    if not available_mcad_files():
        pytest.skip("MCAD is not installed on this host")
    result = await _unwrap(render_single)(
        scad_content="use <MCAD/boxes.scad>\nroundedBox([20, 20, 10], 2, true);",
        view="isometric",
        quality="draft",
        output_format="base64",
    )
    assert result["success"] is True, result.get("error")


async def test_japanese_font_name_still_renders():
    result = await _unwrap(render_single)(
        scad_content='linear_extrude(2) text("日本語", size=6, font="Noto Sans CJK JP");',
        view="top",
        quality="draft",
        output_format="base64",
    )
    assert result["success"] is True, result.get("error")


async def test_export_refuses_inline_reference(tmp_path):
    token = f"MH254EXP{uuid.uuid4().hex}"
    canary = tmp_path / "canary.scad"
    canary.write_text(f'echo("{token}");\n')
    out = tmp_path / "out.stl"
    result = await _unwrap(export_model)(
        scad_content=f"include <{canary}>\ncube(1);",
        output_format="stl",
        output_path=str(out),
    )
    assert result["success"] is False
    assert token not in repr(result)
    assert not out.exists()
