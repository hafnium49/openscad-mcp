"""Refuse external-file references in model-supplied OpenSCAD source (MH-254).

This daemon runs ``openscad`` as the ``hafnium`` user, unsandboxed, with network
access, and the model always sends its program as inline ``scad_content``. OpenSCAD
can read files: ``include <...>``, ``use <...>`` (including ``use <x.ttf>``, which
loads a font FILE), ``import(...)`` and ``surface(file=...)``. That is a
local-file-read primitive whose argument the MODEL chooses, and ``openscad``'s
stderr is returned to the caller — a read-and-report channel over any absolute host
path the daemon user can read.

The upstream P1 sandbox (``MCP_ALLOWED_PATHS``) only ever inspected the ``scad_file``
and ``include_paths`` parameters; inline ``scad_content`` was never looked at
(``scripts/run-sse.sh`` records this gap). This module closes it.

## What is refused, and why it is a FEATURE not a denylist

The whole file-referencing feature is refused, which is a POSITIVE, checkable
property of the source — *this program references no external file* — rather than a
path denylist, which is a list of what someone thought of. This mirrors v6's
``scad_compute.assert_no_external_reference`` (``mkc-holmes-v5/v6/services/openscad``).

The ONE exception is a whole-line ``include <MCAD/...>`` / ``use <MCAD/...>`` that
names a file in the host's installed MCAD library. v5's host ships Debian's
``openscad-mcad`` and resolves such a line from OpenSCAD's compiled-in resource
path, so the line is left in the source and reaches only that closed, trusted set.
BOSL2 is NOT installed on this host, so no BOSL2 line is admitted.

A font passed by NAME (``font="Noto Sans CJK JP"``) is a fontconfig pattern, not a
file, so it is left alone — only the font FILE route (``use <x.ttf>``) is refused,
by the ``use`` rule above. That keeps v5's Japanese engraving working.

The refusal is not the only control. The renderer additionally runs in a fresh
EMPTY working directory with a MINIMAL allow-listed environment (see
``child_environment``) and no ``OPENSCADPATH``, so a relative reference that slipped
the lexer resolves to nothing. An ABSOLUTE reference would not — which is why the
lexical refusal is the primary control, not the directory.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: ``include <...>`` / ``use <...>`` — the ``<...>``-syntax file references. Word-
#: boundaried (leading ``[^A-Za-z0-9_]``) so ``included_parts`` is not caught. Only
#: literal whitespace is allowed between the keyword and ``<``: OpenSCAD 2021.01's
#: include lexer treats a ``/*comment*/`` there as a PARSE ERROR, not whitespace, so
#: ``include/**/<x>`` reads nothing and is deliberately NOT matched (pinned by test).
#: ``\s`` spans newlines, matching the lexer, which accepts a real newline there.
_INCLUDE_USE = re.compile(
    r"(?:^|[^A-Za-z0-9_])(include|use)\s*<", re.IGNORECASE
)

#: Comments are whitespace to OpenSCAD's expression lexer, so ``import/**/("x")`` and
#: ``surface/**/(file=...)`` are real file reads. A check that skipped comments would
#: be one comment away from not running. Mirrors v6 ``scad_compute._GAP``. Used only
#: for the CALL forms below (``name(...)``), never for the ``<...>`` include/use rule.
_GAP = r"(?:\s|/\*[\s\S]*?\*/|//[^\n]*(?:\n|$))*"

#: The call-syntax file-reading builtins, refused as a FEATURE. ``import`` and its
#: ``import_stl``/``import_off``/``import_dxf`` aliases, ``surface``, and every
#: deprecated ``dxf_*`` reader (``dxf_linear_extrude``/``dxf_rotate_extrude``/
#: ``dxf_dim``/``dxf_cross``). The leading ``(?<![A-Za-z0-9_$.])`` keeps
#: ``my_surface_area(`` / ``do_import(`` out. The trailing ``_GAP\(`` tolerates a
#: comment or newline before the paren, matching the lexer.
_FILE_CALL = re.compile(
    r"(?<![A-Za-z0-9_$.])"
    r"(import_stl|import_off|import_dxf|import"
    r"|surface"
    r"|dxf_linear_extrude|dxf_rotate_extrude|dxf_dim|dxf_cross)"
    + _GAP + r"\(",
    re.IGNORECASE,
)

#: A ``file =`` named argument anywhere — the shared file route of ``surface``,
#: ``import``, the ``dxf_*`` family and ``linear_extrude``/``rotate_extrude``. The
#: lookbehind keeps ``myfile``/``file_name`` out; ``(?!=)`` keeps the ``==``
#: comparison out. ``file`` as a plain variable is vanishingly rare and refusing it
#: is safe, so this is not narrowed further.
_FILE_KWARG = re.compile(
    r"(?<![A-Za-z0-9_$.])file" + _GAP + r"=(?!=)", re.IGNORECASE
)

#: ``linear_extrude``/``rotate_extrude`` whose FIRST positional argument is a string
#: literal or a bare identifier — i.e. the deprecated ``linear_extrude("x.dxf")`` /
#: ``linear_extrude(f)`` DXF-file form (``f`` holding a path is indistinguishable
#: here from ``f`` holding a height, so a bare identifier is refused; write
#: ``linear_extrude(height=h)`` or an arithmetic expression instead). A numeric or
#: vector first positional (``linear_extrude(10)``, ``linear_extrude([...])``), a
#: named argument (``height=``/``angle=``/``$fn=``) and an expression starting with a
#: number are all admitted. Capture group 2 is the identifier (or empty for a
#: string), group 3 the first char after it.
_EXTRUDE_HEAD = re.compile(
    r"(?<![A-Za-z0-9_$.])(linear_extrude|rotate_extrude)" + _GAP + r"\(" + _GAP
    + r'(?:(")|([A-Za-z_$][A-Za-z0-9_$]*)' + _GAP + r"(.?))",
    re.IGNORECASE,
)

#: The ONE library reference a caller may write, matched as a WHOLE LINE, mirroring
#: v6's ``_MCAD_LINE``. ``bitmap/`` is the one sub-directory admitted (as on the host);
#: anything else with a ``/`` never matches. Traversal (``MCAD/../x``), an absolute
#: path or a mid-line reference cannot match and is refused by ``_FILE_REFERENCING``.
_MCAD_LINE = re.compile(
    r"^[ \t]*(include|use)[ \t]*<[ \t]*MCAD/((?:bitmap/)?[A-Za-z0-9_]+\.scad)[ \t]*>"
    r"[ \t]*;?[ \t]*$"
)

#: Standard install locations OpenSCAD compiles in / Debian ships MCAD under. The
#: ``OPENSCADPATH`` env var is deliberately NOT consulted: the renderer clears it, and
#: a caller-settable search path is exactly what must not decide what resolves.
_MCAD_SEARCH_ROOTS: tuple[str, ...] = (
    "/usr/share/openscad/libraries",
    "/usr/local/share/openscad/libraries",
    "/usr/share/openscad/libraries/openscad",
)


class ScadRefused(ValueError):
    """A source or request we looked at and refused. NOT a transport failure."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _discover_mcad_files() -> dict[str, str]:
    """Closed map of installed MCAD files, keyed by the path a caller writes after
    ``MCAD/`` (``boxes.scad``, ``bitmap/bitmap.scad``). Empty if MCAD is not installed."""
    files: dict[str, str] = {}
    for root in _MCAD_SEARCH_ROOTS:
        mcad = Path(root) / "MCAD"
        if not mcad.is_dir():
            continue
        for scad in mcad.glob("*.scad"):
            files.setdefault(scad.name, str(scad))
        bitmap = mcad / "bitmap"
        if bitmap.is_dir():
            for scad in bitmap.glob("*.scad"):
                files.setdefault(f"bitmap/{scad.name}", str(scad))
        if files:
            break
    return files


