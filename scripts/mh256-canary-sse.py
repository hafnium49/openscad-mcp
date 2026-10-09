"""MH-256 canary: prove the live SSE daemon cannot leak a host file.

This is the end-to-end security gate for the MH-256 fix. It drives the SSE endpoint
with the file-reading bypass programs (``import``/``import_*``/``surface``/
``linear_extrude(file=...)``/``dxf_*``) pointed at a DUMMY canary file this script
creates under ``$HOME`` with a unique token, and asserts the token never comes back
in any response — refused by the guard, blocked by the sandbox, or scrubbed by the
output filter.

Two ways to run it:

  # A) spin up a TEMPORARY instance from this checkout on a free port and test it
  #    (used during development; does NOT touch the live :9300 daemon)
  uv run --with fastmcp python scripts/mh256-canary-sse.py --start

  # B) test an ALREADY-RUNNING daemon — this is what the v5 session runs after
  #    deploying + restarting the :9300 service
  uv run --with fastmcp python scripts/mh256-canary-sse.py http://localhost:9300/sse

Exit code 0 = no leak (PASS); non-zero = a leak or a harness failure.

The canary is a throwaway file with a random token, never a real secret; it is
removed on exit.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from fastmcp import Client

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ENDPOINT = "http://127.0.0.1:19300/sse"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _result_text(call_result) -> str:
    """Flatten a CallToolResult (any fastmcp version) to a string for leak scanning."""
    parts = [repr(call_result)]
    for attr in ("data", "structured_content", "content"):
        val = getattr(call_result, attr, None)
        if val is not None:
            parts.append(repr(val))
    return "\n".join(parts)


def _ensure_xvfb() -> None:
    try:
        subprocess.run(["pgrep", "-f", "Xvfb :99"], check=True,
                       capture_output=True)
        os.environ.setdefault("DISPLAY", ":99")
        return
    except Exception:
        pass
    subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    os.environ["DISPLAY"] = ":99"


def _start_temp_server(port: int) -> subprocess.Popen:
    """Launch a throwaway SSE daemon from THIS checkout, mirroring run-sse.sh."""
    _ensure_xvfb()
    env = dict(os.environ)
    renders = REPO / ".renders-canary"
    renders.mkdir(exist_ok=True)
    env.update(
        OPENSCAD_PATH=env.get("OPENSCAD_PATH", "/usr/bin/openscad"),
        MCP_TEMP_DIR=str(renders),
        MCP_RENDER_TIMEOUT="120",
        MCP_MAX_FILE_SIZE_MB="10",
        MCP_LOG_LEVEL="INFO",
        MCP_TRANSPORT="sse",
        MCP_HOST="127.0.0.1",
        MCP_PORT=str(port),
        PYTHONPATH=str(REPO / "src"),
        PYTHONUNBUFFERED="1",
        DISPLAY=env.get("DISPLAY", ":99"),
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "openscad_mcp"],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


async def _wait_ready(endpoint: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            async with Client(endpoint) as c:
                await c.ping()
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
            await asyncio.sleep(0.5)
    raise RuntimeError(f"server at {endpoint} not ready: {last}")


async def _run_canary(endpoint: str, token: str, stem: Path) -> int:
    programs = [
        ("render_single", {"scad_content": f'import("{stem}.svg");',
                            "output_format": "base64"}),
        ("render_single", {"scad_content": f'import_dxf("{stem}.dxf");',
                            "output_format": "base64"}),
        ("render_single", {"scad_content": f'surface(file="{stem}.dat");',
                            "output_format": "base64"}),
        ("render_single", {"scad_content":
                           f'linear_extrude(file="{stem}.dxf") square(1);',
                            "output_format": "base64"}),
        ("render_single", {"scad_content": f'import_stl("{stem}.stl");',
                            "output_format": "base64"}),
        ("export_model", {"scad_content": f'surface(file="{stem}.dat");',
                          "output_format": "stl"}),
        ("validate_scad", {"scad_content": f'import("{stem}.svg");'}),
    ]
    leaks = 0
    async with Client(endpoint) as c:
        for tool, args in programs:
            label = f"{tool}: {args['scad_content'][:46]}"
            try:
                res = await c.call_tool(tool, args)
                blob = _result_text(res)
            except Exception as exc:  # noqa: BLE001
                # A refusal raised as an error is a PASS as long as it carries no token.
                blob = f"{type(exc).__name__}: {exc}"
            leaked = token in blob or str(stem) in blob or str(Path.home()) in blob
            status = "LEAK!" if leaked else "no-leak"
            if leaked:
                leaks += 1
            print(f"  [{status:>7}] {label}")
    return leaks


async def _amain(endpoint: str) -> int:
    token = f"MH256CANARY{uuid.uuid4().hex}"
    stem = Path.home() / f".mh256_canary_{uuid.uuid4().hex}"
    made = []
    for suffix in (".svg", ".dxf", ".dat", ".stl"):
        p = stem.with_suffix(suffix)
        p.write_text(f"{token}\nsecond-line-{token}\n")
        made.append(p)
    try:
        await _wait_ready(endpoint)
        print(f"MH-256 canary against {endpoint}")
        print(f"  canary token: {token}")
        leaks = await _run_canary(endpoint, token, stem)
    finally:
        for p in made:
            try:
                p.unlink()
            except OSError:
                pass
    if leaks:
        print(f"\nRESULT: FAIL — {leaks} leak(s) detected")
        return 1
    print("\nRESULT: PASS — no canary content reached the caller")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="MH-256 canary SSE gate")
    ap.add_argument("endpoint", nargs="?", default=None,
                    help="SSE endpoint, e.g. http://localhost:9300/sse")
    ap.add_argument("--start", action="store_true",
                    help="start a temporary daemon from this checkout and test it")
    args = ap.parse_args()

    proc = None
    try:
        if args.start:
            port = _free_port()
            endpoint = f"http://127.0.0.1:{port}/sse"
            proc = _start_temp_server(port)
        else:
            endpoint = args.endpoint or DEFAULT_ENDPOINT
        return asyncio.run(_amain(endpoint))
    finally:
        if proc is not None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
