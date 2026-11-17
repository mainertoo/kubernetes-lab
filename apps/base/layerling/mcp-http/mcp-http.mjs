// Streamable-HTTP front for Layerling's stdio MCP bridge, so any MCP client on
// the tailnet can reach the in-cluster Layerling without a local checkout.
//
//   MCP client --HTTP POST /mcp (Bearer LAYERLING_MCP_TOKEN)--> this wrapper
//     --stdio--> upstream scripts/layerling-mcp-server.mjs
//     --HTTP--> http://127.0.0.1:3000/api/layerling-mcp (app container, same pod)
//     <--polls-- the editor tab open in someone's browser (where the CAD runs)
//
// The bridge script is not in the image, so it is fetched from GitHub at the
// tag matching the app's own /app/package.json version (this sidecar runs the
// same image), keeping bridge and app in lockstep across Renovate bumps.
// The wrapper never exits on a fetch failure: it answers 503 and retries, so
// a GitHub hiccup can't take the pod (and the editor) down.
import { spawn } from "node:child_process";
import { timingSafeEqual } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { join } from "node:path";

const PORT = Number(process.env.MCP_HTTP_PORT || 3100);
const TOKEN = process.env.LAYERLING_MCP_TOKEN || "";
const BRIDGE_DIR = process.env.BRIDGE_DIR || "/tmp/layerling-bridge";
const BRIDGE_CWD = process.env.BRIDGE_CWD || "/tmp";
const REPO = process.env.LAYERLING_REPO || "henmedia/layerling";
const ENTRY = "layerling-mcp-server.mjs";
const APP_PACKAGE_JSON = process.env.APP_PACKAGE_JSON || "/app/package.json";
const APP_URL = process.env.APP_URL || "http://127.0.0.1:3000";
const MAX_BODY = 8 * 1024 * 1024;

const log = (...args) => console.log(new Date().toISOString(), ...args);

if (!TOKEN) {
  log("LAYERLING_MCP_TOKEN is empty; refusing to serve");
}

// ---- fetch the bridge at the app's version --------------------------------
async function fetchText(url) {
  const res = await fetch(url, { signal: AbortSignal.timeout(30000) });
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return res.text();
}

async function fetchBridge() {
  const { version } = JSON.parse(await readFile(APP_PACKAGE_JSON, "utf8"));
  const base = `https://raw.githubusercontent.com/${REPO}/v${version}/scripts/`;
  await mkdir(BRIDGE_DIR, { recursive: true });
  // Follow relative imports so new helper modules upstream are picked up.
  const pending = [ENTRY];
  const seen = new Set();
  while (pending.length) {
    const name = pending.pop();
    if (seen.has(name)) continue;
    seen.add(name);
    const source = await fetchText(base + name);
    await writeFile(join(BRIDGE_DIR, name), source);
    for (const m of source.matchAll(/from\s+["']\.\/([\w.-]+\.mjs)["']/g)) pending.push(m[1]);
  }
  log(`bridge v${version} fetched: ${[...seen].join(", ")}`);
  return version;
}

// ---- one long-lived bridge process, request ids remapped ------------------
let bridge = null;
let bridgeVersion = null;
let nextId = 1;
const waiting = new Map(); // internal id -> { resolve, originalId }

function startBridge() {
  const child = spawn(process.execPath, [join(BRIDGE_DIR, ENTRY)], {
    cwd: BRIDGE_CWD,
    env: {
      PATH: process.env.PATH,
      HOME: "/tmp",
      LAYERLING_URL: APP_URL,
      LAYERLING_MCP_TOKEN: TOKEN,
      LAYERLING_MCP_OPEN_EDITOR: "0",
    },
    stdio: ["pipe", "pipe", "inherit"],
  });
  let buffer = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    buffer += chunk;
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        log("bridge emitted non-JSON line", line.slice(0, 200));
        continue;
      }
      const entry = waiting.get(msg.id);
      if (!entry) continue;
      waiting.delete(msg.id);
      entry.resolve({ ...msg, id: entry.originalId });
    }
  });
  child.on("exit", (code, signal) => {
    log(`bridge exited (code=${code} signal=${signal}); restarting in 2s`);
    bridge = null;
    for (const [id, entry] of waiting) {
      waiting.delete(id);
      entry.resolve({ jsonrpc: "2.0", id: entry.originalId, error: { code: -32000, message: "Layerling bridge restarted; retry the call." } });
    }
    setTimeout(() => (bridge = startBridge()), 2000);
  });
  return child;
}

function forward(message) {
  return new Promise((resolve) => {
    const id = nextId++;
    waiting.set(id, { resolve, originalId: message.id });
    bridge.stdin.write(`${JSON.stringify({ ...message, id })}\n`);
  });
}

async function boot() {
  for (let delay = 5000; ; delay = Math.min(delay * 2, 300000)) {
    try {
      bridgeVersion = await fetchBridge();
      // Relative export paths (layerling_export_mesh outputPath) land here.
      await mkdir(BRIDGE_CWD, { recursive: true });
      bridge = startBridge();
      return;
    } catch (error) {
      log(`bridge fetch failed (${error.message}); retrying in ${delay / 1000}s`);
      await new Promise((r) => setTimeout(r, delay));
    }
  }
}

// ---- HTTP -----------------------------------------------------------------
function authorized(req) {
  const header = req.headers.authorization || "";
  const given = Buffer.from(header.startsWith("Bearer ") ? header.slice(7).trim() : "");
  const want = Buffer.from(TOKEN);
  return TOKEN !== "" && given.length === want.length && timingSafeEqual(given, want);
}

function reply(res, status, body) {
  if (body === undefined) {
    res.writeHead(status).end();
    return;
  }
  res.writeHead(status, { "Content-Type": "application/json" }).end(JSON.stringify(body));
}

const rpcError = (id, code, message) => ({ jsonrpc: "2.0", id: id ?? null, error: { code, message } });

createServer(async (req, res) => {
  const path = new URL(req.url, "http://x").pathname;
  if (path === "/healthz") return reply(res, 200, { ok: true, bridge: Boolean(bridge), version: bridgeVersion });
  if (path !== "/mcp") return reply(res, 404, { error: "not found" });
  if (!authorized(req)) {
    res.setHeader("WWW-Authenticate", 'Bearer realm="layerling-mcp"');
    return reply(res, 401, { error: "Bearer token required" });
  }
  // No server-initiated SSE stream and no sessions: plain JSON responses only.
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return reply(res, 405, { error: "POST only" });
  }
  if (!bridge) return reply(res, 503, rpcError(null, -32000, "Layerling bridge is starting; retry shortly."));

  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY) return reply(res, 413, rpcError(null, -32600, "Request too large"));
    chunks.push(chunk);
  }
  let payload;
  try {
    payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    return reply(res, 400, rpcError(null, -32700, "Parse error"));
  }

  const batch = Array.isArray(payload);
  const messages = batch ? payload : [payload];
  const requests = messages.filter((m) => m && typeof m === "object" && m.id !== undefined && m.id !== null);
  // Notifications and client responses need no answer (the bridge ignores them).
  if (requests.length === 0) return reply(res, 202);
  const results = await Promise.all(requests.map(forward));
  reply(res, 200, batch ? results : results[0]);
}).listen(PORT, () => log(`listening on :${PORT}`));

boot();
