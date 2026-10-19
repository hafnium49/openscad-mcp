"""MH-256 end-to-end: the bubblewrap confinement actually stops host-file reads, and
every legitimate render/export still works inside it.

Runs against the real ``openscad`` binary AND real ``bwrap`` (skip-if-missing). The
central test deliberately BYPASSES the lexical guard (layer 2) so a file-reading
program reaches OpenSCAD, and proves the sandbox (layer 1) makes a ``$HOME`` canary
physically unreadable — its content never appears in stdout/stderr or any output
file. The others prove a cube PNG, a Japanese ``text()``, an MCAD include and
STL/3MF/DXF/SVG exports still succeed under the sandbox.
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import uuid
from pathlib import Path

import pytest

from openscad_mcp import server
from openscad_mcp.scad_guard import available_mcad_files
from openscad_mcp.server import export_model, render_single
from openscad_mcp.utils.config import (
    CacheConfig,
    Config,
    SecurityConfig,
    set_config,
)
from openscad_mcp.sandbox import run_openscad, sandbox_available

pytestmark = [
    pytest.mark.skipif(
        shutil.which("openscad") is None,
        reason="openscad binary not installed",
    ),
    pytest.mark.skipif(
        not sandbox_available(),
        reason="bubblewrap sandbox cannot be established on this host",
    ),
    pytest.mark.integration,
    pytest.mark.slow,
]


def _unwrap(tool):
    return tool.fn if hasattr(tool, "fn") else tool


@pytest.fixture(autouse=True)
def _display(monkeypatch):
    if "DISPLAY" not in os.environ:
        monkeypatch.setenv("DISPLAY", ":99")


@pytest.fixture(autouse=True)
def _config(tmp_path):
    set_config(
        Config(
            temp_dir=tmp_path,
            cache=CacheConfig(enabled=False, directory=tmp_path / "cache"),
            security=SecurityConfig(allowed_paths=None),
        )
    )
    yield
    set_config(None)


@pytest.fixture
def home_canary():
    """A dummy file under the REAL $HOME with a unique token on its first line.
    It is NOT a secret — it is the thing the sandbox must make unreadable. The
    first line is the token because a DXF reader echoes it as ``Illegal ID '<line>'``."""
    token = f"MH256HOMECANARY{uuid.uuid4().hex}"
    stem = Path.home() / f".mh256_test_canary_{uuid.uuid4().hex}"
    made: list[Path] = []
    for suffix in (".svg", ".dxf", ".dat", ".stl"):
        p = stem.with_suffix(suffix)
        p.write_text(f"{token}\nsecond-line-{token}\n")
        made.append(p)
    try:
        yield token, stem
    finally:
        for p in made:
            try:
                p.unlink()
            except OSError:
                pass


@pytest.fixture
def no_guard(monkeypatch):
    """Deliberately disable the lexical guard (layer 2) so a file-reading program
    reaches OpenSCAD and the SANDBOX is the only thing that can stop the read."""
    monkeypatch.setattr(server, "assert_no_external_reference", lambda _src: None)


def test_sandbox_makes_home_canary_unreadable_at_the_runner(home_canary, tmp_path):
    """The sandbox-isolating proof: drive ``run_openscad`` directly (no guard, no
    server) with every file-reading form and assert the canary's content appears in
    NEITHER stdout/stderr NOR any file the run produced.

    This checks the confinement itself: the SVG importer echoes raw file lines that
    carry no path and no quotes, so the layer-3 output filter does NOT scrub them —
    only the sandbox (the file is absent) stops the read. Remove the sandbox and this
    goes red.
    """
    import shutil as _shutil

    token, stem = home_canary
    openscad = _shutil.which("openscad")
    programs = [
        f'import("{stem}.svg");',
        f'surface(file="{stem}.dat");',
        f'import_dxf("{stem}.dxf");',
        f'linear_extrude(file="{stem}.dxf") square(1);',
    ]
    for i, program in enumerate(programs):
        work = tmp_path / f"run{i}"
        work.mkdir()
        (work / "input.scad").write_text(program)
        result = run_openscad(
            [openscad, "--hardwarnings", "-o", str(work / "out.png"),
             str(work / "input.scad")],
            work=work,
            source=program,
            timeout=60,
        )
        assert token not in result.stdout, (program, result.stdout)
        assert token not in result.stderr, (program, result.stderr)
        # No file the run produced may contain the canary content either.
        for f in work.rglob("*"):
            if f.is_file():
                try:
                    body = f.read_text(errors="replace")
                except OSError:
                    continue
                assert token not in body, (program, str(f))


async def test_bypass_cannot_read_home_canary(home_canary, no_guard):
    """With the guard bypassed, no file-reading form can read a $HOME canary inside
    the sandbox — the token never leaks, whatever OpenSCAD's exit status.

    (OpenSCAD treats a missing ``import()`` as a warning and renders an empty scene,
    so the invariant is 'the content never reaches the caller', not 'the render
    fails'.)"""
    token, stem = home_canary
    programs = [
        f'import("{stem}.svg");',
        f'import_dxf("{stem}.dxf");',
        f'linear_extrude(file="{stem}.dxf") square(1);',
        f'surface(file="{stem}.dat");',
        f'import_stl("{stem}.stl");',
    ]
    for program in programs:
        result = await _unwrap(render_single)(
            scad_content=program, output_format="base64"
        )
        # The canary's content and its host path must appear NOWHERE in the result.
        assert token not in repr(result), f"LEAK via {program}: {result!r}"
        assert str(stem) not in repr(result), f"PATH LEAK via {program}: {result!r}"
        assert str(Path.home()) not in repr(result), (program, result)


async def test_bypass_cannot_read_home_canary_via_export(home_canary, no_guard):
    token, stem = home_canary
    result = await _unwrap(export_model)(
        scad_content=f'surface(file="{stem}.dat");', output_format="stl"
    )
    assert token not in repr(result)
    assert str(stem) not in repr(result)


async def test_output_filter_strips_foreign_path_from_error(home_canary, no_guard):
    """A read attempt that DOES error (``surface`` under --hardwarnings) names the
    foreign path in stderr; the filter drops it, so neither the token nor the host
    path reaches the caller."""
    token, stem = home_canary
    result = await _unwrap(render_single)(
        scad_content=f'surface(file="{stem}.dat");', output_format="base64"
    )
    blob = repr(result)
    assert token not in blob
    assert str(stem) not in blob
    assert str(Path.home()) not in blob


def _decode_png(result) -> bytes:
    assert result["success"] is True, result
    data = result.get("data")
    assert data, result
    raw = base64.b64decode(data)
    # Pillow must be able to open it — proves it is a real, non-empty PNG.
    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    img.verify()
    assert img.format == "PNG"
    return raw


async def test_cube_png_renders_under_sandbox():
    result = await _unwrap(render_single)(
        scad_content="cube([10,10,10]);",
        view="isometric",
        quality="draft",
        output_format="base64",
    )
    raw = _decode_png(result)
    assert len(raw) > 100


async def test_japanese_text_renders_under_sandbox():
    result = await _unwrap(render_single)(
        scad_content=(
            'linear_extrude(2) text("日本語", size=8, font="Noto Sans CJK JP");'
        ),
        output_format="base64",
    )
    _decode_png(result)


async def test_mcad_include_renders_under_sandbox():
    files = available_mcad_files()
    if not files:
        pytest.skip("MCAD is not installed on this host")
    top = [f for f in files if "/" not in f]
    name = top[0] if top else files[0]
    result = await _unwrap(render_single)(
        scad_content=f"use <MCAD/{name}>\ncube([5,5,5]);",
        output_format="base64",
    )
    _decode_png(result)


@pytest.mark.parametrize(
    "fmt,program",
    [
        ("stl", "cube([8,8,8]);"),
        ("3mf", "cube([8,8,8]);"),
        ("off", "sphere(5);"),
        ("dxf", "square([10,10]);"),      # 2D → DXF
        ("svg", "circle(6);"),            # 2D → SVG
    ],
)
async def test_exports_work_under_sandbox(tmp_path, fmt, program):
    out = tmp_path / f"out.{fmt}"
    result = await _unwrap(export_model)(
        scad_content=program,
        output_format=fmt,
        output_path=str(out),
    )
    assert result["success"] is True, (fmt, result)
    assert out.exists() and out.stat().st_size > 0, (fmt, result)
    assert result["file_size_bytes"] > 0
