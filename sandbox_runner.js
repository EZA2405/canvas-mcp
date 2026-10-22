// Code Mode sandbox for canvas_run_code. Run by canvas_mcp.py as:
//   deno run --no-prompt --no-remote --no-npm --no-config --no-lock sandbox_runner.js
// With no --allow-* flags the code has no network, file, env, or subprocess
// access. The only way out is the `canvas` object below, which sends JSON-RPC
// lines over stdout to the Python server; the server makes the (GET-only)
// Canvas calls with the token, which never enters this process.
//
// User code is evaluated with AsyncFunction rather than written into this file:
// string-literal import() calls in a main module are preloaded by Deno without
// a read-permission check, but import() inside eval'd code is checked.

const enc = new TextEncoder();
const out = Deno.stdout.writable.getWriter();
const send = (msg) => out.write(enc.encode(JSON.stringify(msg) + "\n"));

const lines = Deno.stdin.readable.pipeThrough(new TextDecoderStream()).getReader();
let buf = "";
async function nextLine() {
  while (!buf.includes("\n")) {
    const { value, done } = await lines.read();
    if (done) return null;
    buf += value;
  }
  const i = buf.indexOf("\n");
  const line = buf.slice(0, i);
  buf = buf.slice(i + 1);
  return line;
}

const pending = new Map();
let nextId = 0;
function call(method, args) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    send({ t: "call", id, m: method, a: args });
  });
}

const init = JSON.parse(await nextLine());

(async () => {
  for (let line; (line = await nextLine()) !== null;) {
    const msg = JSON.parse(line);
    const p = pending.get(msg.id);
    if (!p) continue;
    pending.delete(msg.id);
    msg.ok ? p.resolve(msg.v) : p.reject(new Error(msg.e));
  }
})();

const logs = [];
const fmtArg = (a) => typeof a === "string" ? a : (() => {
  try { return JSON.stringify(a, null, 1); } catch { return String(a); }
})();
const log = (...a) => logs.push(a.map(fmtArg).join(" "));
globalThis.console = { log, info: log, warn: log, error: log, debug: log, table: log, dir: log };

const canvas = Object.freeze({
  tz: init.tz,
  get: (path, params) => call("get", [path, params ?? null]),
  getAll: (path, params, maxItems) => call("getAll", [path, params ?? null, maxItems ?? 500]),
  courses: () => call("courses", []),
  course: (ref) => call("course", [ref]),
  fileText: (fileId, limit) => call("fileText", [fileId, limit ?? 20000]),
  htmlToText: (html, limit) => call("htmlToText", [html, limit ?? 20000]),
  tool: (name, args) => call("tool", [name, args ?? {}]),
  fmt: (iso) => iso
    ? new Date(iso).toLocaleString("sv-SE", { timeZone: init.tz }).slice(0, 16)
    : "—",
});

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
let result, error;
try {
  result = await new AsyncFunction("canvas", init.code)(canvas);
} catch (e) {
  error = e instanceof Error ? `${e.name}: ${e.message}` : String(e);
}
let safeResult;
try { safeResult = JSON.parse(JSON.stringify(result ?? null)); } catch { safeResult = String(result); }
await send({ t: "done", logs, result: safeResult, error: error ?? null });
Deno.exit(0);
