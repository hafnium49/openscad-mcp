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

#: The file-referencing constructs, refused as a FEATURE. Word-boundaried (leading
#: ``[^A-Za-z0-9_]`` and the trailing ``\s*[<(]``) so ``included_parts`` or
#: ``surface_area(`` are not caught — a guard that fires on ordinary code is one that
#: gets bypassed. ``\s`` spans newlines, matching OpenSCAD's own lexer, which accepts
#: ``include`` and ``<`` separated by whitespace. Ported from v6's ``_FILE_REFERENCING``.
_FILE_REFERENCING = re.compile(
    r"(?:^|[^A-Za-z0-9_])(include|use|import|surface)\s*[<(]", re.IGNORECASE
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


def assert_no_external_reference(source: str) -> None:
    """Refuse the whole file-referencing feature, except an installed MCAD whole line.

    ⚠️ POSITIVE PROPERTY, NOT A PATH DENYLIST. The claim this makes is "this program
    references no external file", which is checkable; "this path is safe" is not.

    ⚠️ RESIDUAL, STATED, matching v6's identical scope: the DEPRECATED file-reading
    forms ``linear_extrude(file=...)`` / ``rotate_extrude(file=...)`` and the
    ``dxf_dim``/``dxf_cross`` functions are not matched here, because blanket-refusing
    ``linear_extrude`` would refuse the single most common OpenSCAD idiom. They reach
    only the deprecated DXF-file path, the model never emits them, and they are
    further bounded by the empty working directory + cleared ``OPENSCADPATH``.
    """
    scan = _strip_allowed_mcad_lines(source)
    m = _FILE_REFERENCING.search(scan)
    if m:
        raise ScadRefused(
            "external_reference_refused",
            f"the source uses {m.group(0).strip()!r}. This daemon renders "
            f"self-contained programs only: include/use/import/surface and font "
            f"files are refused as a FEATURE, because validating a model-chosen path "
            f"is not a property anyone can check. Installed MCAD files may be "
            f"referenced as a whole-line `include <MCAD/<file>.scad>`.",
        )


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
]
