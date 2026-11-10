#!/usr/bin/env python3
"""Prove the plugin loads and is callable through Hermes itself.

Not an import test: this creates a throwaway HERMES_HOME, installs the plugin
into ``<home>/plugins/agentpass``, enables it in ``config.yaml``, runs Hermes's
own ``PluginManager.discover_and_load()``, and then invokes each tool through
``tools.registry.dispatch`` — the same call path the agent loop uses.

Requires a reachable MCP endpoint in ``AGENTPASS_MCP_URL`` (test_plugin.mjs
supplies the stub).

    HERMES_HOME=/tmp/x AGENTPASS_MCP_URL=http://127.0.0.1:PORT/mcp python3 test_hermes_load.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        _failures.append(label)


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="hermes-home-agentpass-"))
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    target = plugins_dir / "agentpass"
    shutil.copytree(PLUGIN_DIR, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - agentpass\n", encoding="utf-8"
    )
    os.environ["HERMES_HOME"] = str(home)

    endpoint = os.environ.get("AGENTPASS_MCP_URL", "http://127.0.0.1:8787/mcp")
    os.environ["AGENTPASS_MCP_URL"] = endpoint

    from hermes_cli.plugins import PluginManager  # noqa: PLC0415 - must follow HERMES_HOME
    from tools.registry import registry  # noqa: PLC0415

    print(f"  HERMES_HOME  → {home}")
    print(f"  installed to → {target}")
    print(f"  MCP endpoint → {endpoint}")

    manager = PluginManager(scope_key=str(home))
    manager.discover_and_load(force=True)

    listed = [p for p in manager.list_plugins() if p.get("name") == "agentpass"]
    print(f"  discovered   → {len(listed)} plugin record(s) named agentpass")
    if listed:
        record = listed[0]
        interesting = {k: record.get(k) for k in ("name", "version", "enabled", "loaded", "error", "tools") if k in record}
        print(f"  plugin       → {json.dumps(interesting, default=str)}")
    check("Hermes discovered the plugin", bool(listed))
    check("plugin loaded without an error", bool(listed) and not (listed[0].get("error")), str(listed[0].get("error") if listed else ""))

    names = [n for n in registry.get_all_tool_names() if n.startswith("agent_readiness_")]
    print(f"  registry     → {', '.join(names) or '(none)'}")
    check("agent_readiness_scan is in the global tool registry", "agent_readiness_scan" in names)
    check("agent_readiness_fix_prompt is in the global tool registry", "agent_readiness_fix_prompt" in names)

    entry = registry.get_entry("agent_readiness_scan")
    if entry:
        print(f"  toolset      → {entry.toolset}")
        print(f"  schema        → required={entry.schema.get('parameters', {}).get('required')} "
              f"props={sorted((entry.schema.get('parameters') or {}).get('properties', {}))}")
        check("toolset is agentpass", entry.toolset == "agentpass", str(entry.toolset))

    print("\n  dispatch agent_readiness_scan({'url': 'example.com'})")
    raw = registry.dispatch("agent_readiness_scan", {"url": "example.com"})
    payload = json.loads(raw) if isinstance(raw, str) else raw
    body = payload.get("report") if isinstance(payload.get("report"), str) else ""
    print("    " + "\n    ".join(body.splitlines()[:6]))
    print(f"    → ok={payload.get('ok')} score={payload.get('score')} grade={payload.get('grade')}")
    check("scan dispatched through the registry", payload.get("ok") is True)
    check("scan returned a real report", isinstance(payload.get("score"), (int, float)))

    print("\n  dispatch agent_readiness_fix_prompt({'url': 'example.com'})")
    raw = registry.dispatch("agent_readiness_fix_prompt", {"url": "example.com"})
    payload = json.loads(raw) if isinstance(raw, str) else raw
    prompt = payload.get("prompt") or ""
    print("    " + "\n    ".join(prompt.splitlines()[:5]))
    print(f"    → ok={payload.get('ok')} items={payload.get('items')} chars={len(prompt)}")
    check("fix prompt dispatched through the registry", payload.get("ok") is True)
    check("fix prompt is ordered", "\n1. [" in prompt)

    manager.shutdown() if hasattr(manager, "shutdown") else None
    shutil.rmtree(home, ignore_errors=True)

    if _failures:
        print(f"\n  {len(_failures)} failure(s): {', '.join(_failures)}", file=sys.stderr)
        return 1
    print("\n  Hermes load + registry call path ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