#: Discovered once at import. A missing MCAD install leaves this empty and an MCAD
#: line is then a NAMED ``library_unavailable`` refusal, never a bare parse error.
_MCAD_FILES: dict[str, str] = _discover_mcad_files()


def available_mcad_files() -> tuple[str, ...]:
    """The installed MCAD files a caller may reference, sorted."""
    return tuple(sorted(_MCAD_FILES))


def _strip_allowed_mcad_lines(source: str) -> str:
    """Return ``source`` with every ALLOWED whole-line MCAD include/use blanked.

    Only used to build the text the refusal scan runs over — the ORIGINAL source
    (MCAD lines intact) is what reaches OpenSCAD, which resolves those lines from the
    host's installed MCAD. A line that LOOKS like an MCAD include but names a file
    this host does not have is refused here rather than blanked, so it then trips the
    unchanged ``_FILE_REFERENCING`` refusal only if it is also not whole-line — but a
    missing MCAD file is the clearer error, so it is raised directly.

    Blanking (not deleting) keeps line numbers aligned with the caller's source.
    """
    lines = source.split("\n")
    for i, line in enumerate(lines):
        m = _MCAD_LINE.match(line)
        if not m:
            continue
        if not _MCAD_FILES:
            raise ScadRefused(
                "library_unavailable",
                "MCAD is referenced but is not installed on this host",
            )
        name = m.group(2)
        if name not in _MCAD_FILES:
            raise ScadRefused(
                "library_subset_unavailable",
                f"MCAD/{name} is not an installed MCAD file; available: "
                f"{', '.join(available_mcad_files())}",
            )
        lines[i] = ""
    return "\n".join(lines)


