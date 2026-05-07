"""Defensive coercion for tool-call arguments that arrive JSON-stringified.

Some clients' tool-call argument encoders ``JSON.stringify`` array values
before handing them to the JSON-RPC layer (Claude Desktop on Windows is
the observed reproduction path; the encoder itself has not been directly
inspected). The openscad-mcp SSE daemon then sees ``views=["a","b"]``
arrive as the literal string ``'["a","b"]'`` and fails the
``Optional[List[str]]`` Pydantic check. Direct ``fastmcp.Client`` calls
bypass that encoder and work correctly.

This module's helper is applied via ``Annotated[..., BeforeValidator(...)]``
at the FastMCP tool boundary so Pydantic re-parses the string before
list-type validation runs. mcp-remote v0.1.38 was empirically ruled out
as the cause (read end-to-end on 2026-05-07: ``mcpProxy`` forwarder is a
pure pass-through and the ``CallToolRequestParamsSchema`` declares
``arguments: record(string, unknown)``).
"""
from __future__ import annotations

import json
from typing import Any

# 64 KB ceiling — defends against a multi-MB log line being JSON.parsed
# by accident (Security review F8).
_MAX_REPARSE_LEN = 64 * 1024


def coerce_jsonish_list(
    value: Any,
    *,
    expected_item_type: type | None = None,
) -> Any:
    """If value is a string that looks like a JSON-encoded array, parse it.

    Returns the parsed list on success, or the original value unchanged
    if parsing fails or the input is not a string. **Never raises** —
    Pydantic's downstream list-type validator runs after this and
    produces the canonical type error if the value still isn't a list
    of the expected shape.

    The default ``expected_item_type=None`` accepts any item types and
    defers per-item type-checking to Pydantic. For example, with
    ``views: List[str]`` and a payload of ``'[1,2,3]'``, the helper
    parses to ``[1,2,3]`` and Pydantic produces a clean per-item
    error like ``views.0: Input should be a valid string`` — better
    failure mode than the helper silently rejecting and Pydantic
    producing the less-informative ``Input should be a valid list``.

    A non-None ``expected_item_type`` makes the helper "fail closed":
    if any element fails the isinstance check, the original string is
    returned unchanged. Most call sites should leave this at the
    default.

    Caught exception set explicitly includes ``RecursionError`` because
    deeply nested JSON like ``'[' * 1500 + ']' * 1500`` (3000 chars,
    well under the 64 KB cap) trips CPython's default recursion limit
    inside ``json.loads``. Without that catch the worker thread
    stack-exhausts and Pydantic's own recursive ``model_validate`` may
    re-trip recursion at unrelated call sites. (Security review Q1.)
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s.startswith("[") or not s.endswith("]"):
        return value
    if len(s) > _MAX_REPARSE_LEN:
        return value
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return value
    if not isinstance(parsed, list):
        return value
    if expected_item_type is not None and not all(
        isinstance(x, expected_item_type) for x in parsed
    ):
        return value
    return parsed


__all__ = ["coerce_jsonish_list"]
