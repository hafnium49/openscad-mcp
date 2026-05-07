#!/bin/bash
# Run openscad-mcp as SSE server on port 9300
# Used by MKC-Holmes v3 backend to connect via MCPServerSse
#
# Mirrors /home/hafnium/mcp-mi-regression/scripts/run-sse.sh (port 9100) and
# /home/hafnium/physics-ai-workshop/scripts/run-mcp-sse.sh (port 9200) so all
# three MCP daemons share one operational shape: nohup-backgrounded, no
# systemd, /tmp log, exec-into-fastmcp for clean signal handling.
#
# Cowork-laptop stdio distribution (dist/openscad-mcp-windows.zip on the
# mkc-holmes side) uses its own `uv run openscad-mcp` entrypoint and is
# unaffected by this script.
set -e

cd /home/hafnium/openscad-mcp

# --- X display pre-flight (disown-safe) ----------------------------------
# OpenSCAD's Linux build links Qt's QApplication even for headless
# `openscad -o output.png input.scad` rendering, so subprocess.run in
# server.py:398 needs DISPLAY pointing at a live X server. We launch Xvfb
# on :99 if it isn't already running. nohup + disown so the X server
# survives `pkill -f fastmcp` (which terminates this wrapper shell).
if ! pgrep -af 'Xvfb :99' >/dev/null; then
    nohup Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp >/tmp/xvfb-99.log 2>&1 &
    disown
    sleep 1
fi
export DISPLAY=:99

# --- Daemon environment --------------------------------------------------
# Resolve OpenSCAD binary at script start so the daemon doesn't have to
# re-probe per-render. Falls back to the canonical apt install path.
export OPENSCAD_PATH="$(command -v openscad || echo /usr/bin/openscad)"

# Render output and the Patch P1 sandbox. .renders/ is gitignored upstream;
# sample_parts/ is an empty placeholder for a future curated SCAD library
# (see docs/MKC_Holmes_OpenSCAD_MCP_Cowork_Distribution_Plan.md "Out of
# scope" #6). Both directories must exist before the daemon binds the port.
export MCP_TEMP_DIR="${MCP_TEMP_DIR:-/home/hafnium/openscad-mcp/.renders}"
mkdir -p "$MCP_TEMP_DIR"
mkdir -p /home/hafnium/openscad-mcp/sample_parts

# Patch P1 (commit f82287c0) wires this through Config.from_env() →
# SecurityConfig.allowed_paths → render_scad_to_png. Note: covers the
# scad_file argument and -I include_paths only; inline scad_content
# include/use/surface is NOT inspected by the validator (review-surfaced
# limitation; see Risks table in the plan doc).
export MCP_ALLOWED_PATHS="$MCP_TEMP_DIR:/home/hafnium/openscad-mcp/sample_parts"

# Daemon-tuning knobs. Render timeout 120 s is well under the upstream
# default 300 s — fail fast on stuck CGAL kernels under concurrent load.
# Rate-limit is declared in upstream config but not yet enforced; set
# anyway so a future upstream wiring picks it up at no extra cost.
export MCP_RENDER_TIMEOUT=120
export MCP_MAX_FILE_SIZE_MB=10
export MCP_RATE_LIMIT=60
export MCP_LOG_LEVEL=INFO

# Log path lives under /tmp, deliberately OUTSIDE MCP_ALLOWED_PATHS
# (security review #4): a SCAD `include </tmp/openscad_mcp.log>` would
# otherwise pass the prefix-based sandbox check and leak prior-render
# stderr fragments through OpenSCAD's parse-error message.
export MCP_LOG_FILE=/tmp/openscad_mcp.log
export PYTHONUNBUFFERED=1

# Transport / host / port consumed by openscad_mcp.server.main() at startup.
# Unlike mcp-mi-regression (which uses absolute imports and can be loaded
# directly via `fastmcp run server.py`), openscad-mcp uses relative imports
# (e.g. `from .types import ...`) and MUST be invoked through its
# pyproject.toml [project.scripts] entry point `openscad-mcp`. The entry
# point's main() reads these env vars instead of CLI flags.
export MCP_TRANSPORT=sse
export MCP_HOST=0.0.0.0
export MCP_PORT=9300

# --- Log file ownership --------------------------------------------------
# Matches mcp-mi-regression's pattern: rotate the prior session's log on
# restart so post-mortem of the previous run survives, then redirect this
# shell's stdout/stderr into the live log so FastMCP's startup banners and
# uvicorn access lines join the structured JSON the server emits itself.
LOG_FILE="$MCP_LOG_FILE"
if [ -f "$LOG_FILE" ]; then
    mv "$LOG_FILE" "${LOG_FILE%.log}.$(date +%Y%m%d-%H%M%S).log"
fi

echo "Starting OpenSCAD MCP server on port 9300..."
echo "OPENSCAD_PATH=$OPENSCAD_PATH"
echo "DISPLAY=$DISPLAY"
echo "MCP_TEMP_DIR=$MCP_TEMP_DIR"
echo "MCP_ALLOWED_PATHS=$MCP_ALLOWED_PATHS"
echo "LOG_FILE=$LOG_FILE"

# exec replaces this shell with the openscad-mcp entry point so SIGTERM
# (e.g. pkill -f) reaches the Python process directly. stdout/stderr
# append into the same log file. Transport/host/port are read from the
# env vars exported above.
exec >>"$LOG_FILE" 2>&1
uv run openscad-mcp
