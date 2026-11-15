#!/usr/bin/env node
'use strict';
/*
 * buildviz — a small, zero-dependency build-graph visualiser.
 *
 *   node buildviz.js init                 create buildviz.config.json
 *   node buildviz.js                      serve the interactive graph (http://localhost:4477)
 *   node buildviz.js run [ids...]         build from the terminal (all modules if no ids)
 *   node buildviz.js status               print the last known status of every module
 *
 * Modules are declared in buildviz.config.json. Each has a build command and
 * optional dependencies; independent modules build in parallel (a branching
 * graph, like n8n). Status comes from process exit codes and is written to
 * .buildviz/state.json, so a build run from the terminal (or by Claude Code)
 * shows up in the browser view on the next refresh.
 */

const fs = require('fs');
const path = require('path');
const http = require('http');
const { spawn } = require('child_process');

const IS_WIN = process.platform === 'win32';
const CONFIG_NAME = 'buildviz.config.json';
const STATE_DIR = '.buildviz';
const DEFAULT_PORT = 4477;
const BOOL_FLAGS = new Set(['downstream', 'force', 'help', 'h', 'json']);
const ANSI = /\u001b\[[0-9;?]*[ -/]*[@-~]/g;

const TEMPLATE = {
  project: 'My web app',
  concurrency: 4,
  modules: [
    { id: 'shared', label: 'Shared library', phase: 'Phase 1', cmd: 'npm run build --workspace=packages/shared' },
    { id: 'db', label: 'Database schema', phase: 'Phase 1', cmd: 'npm run build --workspace=packages/db' },
    { id: 'auth', label: 'Auth service', phase: 'Phase 2', cmd: 'npm run build --workspace=services/auth', dependsOn: ['shared', 'db'] },
    { id: 'billing', label: 'Billing service', phase: 'Phase 2', cmd: 'npm run build --workspace=services/billing', dependsOn: ['shared', 'db'] },
    { id: 'web', label: 'Web UI', phase: 'Phase 3', cmd: 'npm run build --workspace=apps/web', dependsOn: ['auth', 'billing'] },
    { id: 'admin', label: 'Admin UI', phase: 'Phase 3', cmd: 'npm run build --workspace=apps/admin', dependsOn: ['auth'] },
    { id: 'e2e', label: 'End-to-end checks', phase: 'Phase 4', cmd: 'npm run test:e2e', dependsOn: ['web', 'admin'] },
  ],
};

// ───────────────────────── helpers ─────────────────────────

function parseArgs(argv) {
  const out = { _: [], flags: {} };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const eq = a.indexOf('=');
      const key = eq === -1 ? a.slice(2) : a.slice(2, eq);
      if (eq !== -1) out.flags[key] = a.slice(eq + 1);
      else if (BOOL_FLAGS.has(key)) out.flags[key] = true;
      else if (argv[i + 1] !== undefined && !argv[i + 1].startsWith('--')) out.flags[key] = argv[++i];
      else out.flags[key] = true;
    } else if (a === '-h') out.flags.help = true;
    else out._.push(a);
  }
  return out;
}

function pidAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
}

function fmtDur(ms) {
  if (ms == null) return '';
  const s = ms / 1000;
  if (s < 60) return (s < 10 ? s.toFixed(1) : String(Math.round(s))) + 's';
  return Math.floor(s / 60) + 'm ' + String(Math.round(s % 60)).padStart(2, '0') + 's';
}

function tailFile(file, maxBytes) {
  const fd = fs.openSync(file, 'r');
  try {
    const { size } = fs.fstatSync(fd);
    const start = Math.max(0, size - maxBytes);
    const buf = Buffer.alloc(size - start);
    fs.readSync(fd, buf, 0, buf.length, start);
    let text = buf.toString('utf8');
    if (start > 0) text = '… (earlier output truncated)\n' + text.slice(text.indexOf('\n') + 1);
    return text;
  } finally { fs.closeSync(fd); }
}

// ───────────────────────── config ─────────────────────────

