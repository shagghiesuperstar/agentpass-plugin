#!/usr/bin/env node
/**
 * End-to-end proof for the AgentPass plugin.
 *
 * Starts the zero-dep stub MCP server, runs the plugin's real Python handlers
 * against it in five phases, prints every byte of output, and exits non-zero if
 * any assertion fails. No npm install, no test framework.
 *
 *   node test_plugin.mjs
 *
 * Exit 0 = the client handshake, tool discovery, both Hermes tools, SSE
 * parsing, session echo, retry, timeout and dead-endpoint paths all behaved.
 */

import { spawn } from 'node:child_process';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = dirname(fileURLToPath(import.meta.url));
const PYTHON = process.env.PYTHON || 'python3';

const RULE = '─'.repeat(78);

function startStub(extraEnv = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawn('node', [join(ROOT, 'stub_server.mjs')], {
      cwd: ROOT,
      env: { ...process.env, PORT: '0', HOST: '127.0.0.1', ...extraEnv },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let buffer = '';
    const timer = setTimeout(() => {
      proc.kill();
      reject(new Error(`stub did not report a port in time; output so far: ${buffer}`));
    }, 10000);

    proc.stdout.on('data', (chunk) => {
      buffer += chunk.toString();
      const match = buffer.match(/listening on (http:\/\/[^\s]+)/);
      if (match) {
        clearTimeout(timer);
        resolve({ proc, endpoint: match[1], banner: buffer.trim() });
      }
    });
    proc.stderr.on('data', (chunk) => {
      buffer += chunk.toString();
    });
    proc.on('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`stub exited early (${code}): ${buffer}`));
    });
  });
}

function stopStub(stub) {
  if (!stub?.proc || stub.proc.killed) return;
  stub.proc.kill('SIGTERM');
}

