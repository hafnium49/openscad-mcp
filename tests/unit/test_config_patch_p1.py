"""
Tests for Cowork Patch P1: MCP_ALLOWED_PATHS env-wiring through Config.from_env().

The patch (commit f82287c0 on branch feat/cowork-stdio) added:

    if allowed_paths := os.getenv("MCP_ALLOWED_PATHS"):
        paths = [p for p in allowed_paths.split(os.pathsep) if p.strip()]
        if paths:
            security_config["allowed_paths"] = paths

so the Windows Cowork installer can sandbox import()/include path resolution
to the install dir + per-user data dir via env var without the laptop user
editing a YAML config.

These tests exclusively use ``monkeypatch.setenv`` so they don't pollute
other tests via the autouse ``reset_environment`` fixture (which doesn't
clear ``MCP_*`` vars). They reuse ``reset_config`` from
``tests/unit/conftest.py`` so the singleton is fresh per test.
"""
import os

import pytest

from openscad_mcp.utils.config import Config


@pytest.mark.unit
@pytest.mark.config
class TestPatchP1AllowedPathsFromEnv:
    """Stage 2 (positive) — MCP_ALLOWED_PATHS env var parses correctly."""

    def test_allowed_paths_unset_keeps_default_none(
        self, monkeypatch, reset_config
    ):
        """Without the env var, security.allowed_paths stays None
        (backward-compatible default)."""
        monkeypatch.delenv("MCP_ALLOWED_PATHS", raising=False)
        cfg = Config.from_env()
        assert cfg.security.allowed_paths is None

    def test_allowed_paths_single_value_parses(self, monkeypatch, reset_config):
        """Single path → 1-element list."""
        monkeypatch.setenv("MCP_ALLOWED_PATHS", "/tmp")
        cfg = Config.from_env()
        assert cfg.security.allowed_paths == ["/tmp"]

    def test_allowed_paths_multi_value_uses_pathsep(
        self, monkeypatch, reset_config
    ):
        """``os.pathsep``-separated value → split into list (cross-platform:
        ``:`` on POSIX, ``;`` on Windows)."""
        value = f"/tmp{os.pathsep}/var/tmp"
        monkeypatch.setenv("MCP_ALLOWED_PATHS", value)
        cfg = Config.from_env()
        assert cfg.security.allowed_paths == ["/tmp", "/var/tmp"]

    def test_allowed_paths_empty_value_keeps_none(
        self, monkeypatch, reset_config
    ):
        """The patch's outer ``if allowed_paths:`` short-circuits on empty
        string; security.allowed_paths stays None."""
        monkeypatch.setenv("MCP_ALLOWED_PATHS", "")
        cfg = Config.from_env()
        assert cfg.security.allowed_paths is None

    def test_allowed_paths_pathsep_only_keeps_none(
        self, monkeypatch, reset_config
    ):
        """``MCP_ALLOWED_PATHS=":"`` (POSIX) or ``";"`` (Windows) splits to
        all-empty entries; the inner ``if paths:`` keeps allowed_paths None."""
        monkeypatch.setenv("MCP_ALLOWED_PATHS", os.pathsep)
        cfg = Config.from_env()
        assert cfg.security.allowed_paths is None
