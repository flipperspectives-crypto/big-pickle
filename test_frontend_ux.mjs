// Behavioral verification for the Clarity frontend UX logic.
// Runs app.js inside a minimal DOM stub (no real browser/jsdom needed) and
// exercises the pure helpers exposed on window.ClarityUX.
import fs from "fs";
import path from "path";
import url from "url";
import vm from "vm";

const __dirname = path.dirname(url.fileURLToPath(import.meta.url));
const code = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");

function makeEl() {
  const el = {
    _children: [], style: {}, dataset: {}, title: "",
    classList: {
      _s: new Set(),
      add(...a) { a.forEach((x) => this._s.add(x)); },
      remove(...a) { a.forEach((x) => this._s.delete(x)); },
      toggle(c, f) {
        if (f === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); }
        else { f ? this._s.add(c) : this._s.delete(c); }
        return this._s.has(c);
      },
      contains(c) { return this._s.has(c); },
    },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { this._children.push(c); return c; },
    insertBefore(c) { this._children.push(c); return c; }, remove() {},
    querySelector() { return makeEl(); }, querySelectorAll() { return []; },
    focus() {}, scrollIntoView() {},
    set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html || ""; },
    set textContent(v) { this._text = v; }, get textContent() { return this._text || ""; },
    set value(v) { this._value = v; }, get value() { return this._value || ""; },
    set hidden(v) { this._hidden = v; }, get hidden() { return this._hidden; },
    set type(v) { this._type = v; }, get type() { return this._type || ""; },
    set disabled(v) {}, get disabled() { return false; },
    get firstChild() { return null; }, get nextElementSibling() { return null; },
  };
  return el;
}

const idCache = {};
const document = {
  getElementById(id) { return (idCache[id] = idCache[id] || makeEl()); },
  querySelector() { return makeEl(); },
  querySelectorAll() { return []; },
  createElement() { return makeEl(); },
  addEventListener() {},
  body: makeEl(),
};

function fetchStub(u) {
  return Promise.resolve({
    ok: true, status: 200,
    json: async () => {
      const s = String(u);
      if (s.includes("/health")) return { status: "ok" };
      if (s.includes("/models")) return { data: [] };
      if (s.includes("/status")) return { status: "ok", providers: {}, gateway: { active_keys: 0, total_balance_usd: 0 }, timestamp: "2026-01-01T00:00:00Z" };
      return {};
    },
  });
}

let unhandled = null;
process.on("unhandledRejection", (e) => { unhandled = e; });

const sandbox = {
  document,
  console: { log: () => {}, warn: () => {}, error: () => {} },
  navigator: { clipboard: { writeText: async () => {} } },
  performance: { now: () => Date.now() },
  fetch: fetchStub,
  setTimeout,
  matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  addEventListener: () => {},
  scrollY: 0,
};
sandbox.window = sandbox;
sandbox.window.location = { origin: "http://test" };
sandbox.window.matchMedia = sandbox.matchMedia;

vm.createContext(sandbox);
vm.runInContext(code, sandbox);

await new Promise((r) => setTimeout(r, 80));

const ux = sandbox.window.ClarityUX;
let failures = 0;
function check(name, cond) {
  if (!cond) { failures++; console.error("FAIL:", name); }
  else console.log("ok  -", name);
}

check("IIFE booted without throwing", !unhandled && !!ux);
check("mapPlaygroundState 200 -> success", ux.mapPlaygroundState(200) === "success");
check("mapPlaygroundState 402 -> insufficient", ux.mapPlaygroundState(402) === "insufficient");
check("mapPlaygroundState 404 -> unavailable", ux.mapPlaygroundState(404) === "unavailable");
check("mapPlaygroundState 422 -> unavailable", ux.mapPlaygroundState(422) === "unavailable");
check("mapPlaygroundState 503 -> unavailable", ux.mapPlaygroundState(503) === "unavailable");
check("mapPlaygroundState 500 -> error", ux.mapPlaygroundState(500) === "error");
check("mapPlaygroundState 401 -> error", ux.mapPlaygroundState(401) === "error");
check("PG_STATE_LABELS has insufficient", ux.PG_STATE_LABELS.insufficient === "Insufficient balance");

const r1 = ux.safeReason("unreachable: ConnectError");
check("safeReason keeps plain reason", r1 === "unreachable: ConnectError");
const r2 = ux.safeReason("connect http://10.0.0.5:11434 failed");
check("safeReason strips IP:port", r2.includes("[host]") && !r2.includes("10.0.0.5"));
const r3 = ux.safeReason("tls error at api.internal.example.com");
check("safeReason strips hostname", r3.includes("[host]") && !r3.includes("api.internal"));
const r4 = ux.safeReason("");
check("safeReason empty -> empty", r4 === "");

console.log(failures === 0 ? "\nALL FRONTEND UX CHECKS PASSED" : `\n${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
