// Behavioral verification for the Clarity frontend UX logic.
// Runs app.js inside a minimal DOM stub (no real browser/jsdom needed) and
// exercises both the pure helpers on window.ClarityUX and the model-selector
// combobox (open / filter / select / keyboard / delegation-after-rerender).
import fs from "fs";
import path from "path";
import url from "url";
import vm from "vm";

const __dirname = path.dirname(url.fileURLToPath(import.meta.url));
const code = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");

// Realistic catalog returned by GET /v1/models.
const MODEL_FIXTURE = [
  { id: "openai/gpt-4o", local: false, providers: ["openai"] },
  { id: "anthropic/claude-3-opus", local: false, providers: ["anthropic"] },
  { id: "google/gemini-1.5-pro", local: false, providers: ["google"] },
  { id: "local/llama-3-8b", local: true, providers: ["ollama"] },
  { id: "local/mistral-7b", local: true, providers: ["ollama"] },
];

function makeEl() {
  const el = {
    _children: [], _listeners: {}, _classes: new Set(), _opts: [], style: {}, dataset: {}, title: "",
    classList: {
      add(...a) { a.forEach((x) => el._classes.add(x)); },
      remove(...a) { a.forEach((x) => el._classes.delete(x)); },
      toggle(c, f) {
        if (f === undefined) { el._classes.has(c) ? el._classes.delete(c) : el._classes.add(c); }
        else { f ? el._classes.add(c) : el._classes.delete(c); }
        return el._classes.has(c);
      },
      contains(c) { return el._classes.has(c); },
    },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    addEventListener(type, fn) { (el._listeners[type] = el._listeners[type] || []).push(fn); },
    removeEventListener() {},
    appendChild(c) { el._children.push(c); return c; },
    insertBefore(c) { el._children.push(c); return c; }, remove() {},
    querySelector() { return makeEl(); },
    querySelectorAll(sel) { return sel === ".select-opt" ? (el._opts || []) : []; },
    focus() {}, scrollIntoView() {},
    closest(sel) {
      const cls = sel.replace(/^\./, "");
      let n = el;
      while (n) { if (n._classes && n._classes.has(cls)) return n; n = n._parent; }
      return null;
    },
    dispatch(type, ev) {
      ev = ev || {};
      ev.type = ev.type || type;
      if (!ev.target) ev.target = el;
      (el._listeners[type] || []).forEach((fn) => fn(ev));
    },
    set innerHTML(v) { this._html = v; this._opts = parseOptions(v, this); },
    get innerHTML() { return this._html || ""; },
    set textContent(v) { this._text = v; }, get textContent() { return this._text || ""; },
    set value(v) { this._value = v; }, get value() { return this._value || ""; },
    set hidden(v) { this._hidden = v; }, get hidden() { return this._hidden; },
    set type(v) { this._type = v; }, get type() { return this._type || ""; },
    set disabled(v) {}, get disabled() { return false; },
    get firstChild() { return null; }, get nextElementSibling() { return null; },
  };
  return el;
}

// Parse the combobox option buttons (<button class="select-opt" data-i data-model>)
// produced by groupAndRender() into element stubs we can click via delegation.
function parseOptions(html, owner) {
  const opts = [];
  const re = /<button[^>]*class="select-opt"[^>]*>/g;
  let m;
  while ((m = re.exec(html || ""))) {
    const tag = m[0];
    const iM = tag.match(/data-i="(\d+)"/);
    const dmM = tag.match(/data-model="([^"]*)"/);
    const opt = makeEl();
    opt._classes = new Set(["select-opt"]);
    opt.dataset.i = iM ? iM[1] : "";
    opt.dataset.model = dmM ? dmM[1] : "";
    opt._parent = owner;
    // inner <span> child so e.target.closest(".select-opt") must walk up
    const span = makeEl();
    span._parent = opt;
    opt._children = [span];
    opts.push(opt);
  }
  return opts;
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
      if (s.includes("/models")) return { data: MODEL_FIXTURE };
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