function loadConfig(file) {
  let cfg;
  try { cfg = JSON.parse(fs.readFileSync(file, 'utf8')); }
  catch (e) { throw new Error(`Cannot read ${path.basename(file)}: ${e.message}`); }
  if (!cfg || !Array.isArray(cfg.modules) || cfg.modules.length === 0)
    throw new Error('Config must contain a non-empty "modules" array.');

  const seen = new Set();
  const modules = cfg.modules.map((m, i) => {
    const where = `modules[${i}]`;
    if (!m || typeof m.id !== 'string' || !/^[A-Za-z0-9_.:-]{1,64}$/.test(m.id))
      throw new Error(`${where}: "id" must be 1–64 characters: letters, digits and _ . : -`);
    if (seen.has(m.id)) throw new Error(`${where}: duplicate id "${m.id}"`);
    seen.add(m.id);
    const deps = m.dependsOn === undefined ? [] : m.dependsOn;
    if (!Array.isArray(deps) || deps.some(d => typeof d !== 'string'))
      throw new Error(`${where} ("${m.id}"): "dependsOn" must be an array of module ids`);
    return {
      id: m.id,
      label: String(m.label || m.id),
      phase: m.phase ? String(m.phase) : '',
      description: m.description ? String(m.description) : '',
      cmd: m.cmd ? String(m.cmd) : '',
      cwd: m.cwd ? String(m.cwd) : '.',
      env: m.env && typeof m.env === 'object' ? m.env : {},
      timeoutSec: Number(m.timeoutSec) > 0 ? Number(m.timeoutSec) : 0,
      dependsOn: [...new Set(deps)],
    };
  });

  const byId = new Map(modules.map(m => [m.id, m]));
  for (const m of modules) for (const d of m.dependsOn) {
    if (d === m.id) throw new Error(`"${m.id}" depends on itself`);
    if (!byId.has(d)) throw new Error(`"${m.id}" depends on unknown module "${d}"`);
  }
  const mark = new Map();
  const visit = (id, stack) => {
    if (mark.get(id) === 2) return;
    if (mark.get(id) === 1) throw new Error(`Dependency cycle: ${[...stack.slice(stack.indexOf(id)), id].join(' → ')}`);
    mark.set(id, 1);
    for (const d of byId.get(id).dependsOn) visit(d, [...stack, id]);
    mark.set(id, 2);
  };
  modules.forEach(m => visit(m.id, []));

  return {
    project: cfg.project ? String(cfg.project) : path.basename(path.dirname(file)),
    concurrency: Math.max(1, Math.floor(Number(cfg.concurrency)) || 4),
    modules,
  };
}

// ───────────────────────── runner ─────────────────────────

class Runner {
  constructor(configPath) {
    this.configPath = path.resolve(configPath);
    this.root = path.dirname(this.configPath);
    this.stateDir = path.join(this.root, STATE_DIR);
    this.logDir = path.join(this.stateDir, 'logs');
    this.stateFile = path.join(this.stateDir, 'state.json');
    fs.mkdirSync(this.logDir, { recursive: true });

    this._cfg = null;
    this._cfgMtime = -1;
    this._cfgError = null;
    this.state = this._emptyState();
    this._stateMtime = -1;
    this.active = new Map();
    this.running = false;
    this.stopping = false;
    this.onEvent = null;
    this.getConfig();
    this._readState();
  }

  _emptyState() {
    return { version: 1, running: false, runnerPid: null, startedAt: null, finishedAt: null, modules: {} };
  }

  // Re-read the config when it changes on disk (Claude Code may add modules while you watch).
  getConfig() {
    let mtime;
    try { mtime = fs.statSync(this.configPath).mtimeMs; }
    catch (e) { this._cfgError = `Cannot find ${path.basename(this.configPath)}`; if (!this._cfg) throw new Error(this._cfgError); return this._cfg; }
    if (mtime !== this._cfgMtime) {
      this._cfgMtime = mtime;
      try { this._cfg = loadConfig(this.configPath); this._cfgError = null; }
      catch (e) { this._cfgError = e.message; if (!this._cfg) throw e; }
    }
    return this._cfg;
  }

  _readState() {
    let mtime;
    try { mtime = fs.statSync(this.stateFile).mtimeMs; } catch { return; }
    if (mtime === this._stateMtime) return;
    this._stateMtime = mtime;
    try {
      const s = JSON.parse(fs.readFileSync(this.stateFile, 'utf8'));
      if (!s || typeof s.modules !== 'object') return;
      this.state = Object.assign(this._emptyState(), s);
      if (this.state.running && this.state.runnerPid !== process.pid && !pidAlive(this.state.runnerPid)) {
        this.state.running = false;
        for (const st of Object.values(this.state.modules))
          if (st.status === 'running' || st.status === 'queued') { st.status = 'pending'; st.note = 'interrupted'; }
      }
    } catch { /* keep previous state */ }
  }

