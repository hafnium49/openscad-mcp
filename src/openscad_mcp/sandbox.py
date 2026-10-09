"""Structural confinement of every ``openscad`` child process (MH-256).

MH-254 refused the file-referencing *feature* in model-supplied source, and MH-256
hardens that lexical refusal with a lexer that is comment/whitespace-aware and that
also covers ``import_*``/``dxf_*``/``surface``/``file=`` (see ``scad_guard``). But a
lexical guard is a denylist of what someone thought of. The load-bearing control is
here: the renderer runs inside a **mount-namespace sandbox** that makes everything
the program could read ABSENT, not merely refused — so even a program that slips the
guard physically cannot open a host file.

## Why bubblewrap, and exactly what it allows

The host (Ubuntu 22.04, kernel 6.8) ships ``bwrap`` 0.6.1 and permits unprivileged
user namespaces, so an unprivileged ``bwrap`` builds a fresh mount + PID + IPC + UTS
+ cgroup namespace with a hand-picked, mostly read-only view of the filesystem:

* **READ** ``/usr`` (system libs, ``/usr/share/openscad`` incl. the MCAD library,
  ``/usr/share/fonts`` + ``/usr/local/share/fonts`` that ``fc-list`` reads, locales)
  with ``/bin``/``/lib``/``/lib64``/``/sbin`` as the host's own symlinks into it;
  ``/etc/fonts`` (+ ``/var/cache/fontconfig``), ``/etc/ld.so.cache``,
  ``/etc/nsswitch.conf``/``/etc/passwd`` (glibc NSS; ``passwd`` is world-readable and
  carries no secret), and the few GL/ld config files OpenSCAD's offscreen Qt build
  touches. These were derived by ``strace``-ing a real PNG render, not guessed.
* **WRITE** only the per-job directory (bound at its real path so the absolute paths
  already in the command line stay valid) plus a throwaway cache dir inside it for
  mesa's shader cache. Nothing else is writable.
* **ABSENT** everything else: all of ``/home`` (so the worktree, ``~/.aws``,
  ``v5/.env`` and every credential are not merely unreadable but gone), ``/root``,
  ``/srv``, other tenants' ``/tmp`` (a fresh ``/tmp`` could be requested, but the X
  socket dir is what we need), and — via ``--unshare-pid`` + a fresh ``--proc`` —
  the ``/proc`` of every other process on the host.

The X server (``Xvfb :99``, how this host renders PNGs headlessly — see
``scripts/run-sse.sh``) is reached by bind-mounting ``/tmp/.X11-unix`` and leaving the
network namespace shared, so Qt's offscreen GL has a display. OpenSCAD opens no
network sockets of its own, so a shared netns is not an exfiltration path; the threat
MH-256 closes is host-file *reads*, and those are closed by the mount namespace.

## Fail CLOSED

If the sandbox cannot be constructed — ``bwrap`` missing, userns disabled, a
self-test that does not return 0 — ``run_openscad`` raises ``SandboxUnavailable`` and
the render is refused. It is NEVER run unconfined. ``scripts/run-sse.sh`` installs
``bwrap`` as a hard dependency for exactly this reason.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence

from .scad_guard import child_environment, redact_external_output


class SandboxUnavailable(RuntimeError):
    """The confinement could not be established. The render is refused, never run
    unconfined. This is a transport/host failure, distinct from ``ScadRefused``."""


#: Read-only host paths OpenSCAD needs, derived by strace-ing a real render (cube +
#: a Japanese ``text()``). ``/usr`` carries the binary, every shared library, the
#: OpenSCAD resource tree (MCAD included) and all font files; the rest are glibc/
#: fontconfig/GL config. Missing entries are skipped (``--ro-bind-try``) so the set
#: stays valid across hosts that lack an optional file.
_RO_BINDS_REQUIRED: tuple[str, ...] = (
    "/usr",
    "/etc/fonts",
    "/etc/ld.so.cache",
    "/etc/nsswitch.conf",
    "/etc/passwd",
)
_RO_BINDS_OPTIONAL: tuple[str, ...] = (
    "/etc/ld.so.preload",
    "/etc/drirc",
    "/usr/share/drirc.d",
    "/var/cache/fontconfig",
    "/etc/alternatives",
    "/etc/resolv.conf",
)

#: The host's top-level symlinks into /usr. Recreated inside the sandbox so an
#: ``ld.so.cache`` entry or a ``#!`` line naming ``/bin`` or ``/lib64`` resolves.
_USR_SYMLINKS: tuple[tuple[str, str], ...] = (
    ("usr/bin", "/bin"),
    ("usr/lib", "/lib"),
    ("usr/lib64", "/lib64"),
    ("usr/sbin", "/sbin"),
)

#: The X socket directory. Bound read-write because connecting to a unix socket
#: needs write access to the socket inode. It holds only display sockets.
_X11_SOCKET_DIR = "/tmp/.X11-unix"


def _bwrap_path() -> Optional[str]:
    return shutil.which("bwrap") or (
        "/usr/bin/bwrap" if Path("/usr/bin/bwrap").exists() else None
    )


@lru_cache(maxsize=1)
def sandbox_available() -> bool:
    """True iff a minimal ``bwrap`` namespace can actually be created on this host.

    Cached: the answer does not change within a daemon's lifetime. A self-test that
    returns non-zero (userns blocked, seccomp denial) means the sandbox is NOT usable
    and the caller must fail closed, never downgrade to an unconfined run.
    """
    bwrap = _bwrap_path()
    if not bwrap:
        return False
    # Representative of the real render bind-set: /usr plus the host's top-level
    # symlinks into it (so the dynamic loader at /lib64 is reachable — without the
    # symlinks execvp of a dynamically-linked /usr/bin/true fails ENOENT), a fresh
    # PID namespace + /proc, and /dev. If this cannot run, the real render cannot
    # either, and run_openscad must fail closed.
    selftest = [bwrap, "--unshare-user-try", "--unshare-ipc", "--unshare-pid"]
    selftest += ["--ro-bind", "/usr", "/usr"]
    for target, link in _USR_SYMLINKS:
        selftest += ["--symlink", target, link]
    selftest += ["--proc", "/proc", "--dev", "/dev", "--die-with-parent", "/usr/bin/true"]
    try:
        proc = subprocess.run(
            selftest, capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _env_as_setenv_flags(env: dict) -> List[str]:
    flags: List[str] = []
    for key, value in env.items():
        flags += ["--setenv", key, value]
    return flags


def build_sandbox_command(
    argv: Sequence[str],
    *,
    work: Path,
    env: dict,
    extra_ro_binds: Sequence[str] = (),
) -> List[str]:
    """Wrap ``argv`` (an openscad command line) in the bwrap confinement.

    ``work`` is the per-job directory, the ONLY writable path, bound at its real path
    so absolute paths already present in ``argv`` remain valid. ``env`` is the
    complete child environment from :func:`scad_guard.child_environment`; the sandbox
    starts from ``--clearenv`` and sets exactly these, so nothing of the daemon's own
    environment leaks even if a future edit widens ``child_environment``.
    """
    bwrap = _bwrap_path()
    if not bwrap:
        raise SandboxUnavailable("bwrap is not installed")

    work = work.resolve()
    cmd: List[str] = [
        bwrap,
        # Fresh namespaces. Net stays shared so the Xvfb display is reachable;
        # OpenSCAD opens no network sockets of its own, so this is not an egress path.
        "--unshare-user-try",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--new-session",
        "--die-with-parent",
        "--clearenv",
    ]

    # Read-only system view.
    cmd += ["--ro-bind", "/usr", "/usr"]
    for target, link in _USR_SYMLINKS:
        cmd += ["--symlink", target, link]
    for path in _RO_BINDS_REQUIRED:
        if path == "/usr":
            continue
        cmd += ["--ro-bind", path, path]
    for path in _RO_BINDS_OPTIONAL:
        cmd += ["--ro-bind-try", path, path]
    for path in extra_ro_binds:
        cmd += ["--ro-bind-try", path, path]

    # Kernel filesystems + a fresh /proc (hides every other process's /proc).
    cmd += ["--proc", "/proc", "--dev", "/dev"]

    # The headless X display socket.
    if Path(_X11_SOCKET_DIR).exists():
        cmd += ["--bind", _X11_SOCKET_DIR, _X11_SOCKET_DIR]

    # If XAUTHORITY names a real file, expose it read-only.
    xauth = env.get("XAUTHORITY")
    if xauth and Path(xauth).is_file():
        cmd += ["--ro-bind-try", xauth, xauth]

    # The sole writable path.
    cmd += ["--bind", str(work), str(work), "--chdir", str(work)]

    cmd += _env_as_setenv_flags(env)

    cmd.append("--")
    cmd += list(argv)
    return cmd


@dataclass
class SandboxResult:
    """A completed sandboxed render. ``stdout``/``stderr`` have ALREADY been passed
    through :func:`scad_guard.redact_external_output`, so nothing derived from a file
    the caller did not write reaches whoever reads them."""

    returncode: int
    stdout: str
    stderr: str


def run_openscad(
    argv: Sequence[str],
    *,
    work: Path,
    source: str,
    timeout: float,
    extra_ro_binds: Sequence[str] = (),
) -> SandboxResult:
    """The ONE runner every openscad child process goes through (MH-256).

    Confines the process (see module docstring), runs it with the minimal
    allow-listed environment, and filters its stdout/stderr before returning. Raises
    :class:`SandboxUnavailable` (fail closed) if the sandbox cannot be established;
    re-raises :class:`subprocess.TimeoutExpired` so callers keep their timeout
    messages.

    ``argv`` must be the openscad command line (binary first). ``work`` is the
    per-job directory and must be the only path the command writes to — callers write
    OpenSCAD's ``-o`` target inside ``work`` and move it out afterwards, so the
    sandbox needs no other writable bind.
    """
    if not sandbox_available():
        raise SandboxUnavailable(
            "the render sandbox (bubblewrap) could not be established on this host; "
            "refusing to run OpenSCAD unconfined"
        )

    work.mkdir(parents=True, exist_ok=True)
    # mesa writes a shader cache; keep it inside the (writable) job dir, not $HOME.
    cache_dir = work / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = dict(env_for(work))

    command = build_sandbox_command(
        argv, work=work, env=env, extra_ro_binds=extra_ro_binds
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    # subprocess.run(text=True) always yields str, but coerce defensively so a
    # non-str never reaches the line-oriented filter.
    out = result.stdout if isinstance(result.stdout, str) else ""
    err = result.stderr if isinstance(result.stderr, str) else ""
    return SandboxResult(
        returncode=result.returncode,
        stdout=redact_external_output(out, job_dir=work, source=source),
        stderr=redact_external_output(err, job_dir=work, source=source),
    )


def env_for(work: Path) -> dict:
    """The complete child environment: the MH-254 allow-list plus the sandbox-only
    cache redirections that keep mesa/XDG writes inside the writable job dir."""
    env = dict(child_environment(work))
    cache = work / "cache"
    env.update(
        XDG_CACHE_HOME=str(cache),
        MESA_SHADER_CACHE_DIR=str(cache),
        __GL_SHADER_DISK_CACHE_PATH=str(cache),
    )
    return env


__all__ = [
    "SandboxUnavailable",
    "SandboxResult",
    "sandbox_available",
    "build_sandbox_command",
    "run_openscad",
    "env_for",
]
