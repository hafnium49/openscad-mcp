"""
Unit tests for ``_validate_variable_names`` (B2 fix, 2026-05-07).

The helper widens the previous Python-identifier-only regex
(``^[a-zA-Z_][a-zA-Z0-9_]*$``) to also accept OpenSCAD's special
variables (``$fn``, ``$fa``, ``$fs``, ``$t``, etc.). Bug B2 from
``docs/openscad_mcp_bug_report.md`` was: ``quality:"draft"`` injects
``$fn=8`` which the old regex rejected with
``Invalid variable name '$fn': must match ^[a-zA-Z_][a-zA-Z0-9_]*$``,
breaking ``compare_renders``'s default-quality path.

To prevent the widening from opening a DoS window
(``{"$fn": 999999}`` → 120 s of CGAL geometry on a 5-slot
semaphore), the helper also bounds numeric values for known special
variables. OpenSCAD reserved words are explicitly rejected.
"""
from __future__ import annotations

import pytest

from openscad_mcp.server import _validate_variable_names


# =============================================================================
# Positive cases — names accepted
# =============================================================================


@pytest.mark.unit
class TestVariableNameAccepts:
    """Names the validator must accept."""

    def test_dollar_prefix_special_vars(self):
        """OpenSCAD special variables ($fn, $fa, $fs, $t) — the bug-B2
        case. Bounded values; helper must not raise."""
        _validate_variable_names({"$fn": 64})
        _validate_variable_names({"$fa": 12})
        _validate_variable_names({"$fs": 2})
        _validate_variable_names({"$t": 0.5})

    def test_dollar_prefix_unbounded_vars(self):
        """Variables outside the ``_OPENSCAD_SPECIAL_BOUNDS`` set
        (``$preview``, ``$vpr``, etc.) only need name validation —
        any value passes."""
        _validate_variable_names({"$preview": True})
        _validate_variable_names({"$vpr": [10, 20, 30]})
        _validate_variable_names({"$vpt": [0, 0, 0]})
        _validate_variable_names({"$vpd": 200})

    def test_dollar_underscore(self):
        """``$_`` and ``$_x`` — verified empirically valid in OpenSCAD
        2021.01 (``$_=5; echo($_)`` runs)."""
        _validate_variable_names({"$_": 5})
        _validate_variable_names({"$_x": 5})

    def test_plain_identifiers(self):
        """Normal Python-identifier-shaped names — no regression on
        the existing accept set."""
        _validate_variable_names({"foo": 1})
        _validate_variable_names({"_bar": 1})
        _validate_variable_names({"x1": 1})
        _validate_variable_names({"MAX_SIZE": 100})

    def test_none_or_empty_dict_skips_validation(self):
        """Helper must short-circuit on None / empty dict."""
        _validate_variable_names(None)
        _validate_variable_names({})


# =============================================================================
# Negative cases — names rejected (regression pins)
# =============================================================================


@pytest.mark.unit
class TestVariableNameRejects:
    """Names the validator must reject. Pin tests so a future
    permissive refactor regresses with a failing test, not silently."""

    def test_rejects_double_dollar(self):
        with pytest.raises(ValueError, match="Invalid variable name"):
            _validate_variable_names({"$$fn": 8})

    def test_rejects_mid_dollar(self):
        """Regression pin against permissive refactors: only a leading
        `$` is accepted; `f$n` is not a valid OpenSCAD identifier."""
        with pytest.raises(ValueError, match="Invalid variable name"):
            _validate_variable_names({"f$n": 8})

    def test_rejects_dash_or_space(self):
        with pytest.raises(ValueError, match="Invalid variable name"):
            _validate_variable_names({"bad-name": 1})
        with pytest.raises(ValueError, match="Invalid variable name"):
            _validate_variable_names({"bad name": 1})

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="Invalid variable name"):
            _validate_variable_names({"": 1})

    def test_rejects_leading_digit(self):
        with pytest.raises(ValueError, match="Invalid variable name"):
            _validate_variable_names({"1foo": 1})

    def test_rejects_unicode(self):
        """OpenSCAD itself does not accept Unicode identifiers; pin
        rejection so a future ``\\w`` refactor doesn't silently
        accept them and break OpenSCAD downstream."""
        with pytest.raises(ValueError, match="Invalid variable name"):
            _validate_variable_names({"α": 1})
        with pytest.raises(ValueError, match="Invalid variable name"):
            _validate_variable_names({"フォント": 1})

    def test_rejects_sql_shaped(self):
        """The regex shape-rejection IS the security guarantee for
        ``-D key=val`` shell-arg-quoting. Pin a SQL-injection-style
        name so any regression here is caught immediately."""
        with pytest.raises(ValueError, match="Invalid variable name"):
            _validate_variable_names({"name'; DROP TABLE": 1})


