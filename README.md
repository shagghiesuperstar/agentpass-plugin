# AgentPass — Hermes plugin

Score any website on the **Agent Readiness Index (ARI-100)** and get an ordered,
paste-ready remediation prompt for a coding agent — from inside a Hermes session.

The plugin is a real MCP client, not a prompt wrapper: JSON-RPC 2.0 over
Streamable HTTP, `initialize` handshake, session echo, server-sent events,
tool discovery, retries with backoff, and an error taxonomy that never throws
into the agent loop.

```
you:    how agent-ready is example.com?

hermes: ⚙ agent_readiness_scan  url=example.com
        Agent readiness — https://example.com/
          ARI-100 39/100 · grade F — Unreadable · level L0
          71 of 113 checks scored · 24 pass · 10 partial · 37 fail
          crawler access 100/100 · accessibility 78/100 · core 39 + bonus 0

          Domains
               83%  Reachability & Access   9/14  w20
             80.8%  Render & Delivery       9/10  w10
             30.8%  Structure & Semantics   2/12  w12
             15.8%  Structured Data          1/7   w12
                0%  Discovery Files          0/17  w14
                —  Agent Interfaces         0/0   w14
                0%  Operability & Commerce 0/1   w10
             41.7%  Trust & Authority       3/10  w8

          Recoverable fixes — 47 open, up to +90 points
             1. +5 pts  render/ssr-home — Homepage content is in the raw HTML
             2. +4 pts  data/jsonld — JSON-LD structured data is present
             3. +4 pts  data/jsonld-valid — Structured data validates
             ...
```

## What it does

| Tool | Input | Output |
|---|---|---|
| `agent_readiness_scan` | `url` (required), `format` = `text` \| `json` | Score, grade, per-domain breakdown, crawler-access verdicts, top recoverable fixes |
| `agent_readiness_fix_prompt` | `url` (required), `max_items` (default 12) | Numbered remediation prompt: observed evidence → fix → check id to verify |

Both tools call an MCP server (`scan_site` / `get_report`), and the scan is
cached per host so `fix_prompt` after `scan` costs no second crawl.

The numbers above are a real scan: `example.com` grades **F (39/100)** — it ships
no robots.txt, no JSON-LD and no canonical, and serves ~170 characters of text to a
client that does not run JavaScript. Rendered from `http://localhost:8787/mcp`.
The exact score moves as the site changes (an earlier run of the same URL returned
43/100, grade D); the grade band is the stable signal.

## Install

```bash
git clone <this-repo> ~/Projects/agentpass          # or use the existing checkout
cp -R ~/Projects/agentpass/plugin "${HERMES_HOME:-$HOME/.hermes}/plugins/agentpass"
hermes plugins enable agentpass
hermes plugins doctor agentpass                     # optional: verify load + registration
```

`$HERMES_HOME` is `~/.hermes` for the default profile and
`~/.hermes/profiles/<name>` for a named one; the plugin belongs to whichever
profile should see the tools.

Plugin tools load in every surface (CLI, TUI, Desktop, gateway) once enabled.
Point it at a local server while developing:

```bash
hermes config set plugins.entries.agentpass.settings.mcp_url http://localhost:8787/mcp
```

### Settings

| Key | Default | Meaning |
|---|---|---|
| `mcp_url` | `https://agentpass.dev/mcp` | Streamable HTTP endpoint |
| `timeout_seconds` | `45` | Per-attempt deadline (a cold scan of a large site takes 20–40 s) |
| `max_retries` | `2` | Extra attempts on 429/5xx or a dropped connection |
| `cache_ttl_seconds` | `600` | How long a scan is reused by `agent_readiness_fix_prompt` |

Environment fallbacks (for running the module outside Hermes):
`AGENTPASS_MCP_URL`, `AGENTPASS_TIMEOUT`, `AGENTPASS_MAX_RETRIES`,
`AGENTPASS_CACHE_TTL`, and `AGENTPASS_API_KEY` (sent as a bearer token when set).

## Tool signatures

```json
{
  "name": "agent_readiness_scan",
  "parameters": {
    "type": "object",
    "properties": {
      "url":    { "type": "string", "description": "https://example.com or example.com" },
      "format": { "type": "string", "enum": ["text", "json"], "default": "text" }
    },
    "required": ["url"]
  }
}
```

