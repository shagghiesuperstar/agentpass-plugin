#!/usr/bin/env python3
"""Exercise the plugin's real code paths against a running MCP endpoint.

Run by ``test_plugin.mjs``; can also be run by hand:

    AGENTPASS_MCP_URL=http://127.0.0.1:8787/mcp python3 test_driver.py main

Phases
  main     handshake, discovery, scan (text + json), fix prompt, cache reuse,
           second MCP tool (get_report)
  retry    a 503 on the first tools/call is retried and still succeeds
  timeout  a slow server surfaces a timeout envelope instead of hanging/raising
  dead     a closed port surfaces a network error envelope instead of raising
  badurl   invalid input is rejected before any network call

Exit code 0 = every assertion in the phase passed.
"""

from __future__ import annotations

import json
import os
import sys

import tools
from mcp_client import McpHttpClient

ENDPOINT = os.environ.get("AGENTPASS_MCP_URL", "http://127.0.0.1:8787/mcp")
TARGET = "https://example.com"

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
        _failures.append(label)


def parse(payload: str) -> dict:
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:  # a non-JSON return is itself a bug
        print(f"  !! handler returned non-JSON: {exc}: {payload[:200]!r}")
        _failures.append("handler returned JSON")
        return {}


def phase_main() -> None:
    tools.configure(endpoint=ENDPOINT, timeout=45, max_retries=2, cache_ttl=600)

    print("\n[1] MCP handshake + tool discovery (raw client)")
    client = McpHttpClient(ENDPOINT, timeout=45, max_retries=2)
    handshake = client.ensure_initialized()
    server = handshake.get("serverInfo") or {}
    print(f"  initialize   → serverInfo={server.get('name')} v{server.get('version')} "
          f"protocol={handshake.get('protocolVersion')}")
    discovered = client.list_tools()
    print(f"  tools/list   → {', '.join(t['name'] for t in discovered)}")
    check("handshake negotiated a protocol version", bool(handshake.get("protocolVersion")))
    check("server advertises scan_site", any(t["name"] == "scan_site" for t in discovered))
    check("server advertises get_report", any(t["name"] == "get_report" for t in discovered))

    print("\n[2] agent_readiness_scan(url, format='text')")
    first = parse(tools.agent_readiness_scan({"url": TARGET, "format": "text"}))
    body = first.get("report") if isinstance(first.get("report"), str) else ""
    print(indent(body, "  "))
    meta = first.get("meta") or {}
    print(f"  envelope     → ok={first.get('ok')} score={first.get('score')} grade={first.get('grade')} "
          f"cached={meta.get('cached')} transport={meta.get('transport')} retries={meta.get('retries')}")
    check("text scan ok", first.get("ok") is True)
    check("report carries an ARI-100 score", isinstance(first.get("score"), (int, float)))
    check("text body has the domain table", "Domains" in body and "Reachability" in body)
    check("text body lists recoverable fixes", "Recoverable fixes" in body)
    check(
        "tools/call response parsed (sse or json)",
        meta.get("transport") in ("sse", "json"),
        f"{meta.get('transport')} — the stub answers tools/call as SSE, the hosted server as JSON",
    )

    print("\n[3] agent_readiness_scan(url, format='json')  → cache hit, no second crawl")
    second = parse(tools.agent_readiness_scan({"url": TARGET, "format": "json"}))
    report = second.get("report") or {}
    print(f"  envelope     → ok={second.get('ok')} format={second.get('format')} "
          f"cached={(second.get('meta') or {}).get('cached')}")
    print(f"  report keys  → {', '.join(sorted(report.keys())[:12])} …")
    print(f"  report shape  → domains={len(report.get('domains') or [])} "
          f"checks={len(report.get('checks') or [])} fixes={len(report.get('fixes') or [])} "
          f"crawlers={len(report.get('crawlers') or [])}")
    check("json scan ok", second.get("ok") is True)
    check("json body is the report object", isinstance(report, dict) and "domains" in report)
    check("second call served from cache", (second.get("meta") or {}).get("cached") is True)

    print("\n[4] agent_readiness_fix_prompt(url)")
    third = parse(tools.agent_readiness_fix_prompt({"url": TARGET}))
    prompt = third.get("prompt") or ""
    print(indent(first_lines(prompt, 16), "  "))
    print("  …")
    print(f"  envelope     → ok={third.get('ok')} items={third.get('items')} "
          f"chars={len(prompt)} cached={(third.get('meta') or {}).get('cached')}")
    check("fix prompt ok", third.get("ok") is True)
    check("prompt is ordered and numbered", "\n1. [" in prompt and "\n2. [" in prompt)
    check("prompt carries observed evidence", "Observed:" in prompt)
    check("prompt carries a verify step", "Verify: re-run check" in prompt)
    check("fix prompt reused the scan from cache", (third.get("meta") or {}).get("cached") is True)

    print("\n[5] second MCP tool: get_report {host} via the same client")
    call = client.call_tool_safe("get_report", {"host": "example.com"})
    cached_report = (call.get("structured") or {})
    print(f"  get_report   → ok={call.get('ok')} score={cached_report.get('score')} "
          f"url={cached_report.get('finalUrl')}")
    check("get_report returns the cached report", call.get("ok") is True and cached_report.get("url") is not None)

    print("\n[6] unknown tool → JSON-RPC error, surfaced not raised")
    missing = client.call_tool_safe("does_not_exist", {})
    print(f"  envelope     → ok={missing.get('ok')} kind={(missing.get('error') or {}).get('kind')} "
          f"message={(missing.get('error') or {}).get('message', '')[:80]}")
    check("unknown tool surfaces as an error envelope", missing.get("ok") is False)

    print(f"\n  transport stats → {json.dumps(client.stats)}")


