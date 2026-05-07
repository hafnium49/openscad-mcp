"""
Unit tests for ``render_perspectives``'s per-call view-count cap
(B3 DoS hardening, 2026-05-07).

Bug B3's fix lets clients pass ``views=["front","top",...]`` as a
JSON-stringified string. Without a length cap, a 50 KB payload like
``'["front",' * 5000 + '"top"]'`` parses to 5,001 view names, all
whitelisted by ``server.py:1183``, and ``asyncio.gather`` queues
5,001 OpenSCAD subprocesses (Security review Q4 2026-05-07).

The cap at ``server.py:_MAX_VIEWS_PER_CALL = 32`` runs *after* the
BeforeValidator coercion + ``dict.fromkeys`` dedupe, *before* the
whitelist check. This module pins the cap's behavior + the dedupe.

Tests use a mocked OpenSCAD subprocess so they don't depend on the
real binary; the dedupe + cap logic runs entirely in Python.
"""
from __future__ import annotations

from unittest.mock import patch, Mock

import pytest

from openscad_mcp.server import render_perspectives, _MAX_VIEWS_PER_CALL


def _unwrap(tool):
    return tool.fn if hasattr(tool, "fn") else tool


# Distinct synthetic view names. Real OpenSCAD would reject these at
# the whitelist check (server.py:1183), but we never get that far
# because the cap fires first. We just need something that's >32 and
# ≤32 in the right tests.
_THIRTY_TWO_NAMES = [f"view_{i}" for i in range(32)]
_THIRTY_THREE_NAMES = [f"view_{i}" for i in range(33)]


@pytest.mark.unit
class TestViewsCountCap:
    """Cap fires when len(views) > _MAX_VIEWS_PER_CALL."""

    def test_cap_constant_is_thirty_two(self):
        """Pin the constant — bug-fix doc cites '32' explicitly."""
        assert _MAX_VIEWS_PER_CALL == 32

    async def test_views_count_at_cap_does_not_trigger_cap_error(self):
        """32 distinct names is at the cap — the cap itself must NOT
        fire. (The call still fails downstream at the whitelist
        because these are synthetic names; we just pin that the
        failure is the whitelist failure, not the cap failure.)

        ``render_perspectives`` wraps its body in try/except that
        converts ``ValueError`` into ``{"success": False, "error": ...}``,
        so we inspect the returned envelope rather than catching."""
        with patch(
            "openscad_mcp.server.subprocess.run",
            return_value=Mock(returncode=0, stderr="", stdout=""),
        ):
            result = await _unwrap(render_perspectives)(
                scad_content="cube([1,1,1]);",
                views=_THIRTY_TWO_NAMES,
            )
        assert isinstance(result, dict), result
        assert result.get("success") is False, result
        err = result.get("error", "")
        assert "Too many views" not in err, (
            f"Cap fired at len == 32 (should fire only at >32). Error: {err}"
        )
        # Whitelist rejection is the expected failure shape here.
        assert "Invalid view name" in err, err

    async def test_views_count_over_cap_raises_via_envelope(self):
        """33 distinct names is over the cap → ``render_perspectives``
        must return ``{"success": False, "error": "Too many views ..."}``.
        Closes Security Q4."""
        with patch(
            "openscad_mcp.server.subprocess.run",
            return_value=Mock(returncode=0, stderr="", stdout=""),
        ):
            result = await _unwrap(render_perspectives)(
                scad_content="cube([1,1,1]);",
                views=_THIRTY_THREE_NAMES,
            )
        assert isinstance(result, dict), result
        assert result.get("success") is False, result
        err = result.get("error", "")
        assert "Too many views" in err, (
            f"Expected 'Too many views' in envelope error; got: {err}"
        )
        assert "33" in err, f"Expected count '33' in error; got: {err}"
        assert "32" in err, f"Expected cap '32' in error; got: {err}"

    async def test_views_dedupes_before_cap_fires(self):
        """50 entries that are all ``"front"`` — after ``dict.fromkeys``
        dedup the list is 1 entry, well under the cap. The cap MUST
        NOT fire. Pins the dedupe semantics: a malicious 'spam the
        same view 50,000 times' payload doesn't bypass the cap by
        being post-coerce-but-pre-cap."""
        spam_front = ["front"] * 50

        with patch(
            "openscad_mcp.server.subprocess.run",
            return_value=Mock(returncode=0, stderr="", stdout=b""),
        ), patch(
            "openscad_mcp.server.Path.exists",
            return_value=True,
        ), patch(
            "openscad_mcp.server.Path.read_bytes",
            return_value=b"\x89PNG\r\n\x1a\n" + b"x" * 256,
        ):
            result = await _unwrap(render_perspectives)(
                scad_content="cube([1,1,1]);",
                views=spam_front,
            )
        # Render may succeed (mocked subprocess) OR fail downstream
        # for some other mocked-related reason; the only thing we pin
        # here is that the cap-error string did NOT appear.
        if isinstance(result, dict):
            err = result.get("error", "") if not result.get("success") else ""
            assert "Too many views" not in err, (
                f"Cap mistakenly fired on a list that dedupes to 1: {err}"
            )