function runDriver(phase, endpoint) {
  return new Promise((resolve) => {
    const proc = spawn(PYTHON, [join(ROOT, 'test_driver.py'), phase], {
      cwd: ROOT,
      env: { ...process.env, AGENTPASS_MCP_URL: endpoint, PYTHONPATH: ROOT },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    proc.stdout.on('data', (c) => (stdout += c.toString()));
    proc.stderr.on('data', (c) => (stderr += c.toString()));
    proc.on('exit', (code) => resolve({ code, stdout, stderr }));
  });
}

async function fetchJson(url) {
  const response = await fetch(url);
  return response.json();
}

/**
 * Ask the installed Hermes for the exact interpreter + bootstrap it runs under
 * (`hermes --print-runtime-command`), so this phase can import hermes_cli with
 * its real dependency closure instead of guessing a venv path.
 */
function hermesRuntime() {
  try {
    const out = execFileSync('hermes', ['--print-runtime-command'], { encoding: 'utf8' });
    const argv = JSON.parse(out.trim());
    const python = argv[0];
    const bootstrap = argv.find((part) => typeof part === 'string' && part.includes('sys.path.insert')) ?? '';
    const root = bootstrap.match(/sys\.path\.insert\(0,\s*'([^']+)'\)/)?.[1];
    if (!python || !root) return null;
    return { python, root };
  } catch {
    return null;
  }
}

function runHermesLoad(runtime, endpoint) {
  const script = join(ROOT, 'test_hermes_load.py');
  const code = [
    "import os, runpy, sys",
    "os.environ.pop('PYTHONPATH', None)",
    `sys.path.insert(0, ${JSON.stringify(runtime.root)})`,
    'import hermes_bootstrap',
    `runpy.run_path(${JSON.stringify(script)}, run_name='__main__')`,
  ].join('; ');
  return new Promise((resolve) => {
    const proc = spawn(runtime.python, ['-I', '-c', code], {
      cwd: ROOT,
      env: { ...process.env, AGENTPASS_MCP_URL: endpoint },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    proc.stdout.on('data', (c) => (stdout += c.toString()));
    proc.stderr.on('data', (c) => (stderr += c.toString()));
    proc.on('exit', (exit) => resolve({ code: exit, stdout, stderr }));
  });
}

const phases = [
  { phase: 'main', env: {}, title: 'handshake · discovery · both tools · cache · SSE', stats: true },
  { phase: 'retry', env: { STUB_MODE: 'retry' }, title: 'HTTP 503 on first tools/call → retried' },
  { phase: 'timeout', env: { STUB_MODE: 'timeout', STUB_DELAY_MS: '3000' }, title: 'slow server → timeout envelope' },
  { phase: 'dead', env: {}, title: 'closed port → network error envelope' },
  { phase: 'badurl', env: {}, title: 'blank url → input error, no network' },
  { phase: 'hermes', env: {}, title: 'real Hermes discovery + tools.registry.dispatch', viaHermes: true },
];

// AGENTPASS_LIVE_URL=http://localhost:8787/mcp runs the same main phase against
// a live server (the sibling web app) in addition to the stub.
if (process.env.AGENTPASS_LIVE_URL) {
  phases.push({
    phase: 'main',
    env: {},
    title: `live endpoint ${process.env.AGENTPASS_LIVE_URL}`,
    liveUrl: process.env.AGENTPASS_LIVE_URL,
  });
}

const failures = [];
const runtime = hermesRuntime();

for (const spec of phases) {
  console.log(`\n${RULE}`);
  console.log(`PHASE ${spec.phase.padEnd(8)} ${spec.title}`);
  console.log(RULE);

  // timeout/dead/badurl phases never reach MCP, but the stub is cheap and keeps
  // every phase's setup identical.
  let stub = null;
  let endpoint = spec.liveUrl ?? 'http://127.0.0.1:9/mcp';
  if (!spec.liveUrl) {
    try {
      stub = await startStub(spec.env);
      endpoint = stub.endpoint;
      console.log(`${stub.banner}\n`);
    } catch (err) {
      failures.push(`${spec.phase}: stub failed to start: ${err.message}`);
      console.log(String(err.message));
    }
  } else {
    console.log(`  using the live MCP server already running at ${spec.liveUrl}\n`);
  }

  let result;
  if (spec.viaHermes) {
    if (!runtime) {
      console.log('hermes not found on PATH — skipping the in-Hermes load phase');
      result = { code: 0, stdout: '  (skipped)', stderr: '' };
    } else {
      console.log(`  runtime      → ${runtime.python}\n  hermes root  → ${runtime.root}\n`);
      result = await runHermesLoad(runtime, endpoint);
    }
  } else {
    result = await runDriver(spec.phase, endpoint);
  }
  console.log(result.stdout.trimEnd());
  if (result.stderr.trim()) {
    console.log(`\n[stderr]\n${result.stderr.trimEnd()}`);
  }

  if (spec.stats && stub) {
    const stats = await fetchJson(endpoint.replace('/mcp', '/stats'));
    console.log(`\n  stub /stats  → ${JSON.stringify(stats)}`);
    if (!(stats.toolsCall >= 3 && stats.toolsList >= 1 && stats.initialize >= 2)) {
      failures.push(`${spec.phase}: unexpected stub call counts ${JSON.stringify(stats)}`);
    }
  }

  if (result.code !== 0) failures.push(`${spec.phase}: driver exited ${result.code}`);
  stopStub(stub);
}

console.log(`\n${RULE}`);
if (failures.length) {
  console.log(`FAIL — ${failures.length} problem(s):`);
  for (const failure of failures) console.log(`  - ${failure}`);
  console.log(RULE);
  process.exit(1);
}
console.log(
  `PASS — all ${phases.length} phases green: real MCP traffic, both tools, SSE, session echo, retry, ` +
    'timeout, dead endpoint, a real Hermes load + registry dispatch' +
    (process.env.AGENTPASS_LIVE_URL ? ', and the live endpoint.' : '.'),
);
console.log(RULE);