  _save() {
    const tmp = this.stateFile + '.tmp';
    const data = JSON.stringify(this.state, null, 2);
    try { fs.writeFileSync(tmp, data); fs.renameSync(tmp, this.stateFile); }
    catch { try { fs.writeFileSync(this.stateFile, data); } catch { /* ignore */ } }
    try { this._stateMtime = fs.statSync(this.stateFile).mtimeMs; } catch { /* ignore */ }
  }

  _status(id) { return (this.state.modules[id] && this.state.modules[id].status) || 'pending'; }

  _emit(kind, m) { if (this.onEvent) { try { this.onEvent(kind, m, this.state.modules[m.id]); } catch { /* ignore */ } } }

  snapshot() {
    const cfg = this.getConfig();
    if (!this.running) this._readState();
    const modules = cfg.modules.map(m => {
      const st = this.state.modules[m.id] || {};
      return {
        id: m.id, label: m.label, phase: m.phase, description: m.description,
        cmd: m.cmd, cwd: m.cwd, dependsOn: m.dependsOn,
        status: st.status || 'pending',
        startedAt: st.startedAt || null, endedAt: st.endedAt || null,
        durationMs: st.durationMs == null ? null : st.durationMs,
        exitCode: st.exitCode == null ? null : st.exitCode,
        note: st.note || '',
      };
    });
    return {
      project: cfg.project,
      configFile: path.basename(this.configPath),
      configError: this._cfgError,
      running: !!this.state.running,
      startedAt: this.state.startedAt,
      finishedAt: this.state.finishedAt,
      modules,
    };
  }

  // Which modules need to run for "run this module" (mode "only") or "this + everything after it".
  plan(id, mode) {
    const cfg = this.getConfig();
    if (!id) return cfg.modules.map(m => m.id);
    const byId = new Map(cfg.modules.map(m => [m.id, m]));
    if (!byId.has(id)) throw new Error(`Unknown module "${id}"`);
    const target = new Set([id]);
    const up = x => {
      for (const d of byId.get(x).dependsOn)
        if (!target.has(d) && this._status(d) !== 'done') { target.add(d); up(d); }
    };
    up(id);
    if (mode === 'downstream') {
      const kids = new Map(cfg.modules.map(m => [m.id, []]));
      cfg.modules.forEach(m => m.dependsOn.forEach(d => kids.get(d).push(m.id)));
      const down = x => { for (const c of kids.get(x)) if (!target.has(c)) { target.add(c); down(c); } };
      down(id);
      for (const t of [...target]) up(t);
    }
    return [...target];
  }

  // Starts a build. Throws synchronously if it cannot start; returns a promise that resolves when it ends.
  run(ids) {
    if (this.running) throw new Error('A build is already running.');
    const cfg = this.getConfig();
    if (this._cfgError) throw new Error(`Fix ${path.basename(this.configPath)} first: ${this._cfgError}`);
    this._readState();
    if (this.state.running && this.state.runnerPid && this.state.runnerPid !== process.pid && pidAlive(this.state.runnerPid))
      throw new Error(`Another buildviz process (pid ${this.state.runnerPid}) is already running a build.`);

    this._runCfg = cfg;
    this._byId = new Map(cfg.modules.map(m => [m.id, m]));
    this._target = new Set(ids);
    this.running = true;
    this.stopping = false;
    for (const id of this._target) this.state.modules[id] = { status: 'queued' };
    Object.assign(this.state, { running: true, runnerPid: process.pid, startedAt: Date.now(), finishedAt: null });
    this._save();
    const done = new Promise(resolve => { this._resolve = resolve; });
    setImmediate(() => this._tick());
    return done;
  }

  _tick() {
    if (!this.running) return;
    let changed = true;
    while (changed) {
      changed = false;
      for (const id of this._target) {
        const st = this.state.modules[id];
        if (st.status !== 'queued' || this.stopping) continue;
        const m = this._byId.get(id);
        const ds = m.dependsOn.map(d => this._status(d));
        if (ds.some(s => s === 'failed' || s === 'skipped')) {
          const blocker = m.dependsOn.find(d => ['failed', 'skipped'].includes(this._status(d)));
          Object.assign(st, { status: 'skipped', note: `blocked by ${blocker}`, endedAt: Date.now() });
          this._emit('skip', m);
          changed = true;
        } else if (ds.every(s => s === 'done') && this.active.size < this._runCfg.concurrency) {
          this._start(m);
          changed = true;
        }
      }
    }
    const open = [...this._target].some(id => ['queued', 'running'].includes(this._status(id)));
    if (open) this._save(); else this._finish();
  }