def _reason(code: str, detail: str) -> "ScadRefused":
    return ScadRefused(code, detail)


_EXTERNAL_REFERENCE_DETAIL = (
    "This daemon renders self-contained programs only: include/use/import/surface, "
    "the dxf_* readers, a `file=` argument and a string/identifier passed as the "
    "first argument of linear_extrude/rotate_extrude are refused as a FEATURE, "
    "because validating a model-chosen path is not a property anyone can check. "
    "Installed MCAD files may be referenced as a whole-line `include <MCAD/<file>.scad>`."
)


def assert_no_external_reference(source: str) -> None:
    """Refuse the whole file-referencing feature, except an installed MCAD whole line.

    ⚠️ POSITIVE PROPERTY, NOT A PATH DENYLIST. The claim this makes is "this program
    references no external file", which is checkable; "this path is safe" is not.

    MH-256 widened the lexical scope to close the holes MH-254 left open: the lexer
    is now comment/whitespace-aware between a builtin name and its ``(``; it refuses
    ``import``/``import_stl``/``import_off``/``import_dxf``, ``surface``, every
    ``dxf_*`` reader, any ``file =`` named argument, and a string-literal or bare-
    identifier first positional of ``linear_extrude``/``rotate_extrude``.

    ⚠️ This lexical refusal is DEFENSE IN DEPTH. The load-bearing control is the
    mount-namespace sandbox in :mod:`openscad_mcp.sandbox`, which makes every host
    file ABSENT — so a form that slips this lexer still cannot read anything.
    """
    scan = _strip_allowed_mcad_lines(source)

    m = _INCLUDE_USE.search(scan)
    if m:
        raise _reason(
            "external_reference_refused",
            f"the source uses {m.group(1).lower()!r} with a `<...>` reference. "
            + _EXTERNAL_REFERENCE_DETAIL,
        )

    m = _FILE_CALL.search(scan)
    if m:
        raise _reason(
            "external_reference_refused",
            f"the source calls {m.group(1).lower()!r}. " + _EXTERNAL_REFERENCE_DETAIL,
        )

    m = _FILE_KWARG.search(scan)
    if m:
        raise _reason(
            "external_reference_refused",
            "the source passes a `file=` argument. " + _EXTERNAL_REFERENCE_DETAIL,
        )

    _assert_extrude_first_positional(scan)


def _assert_extrude_first_positional(scan: str) -> None:
    """Refuse a string-literal or bare-identifier first positional of
    ``linear_extrude``/``rotate_extrude`` (the deprecated DXF-file form)."""
    for m in _EXTRUDE_HEAD.finditer(scan):
        fn = m.group(1).lower()
        is_string = m.group(2) is not None
        ident = m.group(3)
        after = m.group(4) or ""
        if is_string:
            raise _reason(
                "external_reference_refused",
                f"the source passes a string literal as the first argument of "
                f"{fn!r} (the deprecated DXF-file form). "
                + _EXTERNAL_REFERENCE_DETAIL,
            )
        if ident is not None:
            # A named argument (`height=`, `angle=`, `$fn=`) is fine; a `==` is a
            # comparison, not an assignment. Anything else after a lone identifier
            # (a `)` or `,`) is a bare variable positional — refuse it.
            if after == "=":
                continue  # named argument
            if after in (")", ","):
                raise _reason(
                    "external_reference_refused",
                    f"the source passes a bare identifier as the first argument of "
                    f"{fn!r}; write `{fn}(height=<var>)` for a height or inline the "
                    f"geometry. " + _EXTERNAL_REFERENCE_DETAIL,
                )
            # An operator (`*`, `+`, `.`, `[`) means an arithmetic/index expression
            # that cannot be a filename — admit it.


# ============================================================================
# Output filter (MH-256 layer 3): nothing derived from a file the caller did
# not write may reach the model.
# ============================================================================

#: An absolute path token, as it appears in OpenSCAD diagnostics — bare, quoted, or
#: back-ticked (``WARNING: ... in `/path/file'``). Stops at whitespace and the quote
#: characters OpenSCAD wraps paths in.
_ABS_PATH = re.compile(r"/[^\s'\"`<>(){}]+")

