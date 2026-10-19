"""Unit tests for the MH-256 bubblewrap runner (``openscad_mcp.sandbox``).

These cover the parts that need no OpenSCAD binary: how the confinement command is
built, that the child environment is an allow-list with the writable job dir, and —
the load-bearing safety property — that the runner FAILS CLOSED when the sandbox
cannot be established, never running OpenSCAD unconfined. The end-to-end proof that
the sandbox actually blocks a host-file read lives in the integration suite.
"""

from __future__ import annotations

import subprocess

import pytest

from openscad_mcp import sandbox

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_cache():
    def _clear():
        clear = getattr(sandbox.sandbox_available, "cache_clear", None)
        if clear:
            clear()

    _clear()
    yield
    _clear()


def test_build_sandbox_command_binds_only_the_job_dir_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: "/usr/bin/bwrap")
    work = tmp_path / "job"
    work.mkdir()
    env = {"PATH": "/usr/bin:/bin", "DISPLAY": ":99", "HOME": str(work / "home")}
    cmd = sandbox.build_sandbox_command([
        "/usr/bin/openscad", "-o", str(work / "out.png"), str(work / "input.scad"),
    ], work=work, env=env)

    # The job dir is the ONLY --bind (read-write); everything else is --ro-bind.
    rw_binds = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--bind"]
    assert str(work.resolve()) in rw_binds
    # /usr is read-only, /home is never bound at all.
    assert "--ro-bind" in cmd
    ro_targets = [cmd[i + 2] for i, a in enumerate(cmd) if a == "--ro-bind"]
    assert "/usr" in ro_targets
    assert not any(t == "/home" or t.startswith("/home/") for t in cmd)
    # Namespaces are unshared and the environment is cleared then rebuilt.
    assert "--unshare-pid" in cmd
    assert "--clearenv" in cmd
    assert "--proc" in cmd and "--dev" in cmd


def test_build_sandbox_command_sets_only_the_given_env(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: "/usr/bin/bwrap")
    work = tmp_path / "job"
    work.mkdir()
    env = {"PATH": "/usr/bin", "DISPLAY": ":99", "SECRET_TOKEN": "leaky"}
    cmd = sandbox.build_sandbox_command(
        ["/usr/bin/openscad", "-o", str(work / "o.png")], work=work, env=env
    )
    # Every --setenv pair comes from `env`; nothing else is injected.
    setenv_keys = {cmd[i + 1] for i, a in enumerate(cmd) if a == "--setenv"}
    assert setenv_keys == set(env)


def test_build_sandbox_command_raises_without_bwrap(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: None)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.build_sandbox_command(["openscad"], work=tmp_path, env={})


def test_env_for_redirects_caches_into_job_dir(tmp_path):
    env = sandbox.env_for(tmp_path)
    cache = str((tmp_path / "cache"))
    assert env["XDG_CACHE_HOME"] == cache
    assert env["MESA_SHADER_CACHE_DIR"] == cache
    # child_environment's allow-list is still in force.
    assert env["HOME"].startswith(str(tmp_path))
    assert "AWS_SECRET_ACCESS_KEY" not in env


# ---------------------------------------------------------------------------
# FAIL CLOSED — the load-bearing property.
# ---------------------------------------------------------------------------


def test_run_openscad_fails_closed_when_bwrap_missing(tmp_path, monkeypatch):
    """No bwrap on PATH → the render is refused, never run unconfined."""
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: None)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.run_openscad(
            ["/usr/bin/openscad", "-o", str(tmp_path / "o.png")],
            work=tmp_path,
            source="cube(1);",
            timeout=10,
        )


def test_run_openscad_fails_closed_when_selftest_fails(tmp_path, monkeypatch):
    """bwrap present but the namespace self-test returns non-zero (e.g. userns
    disabled) → still refused, and OpenSCAD is NEVER invoked."""
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: "/usr/bin/bwrap")
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        # The self-test command ends in /usr/bin/true; simulate it failing.
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.run_openscad(
            ["/usr/bin/openscad", "-o", str(tmp_path / "o.png")],
            work=tmp_path,
            source="cube(1);",
            timeout=10,
        )
    # Exactly one subprocess.run (the self-test); the render itself never ran.
    assert calls["n"] == 1


def test_run_openscad_filters_output_before_returning(tmp_path, monkeypatch):
    """stdout/stderr are passed through the output filter in the runner."""
    monkeypatch.setattr(sandbox, "sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: "/usr/bin/bwrap")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="WARNING: Illegal ID 'LEAK=xyz' in `/etc/shadow'",
        )

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    result = sandbox.run_openscad(
        ["/usr/bin/openscad", "-o", str(tmp_path / "o.png")],
        work=tmp_path,
        source="cube(1);",
        timeout=10,
    )
    assert "LEAK=xyz" not in result.stderr
    assert "/etc/shadow" not in result.stderr