Returns a JSON string:

```json
{
  "ok": true,
  "tool": "agent_readiness_scan",
  "url": "https://example.com/",
  "score": 39,
  "grade": "F",
  "format": "text",
  "report": "Agent readiness — https://example.com/\n  ARI-100 39/100 …",
  "meta": { "cached": false, "endpoint": "https://agentpass.dev/mcp",
            "duration_ms": 2140, "retries": 0, "transport": "sse" }
}
```

`format: "json"` puts the full ARI-100 report object under `report`
(`domains`, `checks`, `crawlers`, `fixes`, `counts`, `a11yReport`, …).

```json
{
  "name": "agent_readiness_fix_prompt",
  "parameters": {
    "type": "object",
    "properties": {
      "url":       { "type": "string" },
      "max_items": { "type": "integer", "minimum": 1, "maximum": 40, "default": 12 }
    },
    "required": ["url"]
  }
}
```

Returns `{ "ok": true, "prompt": "…", "items": 12, "score": 39, "grade": "F" }`.
The prompt is ordered by recoverable points, so item 1 is the cheapest real win:

```text
You are fixing the agent readability of https://example.com/.
Agent Readiness Index: 39/100, grade F (Unreadable), level L0.
There are 47 open findings worth up to +90 points. Work top to bottom.

Rules:
- Change only what each item names. Do not restyle the site.
- Do not invent facts: prices, dates, names and claims must already exist in the content.
- After each item, re-run the named check id to confirm it moved from fail to pass.
...

1. [render/ssr-home] +5 pts (weight 5) — Homepage content is in the raw HTML
   Observed: 171 chars of text, 0 H1 in raw HTML
   Fix: Server-render the homepage of https://example.com: ship at least 500 characters
        of real text and an H1 in the initial HTML response rather than after hydration.
   Verify: re-run check `ssr-home` on https://example.com/.
```

### Failures

Every handler returns JSON, never raises. The failure envelope names the cause
and gives the model a next step:

```json
{
  "ok": false,
  "tool": "agent_readiness_scan",
  "url": "https://example.com/",
  "error": { "kind": "network", "message": "cannot reach http://127.0.0.1:9/mcp: [Errno 61] Connection refused" },
  "hint": "The MCP endpoint could not be reached. Check the network or set AGENTPASS_MCP_URL.",
  "meta": { "endpoint": "http://127.0.0.1:9/mcp" }
}
```

`kind` is one of `network`, `timeout`, `http`, `rpc`, `protocol`, `tool`,
`config`, `input`, `unexpected`.

## Prove it

```bash
cd ~/Projects/agentpass/plugin
node test_plugin.mjs        # exits 0, prints every phase
```

No `npm install`; the stub server and harness are Node stdlib, the plugin is
Python stdlib. Six phases:

1. **main** — `initialize` handshake, `tools/list` discovery, both Hermes tools,
   SSE response parsing, session echo, cache reuse, `get_report`, unknown-tool error.
2. **retry** — stub answers the first `tools/call` with 503; the client retries and succeeds.
   (The live server answers `tools/call` as JSON; the stub as SSE — both parsers are proven.)
