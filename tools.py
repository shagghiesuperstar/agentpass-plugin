"""Tool handlers for the AgentPass plugin.

Two model-facing tools, both thin: resolve config, ask the MCP server, render.
Every handler follows the Hermes contract — accept ``(args: dict, **kwargs)``,
always return a JSON string, never raise.

Envelope on success::

    {"ok": true, "tool": "...", "url": "...", "score": 82.4, "grade": "B",
     "report": "<text or object>", "meta": {"cached": false, ...}}

Envelope on failure::

    {"ok": false, "tool": "...", "error": {"kind": "network", "message": "..."},
     "meta": {"endpoint": "...", "attempts": 3}}
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:  # imported as part of the plugin package
    from . import report as report_mod
    from .mcp_client import McpError, McpHttpClient
except ImportError:  # imported standalone (tests, REPL)
    import report as report_mod  # type: ignore[no-redef]
    from mcp_client import McpError, McpHttpClient  # type: ignore[no-redef]

# Where the plugin looks when nothing is configured. The service is small and the
# endpoint moves, so the plugin tries each in order and remembers the one that
# answered -- a freshly installed copy should never need an env var before its
# first call works. Most durable host goes last, not first: a dead host first
# costs a timeout on every cold start.
DEFAULT_ENDPOINTS = (
    "https://sugar-clock-push-accessibility.trycloudflare.com/mcp",
    "https://scooby.tail2eb5d4.ts.net/mcp",
    "https://agentpass.dev/mcp",
)
DEFAULT_ENDPOINT = DEFAULT_ENDPOINTS[0]

# Runtime configuration. `register(ctx)` overwrites this from plugin settings
# (plugins.entries.agentpass.settings.*); env vars are the fallback so the
# module is usable outside Hermes (tests, scripts).
_CFG: dict[str, Any] = {
    # empty means "auto-resolve from DEFAULT_ENDPOINTS on first call"
    "endpoint": os.environ.get("AGENTPASS_MCP_URL", ""),
    "timeout": float(os.environ.get("AGENTPASS_TIMEOUT", "45")),
    "max_retries": int(os.environ.get("AGENTPASS_MAX_RETRIES", "2")),
    "cache_ttl": float(os.environ.get("AGENTPASS_CACHE_TTL", "600")),
}

_LAST_RESOLVE_ERROR: list[Exception | None] = [None]

_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()


def configure(**overrides: Any) -> dict:
    """Update runtime config. Unknown keys are ignored; bad values fall back."""
    for key in ("endpoint", "timeout", "max_retries", "cache_ttl"):
        if key not in overrides or overrides[key] in (None, ""):
            continue
        try:
            if key == "endpoint":
                endpoint = str(overrides[key]).strip()
                if endpoint.lower().startswith(("http://", "https://")):
                    _CFG["endpoint"] = endpoint
            elif key == "max_retries":
                _CFG[key] = max(0, min(int(overrides[key]), 5))
            else:
                _CFG[key] = max(1.0, float(overrides[key]))
        except (TypeError, ValueError):
            continue
    return dict(_CFG)


def get_config() -> dict:
    return dict(_CFG)


# --------------------------------------------------------------------- helpers


def normalise_url(raw: Any) -> str:
    """Accept 'example.com', 'example.com/path' or a full URL. Raise on junk."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("`url` is required (for example https://example.com)")
    text = raw.strip()
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"`url` must be an http(s) URL or a bare host, got {raw!r}")
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _host_key(url: str) -> str:
    return (urlsplit(url).hostname or url).lower()


def _client() -> McpHttpClient:
    return McpHttpClient(_resolve_endpoint(), timeout=_CFG["timeout"], max_retries=int(_CFG["max_retries"]))


def _resolve_endpoint() -> str:
    """Return an endpoint that answers, or the configured one if none does.

    An explicitly configured endpoint is honoured as-is (the operator said so).
    Otherwise the defaults are probed once with a short timeout and the winner is
    remembered for the life of the process.
    """
    configured = str(_CFG.get("endpoint") or "").strip()
    if configured:
        return configured
    last_error: Exception | None = None
    for candidate in DEFAULT_ENDPOINTS:
        try:
            client = McpHttpClient(candidate, timeout=min(6.0, float(_CFG["timeout"])), max_retries=0)
            client.list_tools()
            _CFG["endpoint"] = candidate
            return candidate
        except Exception as exc:  # network, DNS, HTTP, JSON-RPC -- all mean "try the next one"
            last_error = exc
            continue
    _CFG["endpoint"] = DEFAULT_ENDPOINTS[0]
    _LAST_RESOLVE_ERROR[0] = last_error
    return DEFAULT_ENDPOINTS[0]


def _cache_get(url: str) -> dict | None:
    key = _host_key(url)
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        if time.monotonic() - entry["at"] > _CFG["cache_ttl"]:
            _CACHE.pop(key, None)
            return None
        return entry


def _cache_put(url: str, report: dict) -> None:
    with _CACHE_LOCK:
        _CACHE[_host_key(url)] = {"at": time.monotonic(), "report": report}
        if len(_CACHE) > 32:  # bounded: drop the oldest entry
            oldest = min(_CACHE, key=lambda k: _CACHE[k]["at"])
            _CACHE.pop(oldest, None)


