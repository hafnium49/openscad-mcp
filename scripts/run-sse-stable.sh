#!/bin/bash
# Emergency rollback runner for openscad-mcp.
#
# Mirrors /home/hafnium/mcp-mi-regression/scripts/run-sse-stable.sh: pins a
# Known-Good Commit (KGC), checks it out into a detached worktree, and runs
# the SSE daemon from there. Operators reach for this only when a fresh
# deploy has wedged the production server and a fast revert-to-safety is
# needed. Under normal operation, use run-sse.sh.
#
# To update the KGC (after new patches have been stable for N days), bump
# KGC_COMMIT below.
#
# Usage:
#   pkill -f 'fastmcp run src/openscad_mcp/server.py'
#   /home/hafnium/openscad-mcp/scripts/run-sse-stable.sh > /tmp/openscad_mcp.log 2>&1 &
#   disown
#
# Rollback-completed checklist:
#   1. ss -tlnp | grep :9300     # confirm fastmcp listening
#   2. sudo docker logs mkc-holmes-v3-backend 2>&1 | tail -5
#                                # confirm backend heartbeats are green
#   3. git -C /home/hafnium/openscad-mcp log -1 --oneline
#                                # confirm HEAD on disk is unchanged
#                                # (this script does NOT touch HEAD; it
#                                # only uses a detached worktree)
set -e

REPO="/home/hafnium/openscad-mcp"
# Last-known-good commit. f82287c0 = upstream HEAD d438b84 + Cowork Patch
# P1 (MCP_ALLOWED_PATHS env-wiring through Config.from_env). This is the
# first commit on feat/cowork-stdio that the v3 SSE daemon actually
# depends on (Patch P1 is what feeds the run-sse.sh sandbox env block).
KGC_COMMIT="${KGC_COMMIT:-f82287c0}"

WORKTREE="/tmp/openscad-mcp-kgc"

cd "$REPO"

# Idempotent detached worktree at KGC. If a prior rollback left a tree in
# place, refresh it to the configured KGC commit instead of failing.
if [ -d "$WORKTREE/.git" ] || [ -f "$WORKTREE/.git" ]; then
    git -C "$REPO" worktree prune
    if [ -d "$WORKTREE" ]; then
        git -C "$WORKTREE" fetch --all --prune 2>/dev/null || true
        git -C "$WORKTREE" checkout --detach "$KGC_COMMIT"
    fi
else
    git -C "$REPO" worktree add --force --detach "$WORKTREE" "$KGC_COMMIT"
fi

cd "$WORKTREE"

# --- X display pre-flight (same as run-sse.sh) ---------------------------
if ! pgrep -af 'Xvfb :99' >/dev/null; then
    nohup Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp >/tmp/xvfb-99.log 2>&1 &
    disown
    sleep 1
fi
export DISPLAY=:99

# --- Daemon environment (matches run-sse.sh) -----------------------------
export OPENSCAD_PATH="$(command -v openscad || echo /usr/bin/openscad)"
export MCP_TEMP_DIR="${MCP_TEMP_DIR:-/home/hafnium/openscad-mcp/.renders}"
mkdir -p "$MCP_TEMP_DIR"
mkdir -p "$WORKTREE/sample_parts"
export MCP_ALLOWED_PATHS="$MCP_TEMP_DIR:$WORKTREE/sample_parts"
export MCP_RENDER_TIMEOUT=120
export MCP_MAX_FILE_SIZE_MB=10
export MCP_RATE_LIMIT=60
export MCP_LOG_LEVEL=INFO
export MCP_LOG_FILE=/tmp/openscad_mcp.log
export PYTHONUNBUFFERED=1
# Transport/host/port for openscad-mcp's main() (see run-sse.sh for rationale).
export MCP_TRANSPORT=sse
export MCP_HOST=0.0.0.0
export MCP_PORT=9300

LOG_FILE="$MCP_LOG_FILE"
if [ -f "$LOG_FILE" ]; then
    mv "$LOG_FILE" "${LOG_FILE%.log}.$(date +%Y%m%d-%H%M%S).rollback.log"
fi

echo "[rollback] Starting OpenSCAD MCP server from KGC $KGC_COMMIT"
echo "[rollback] worktree=$WORKTREE"
echo "[rollback] OPENSCAD_PATH=$OPENSCAD_PATH"
echo "[rollback] MCP_TEMP_DIR=$MCP_TEMP_DIR"
echo "[rollback] LOG_FILE=$LOG_FILE"
echo "[rollback] To return to main deploy once fixed: pkill -f 'fastmcp run src/openscad_mcp/server.py' && $REPO/scripts/run-sse.sh"

exec >>"$LOG_FILE" 2>&1
uv run openscad-mcp