#: Quoted spans OpenSCAD echoes: single, double and back-tick. The content of a span
#: that is not present verbatim in the caller's own source is redacted, because it can
#: only have come from a file (an ``Illegal ID '<first line of a DXF>'`` leak, a
#: back-ticked path, an SVG parser echoing a raw line).
_QUOTED = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"|`([^`\n]*)`")

_REDACTED = "[redacted]"


def _names_foreign_path(line: str, job_prefix: str) -> bool:
    """True if ``line`` names any absolute path that is not inside the job dir."""
    for m in _ABS_PATH.finditer(line):
        token = m.group(0).rstrip(".,:;'\"`)")
        try:
            resolved = os.path.normpath(token)
        except ValueError:
            return True
        if resolved == job_prefix or resolved.startswith(job_prefix + os.sep):
            continue
        return True
    return False


def _redact_foreign_quotes(line: str, source: str) -> str:
    """Replace the content of any quoted span absent from ``source`` with a marker."""

    def repl(m: "re.Match[str]") -> str:
        for gi, quote in ((1, "'"), (2, '"'), (3, "`")):
            inner = m.group(gi)
            if inner is None:
                continue
            if inner == "" or inner in source:
                return m.group(0)
            return f"{quote}{_REDACTED}{quote}"
        return m.group(0)

    return _QUOTED.sub(repl, line)


def redact_external_output(text: str, *, job_dir, source: str) -> str:
    """Strip anything derived from a file the caller did not write (MH-256 layer 3).

    OpenSCAD's own error channel is a read-and-report primitive: a DXF reader prints
    ``Illegal ID '<first line of the file>'`` and the SVG importer echoes raw lines,
    all of which reach the model through the ``OpenSCAD ... failed: {stderr}`` raise
    and through ``validate_scad``'s parsed warnings. Two passes, line by line:

    1. DROP any line that names an absolute path outside ``job_dir`` — the
       ``in `/etc/...'`` / ``Can't open '/home/...'`` shape, which both carries a host
       path and tends to carry a fragment of the file beside it.
    2. In the lines that remain, REDACT the content of any quoted span that does not
       occur verbatim in the caller's ``source`` — so a leaked ``'SECRET=...'`` is
       blanked while the caller's own echoed strings and variable names survive.

    This is the last net, behind the sandbox (the file is unreadable) and the guard
    (the reference is refused); it exists so a path that reaches OpenSCAD's stderr by
    some route we did not foresee still cannot copy file content back to the model.
    """
    if not text:
        return text
    try:
        job_prefix = os.path.normpath(str(Path(job_dir).resolve()))
    except (OSError, ValueError):
        job_prefix = os.path.normpath(str(job_dir))
    kept: list[str] = []
    for line in text.split("\n"):
        if _names_foreign_path(line, job_prefix):
            continue
        kept.append(_redact_foreign_quotes(line, source))
    return "\n".join(kept)


#: 🔴 THE RENDERER'S ENVIRONMENT IS AN ALLOW-LIST, NOT ``os.environ`` MINUS A FEW.
#: A denylist is a list of what someone thought of. PASSED THROUGH: ``PATH`` (to find
#: ``openscad``/``xvfb-run``) and the two X variables the live Xvfb display needs. SET
#: HERE: a writable ``HOME``/``TMPDIR`` inside the per-request directory and a UTF-8
#: locale. Nothing else from the daemon's environment reaches the model's program.
#: Mirrors v6's ``child_environment``. ``OPENSCADPATH`` is intentionally absent.
_CHILD_ENV_PASSTHROUGH: tuple[str, ...] = ("PATH", "DISPLAY", "XAUTHORITY")
_CHILD_ENV_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


def child_environment(work: Path) -> dict[str, str]:
    """The complete environment of the renderer subprocess, for one working directory.

    Only ``PATH`` and the X display variables are inherited; everything else is set
    here. So no token, key or ``AWS_*``/``OPENAI_*`` value that happens to be in the
    daemon's environment is visible to the program the model wrote.
    """
    env = {k: os.environ[k] for k in _CHILD_ENV_PASSTHROUGH if os.environ.get(k)}
    env.setdefault("PATH", _CHILD_ENV_DEFAULT_PATH)
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    env.update(
        HOME=str(home),
        TMPDIR=str(work),
        LANG="C.UTF-8",
        LC_ALL="C.UTF-8",
    )
    return env


__all__ = [
    "ScadRefused",
    "assert_no_external_reference",
    "available_mcad_files",
    "child_environment",
    "redact_external_output",
]
