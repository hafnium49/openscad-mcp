"""Unit tests for the MH-254 inline-source guard (``openscad_mcp.scad_guard``).

MH-254: the daemon only ever path-checked the ``scad_file`` and ``include_paths``
parameters. The model always sends inline ``scad_content``, which was never
inspected, so an inline ``include``/``use``/``import``/``surface`` reference could
read any absolute host path the daemon user can read and return OpenSCAD's stderr
to the caller. These tests pin the refusal (no OpenSCAD binary required).
"""

from __future__ import annotations

import pytest

from openscad_mcp.scad_guard import (
    ScadRefused,
    assert_no_external_reference,
    available_mcad_files,
    child_environment,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Every file-referencing form is refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "include </etc/hostname>",  # absolute include
        "include <../../../../etc/passwd>",  # relative traversal
        "use </etc/hostname>",  # use (modules/functions)
        "use <secret.ttf>",  # font FILE (not a name)
        'import("/abs/model.stl");',  # import a mesh
        'surface(file="/abs/data.dat");',  # surface() height map
        "include\n   </etc/hostname>",  # whitespace/newline split (a real read)
        "  // note\ninclude </etc/hostname>",  # include on a later line
        "/* comment */ include </etc/hostname>",  # over-refused: conservative is safe
        "cube(1); // include </etc/hostname>",  # commented-out, still over-refused
    ],
)
def test_external_reference_is_refused(source):
    with pytest.raises(ScadRefused) as exc:
        assert_no_external_reference(source)
    assert exc.value.code == "external_reference_refused"


def test_comment_gap_between_include_and_bracket_is_not_a_read():
    """`include/**/<file>` is a PARSE ERROR in OpenSCAD 2021.01 (a comment is not
    whitespace to its include lexer rule), so it is not a file read and the guard
    does not need to match it — mirroring v6's `_FILE_REFERENCING` scope."""
    assert_no_external_reference("include/**/ <lib.scad>\ncube(1);")  # must not raise


def test_absolute_path_token_never_echoed_in_message():
    """The refusal names only the construct, never the model-chosen path."""
    with pytest.raises(ScadRefused) as exc:
        assert_no_external_reference("include </etc/some_secret_path>")
    assert "/etc/some_secret_path" not in str(exc.value)


# ---------------------------------------------------------------------------
# Ordinary self-contained programs are NOT refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "cube([10, 10, 10]);",
        "difference() { cube(20, center=true); sphere(12); }",
        "included_parts = 3; translate([included_parts, 0, 0]) cube(1);",  # var, not `include`
        "my_surface_area = 5; cube(my_surface_area);",  # not `surface(`
        'linear_extrude(2) text("日本語", size=6, font="Noto Sans CJK JP");',  # font NAME ok
    ],
)
def test_ordinary_program_is_allowed(source):
    assert_no_external_reference(source)  # must not raise


# ---------------------------------------------------------------------------
# The one allowed exception: a whole-line installed-MCAD include/use
# ---------------------------------------------------------------------------


def _some_mcad():
    files = available_mcad_files()
    if not files:
        pytest.skip("MCAD is not installed on this host")
    # Prefer a top-level file (no 'bitmap/' prefix) for the simple cases.
    top = [f for f in files if "/" not in f]
    return top[0] if top else files[0]


def test_whole_line_mcad_include_is_allowed():
    name = _some_mcad()
    assert_no_external_reference(f"include <MCAD/{name}>\ncube(1);")  # must not raise


def test_whole_line_mcad_use_is_allowed():
    name = _some_mcad()
    assert_no_external_reference(f"use <MCAD/{name}>\ncube(1);")  # must not raise


def test_unknown_mcad_file_is_refused_by_name():
    if not available_mcad_files():
        pytest.skip("MCAD is not installed on this host")
    with pytest.raises(ScadRefused) as exc:
        assert_no_external_reference("include <MCAD/definitely_not_real_zzz.scad>\ncube(1);")
    assert exc.value.code == "library_subset_unavailable"


def test_mcad_traversal_is_not_treated_as_a_library_line():
    """`MCAD/../etc/x` is not a whole-line MCAD include, so it hits the general refusal."""
    with pytest.raises(ScadRefused) as exc:
        assert_no_external_reference("include <MCAD/../../../etc/hostname>")
    assert exc.value.code == "external_reference_refused"


def test_mid_line_mcad_reference_is_refused():
    """A trailing statement on the include line means it is not whole-line MCAD."""
    name = available_mcad_files()[0] if available_mcad_files() else "boxes.scad"
    with pytest.raises(ScadRefused) as exc:
        assert_no_external_reference(f"include <MCAD/{name}> include </etc/hostname>")
    assert exc.value.code == "external_reference_refused"


# ---------------------------------------------------------------------------
# The renderer subprocess environment is an allow-list, not os.environ
# ---------------------------------------------------------------------------


def test_child_environment_excludes_secrets(tmp_path, monkeypatch):
    """A secret in the daemon's own environment must not reach the renderer."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "FAKE_SHOULD_NOT_LEAK")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "FAKE_TOKEN")
    monkeypatch.setenv("DIFFBOT_KEY", "FAKE_DIFFBOT")
    monkeypatch.setenv("OPENAI_API_KEY", "FAKE_OPENAI")
    monkeypatch.setenv("OPENSCADPATH", "/attacker/controlled")
    env = child_environment(tmp_path)
    for leaked in (
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "DIFFBOT_KEY",
        "OPENAI_API_KEY",
        "OPENSCADPATH",
    ):
        assert leaked not in env, f"{leaked} leaked into the renderer environment"


def test_child_environment_passes_through_path_and_sets_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/custom/bin")
    env = child_environment(tmp_path)
    assert env["PATH"] == "/custom/bin"
    assert env["HOME"].startswith(str(tmp_path))
    assert env["TMPDIR"] == str(tmp_path)
    assert env.get("LC_ALL") == "C.UTF-8"


def test_child_environment_defaults_path_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("PATH", raising=False)
    env = child_environment(tmp_path)
    assert "bin" in env["PATH"]