def phase_retry() -> None:
    tools.configure(endpoint=ENDPOINT, timeout=10, max_retries=2, cache_ttl=600)
    print("\nStub answers the first tools/call with HTTP 503, then 200.")
    result = parse(tools.agent_readiness_scan({"url": TARGET}))
    meta = result.get("meta") or {}
    print(f"  envelope     → ok={result.get('ok')} retries={meta.get('retries')} score={result.get('score')}")
    check("retry recovered the scan", result.get("ok") is True)
    check("the retry counter moved", int(meta.get("retries") or 0) >= 1, str(meta.get("retries")))


def phase_timeout() -> None:
    tools.configure(endpoint=ENDPOINT, timeout=1, max_retries=0, cache_ttl=600)
    print("\nStub sleeps 3s; client timeout is 1s with retries off.")
    result = parse(tools.agent_readiness_scan({"url": TARGET}))
    error = result.get("error") or {}
    print(f"  envelope     → ok={result.get('ok')} kind={error.get('kind')} message={error.get('message', '')[:90]}")
    check("timeout returns an envelope (did not raise)", result.get("ok") is False)
    check("timeout is classified", error.get("kind") == "timeout", str(error.get("kind")))
    check("envelope carries a hint for the model", bool(result.get("hint")))


def phase_dead() -> None:
    tools.configure(endpoint="http://127.0.0.1:9/mcp", timeout=2, max_retries=1, cache_ttl=600)
    print("\nNothing listening on 127.0.0.1:9 (discard port).")
    result = parse(tools.agent_readiness_scan({"url": TARGET}))
    error = result.get("error") or {}
    print(f"  envelope     → ok={result.get('ok')} kind={error.get('kind')} message={error.get('message', '')[:90]}")
    check("dead endpoint returns an envelope (did not raise)", result.get("ok") is False)
    check("dead endpoint is classified as network", error.get("kind") in ("network", "timeout"), str(error.get("kind")))


def phase_badurl() -> None:
    print("\nNo scheme, no host.")
    result = parse(tools.agent_readiness_scan({"url": "   "}))
    error = result.get("error") or {}
    print(f"  envelope     → ok={result.get('ok')} kind={error.get('kind')} message={error.get('message', '')[:90]}")
    check("blank url is rejected before any network call", result.get("ok") is False and error.get("kind") == "input")


PHASES = {
    "main": phase_main,
    "retry": phase_retry,
    "timeout": phase_timeout,
    "dead": phase_dead,
    "badurl": phase_badurl,
}


def indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in (text or "").splitlines())


def first_lines(text: str, count: int) -> str:
    return "\n".join((text or "").splitlines()[:count])


def main() -> int:
    phase = sys.argv[1] if len(sys.argv) > 1 else "main"
    if phase not in PHASES:
        print(f"unknown phase {phase!r}; expected one of {', '.join(PHASES)}", file=sys.stderr)
        return 2
    PHASES[phase]()
    if _failures:
        print(f"\n  {len(_failures)} assertion(s) failed: {', '.join(_failures)}", file=sys.stderr)
        return 1
    print("  phase ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
