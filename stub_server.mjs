#!/usr/bin/env node
/**
 * Stub AgentPass MCP server — zero dependencies, Node stdlib only.
 *
 * Speaks JSON-RPC 2.0 over the MCP Streamable HTTP transport so the plugin's
 * client can be exercised without the real endpoint:
 *
 *   POST /mcp   initialize | notifications/initialized | tools/list | tools/call
 *   GET  /health, GET /stats   test-harness introspection (non-MCP)
 *
 * Transport behaviours it deliberately implements, because the client must
 * handle them:
 *   - issues `Mcp-Session-Id` on initialize and 404s any later request that
 *     does not carry it (so session echo is proven, not assumed)
 *   - answers `tools/call` as `text/event-stream` and everything else as
 *     `application/json` (so both response parsers are exercised)
 *   - `notifications/initialized` gets a bare 202 with no body
 *
 * Env knobs (all optional):
 *   PORT=8787            listen port (0 = ephemeral, printed on stdout)
 *   HOST=127.0.0.1
 *   STUB_MODE=ok         ok | retry | timeout
 *                          retry   → first tools/call answers 503, then succeeds
 *                          timeout → tools/call sleeps STUB_DELAY_MS
 *   STUB_DELAY_MS=2000   delay used by STUB_MODE=timeout
 *   STUB_SSE=1           set 0 to answer tools/call with application/json
 *
 * Tools exposed (mirroring agentpass.dev/mcp):
 *   scan_site {url}  → full ARI-100 report
 *   get_report {host} → last cached report for that host
 */

import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE = JSON.parse(readFileSync(join(__dirname, 'fixtures', 'sample-report.json'), 'utf8'));

const PORT = Number(process.env.PORT ?? 8787);
const HOST = process.env.HOST ?? '127.0.0.1';
const MODE = process.env.STUB_MODE ?? 'ok';
const DELAY_MS = Number(process.env.STUB_DELAY_MS ?? 2000);
const USE_SSE = process.env.STUB_SSE !== '0';
const PROTOCOL = '2025-06-18';
const SERVER_INFO = { name: 'agentpass-stub', version: '0.1.0' };

const sessions = new Set();
const reports = new Map(); // host -> report
const stats = { initialize: 0, toolsList: 0, toolsCall: 0, byTool: {}, rejected: 0, retryInjected: 0 };
let toolCalls = 0;

