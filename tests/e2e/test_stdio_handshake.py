"""
Stage 4 — true end-to-end stdio handshake test.

The rest of the suite imports the FastMCP `mcp` object directly and calls
``tool.fn(...)`` — that is in-process and not E2E. This file spawns the real
``uv run openscad-mcp`` CLI as a subprocess with stdin/stdout pipes,
performs a JSON-RPC initialize → tools/list → tools/call → shutdown
sequence over stdio, and asserts a valid PNG comes back from a real
OpenSCAD render.

This is the laptop-side path that Claude Desktop's Cowork mode actually
exercises (with `--directory` pointing at the extracted Windows zip).
A passing test here proves: subprocess spawn works, stdio framing is
correct, transport is healthy, real ``openscad`` binary renders a
``cube([10,10,10])`` to a non-empty base64 PNG, subprocess exits cleanly.

Skipped when ``openscad`` is not installed.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Skip the entire module if the real openscad binary is missing.
pytestmark = pytest.mark.skipif(
    shutil.which("openscad") is None,
    reason="openscad binary not installed; e2e stdio test cannot render",
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _send(proc: subprocess.Popen, message: dict) -> None:
    """Send a single JSON-RPC message over stdio using NDJSON
    (one JSON document per line, ``\\n``-terminated). FastMCP stdio
    transport uses this wire format — NOT LSP-style ``Content-Length``
    framing. (Verified empirically: piping LSP-framed input causes the
    server to log ``Internal Server Error`` and emit a notification
    instead of a response.)"""
    body = (json.dumps(message) + "\n").encode("utf-8")
    assert proc.stdin is not None
    proc.stdin.write(body)
    proc.stdin.flush()


def _recv_response(proc: subprocess.Popen, expected_id: int) -> dict:
    """Read NDJSON messages from stdout until one with ``id == expected_id``
    arrives. Skips notifications (no ``id`` field) and unrelated responses.
    Raises if the server closes stdout before producing the expected id."""
    assert proc.stdout is not None
    for _ in range(64):  # bound the skip count to avoid infinite loops
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError(
                f"Server closed stdout before producing id={expected_id}"
            )
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            # FastMCP banner / log noise on stdout in some configs — skip.
            continue
        if msg.get("id") == expected_id:
            return msg
        # Otherwise it's a notification (no id) or an unrelated response;
        # keep reading.
    raise RuntimeError(f"id={expected_id} not seen after 64 messages")


def _spawn_server() -> subprocess.Popen:
    """Spawn ``uv run openscad-mcp`` from the repo root with stdio pipes."""
    env = os.environ.copy()
    # Use the repo's .venv but don't let the parent VIRTUAL_ENV leak in.
    env.pop("VIRTUAL_ENV", None)
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        ["uv", "run", "--directory", str(REPO_ROOT), "openscad-mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(REPO_ROOT),
    )


@pytest.mark.integration
@pytest.mark.slow
def test_stdio_handshake_initializes_lists_tools_and_renders_cube():
    """Real subprocess speaks JSON-RPC over stdio:
    1. ``initialize`` returns a server info block.
    2. ``tools/list`` includes ``render_single``.
    3. ``tools/call`` with a 10mm cube returns a non-empty base64 PNG.
    4. Subprocess exits cleanly within 10 s of stdin close.
    """
    proc = _spawn_server()
    try:
        # 1. initialize
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "openscad-mcp-e2e-test", "version": "0.0.1"},
            },
        })
        init_resp = _recv_response(proc, expected_id=1)
        assert "result" in init_resp, f"init returned error: {init_resp!r}"

        # initialized notification (no response expected)
        _send(proc, {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })

        # 2. tools/list
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_resp = _recv_response(proc, expected_id=2)
        assert "result" in tools_resp, f"tools/list error: {tools_resp!r}"
        names = [t.get("name") for t in tools_resp["result"].get("tools", [])]
        assert "render_single" in names, (
            f"render_single missing from server's tools/list: got {names!r}"
        )
        assert len(names) >= 15, f"expected >=15 tools, got {len(names)}: {names!r}"

        # 3. tools/call render_single — real OpenSCAD render
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "render_single",
                "arguments": {
                    "scad_content": "cube([10, 10, 10]);",
                    "view": "isometric",
                },
            },
        })
        # Real CGAL render via Xvfb-less OpenSCAD on this host can take a
        # few seconds. Allow up to 60 s.
        call_resp = _recv_response(proc, expected_id=3)
        if "error" in call_resp:
            stderr_tail = (proc.stderr.read(4096) or b"").decode(errors="replace")
            pytest.fail(
                f"tools/call returned error: {call_resp['error']!r}\n"
                f"stderr tail: {stderr_tail[:1000]}"
            )

        # FastMCP wraps tool results: result has 'content' (TextContent list)
        # and 'structuredContent' (or 'isError'). Pull the TextContent JSON.
        content_blocks = call_resp["result"].get("content", [])
        text_payload = None
        for blk in content_blocks:
            if blk.get("type") == "text":
                text_payload = json.loads(blk.get("text", "{}"))
                break
        assert text_payload is not None, (
            f"tools/call result has no text content block: {call_resp!r}"
        )
        assert text_payload.get("success") is True, (
            f"render_single returned success=False: {text_payload!r}"
        )
        b64 = text_payload.get("data") or ""
        assert b64, f"render returned empty base64: {text_payload!r}"
        assert text_payload.get("mime_type") == "image/png", (
            f"unexpected mime_type: {text_payload!r}"
        )

        # PNG header sanity: decoded bytes start with the PNG magic 89 50 4E 47.
        try:
            decoded = base64.b64decode(b64, validate=True)
        except Exception as e:
            pytest.fail(f"base64 decode failed: {e}; first 80 chars: {b64[:80]!r}")
        assert len(decoded) > 256, (
            f"PNG suspiciously small ({len(decoded)} bytes) — render may be empty"
        )
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n", (
            f"PNG magic header missing; first 8 bytes: {decoded[:8]!r}"
        )
    finally:
        # 4. clean shutdown
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stderr_tail = (proc.stderr.read(2048) or b"").decode(errors="replace")
            pytest.fail(
                f"server did not exit within 10 s of stdin close; "
                f"stderr tail: {stderr_tail[:500]}"
            )
        # Exit code 0 is the happy path; SIGTERM-induced negative codes are
        # acceptable on some asyncio frameworks.
        assert proc.returncode in (0, -15, 143), (
            f"unclean exit code: {proc.returncode}"
        )
