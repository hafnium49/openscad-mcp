"""SSE smoke test for the VM-hosted OpenSCAD MCP daemon.

Operational gate: this script is the manual pre-deploy smoke test, run by
the daemon-restart operator (currently Fujiwara-san) after every commit on
``feat/cowork-stdio`` that touches ``src/openscad_mcp/``. It is NOT
auto-discovered by pytest (no ``test_`` prefix), so adding it doesn't
slow the unit/integration suite. CI integration is a deferred follow-up.

Mirrors the plan's smoke-test idiom (MKC_Holmes_OpenSCAD_MCP_Cowork_Distribution_Plan.md
Stage 3, lines 91-104) but uses the Streamable SSE Client against the live :9300/sse
endpoint instead of an in-memory Client(mcp). Exercises all 15 tools end-to-end.

After the B1/B2/B3 fixes deploy, the in-line ``# Workaround:`` comments below
should be removed and the assertions tightened — see
docs/openscad_mcp_bug_report.md §6-§7 for the acceptance gate.

Run:
    uv run --with fastmcp python smoke-test-sse.py [endpoint]

Default endpoint: http://172.24.34.90:9300/sse
"""

import asyncio
import base64
import sys
import time
from typing import Any

from fastmcp import Client

DEFAULT_ENDPOINT = "http://172.24.34.90:9300/sse"
CUBE = "$fn=32; cube([10,10,10]);"
SPHERE = "$fn=32; sphere(r=10);"

# ---------- helpers ----------

class Result:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.duration_ms = 0
        self.detail = ""
        self.error: str | None = None


def _data_of(call_result: Any) -> Any:
    """FastMCP Client may return either a CallToolResult with .data or raw structured
    content depending on version. Normalise to a Python object."""
    if hasattr(call_result, "data") and call_result.data is not None:
        return call_result.data
    if hasattr(call_result, "structured_content") and call_result.structured_content:
        return call_result.structured_content
    if hasattr(call_result, "content") and call_result.content:
        return call_result.content
    return call_result


async def run(test_name: str, coro):
    r = Result(test_name)
    t0 = time.perf_counter()
    try:
        detail = await coro
        r.passed = True
        r.detail = detail or ""
    except AssertionError as e:
        r.error = f"AssertionError: {e}"
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}"
    r.duration_ms = int((time.perf_counter() - t0) * 1000)
    status = "PASS" if r.passed else "FAIL"
    line = f"  [{status}] {r.name:<28} ({r.duration_ms:>5} ms)"
    if r.detail:
        line += f"  {r.detail}"
    if r.error:
        line += f"  -> {r.error}"
    print(line)
    return r


# ---------- per-tool tests ----------

async def t_check_openscad(c):
    d = _data_of(await c.call_tool("check_openscad", {"include_paths": True}))
    assert d.get("installed") is True, f"installed != True: {d}"
    assert "version" in d, f"no version field: {d}"
    return f"v={d['version']!s:<26} path={d.get('path','?')}"

async def t_get_libraries(c):
    d = _data_of(await c.call_tool("get_libraries", {}))
    assert d.get("success") is True, d
    libs = d.get("libraries", [])
    return f"{len(libs)} libs: {[l['name'] for l in libs]}"

async def t_list_models(c):
    d = _data_of(await c.call_tool("list_models", {}))
    assert d.get("success") is True, d
    return f"count={d.get('count', 0)}"

async def t_validate_scad(c):
    # Post-fix (B1, 2026-05-07): validate_scad now returns valid:true for
    # valid SCAD. Pre-fix this assertion would have failed because OpenSCAD
    # 2021.01 rejected `-o /dev/null` before parsing.
    d = _data_of(await c.call_tool("validate_scad", {
        "scad_content": "echo(\"ok\"); $fn=32; cube([10,10,10]);",
    }))
    assert d.get("success") is True, d
    assert d.get("valid") is True, f"expected valid=True post-B1-fix; got {d}"
    return f"valid=True (B1 fix verified)"

async def t_render_single(c):
    # Post-fix (B2, 2026-05-07): quality:"draft" now passes the variable
    # validator. The widened regex accepts $-prefixed names; values for
    # known special vars are bounded to mitigate DoS.
    d = _data_of(await c.call_tool("render_single", {
        "scad_content": CUBE,
        "view": "isometric",
        "quality": "draft",
        "output_format": "file_path",
    }))
    assert d.get("success") is True, d
    assert d.get("type") == "file_path", d
    assert d.get("mime_type") == "image/png", d
    assert d.get("path", "").endswith(".png"), d
    return f"path={d['path'].rsplit('/',1)[-1]} quality=draft (B2 fix verified)"

async def t_render_perspectives(c):
    # B3 attribution: pre-fix, passing `views=[...]` over SSE+mcp-remote
    # (Claude Desktop on Windows) failed with Pydantic list_type validation.
    # Stage-2 diagnosis on 2026-05-07 found the bug is in mcp-remote's
    # JSON-stringification of nested arrays — NOT in openscad-mcp or in
    # FastMCP's SSE handler. Calling via fastmcp.Client (this script)
    # accepts the native list correctly. This call exercises the
    # working-baseline path and serves as the smoke gate.
    d = _data_of(await c.call_tool("render_perspectives", {
        "scad_content": CUBE,
        "views": ["front", "top", "isometric"],
        "output_format": "file_path",
    }))
    assert d.get("success") is True, d
    views = d.get("views", {})
    assert len(views) == 3, f"expected exactly 3 views, got: {sorted(views)}"
    for name, view in views.items():
        assert view.get("type") == "file_path", f"view {name} not file_path: {view}"
        assert view.get("mime_type") == "image/png", view
    return f"{len(views)} views: {sorted(views)} (B3 baseline verified)"

