"""Unit tests for the MH-256 guard widening and the output filter.

MH-254 refused ``include``/``use``/``import``/``surface`` but left open, on OpenSCAD
2021.01: ``linear_extrude(file=...)``/``rotate_extrude(file=...)``, a positional
``linear_extrude("x.dxf")`` or a variable holding a path, ``import/**/("x")`` with a
comment before the paren, ``import_stl``/``import_off``/``import_dxf``, the
``dxf_linear_extrude``/``dxf_rotate_extrude``/``dxf_dim``/``dxf_cross`` family, and
``surface`` variants. These pin the widened lexical refusal (layer 2) and the output
filter (layer 3). No OpenSCAD binary is required.
"""

from __future__ import annotations

import pytest

from openscad_mcp.scad_guard import (
    ScadRefused,
    assert_no_external_reference,
    redact_external_output,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Layer 2: every MH-256 bypass is refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        # linear/rotate_extrude DXF-file form, named and positional.
        'linear_extrude(file="x.dxf") square(1);',
        'rotate_extrude(file="x.dxf");',
        'linear_extrude("x.dxf");',
        'rotate_extrude("x.dxf");',
        'f="x.dxf"; linear_extrude(f);',          # bare variable positional
        'g = "x.dxf"; rotate_extrude( g );',      # with whitespace
        # import and its aliases, comment-gapped and plain.
        'import("x.stl");',
        'import/**/("x.stl");',                   # comment between name and (
        'import //c\n ("x.svg");',                # line comment between name and (
        'import_stl("x.stl");',
        'import_off("x.off");',
        'import_dxf("x.dxf");',
        # the deprecated dxf_* readers.
        'dxf_linear_extrude(file="x.dxf");',
        'dxf_rotate_extrude(file="x.dxf");',
        'echo(dxf_dim(file="x.dxf", name="a")); cube(1);',
        'echo(dxf_cross(file="x.dxf")); cube(1);',
        # surface variants.
        'surface(file="x.dat");',
        'surface/**/(file="x.dat");',
        'surface("x.png");',
        # a `file=` argument anywhere, comment-gapped.
        'my_module(file = "x.dat");',
        'my_module(file/**/= "x.dat");',
        # include/use with <...> and the font FILE route.
        "include </etc/hostname>",
        "use <secret.ttf>",
    ],
)
def test_mh256_bypass_is_refused(source):
    with pytest.raises(ScadRefused) as exc:
        assert_no_external_reference(source)
    assert exc.value.code in (
        "external_reference_refused",
        "library_subset_unavailable",
        "library_unavailable",
    )


def test_refusal_message_never_echoes_the_model_path():
    """The refusal names the construct, never the model-chosen path."""
    with pytest.raises(ScadRefused) as exc:
        assert_no_external_reference('import("/etc/some_secret_path.stl");')
    assert "/etc/some_secret_path" not in str(exc.value)


# ---------------------------------------------------------------------------
# Layer 2: legitimate programs are NOT refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "cube([10,10,10]);",
        "linear_extrude(2) square(1);",                  # numeric first positional
        "linear_extrude(height=10) circle(3);",          # named argument
        "linear_extrude(height = h) circle(3);",         # named, whitespace
        "h=5; linear_extrude(h*2) square(1);",           # arithmetic expression
        "linear_extrude(10, twist=90) square(1);",       # numeric + named
        "rotate_extrude() translate([10,0]) circle(2);", # empty args
        "rotate_extrude(angle=270) translate([5,0]) circle(1);",
        'linear_extrude(2) text("日本語", size=6, font="Noto Sans CJK JP");',
        "included_parts = 3; translate([included_parts,0,0]) cube(1);",
        "my_surface_area = 5; cube(my_surface_area);",
        "file_count = 2; cube(file_count);",             # `file` as a name prefix
        "linear_extrude($fn=64, height=3) circle(2);",   # special-var named arg
    ],
)
def test_legitimate_program_is_allowed(source):
    assert_no_external_reference(source)  # must not raise


# ---------------------------------------------------------------------------
# Layer 3: the output filter strips anything derived from a foreign file
# ---------------------------------------------------------------------------


def test_filter_drops_line_naming_a_foreign_path():
    src = "cube(1);"
    leak = (
        "WARNING: Illegal ID 'CANARY=topsecret' in `/home/hafnium/.aws/credentials'"
    )
    out = redact_external_output(leak, job_dir="/tmp/job", source=src)
    assert "topsecret" not in out
    assert "/home/hafnium" not in out
    assert out == ""  # the whole line, which named a foreign path, is gone


def test_filter_redacts_crafted_illegal_id_without_a_path():
    """The crafted no-path leak: a quoted span absent from source is redacted."""
    src = "cube(1);"
    leak = "WARNING: Illegal ID 'SECRET=abc123def' at line 1"
    out = redact_external_output(leak, job_dir="/tmp/job", source=src)
    assert "SECRET=abc123def" not in out
    assert "[redacted]" in out
    assert "Illegal ID" in out  # the structural message survives


def test_filter_keeps_the_callers_own_echoed_strings():
    src = 'echo("hello-from-user"); cube(1);'
    line = 'ECHO: "hello-from-user"'
    out = redact_external_output(line, job_dir="/tmp/job", source=src)
    assert out == line  # present verbatim in source → kept


def test_filter_keeps_paths_inside_the_job_dir():
    src = "cube(1);"
    line = "ERROR: Parser error in file /tmp/job/input.scad, line 1"
    out = redact_external_output(line, job_dir="/tmp/job", source=src)
    assert out == line  # the job's own input path is not foreign


def test_filter_handles_backticked_foreign_path():
    src = "cube(1);"
    line = "Can't open DXF file `/home/hafnium/mkc-holmes-v5/v5/.env'."
    out = redact_external_output(line, job_dir="/tmp/job", source=src)
    assert out == ""
    assert ".env" not in out


def test_filter_is_a_noop_on_empty_text():
    assert redact_external_output("", job_dir="/tmp/job", source="cube(1);") == ""