const TOOLS = [
  {
    name: 'scan_site',
    title: 'Scan a site for agent readiness',
    description:
      'Crawl a URL and return the full Agent Readiness Index (ARI-100) report: score, grade, ' +
      'per-domain breakdown, crawler access verdicts, and recoverable fixes with prompts.',
    inputSchema: {
      type: 'object',
      properties: { url: { type: 'string', description: 'Absolute http(s) URL to scan.' } },
      required: ['url'],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  {
    name: 'get_report',
    title: 'Get a cached report',
    description: 'Return the most recent cached ARI-100 report for a host, without re-crawling.',
    inputSchema: {
      type: 'object',
      properties: { host: { type: 'string', description: 'Hostname, e.g. example.com' } },
      required: ['host'],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
];

function nowIso() {
  return new Date().toISOString();
}

function reportFor(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new RpcError(-32602, `url is not absolute: ${url}`);
  }
  if (!/^https?:$/.test(parsed.protocol)) throw new RpcError(-32602, `url must be http(s): ${url}`);
  const host = parsed.hostname.toLowerCase();
  // Re-stamp the recorded sample against the URL the caller asked for, so the
  // fixture reads as "a scan of your site" rather than one captured site.
  const source = JSON.stringify(FIXTURE)
    .replaceAll('humanpeptides.fyi', host)
    .replaceAll('Human Peptides', host);
  const report = JSON.parse(source);
  report.url = `${parsed.protocol}//${parsed.host}/`;
  report.finalUrl = report.url;
  report.scannedAt = nowIso();
  report.engine = 'ARI-100';
  reports.set(host, report);
  return report;
}

class RpcError extends Error {
  constructor(code, message, data) {
    super(message);
    this.code = code;
    this.data = data;
  }
}

function jsonRpcResult(id, result) {
  return { jsonrpc: '2.0', id, result };
}

function jsonRpcError(id, code, message, data) {
  return { jsonrpc: '2.0', id, error: { code, message, ...(data === undefined ? {} : { data }) } };
}

function callTool(name, args) {
  stats.byTool[name] = (stats.byTool[name] ?? 0) + 1;
  switch (name) {
    case 'scan_site': {
      const url = typeof args?.url === 'string' ? args.url : '';
      if (!url) throw new RpcError(-32602, 'scan_site requires `url`');
      const report = reportFor(url);
      return {
        content: [{ type: 'text', text: JSON.stringify(report) }],
        structuredContent: report,
        isError: false,
      };
    }
    case 'get_report': {
      const host = String(args?.host ?? '').toLowerCase().replace(/^https?:\/\//, '').split('/')[0];
      if (!host) throw new RpcError(-32602, 'get_report requires `host`');
      const report = reports.get(host);
      if (!report) {
        return { content: [{ type: 'text', text: `no cached report for ${host}; call scan_site first` }], isError: true };
      }
      return { content: [{ type: 'text', text: JSON.stringify(report) }], structuredContent: report, isError: false };
    }
    default:
      throw new RpcError(-32602, `unknown tool: ${name}`);
  }
}

function handleRpc(message) {
  const { id, method, params } = message ?? {};
  const isNotification = id === undefined || id === null;

  if (method === 'initialize') {
    stats.initialize += 1;
    return {
      message: jsonRpcResult(id, {
        protocolVersion: PROTOCOL,
        capabilities: { tools: { listChanged: false } },
        serverInfo: SERVER_INFO,
        instructions: 'Agent readiness scanner (stub). Call scan_site with a URL.',
      }),
      newSession: true,
    };
  }

  if (method === 'notifications/initialized' || method === 'initialized') {
    return { notification: true };
  }

  if (method === 'ping') {
    return { message: jsonRpcResult(id, {}) };
  }

  if (method === 'tools/list') {
    stats.toolsList += 1;
    return { message: jsonRpcResult(id, { tools: TOOLS }) };
  }

  if (method === 'tools/call') {
    stats.toolsCall += 1;
    toolCalls += 1;
    if (MODE === 'retry' && toolCalls === 1) {
      stats.retryInjected += 1;
      return { retryNow: true };
    }
    try {
      const result = callTool(params?.name, params?.arguments ?? {});
      return { message: jsonRpcResult(id, result) };
    } catch (err) {
      if (err instanceof RpcError) {
        return { message: jsonRpcResult(id, { content: [{ type: 'text', text: err.message }], isError: true }) };
      }
      throw err;
    }
  }

  if (isNotification) return { notification: true };
  return { message: jsonRpcError(id, -32601, `method not found: ${method}`) };
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://${req.headers.host ?? 'localhost'}`);

  if (req.method === 'GET' && url.pathname === '/health') {
    const body = JSON.stringify({ ok: true, server: SERVER_INFO, mode: MODE, sessions: sessions.size, scans: reports.size });
    res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) });
    return res.end(body);
  }

  if (req.method === 'GET' && url.pathname === '/stats') {
    const body = JSON.stringify(stats, null, 2);
    res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) });
    return res.end(body);
  }

  if (url.pathname !== '/mcp') {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ error: 'not found' }));
  }
  if (req.method !== 'POST') {
    res.writeHead(405, { 'Content-Type': 'application/json', Allow: 'POST' });
    return res.end(JSON.stringify({ error: 'POST only' }));
  }

  let message;
  try {
    message = JSON.parse(await readBody(req));
  } catch {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify(jsonRpcError(null, -32700, 'parse error')));
  }

  const sessionHeader = req.headers['mcp-session-id'];
  const method = message?.method ?? '';
  const isInitialize = method === 'initialize';

  if (!isInitialize && (!sessionHeader || !sessions.has(sessionHeader))) {
    stats.rejected += 1;
    res.writeHead(404, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify(jsonRpcError(message?.id ?? null, -32001, 'unknown session; initialize first')));
  }

  let outcome;
  try {
    outcome = handleRpc(message);
  } catch (err) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify(jsonRpcError(message?.id ?? null, -32603, String(err?.message ?? err))));
  }

  const headers = { 'MCP-Protocol-Version': PROTOCOL };
  if (outcome.newSession) {
    const sessionId = `stub-${Math.random().toString(36).slice(2, 10)}`;
    sessions.add(sessionId);
    headers['Mcp-Session-Id'] = sessionId;
  }

  if (outcome.notification && !outcome.message) {
    res.writeHead(202, headers);
    return res.end();
  }

  if (outcome.retryNow) {
    res.writeHead(503, { ...headers, 'Content-Type': 'application/json', 'Retry-After': '0' });
    return res.end(JSON.stringify({ error: 'injected transient failure' }));
  }

  if (MODE === 'timeout' && method === 'tools/call') {
    await new Promise((r) => setTimeout(r, DELAY_MS));
  }

  const payload = JSON.stringify(outcome.message);
  const wantsSse = USE_SSE && method === 'tools/call';

  if (wantsSse) {
    res.writeHead(200, {
      ...headers,
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    });
    res.write(`event: message\ndata: ${payload}\n\n`);
    return res.end();
  }

  res.writeHead(200, { ...headers, 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) });
  return res.end(payload);
});

server.listen(PORT, HOST, () => {
  const address = server.address();
  console.log(`[stub] agentpass MCP stub listening on http://${HOST}:${address.port}/mcp (mode=${MODE}, sse=${USE_SSE})`);
});
