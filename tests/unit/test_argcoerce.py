"""
Unit tests for ``coerce_jsonish_list`` (B3 fix, 2026-05-07).

Bug B3: openscad-mcp's ``render_perspectives.views`` parameter
(``Optional[List[str]]``) is delivered as a JSON-stringified literal
``'["front","top","isometric"]'`` by some clients with defensive
LLM-side argument encoders (Claude Desktop on Windows is the observed
reproduction path). The helper at ``src/openscad_mcp/utils/argcoerce.py``
re-parses such strings back into native lists at the FastMCP tool
boundary via Pydantic's ``Annotated[..., BeforeValidator(...)]``.

These tests pin:
- correct shape coercion (string → list, native list pass-through, etc.)
- DoS hardening (RecursionError catch, oversize cap)
- the ``expected_item_type`` argument's two modes
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from openscad_mcp.utils.argcoerce import coerce_jsonish_list


# =============================================================================
# Helper-shape tests
# =============================================================================


@pytest.mark.unit
class TestHelperShape:
    """Inputs that must / must not coerce."""

    def test_native_list_passes_through_unchanged(self):
        result = coerce_jsonish_list(["a", "b", "c"])
        assert result == ["a", "b", "c"]

    def test_jsonish_string_parses_to_list(self):
        result = coerce_jsonish_list('["a","b","c"]')
        assert result == ["a", "b", "c"]

    def test_whitespace_padded_jsonish_parses(self):
        # Leading/trailing whitespace + newline must be stripped before
        # the bracket check; this is the common case from a sloppy
        # client that adds line-breaks for readability.
        result = coerce_jsonish_list('  ["a","b"]\n')
        assert result == ["a", "b"]

    def test_oversize_string_returned_unchanged(self):
        # 70 KB input is over the 64 KB ceiling — return unchanged so
        # the helper never spends multi-MB JSON parses on noise.
        big = "[" + ("x" * 70_000) + "]"
        result = coerce_jsonish_list(big)
        assert result == big  # original string returned

    def test_malformed_json_returned_unchanged(self):
        # Brackets present but the inside isn't valid JSON. Pydantic's
        # downstream list-type check then produces the canonical error.
        s = "[a,b,c]"  # unquoted identifiers
        assert coerce_jsonish_list(s) == s

    def test_non_list_json_returned_unchanged(self):
        # JSON object, not array — must NOT silently get converted.
        s = '{"k":"v"}'
        assert coerce_jsonish_list(s) == s

    def test_string_with_internal_garbage_returned_unchanged(self):
        # After strip(), a string that doesn't end with `]` fails the
        # prefix/suffix check and is returned unchanged.
        s = '  ["a","b"]  garbage  '
        assert coerce_jsonish_list(s) == s

    def test_non_string_non_list_passes_through(self):
        # Numbers, dicts, None, booleans — none are strings, so the
        # helper short-circuits at the isinstance check.
        assert coerce_jsonish_list(42) == 42
        assert coerce_jsonish_list(3.14) == 3.14
        assert coerce_jsonish_list({"k": "v"}) == {"k": "v"}
        assert coerce_jsonish_list(True) is True

    def test_none_passes_through(self):
        # None is the default for Optional[List[str]] params; the helper
        # MUST return it unchanged so Pydantic accepts the default path.
        assert coerce_jsonish_list(None) is None


# =============================================================================
# DoS guards (Security review Q1 + the 64 KB cap)
# =============================================================================


@pytest.mark.unit
class TestDoSGuards:
    """RecursionError + oversize hardening."""

    def test_recursion_error_caught_by_helper(self):
        """Closes Security Q1 — pin the RecursionError catch.

        Python 3.13's ``json.loads`` is more iterative than older
        versions and may not actually trigger ``RecursionError`` on
        ``'[' * 1500 + ']' * 1500`` in this build. But ``RecursionError``
        IS the live failure mode under (a) different Python versions,
        (b) reduced stack size, (c) adversarial nested-object input.
        Mocking ``json.loads`` to raise ``RecursionError`` directly
        verifies the helper's ``except`` clause catches it instead
        of letting the exception propagate to the worker thread.
        """
        with patch(
            "openscad_mcp.utils.argcoerce.json.loads",
            side_effect=RecursionError("simulated stack overflow in parser"),
        ):
            # Helper MUST not raise; MUST return original string.
            result = coerce_jsonish_list('["a","b"]')
            assert result == '["a","b"]'

    def test_oversize_payload_with_real_array_returned_unchanged(self):
        """A genuinely-valid JSON array that's bigger than the 64 KB
        cap must not be parsed (otherwise a malicious caller could
        force multi-MB JSON parses to amplify cost)."""
        big = "[" + ('"a",' * 20000) + '"a"]'
        assert len(big) > 64 * 1024, "test setup: expected payload over 64 KB"
        result = coerce_jsonish_list(big)
        assert result == big  # original returned, not parsed


# =============================================================================
# expected_item_type — two modes
# =============================================================================


@pytest.mark.unit
class TestExpectedItemType:
    """Default ``None`` (lenient) vs explicit type (strict)."""

    def test_default_expected_item_type_none_returns_mixed_list(self):
        """With the default ``expected_item_type=None``, the helper
        accepts any item types and lets pydantic produce the
        per-item error downstream (a clearer failure message than
        the helper silently rejecting and pydantic producing
        ``Input should be a valid list``)."""
        result = coerce_jsonish_list('[1,2,3]')
        assert result == [1, 2, 3]

    def test_explicit_expected_item_type_str_rejects_int_list(self):
        """With ``expected_item_type=str``, `[1,2,3]` is mixed-type
        and the helper returns the original string. Caller has opted
        into 'fail closed' semantics."""
        s = '[1,2,3]'
        result = coerce_jsonish_list(s, expected_item_type=str)
        assert result == s