3. **timeout** — stub sleeps 3 s against a 1 s deadline; the tool returns a `timeout` envelope.
4. **dead** — closed port returns a `network` envelope instead of raising.
5. **badurl** — blank URL rejected before any network call.
6. **hermes** — installs the plugin into a throwaway `HERMES_HOME`, runs Hermes's own
   `PluginManager.discover_and_load()`, then calls both tools through
   `tools.registry.dispatch` (the agent's real call path).

Add a real endpoint next to the stub — the sibling web app, once it is running:

```bash
AGENTPASS_LIVE_URL=http://localhost:8787/mcp node test_plugin.mjs   # 7 phases
```

That last run printed **54 assertions, 0 failures**, including a genuine
live scan of `example.com` from `localhost:8787` and the same two tools
dispatched through Hermes's registry.

Static checks:

```bash
hermes plugins validate .     # all checks pass, incl. "declared tools — matches registrations"
hermes plugins doctor .       # discovery, manifest, import, register(ctx)
```

### Manifest notes

Written to the documented schema (`developer-guide/plugins` → *Step 2: Write the
manifest* and *Manifest v2 reference*): `name`, `version`, `description`,
`author`, `provides_tools`, `capabilities`, plus the v2 `manifest_version`,
`api_version`, `license`, `homepage`, `tags`, `config_schema`.

`entrypoint: __init__.py` is declared explicitly even though Hermes loads a
directory plugin through `__init__.py:register(ctx)` — the manifest parser lists
it as an unknown field and says so once at load:

```
Plugin agentpass: unknown manifest field(s) ignored: entrypoint (plugin still loads)
```

Per the manifest docs, unknown fields are additive and never break loading, so
it is documentation of the entry point rather than a switch. `hermes plugins
validate` reports the real entry as `✓ loadable — entry: __init__.py`.

No `capabilities:` are requested — the plugin adds two read-only tools and never
overrides a built-in tool, picks a model, or reaches into another plugin.

## 30-second demo (X video)

Screen: terminal, large font, dark theme. Pre-open a Hermes session with the
plugin enabled and the site loaded in a browser tab.

| # | Time | Beat | On screen |
|---|---|---|---|
| 1 | 0:00 | Hook | Type: **"is example.com readable by AI agents?"** — cut to the tool chip `agent_readiness_scan` firing |
| 2 | 0:04 | Verdict lands | Scroll the report header: **ARI-100 39/100 · grade F — Unreadable**, then the domain table |
| 3 | 0:09 | The problem | Hold on `Structured Data 15.8%` and `Recoverable fixes — 47 open, up to +90 points` |
| 4 | 0:13 | Second ask | Type: **"turn that into a fix plan"** → `agent_readiness_fix_prompt` fires, numbered pack appears |
| 5 | 0:18 | Handoff | Copy item 1 (`[render/ssr-home] +5 pts`) into a coding agent; let the diff land |
| 6 | 0:24 | Loop closes | Re-ask the first question; score moves up. End card: **AgentPass — agent readiness, as a tool** |

Beats 1–3 carry the demo; if you cut for time, drop 5 and end on the prompt pack
at ~0:22. Total runtime 28–32 s.

## Design notes

- **One class per concern.** `mcp_client.py` is transport + JSON-RPC; `tools.py`
  is policy (validation, cache, envelopes); `report.py` is rendering. The
  handlers contain almost no logic.
- **Lazy client.** Constructed per call, so `register(ctx)` opens no socket
  (`hermes plugins doctor` blocks sockets during registration) and a down
  endpoint never delays startup.
- **Retries.** Only on 429/5xx/dropped connection, with exponential backoff and
  jitter; JSON-RPC errors are deterministic and are not retried. A 404 on a
  session the server forgot triggers one re-`initialize`.
- **Bounded output.** Text rendering shows 8 fixes; the prompt pack 12 by
  default. Full data is always available via `format: "json"`.
- **Untrusted content.** Report text is data. It is rendered, never executed,
  and never fed into an approval decision.

## Files

| Path | Role |
|---|---|
| `plugin.yaml` | Manifest (v2) |
| `__init__.py` | `register(ctx)`: schemas → handlers, settings → config |
| `schemas.py` | The two model-facing tool schemas |
| `tools.py` | Handlers, URL validation, cache, error envelopes |
| `mcp_client.py` | MCP JSON-RPC 2.0 client over Streamable HTTP (stdlib only) |
| `report.py` | Text report renderer + remediation prompt pack |
| `stub_server.mjs` | Zero-dep MCP stub server (`scan_site`, `get_report`) |
| `test_driver.py` | Phase driver: handshake, tools, retry, timeout, dead endpoint |
| `test_hermes_load.py` | Loads the plugin through Hermes's real `PluginManager` |
| `test_plugin.mjs` | Starts the stub, runs all six phases, exits non-zero on failure |
| `fixtures/sample-report.json` | A recorded ARI-100 report (88/100) the stub serves, re-stamped per URL |
| `catalog-entry.yaml` | Catalog submission template (pin `sha` before opening the PR) |