async def t_compare_renders(c):
    # Post-fix (B2, 2026-05-07): default quality "draft" now succeeds —
    # no more "quality=normal" workaround needed.
    d = _data_of(await c.call_tool("compare_renders", {
        "scad_content_before": CUBE,
        "scad_content_after":  SPHERE,
        "view": "isometric",
    }))
    assert d.get("success") is True, d
    before = d.get("before", {})
    after  = d.get("after", {})
    # Inline base64 — verify both decode to PNG magic bytes
    for label, blob in (("before", before), ("after", after)):
        b64 = blob.get("data", "")
        assert b64, f"{label}: no data"
        png = base64.b64decode(b64)
        assert png.startswith(b"\x89PNG\r\n\x1a\n"), f"{label}: not PNG ({png[:8]!r})"
        assert len(png) > 256, f"{label}: PNG too small ({len(png)} B)"
    return f"before={len(base64.b64decode(before['data']))} B  after={len(base64.b64decode(after['data']))} B"

async def t_analyze_model(c):
    d = _data_of(await c.call_tool("analyze_model", {"scad_content": CUBE}))
    assert d.get("success") is True, d
    bb = d["bounding_box"]
    assert bb["min"] == [0,0,0] and bb["max"] == [10,10,10], bb
    assert d["dimensions"] == {"width":10,"height":10,"depth":10}, d["dimensions"]
    assert d["triangle_count"] == 12, d["triangle_count"]
    return f"bbox={bb['min']}->{bb['max']}  tris={d['triangle_count']}"

async def t_export_model(c):
    d = _data_of(await c.call_tool("export_model", {
        "scad_content": CUBE,
        "output_format": "stl",
    }))
    assert d.get("success") is True, d
    assert d.get("format") == "stl", d
    assert d.get("file_size_bytes", 0) > 100, d
    return f"stl={d['file_size_bytes']} B  path={d['output_path'].rsplit('/',1)[-1]}"

async def t_get_project_files(c):
    d = _data_of(await c.call_tool("get_project_files", {
        "project_dir": "/home/hafnium/openscad-mcp/sample_parts",
    }))
    assert d.get("success") is True, d
    return f"files={len(d.get('files', []))}"

# CRUD chain — sequential
MODEL_NAME = "smoke_test_sse"

async def t_create_model(c):
    d = _data_of(await c.call_tool("create_model", {
        "name": MODEL_NAME,
        "content": CUBE,
    }))
    assert d.get("success") is True, d
    assert d.get("name") == f"{MODEL_NAME}.scad", d
    return f"name={d['name']}"

async def t_get_model(c):
    d = _data_of(await c.call_tool("get_model", {"name": MODEL_NAME}))
    assert d.get("success") is True, d
    assert d.get("content") == CUBE, f"content roundtrip failed: {d.get('content')!r}"
    return f"size_bytes={d.get('size_bytes')}  content_match=True"

async def t_update_model(c):
    d = _data_of(await c.call_tool("update_model", {
        "name": MODEL_NAME,
        "content": SPHERE,
    }))
    assert d.get("success") is True, d
    # Verify by re-reading
    d2 = _data_of(await c.call_tool("get_model", {"name": MODEL_NAME}))
    assert d2.get("content") == SPHERE, f"update roundtrip failed: {d2.get('content')!r}"
    return f"new_content_verified=True"

async def t_delete_model(c):
    d = _data_of(await c.call_tool("delete_model", {"name": MODEL_NAME}))
    assert d.get("success") is True, d
    return f"deleted={d.get('name')}"

async def t_clear_cache(c):
    d = _data_of(await c.call_tool("clear_cache", {}))
    assert d.get("success") is True, d
    return f"cleared={d.get('cleared_files', 0)} files  freed={d.get('freed_bytes', 0)} B"


# ---------- driver ----------

async def main(endpoint: str) -> int:
    print(f"Endpoint: {endpoint}")
    print(f"Started:  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 78)

    results: list[Result] = []
    async with Client(endpoint) as c:
        # Connection sanity
        tools = await c.list_tools()
        names = sorted(t.name for t in tools)
        print(f"Connected. {len(tools)} tools advertised:")
        for n in names:
            print(f"  - {n}")
        print("-" * 78)
        print("Per-tool tests:")

        # Independent reads/renders (sequential here for tidy output, but could be parallel)
        for name, coro in [
            ("check_openscad",      t_check_openscad(c)),
            ("get_libraries",       t_get_libraries(c)),
            ("list_models",         t_list_models(c)),
            ("validate_scad",       t_validate_scad(c)),
            ("render_single",       t_render_single(c)),
            ("render_perspectives", t_render_perspectives(c)),
            ("compare_renders",     t_compare_renders(c)),
            ("analyze_model",       t_analyze_model(c)),
            ("export_model",        t_export_model(c)),
            ("get_project_files",   t_get_project_files(c)),
        ]:
            results.append(await run(name, coro))

        # CRUD chain — strict sequential
        results.append(await run("create_model", t_create_model(c)))
        results.append(await run("get_model",    t_get_model(c)))
        results.append(await run("update_model", t_update_model(c)))
        results.append(await run("delete_model", t_delete_model(c)))
        results.append(await run("clear_cache",  t_clear_cache(c)))

    print("-" * 78)
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total_ms = sum(r.duration_ms for r in results)
    print(f"Summary: {passed}/{len(results)} passed, {failed} failed, "
          f"{total_ms} ms total ({total_ms/len(results):.0f} ms/tool avg)")

    if failed:
        print("\nFailures:")
        for r in results:
            if not r.passed:
                print(f"  - {r.name}: {r.error}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    endpoint = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ENDPOINT
    sys.exit(asyncio.run(main(endpoint)))