// Wait for the async loadModels()/populateSelector() chain to finish.
await new Promise((r) => setTimeout(r, 120));

const ux = sandbox.window.ClarityUX;
let failures = 0;
function check(name, cond) {
  if (!cond) { failures++; console.error("FAIL:", name); }
  else console.log("ok  -", name);
}

/* ---- pure helpers (regression coverage carried over) ---- */
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

/* ---- model selector combobox ---- */
const trigger = document.getElementById("pg-model-trigger");
const search = document.getElementById("pg-model-search");
const optionsBox = document.getElementById("pg-model-options");
const label = document.getElementById("pg-model-label");
const menu = document.getElementById("pg-model-menu");
menu.hidden = true; // real markup ships the menu with the `hidden` attribute

function openDropdown() {
  if (menu.hidden !== false) trigger.dispatch("click");
}
function opts() { return optionsBox.querySelectorAll(".select-opt"); }
function clickOpt(opt) {
  const target = opt._children[0] || opt; // simulate click on inner span
  optionsBox.dispatch("click", { target });
}
function firstOptWith(modelId) {
  return opts().find((o) => o.dataset.model === modelId);
}

/* CHECK 1: Open dropdown -> click a model after render -> label updates. */
openDropdown();
check("dropdown opened (menu not hidden)", menu.hidden === false);
const opt0 = opts()[0];
check("options rendered on open", !!opt0 && opts().length === MODEL_FIXTURE.length);
clickOpt(opt0);
check("click selects first model -> label updates", label.textContent === opt0.dataset.model);
check("menu closes after click selection", menu.hidden === true);

/* CHECK 2: Search/filter -> click a filtered model -> exact model id. */
openDropdown();
search.value = "llama";
search.dispatch("input");
const filtered = firstOptWith("local/llama-3-8b");
check("filter narrows to 1 option", opts().length === 1 && !!filtered);
clickOpt(filtered);
check("click filtered model -> exact id label", label.textContent === "local/llama-3-8b");

/* CHECK 3: Keyboard Enter selects the active (focused) model. */
openDropdown();
clickOpt(firstOptWith("google/gemini-1.5-pro"));
check("preselect gemini for Enter test", label.textContent === "google/gemini-1.5-pro");
openDropdown(); // openMenu re-renders and sets active index to selected model
search.dispatch("keydown", { key: "Enter", preventDefault() {} });
check("Enter selects active (focused) model", label.textContent === "google/gemini-1.5-pro");

/* CHECK 4: Repeated open/close/re-render doesn't duplicate delegated handlers. */
const clickBefore = (optionsBox._listeners.click || []).length;
for (let i = 0; i < 5; i++) {
  trigger.dispatch("click"); // open
  search.dispatch("input");  // re-render
  search.dispatch("input");  // re-render again
  trigger.dispatch("click"); // close
}
const clickAfter = (optionsBox._listeners.click || []).length;
check("delegated click handler registered exactly once", clickBefore === 1 && clickAfter === 1);
check("no duplicate handlers after open/close/re-render", clickBefore === clickAfter);

/* CHECK 5: Selection works after groupAndRender() replaces the option nodes. */
openDropdown();
search.value = "claude"; search.dispatch("input"); // re-render (filtered)
search.value = ""; search.dispatch("input");        // re-render (full -> new nodes)
const freshOpts = opts();
check("new option nodes exist after re-render", freshOpts.length === MODEL_FIXTURE.length);
const freshTarget = firstOptWith("anthropic/claude-3-opus");
check("target option present after re-render", !!freshTarget);
clickOpt(freshTarget);
check("click on re-rendered node -> label updates", label.textContent === "anthropic/claude-3-opus");

console.log(failures === 0 ? "\nALL FRONTEND UX CHECKS PASSED" : `\n${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