def _extract_report(call: dict) -> dict | None:
    """Find the report in an MCP tools/call result.

    Prefers ``structuredContent`` (the spec's machine-readable field), then
    parses the text content parts as JSON. Returns None when neither yields an
    object.
    """
    structured = call.get("structured")
    if isinstance(structured, dict):
        # Some servers wrap it: {"report": {...}} / {"data": {...}}
        for key in ("report", "data", "result"):
            inner = structured.get(key)
            if isinstance(inner, dict):
                return inner
        if structured.get("score") is not None or structured.get("checks"):
            return structured

    text = (call.get("text") or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, dict):
        for key in ("report", "data", "result"):
            inner = parsed.get(key)
            if isinstance(inner, dict):
                return inner
        return parsed
    return None


def _error_envelope(tool: str, exc: McpError | Exception, *, url: str | None = None) -> dict:
    if isinstance(exc, McpError):
        error = exc.to_dict()
        hint = {
            "network": "The MCP endpoint could not be reached. Check the network or set AGENTPASS_MCP_URL.",
            "timeout": "The endpoint accepted the connection but did not answer in time. A scan of a large site can take a while; raise timeout_seconds.",
            "http": "The endpoint returned an HTTP error. 401/403 usually means AGENTPASS_API_KEY is missing or wrong.",
            "rpc": "The MCP server rejected the tool call. The message is the server's own.",
            "protocol": "The endpoint answered, but not with JSON-RPC over streamable HTTP.",
            "config": "The configured endpoint is not an absolute http(s) URL.",
        }.get(exc.kind)
    else:
        error = {"kind": "input" if isinstance(exc, ValueError) else "unexpected", "message": str(exc)}
        hint = None
    envelope: dict[str, Any] = {"ok": False, "tool": tool, "error": error}
    if url:
        envelope["url"] = url
    if hint:
        envelope["hint"] = hint
    envelope["meta"] = {"endpoint": _CFG["endpoint"]}
    return envelope


def _scan(url: str) -> tuple[dict, dict]:
    """Scan via MCP, with a cache hit short-circuit. Returns (report, meta)."""
    cached = _cache_get(url)
    if cached:
        return cached["report"], {"cached": True, "endpoint": _CFG["endpoint"], "age_s": round(time.monotonic() - cached["at"], 1)}

    client = _client()
    call = client.call_tool_safe("scan_site", {"url": url})
    meta: dict[str, Any] = {
        "cached": False,
        "endpoint": _CFG["endpoint"],
        "duration_ms": call.get("duration_ms"),
        "retries": (call.get("stats") or {}).get("retries", 0),
        "transport": "sse" if (call.get("stats") or {}).get("sse_responses") else "json",
    }
    if not call.get("ok"):
        error = call.get("error") or {"kind": "unknown", "message": "scan_site failed without detail"}
        raise McpError(str(error.get("message") or "scan_site failed"), kind=str(error.get("kind") or "unknown"))

    report = _extract_report(call)
    if report is None:
        snippet = (call.get("text") or "")[:300]
        raise McpError(
            f"scan_site returned no report object (content: {snippet!r})", kind="protocol", method="tools/call"
        )
    if report.get("error") and not report.get("score"):
        raise McpError(str(report["error"]), kind="rpc", method="tools/call")

    _cache_put(url, report)
    meta["server"] = report.get("engine")
    return report, meta


# ---------------------------------------------------------------------- tools


def agent_readiness_scan(args: dict, **kwargs: Any) -> str:
    """Scan a URL for agent readiness (ARI-100) and return the report."""
    del kwargs
    tool = "agent_readiness_scan"
    url: str | None = None
    try:
        args = args or {}
        url = normalise_url(args.get("url"))
        fmt = str(args.get("format") or "text").strip().lower()
        if fmt not in ("text", "json"):
            fmt = "text"

        report_obj, meta = _scan(url)


        body: Any = report_obj if fmt == "json" else report_mod.render_text(report_obj)
        return json.dumps(
            {
                "ok": True,
                "tool": tool,
                "url": report_obj.get("finalUrl") or url,
                "score": report_obj.get("score"),
                "grade": report_obj.get("grade"),
                "format": fmt,
                "report": body,
                "meta": meta,
            },
            ensure_ascii=False,
        )
    except (McpError, ValueError) as exc:
        return json.dumps(_error_envelope(tool, exc, url=url), ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - never raise into the agent loop
        return json.dumps(_error_envelope(tool, exc, url=url), ensure_ascii=False)


def agent_readiness_fix_prompt(args: dict, **kwargs: Any) -> str:
    """Return an ordered, paste-ready remediation prompt for a URL."""
    del kwargs
    tool = "agent_readiness_fix_prompt"
    url: str | None = None
    try:
        args = args or {}
        url = normalise_url(args.get("url"))
        try:
            max_items = int(args.get("max_items") or 12)
        except (TypeError, ValueError):
            max_items = 12
        max_items = max(1, min(max_items, 40))

        report_obj, meta = _scan(url)
        prompt = report_mod.prompt_pack(report_obj, max_items=max_items)
        fixes = report_obj.get("fixes") or []
        return json.dumps(
            {
                "ok": True,
                "tool": tool,
                "url": report_obj.get("finalUrl") or url,
                "score": report_obj.get("score"),
                "grade": report_obj.get("grade"),
                "items": min(len(fixes), max_items),
                "prompt": prompt,
                "meta": meta,
            },
            ensure_ascii=False,
        )
    except (McpError, ValueError) as exc:
        return json.dumps(_error_envelope(tool, exc, url=url), ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps(_error_envelope(tool, exc, url=url), ensure_ascii=False)