# =============================================================================
# OpenSCAD reserved words — explicit rejection
# =============================================================================


@pytest.mark.unit
class TestReservedWords:
    """OpenSCAD reserved words shadow keywords if passed via ``-D``;
    reject them with a distinguishable error."""

    @pytest.mark.parametrize("keyword", [
        "module", "function", "if", "else", "for", "intersection_for",
        "let", "each", "true", "false", "undef", "include", "use",
    ])
    def test_rejects_openscad_reserved(self, keyword):
        with pytest.raises(ValueError, match="reserved word"):
            _validate_variable_names({keyword: 1})


# =============================================================================
# Value bounds — DoS hardening (Security review F2)
# =============================================================================


@pytest.mark.unit
class TestValueBounds:
    """Special variables with known numeric ranges must be bounded.
    Mitigates DoS via large geometry counts (e.g. ``$fn=999999``)."""

    def test_fn_clamped_at_upper_bound(self):
        """``$fn=999999`` would request ~1M-segment cylinders — DoS."""
        with pytest.raises(ValueError, match=r"\$fn"):
            _validate_variable_names({"$fn": 999999})

    def test_fn_at_cap_passes(self):
        """``$fn=256`` is exactly at the cap — must pass."""
        _validate_variable_names({"$fn": 256})

    def test_fn_zero_passes(self):
        """``$fn=0`` is OpenSCAD's default 'use $fa/$fs instead' marker."""
        _validate_variable_names({"$fn": 0})

    def test_t_out_of_range(self):
        """``$t`` (animation time) must be in ``[0, 1]``."""
        with pytest.raises(ValueError, match=r"\$t"):
            _validate_variable_names({"$t": 1.5})
        with pytest.raises(ValueError, match=r"\$t"):
            _validate_variable_names({"$t": -0.1})

    def test_t_endpoints_pass(self):
        _validate_variable_names({"$t": 0.0})
        _validate_variable_names({"$t": 1.0})

    def test_fn_string_rejected_with_type_msg(self):
        """``{"$fn": "many"}`` must raise a numeric-type error, not
        a regex-match error."""
        with pytest.raises(ValueError, match="must be numeric"):
            _validate_variable_names({"$fn": "many"})

    def test_fn_bool_rejected_with_type_msg(self):
        """``bool`` is a subclass of ``int`` in Python; reject it
        explicitly so ``{"$fn": True}`` doesn't sneak through as 1."""
        with pytest.raises(ValueError, match="must be numeric"):
            _validate_variable_names({"$fn": True})

    def test_fa_low_bound(self):
        """``$fa`` lower bound is 0.01 (inclusive)."""
        _validate_variable_names({"$fa": 0.01})
        with pytest.raises(ValueError, match=r"\$fa"):
            _validate_variable_names({"$fa": 0.0})

    def test_fs_low_bound(self):
        """``$fs`` lower bound is 0.001 (inclusive)."""
        _validate_variable_names({"$fs": 0.001})
        with pytest.raises(ValueError, match=r"\$fs"):
            _validate_variable_names({"$fs": 0.0})
