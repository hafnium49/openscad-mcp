"""
Stage 2b — Cowork Patch P1 negative integration test.

Proves the env-var ``MCP_ALLOWED_PATHS`` actually fires through the
``Config.from_env() → SecurityConfig.allowed_paths → render_scad_to_png``
validation chain, rejecting an ``import()`` / ``include`` whose target
sits outside the sandbox.

This complements the positive tests in
``tests/unit/test_config_patch_p1.py`` which only verify the env-var
parses; a real sandbox needs to *reject* something.

Test approach:
  - Set ``MCP_ALLOWED_PATHS = tmp_path`` via ``monkeypatch.setenv`` so
    only ``tmp_path`` is allowed.
  - Mock ``find_openscad`` to return a fake path so the test doesn't
    need OpenSCAD installed (we are testing validation, not rendering).
  - Call ``render_scad_to_png(scad_file=<outside-tmp_path>)`` and assert
    ``ValueError`` matching ``"not within allowed paths"``.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from openscad_mcp.utils.config import Config, set_config


@pytest.mark.integration
def test_env_wired_allowed_paths_rejects_import_outside_sandbox(
    monkeypatch, tmp_path
):
    """End-to-end env → config → server validation:
    ``MCP_ALLOWED_PATHS`` must reject scad_file outside the sandbox."""
    # 1. Stage the env-var sandbox to point at tmp_path only.
    monkeypatch.setenv("MCP_ALLOWED_PATHS", str(tmp_path))

    # 2. Force the global config singleton to reload from env.
    cfg = Config.from_env()
    set_config(cfg)
    # Sanity: the env-wiring populated the list.
    assert cfg.security.allowed_paths == [str(tmp_path)], (
        f"Patch P1 wiring did not populate allowed_paths from env: "
        f"got {cfg.security.allowed_paths!r}"
    )

    # 3. Create a SCAD file OUTSIDE the sandbox.
    outside = Path("/tmp/patch_p1_outside_sandbox.scad")
    try:
        outside.write_text("cube([5,5,5]);\n")

        # 4. Patch find_openscad so we don't need the real binary —
        #    we are testing validation, not rendering.
        from openscad_mcp import server as server_mod
        with patch.object(server_mod, "find_openscad", return_value="/fake/openscad"):
            with pytest.raises(ValueError) as exc_info:
                server_mod.render_scad_to_png(scad_file=str(outside))

        # 5. Assert the rejection message references the allowed-paths
        #    sandbox machinery (proves the validation branch fired,
        #    not some upstream find_openscad bypass).
        msg = str(exc_info.value)
        assert "not within allowed paths" in msg, (
            f"Expected 'not within allowed paths' in error; got: {msg!r}"
        )
        assert str(tmp_path) in msg, (
            f"Error message should reference the configured sandbox; got: {msg!r}"
        )
    finally:
        # Cleanup: tmp_path is auto-cleaned by pytest, but the
        # outside-sandbox file we created in /tmp is not.
        if outside.exists():
            outside.unlink()
        # Reset config so other tests get a clean slate.
        set_config(None)