  _start(m) {
    const st = this.state.modules[m.id];
    Object.assign(st, { status: 'running', startedAt: Date.now(), endedAt: null, durationMs: null, exitCode: null, note: '' });
    this._emit('start', m);
    const cwd = path.resolve(this.root, m.cwd);
    const logFile = path.join(this.logDir, `${m.id}.log`);
    const log = fs.createWriteStream(logFile, { flags: 'w' });
    log.on('error', () => { /* logging must never break a build */ });
    log.write(`$ ${m.cmd || '(milestone: no command)'}\n# cwd: ${cwd}\n# started: ${new Date().toISOString()}\n\n`);

    if (!m.cmd) { log.end(); setImmediate(() => this._complete(m, 0, '')); return; }
    if (!fs.existsSync(cwd)) {
      log.end(`ERROR: working directory does not exist: ${cwd}\n`);
      setImmediate(() => this._complete(m, 127, 'working directory not found'));
      return;
    }

    let child;
    try {
      child = spawn(m.cmd, {
        shell: true, cwd, detached: !IS_WIN, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
        env: Object.assign({}, process.env, m.env, { FORCE_COLOR: '0', NO_COLOR: '1' }),
      });
    } catch (e) {
      log.end(`ERROR: ${e.message}\n`);
      setImmediate(() => this._complete(m, 127, e.message));
      return;
    }

    const entry = { child, timer: null, timedOut: false };
    this.active.set(m.id, entry);
    child.stdout.on('data', d => log.write(d));
    child.stderr.on('data', d => log.write(d));
    if (m.timeoutSec > 0)
      entry.timer = setTimeout(() => { entry.timedOut = true; this._kill(child); }, m.timeoutSec * 1000);

    let settled = false;
    const settle = (code, note) => {
      if (settled) return;
      settled = true;
      clearTimeout(entry.timer);
      this.active.delete(m.id);
      log.end(`\n# exit ${code}${note ? ' (' + note + ')' : ''}\n`);
      this._complete(m, code, note);
    };
    child.on('error', e => settle(127, e.message));
    child.on('close', (code, signal) =>
      settle(code === null ? 1 : code, entry.timedOut ? `timed out after ${m.timeoutSec}s` : signal ? `killed by ${signal}` : ''));
  }

  _complete(m, code, note) {
    const st = this.state.modules[m.id];
    st.endedAt = Date.now();
    st.durationMs = st.endedAt - st.startedAt;
    st.exitCode = code;
    st.note = note || '';
    if (this.stopping && code !== 0) { st.status = 'pending'; st.note = 'stopped'; }
    else st.status = code === 0 ? 'done' : 'failed';
    this._emit('end', m);
    this._tick();
  }

  _kill(child) {
    try {
      if (IS_WIN) spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      else {
        process.kill(-child.pid, 'SIGTERM');
        setTimeout(() => { try { process.kill(-child.pid, 'SIGKILL'); } catch { /* already gone */ } }, 5000).unref();
      }
    } catch { try { child.kill('SIGTERM'); } catch { /* ignore */ } }
  }

  stop() {
    if (!this.running) return false;
    this.stopping = true;
    for (const id of this._target) {
      const st = this.state.modules[id];
      if (st.status === 'queued') { st.status = 'pending'; st.note = 'stopped'; }
    }
    for (const { child } of this.active.values()) this._kill(child);
    this._tick();
    return true;
  }

  _finish() {
    if (!this.running) return;
    this.running = false;
    this.stopping = false;
    Object.assign(this.state, { running: false, runnerPid: null, finishedAt: Date.now() });
    this._save();
    const ids = [...this._target];
    const failed = ids.filter(id => this._status(id) === 'failed');
    const skipped = ids.filter(id => this._status(id) === 'skipped');
    const ok = ids.every(id => this._status(id) === 'done');
    this._resolve({ ok, failed, skipped });
  }

  readLog(id) {
    const cfg = this.getConfig();
    if (!cfg.modules.some(m => m.id === id)) return null;
    const file = path.join(this.logDir, `${id}.log`);
    if (!fs.existsSync(file)) return '(no output yet — this module has not been run)';
    return tailFile(file, 200 * 1024).replace(ANSI, '');
  }
}

// ───────────────────────── server ─────────────────────────

function createServer(runner, extraHosts) {
  const uiFile = path.join(__dirname, 'ui.html');
  const allowedHost = new RegExp('^(localhost|127\\.0\\.0\\.1|\\[::1\\])(:\\d+)?$', 'i');
  const extra = new Set(extraHosts.map(h => h.toLowerCase()));

  const send = (res, status, type, body) => {
    res.writeHead(status, { 'Content-Type': type + '; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
    res.end(body);
  };
  const json = (res, status, obj) => send(res, status, 'application/json', JSON.stringify(obj));

  const readBody = req => new Promise((resolve, reject) => {
    let size = 0; const chunks = [];
    req.on('data', c => { size += c.length; if (size > 10240) { reject(new Error('Body too large')); req.destroy(); } else chunks.push(c); });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });

  return http.createServer(async (req, res) => {
    try {
      const host = String(req.headers.host || '').toLowerCase();
      // Blocks DNS-rebinding: only answer requests addressed to localhost (or hosts you explicitly allow).
      if (!allowedHost.test(host) && !extra.has(host.replace(/:\d+$/, ''))) return send(res, 403, 'text/plain', 'Forbidden host');
      const url = new URL(req.url, 'http://localhost');

      if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/index.html')) {
        return send(res, 200, 'text/html', fs.readFileSync(uiFile, 'utf8'));
      }
      if (req.method === 'GET' && url.pathname === '/api/state') return json(res, 200, runner.snapshot());
      if (req.method === 'GET' && url.pathname.startsWith('/api/log/')) {
        const text = runner.readLog(decodeURIComponent(url.pathname.slice('/api/log/'.length)));
        return text === null ? send(res, 404, 'text/plain', 'Unknown module') : send(res, 200, 'text/plain', text);
      }
      if (req.method === 'POST' && (url.pathname === '/api/run' || url.pathname === '/api/stop')) {
        // A custom header forces a CORS preflight, which this server never grants — so other websites cannot trigger builds.
        if (req.headers['x-buildviz'] !== '1') return json(res, 403, { error: 'Missing X-Buildviz header' });
        if (url.pathname === '/api/stop') {
          if (runner.stop()) return json(res, 200, { stopping: true });
          const st = runner.snapshot();
          return json(res, 409, { error: st.running ? 'That build was started from another terminal — stop it there with Ctrl+C.' : 'Nothing is running.' });
        }
        let body = {};
        const raw = await readBody(req);
        if (raw) { try { body = JSON.parse(raw); } catch { return json(res, 400, { error: 'Invalid JSON' }); } }
        const id = typeof body.id === 'string' ? body.id : null;
        const mode = body.mode === 'downstream' ? 'downstream' : 'only';
        try {
          const ids = runner.plan(id, mode);
          runner.run(ids).catch(() => {});
          return json(res, 200, { started: true, modules: ids });
        } catch (e) { return json(res, 409, { error: e.message }); }
      }
      return send(res, 404, 'text/plain', 'Not found');
    } catch (e) {
      return send(res, 500, 'text/plain', 'Server error: ' + e.message);
    }
  });
}

function listen(server, port, tries = 20) {
  return new Promise((resolve, reject) => {
    const attempt = p => {
      const onError = e => {
        if (e.code === 'EADDRINUSE' && p - port < tries) attempt(p + 1); else reject(e);
      };
      server.once('error', onError);
      server.listen(p, '127.0.0.1', () => { server.removeListener('error', onError); resolve(p); });
    };
    attempt(port);
  });
}

// ───────────────────────── CLI ─────────────────────────

const HELP = `buildviz — visualise a module-by-module build as a branching node graph

Usage
  node buildviz.js init [--force]            write a starter ${CONFIG_NAME}
  node buildviz.js [serve] [--port 4477]     open the interactive graph at http://localhost:4477
  node buildviz.js run [ids...] [--downstream]
                                             build from the terminal (all modules if no ids).
                                             --downstream also rebuilds everything that depends on ids
  node buildviz.js status [--json]           print the last known status of each module

Options
  --config <file>        use a config other than ./${CONFIG_NAME}
  --allow-host <names>   comma-separated extra Host names to accept (e.g. a Codespaces forwarded host)
`;

const ICON = { done: '✔', failed: '✖', skipped: '⊘', running: '▶', pending: '○', queued: '…' };

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const known = ['init', 'serve', 'run', 'status', 'help'];
  let cmd = 'serve';
  if (args._.length && known.includes(args._[0])) cmd = args._.shift();
  else if (args._.length && !known.includes(args._[0])) { console.error(`Unknown command "${args._[0]}".\n\n${HELP}`); process.exit(2); }
  if (args.flags.help || cmd === 'help') { console.log(HELP); return; }

  const configPath = path.resolve(typeof args.flags.config === 'string' ? args.flags.config : path.join(process.cwd(), CONFIG_NAME));

  if (cmd === 'init') {
    if (fs.existsSync(configPath) && !args.flags.force) { console.error(`${configPath} already exists (use --force to overwrite).`); process.exit(1); }
    fs.writeFileSync(configPath, JSON.stringify(TEMPLATE, null, 2) + '\n');
    console.log(`Created ${configPath}\nEdit the modules, then run:  node ${path.basename(__filename)}`);
    return;
  }

  if (!fs.existsSync(configPath)) {
    console.error(`No ${CONFIG_NAME} found at ${configPath}\nCreate one with:  node ${path.basename(__filename)} init`);
    process.exit(1);
  }
  let runner;
  try { runner = new Runner(configPath); } catch (e) { console.error(e.message); process.exit(1); }

  if (cmd === 'status') {
    const snap = runner.snapshot();
    if (args.flags.json) { console.log(JSON.stringify(snap, null, 2)); return; }
    const w = Math.max(...snap.modules.map(m => m.id.length));
    console.log(`${snap.project}${snap.running ? '  (build in progress)' : ''}`);
    for (const m of snap.modules)
      console.log(`  ${ICON[m.status] || '?'} ${m.id.padEnd(w)}  ${m.status.padEnd(8)} ${fmtDur(m.durationMs).padEnd(8)} ${m.phase}`);
    return;
  }

  if (cmd === 'run') {
    let ids;
    try {
      const mode = args.flags.downstream ? 'downstream' : 'only';
      ids = args._.length ? [...new Set(args._.flatMap(id => runner.plan(id, mode)))] : runner.plan(null);
    } catch (e) { console.error(e.message); process.exit(1); }
    const w = Math.max(...ids.map(i => i.length));
    runner.onEvent = (kind, m, st) => {
      const id = m.id.padEnd(w);
      if (kind === 'start') console.log(`  ${ICON.running} ${id}  building…`);
      else if (kind === 'skip') console.log(`  ${ICON.skipped} ${id}  skipped (${st.note})`);
      else if (st.status === 'done') console.log(`  ${ICON.done} ${id}  ${fmtDur(st.durationMs)}`);
      else if (st.status === 'failed') console.log(`  ${ICON.failed} ${id}  failed, exit ${st.exitCode}${st.note ? ' — ' + st.note : ''}  → ${path.relative(process.cwd(), path.join(runner.logDir, m.id + '.log'))}`);
    };
    process.on('SIGINT', () => { console.log('\nStopping…'); runner.stop(); process.exitCode = 130; });
    console.log(`Building ${ids.length} module${ids.length === 1 ? '' : 's'}:`);
    let result;
    try { result = await runner.run(ids); } catch (e) { console.error(e.message); process.exit(1); }
    console.log(result.ok ? '\nAll modules built.' : `\nBuild finished with problems — failed: ${result.failed.join(', ') || 'none'}; skipped: ${result.skipped.join(', ') || 'none'}`);
    process.exit(result.ok ? 0 : process.exitCode || 1);
  }

  // serve
  const extra = typeof args.flags['allow-host'] === 'string' ? args.flags['allow-host'].split(',').map(s => s.trim()).filter(Boolean) : [];
  const server = createServer(runner, extra);
  const wanted = Number(args.flags.port) || DEFAULT_PORT;
  let port;
  try { port = await listen(server, wanted); } catch (e) { console.error(`Could not start server: ${e.message}`); process.exit(1); }
  const url = `http://localhost:${port}`;
  console.log(`buildviz is running at ${url}\n  project: ${runner.getConfig().project}\n  config:  ${configPath}\n`);
  console.log('In VS Code: Ctrl/Cmd+Shift+P → "Simple Browser: Show" → paste the URL above.');
  console.log('Press Ctrl+C to stop.');
  const shutdown = () => { runner.stop(); server.close(); setTimeout(() => process.exit(0), 400); };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

if (require.main === module) main().catch(e => { console.error(e.stack || e.message); process.exit(1); });

module.exports = { Runner, loadConfig, createServer };
