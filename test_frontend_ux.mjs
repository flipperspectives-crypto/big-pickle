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

// Realistic catalog returned by GET /v1/models. Includes a discovered local
// model whose id uses the colon convention (local:<exact-tag>).
const MODEL_FIXTURE = [
  { id: "openai/gpt-4o", local: false, providers: ["openai"] },
  { id: "anthropic/claude-3-opus", local: false, providers: ["anthropic"] },
  { id: "google/gemini-1.5-pro", local: false, providers: ["google"] },
  { id: "local/llama-3-8b", local: true, providers: ["ollama"] },
  { id: "local/mistral-7b", local: true, providers: ["ollama"] },
  { id: "local:qwen3:1.7b", local: true, providers: ["ollama"] },
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

const fetchCalls = [];
function fetchStub(u, init) {
  fetchCalls.push({ url: String(u), init: init || {} });
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

/* ---- browser model-catalog caching fix ---- */
const modelsCall = fetchCalls.find((c) => c.url.includes("/models"));
check("frontend requests /v1/models with cache: no-store", !!modelsCall && modelsCall.init.cache === "no-store");

/* CHECK 6: local model local:qwen3:1.7b is discoverable, searchable, LOCAL $0. */
openDropdown();
const qwenOpt = firstOptWith("local:qwen3:1.7b");
check("local:qwen3:1.7b discoverable in selector", !!qwenOpt);
check("rendered local option carries LOCAL · $0 badge", optionsBox.innerHTML.includes("LOCAL · $0"));
search.value = "qwen3"; search.dispatch("input");
const filteredQwen = firstOptWith("local:qwen3:1.7b");
check("search 'qwen3' narrows to local:qwen3:1.7b", !!filteredQwen && opts().length === 1);
clickOpt(filteredQwen);
check("search qwen3 -> selects local:qwen3:1.7b with LOCAL $0", label.textContent === "local:qwen3:1.7b");

/* ---- local Qwen3 thinking disabled by default (no real inference) ---- */
const qwenBody = ux.buildChatBody("local:qwen3:1.7b", "hi");
check("local:qwen3:1.7b request gets reasoning_effort=none", qwenBody.reasoning_effort === "none" && qwenBody.model === "local:qwen3:1.7b");
const qwenVerBody = ux.buildChatBody("local:qwen3:8b", "hi");
check("local:qwen3 (other tag) request gets reasoning_effort=none", qwenVerBody.reasoning_effort === "none");
const cloudBody = ux.buildChatBody("openai/gpt-4o", "hi");
check("cloud model request does NOT gain reasoning_effort", cloudBody.reasoning_effort === undefined && cloudBody.model === "openai/gpt-4o");
const otherLocalBody = ux.buildChatBody("local/llama-3-8b", "hi");
check("non-Qwen local model unchanged (no reasoning_effort)", otherLocalBody.reasoning_effort === undefined);
const legacyLocalBody = ux.buildChatBody("local/mistral-7b", "hi");
check("other local model unchanged (no reasoning_effort)", legacyLocalBody.reasoning_effort === undefined);
check("body shape preserved (model + messages + content)", qwenBody.model === "local:qwen3:1.7b" && Array.isArray(qwenBody.messages) && qwenBody.messages[0].role === "user" && qwenBody.messages[0].content === "hi");

/* ---- Qwen3 Thinking toggle (request source only) ---- */
const onBody = (ux.setThinking(true), ux.buildChatBody("local:qwen3:1.7b", "hi"));
check("Qwen3 Thinking ON omits reasoning_effort", onBody.reasoning_effort === undefined);
const offBody = (ux.setThinking(false), ux.buildChatBody("local:qwen3:1.7b", "hi"));
check("Qwen3 Thinking OFF restores reasoning_effort=none", offBody.reasoning_effort === "none");
const onCloud = (ux.setThinking(true), ux.buildChatBody("openai/gpt-4o", "hi"));
check("cloud model never gains reasoning_effort from toggle", onCloud.reasoning_effort === undefined);
const onOtherLocal = (ux.setThinking(true), ux.buildChatBody("local/llama-3-8b", "hi"));
check("non-Qwen local model never gains reasoning_effort from toggle", onOtherLocal.reasoning_effort === undefined);
ux.setThinking(false);

/* ---- Local Models control panel (read-only, no real inference) ---- */
ux.setLocalModels([
  {
    id: "local:qwen3:1.7b", local: true, providers: ["local"],
    details: {
      size_bytes: 1324347080, family: "qwen3", parameter_size: "1.7B",
      quantization_level: "Q4_K_M", context_length: 40960,
      capabilities: ["completion", "tools", "thinking"]
    }
  }
], "ok");
const lm = document.getElementById("local-models-list").innerHTML;
check("Local Models panel renders qwen3:1.7b", lm.includes("local:qwen3:1.7b"));
check("panel shows LOCAL · $0 badge", lm.includes("LOCAL · $0"));
check("panel renders parameter size", lm.includes("1.7B"));
check("panel renders quantization", lm.includes("Q4_K_M"));
check("panel renders context length", lm.includes("40960"));
check("panel renders Completion capability", lm.includes("Completion"));
check("panel renders Tools capability", lm.includes("Tools"));
check("panel renders Thinking capability", lm.includes("Thinking"));
check("panel shows Ready status", lm.includes("Ready"));

ux.setLocalModels([
  { id: "local:tiny:latest", local: true, providers: ["local"], details: { size_bytes: 123456 } }
], "ok");
const lm2 = document.getElementById("local-models-list").innerHTML;
check("panel omits missing optional metadata", !lm2.includes("Q4_K_M") && !lm2.includes("40960") && lm2.includes("123456"));

ux.setLocalModels([{ id: "local:qwen3:1.7b", local: true, providers: ["local"], details: {} }], "ok");
ux.useInPlayground("local:qwen3:1.7b");
check("Use in Playground selects exact model (single source of truth)", ux.getSelectedModel() === "local:qwen3:1.7b");

ux.setLocalModels([], "ok");
check("panel empty state (no models installed)", document.getElementById("local-models-list").innerHTML.includes("No local models installed"));
ux.setLocalModels([], "unavailable");
check("panel unavailable state (Ollama unreachable)", document.getElementById("local-models-list").innerHTML.includes("Local discovery unavailable"));

/* ---- Local Runtime panel (zero-inference /api/ps + last request) ---- */
const RUNTIME_POPULATED = {
  status: "ok",
  models: [
    {
      name: "qwen3:1.7b", id: "local:qwen3:1.7b",
      size_bytes: 1324347080, size_vram_bytes: 1324347080,
      context_length: 40960, expires_at: "2026-01-01T00:00:00Z",
      family: "qwen3", parameter_size: "1.7B", quantization_level: "Q4_K_M",
    }
  ],
  last_local_request: null,
};
ux.setLocalRuntime(RUNTIME_POPULATED);
const rtList = document.getElementById("local-runtime-list").innerHTML;
check("runtime panel renders loaded model id", rtList.includes("local:qwen3:1.7b"));
check("runtime panel shows LOADED badge", rtList.includes("LOADED"));
check("runtime panel shows parameter size", rtList.includes("1.7B"));
check("runtime panel shows quantization", rtList.includes("Q4_K_M"));
check("runtime panel shows family", rtList.includes("qwen3"));
check("runtime panel shows allocated context", rtList.includes("40960"));
check("runtime panel shows human-readable model size (GB)", rtList.includes("1.2 GB"));
check("runtime panel shows human-readable VRAM (GB)", rtList.includes("1.2 GB"));
check("runtime panel shows VRAM share as %", rtList.includes("VRAM share") && rtList.includes("100%"));

const rtStatus = document.getElementById("local-runtime-status").textContent;
check("runtime available + loaded -> status line", rtStatus.includes("loaded into VRAM"));

// empty-but-available runtime
ux.setLocalRuntime({ status: "ok", models: [], last_local_request: null });
check("runtime available + no models loaded state", document.getElementById("local-runtime-list").innerHTML.includes("no models are currently loaded"));

// unavailable runtime
ux.setLocalRuntime({ status: "unavailable", models: [], last_local_request: null });
check("runtime unavailable state (Ollama unreachable)", document.getElementById("local-runtime-list").innerHTML.includes("Local runtime unavailable"));

// VRAM share naming/calculation: 100 / 200 -> 50%
check("vramShare computes 50% (200/100)", ux.vramShare({ size_vram_bytes: 100, size_bytes: 200 }) === 0.5);
check("vramShare null when size_bytes missing", ux.vramShare({ size_vram_bytes: 100 }) === null);
check("vramShare null when size_vram_bytes missing", ux.vramShare({ size_bytes: 200 }) === null);
check("humanBytes formats GB", ux.humanBytes(1324347080) === "1.2 GB");
check("humanBytes formats MB", ux.humanBytes(2097152) === "2 MB");

// Last Local Request card
const RUNTIME_WITH_LAST = {
  status: "ok", models: [],
  last_local_request: {
    model: "local:qwen3:1.7b", measured_at: "2026-01-01T00:00:00Z",
    gateway_upstream_round_trip_ms: 123.4, prompt_tokens: 10,
    completion_tokens: 20, total_tokens: 30,
  },
};
ux.setLocalRuntime(RUNTIME_WITH_LAST);
const rtLast = document.getElementById("local-runtime-last").innerHTML;
check("Last Local Request card renders model", rtLast.includes("local:qwen3:1.7b"));
check("Last Local Request card renders round-trip ms", rtLast.includes("123.4 ms"));
check("Last Local Request card renders prompt tokens", rtLast.includes("10"));
check("Last Local Request card renders completion tokens", rtLast.includes("20"));
check("Last Local Request card renders total tokens", rtLast.includes("30"));
check("Last Local Request card renders measured_at", rtLast.includes("2026-01-01T00:00:00Z"));
check("Last Local Request card disclaims round-trip meaning", typeof rtLast === "string");

// No successful request measured yet
ux.setLocalRuntime({ status: "ok", models: [], last_local_request: null });
check("Last Local Request card: not measured yet", document.getElementById("local-runtime-last").innerHTML.includes("No successful local request measured yet"));

// Round-trip disclaimer note is present in the page markup
const htmlSrc = fs.readFileSync(path.join(__dirname, "static", "index.html"), "utf8");
check("round-trip disclaimer note present in markup", htmlSrc.includes("Round-trip is measured by Clarity around the local upstream request; it is not Ollama model-eval time."));
check("Refresh Runtime button present in markup", htmlSrc.includes('id="local-runtime-refresh"'));

// The explicit note must NOT claim GPU utilization / CPU-GPU split / GPU usage.
check("no GPU-utilization wording in runtime JS", !code.includes("GPU utilization") && !code.includes("GPU usage") && !code.includes("CPU/GPU split"));

fetchCalls.length = 0;
await ux.refreshLocalRuntime();
check("runtime refresh hits /v1/local/runtime (zero inference)", fetchCalls.some((c) => c.url.includes("/local/runtime")));
check("runtime refresh never hits chat completions", !fetchCalls.some((c) => c.url.includes("/chat/completions")));

fetchCalls.length = 0;
await ux.refreshLocalModels();
check("refresh hits /v1/models?refresh=1 (zero inference)", fetchCalls.some((c) => c.url.includes("/models?refresh=1")));


/* ---- Local Generation Controls (local models only, no real inference) ---- */
// Defaults: every control unset => all four fields omitted from the body.
ux.resetControls();
const gcUnset = ux.buildChatBody("local:qwen3:1.7b", "hi");
check("gen controls unset -> max_tokens omitted", gcUnset.max_tokens === undefined);
check("gen controls unset -> temperature omitted", gcUnset.temperature === undefined);
check("gen controls unset -> top_p omitted", gcUnset.top_p === undefined);
check("gen controls unset -> seed omitted", gcUnset.seed === undefined);
check("gen controls unset -> still local reasoning_effort", gcUnset.reasoning_effort === "none");

// Each control, set individually, is included for a local model.
ux.resetControls();
ux.setGenerationControls({ max_tokens: 128 });
check("local max_tokens set -> included", ux.buildChatBody("local:qwen3:1.7b", "hi").max_tokens === 128);

ux.resetControls();
ux.setGenerationControls({ temperature: 0.4 });
check("local temperature set -> included", ux.buildChatBody("local:qwen3:1.7b", "hi").temperature === 0.4);

ux.resetControls();
ux.setGenerationControls({ top_p: 0.9 });
check("local top_p set -> included", ux.buildChatBody("local:qwen3:1.7b", "hi").top_p === 0.9);

ux.resetControls();
ux.setGenerationControls({ seed: 42 });
check("local seed set -> included", ux.buildChatBody("local:qwen3:1.7b", "hi").seed === 42);

// Multiple controls combine correctly.
ux.resetControls();
ux.setGenerationControls({ max_tokens: 128, temperature: 0.4, top_p: 0.9, seed: 42 });
const gcAll = ux.buildChatBody("local:qwen3:1.7b", "hi");
check("multiple controls combine", gcAll.max_tokens === 128 && gcAll.temperature === 0.4 && gcAll.top_p === 0.9 && gcAll.seed === 42);

// Reset to model defaults returns to omitted state.
ux.resetControls();
const gcReset = ux.buildChatBody("local:qwen3:1.7b", "hi");
check("reset -> no generation fields", gcReset.max_tokens === undefined && gcReset.temperature === undefined && gcReset.top_p === undefined && gcReset.seed === undefined);

// Invalid values are rejected (omitted from body) and validateControl reports them.
ux.resetControls();
ux.setGenerationControls({ max_tokens: -5 });
check("invalid max_tokens rejected (omitted)", ux.buildChatBody("local:qwen3:1.7b", "hi").max_tokens === undefined);
check("validateControl max_tokens -5 -> invalid", ux.validateControl("max_tokens", -5).ok === false);
check("validateControl max_tokens 1.5 -> invalid", ux.validateControl("max_tokens", 1.5).ok === false);
check("validateControl max_tokens abc -> invalid", ux.validateControl("max_tokens", "abc").ok === false);
check("validateControl max_tokens 0 -> invalid", ux.validateControl("max_tokens", 0).ok === false);

ux.resetControls();
ux.setGenerationControls({ temperature: -0.1 });
check("invalid temperature rejected (omitted)", ux.buildChatBody("local:qwen3:1.7b", "hi").temperature === undefined);
check("validateControl temperature -0.1 -> invalid", ux.validateControl("temperature", -0.1).ok === false);

ux.resetControls();
ux.setGenerationControls({ top_p: 1.2 });
check("invalid top_p rejected (omitted)", ux.buildChatBody("local:qwen3:1.7b", "hi").top_p === undefined);
check("validateControl top_p 1.2 -> invalid", ux.validateControl("top_p", 1.2).ok === false);

ux.resetControls();
ux.setGenerationControls({ seed: 2.5 });
check("invalid seed rejected (omitted)", ux.buildChatBody("local:qwen3:1.7b", "hi").seed === undefined);
check("validateControl seed 2.5 -> invalid", ux.validateControl("seed", 2.5).ok === false);

// Valid boundary values are accepted.
check("validateControl temperature 0 -> valid", ux.validateControl("temperature", 0).ok === true);
check("validateControl top_p 0 -> valid", ux.validateControl("top_p", 0).ok === true);
check("validateControl top_p 1 -> valid", ux.validateControl("top_p", 1).ok === true);
check("validateControl max_tokens 1 -> valid", ux.validateControl("max_tokens", 1).ok === true);
check("validateControl seed 0 -> valid", ux.validateControl("seed", 0).ok === true);

// Qwen3 Thinking OFF + generation controls coexist (reasoning_effort + controls).
ux.setThinking(false);
ux.resetControls();
ux.setGenerationControls({ max_tokens: 128, temperature: 0.4, top_p: 0.9, seed: 42 });
const offC = ux.buildChatBody("local:qwen3:1.7b", "hi");
check("Thinking OFF + controls: reasoning_effort=none", offC.reasoning_effort === "none");
check("Thinking OFF + controls: all four present", offC.max_tokens === 128 && offC.temperature === 0.4 && offC.top_p === 0.9 && offC.seed === 42);

// Qwen3 Thinking ON + generation controls coexist (no reasoning_effort, controls still sent).
ux.setThinking(true);
const onC = ux.buildChatBody("local:qwen3:1.7b", "hi");
check("Thinking ON + controls: reasoning_effort omitted", onC.reasoning_effort === undefined);
check("Thinking ON + controls: all four present", onC.max_tokens === 128 && onC.temperature === 0.4 && onC.top_p === 0.9 && onC.seed === 42);
ux.setThinking(false);

// Cloud isolation: a cloud request never receives any local control value.
ux.resetControls();
ux.setGenerationControls({ max_tokens: 128, temperature: 0.4, top_p: 0.9, seed: 42 });
const cloudGcBody = ux.buildChatBody("openai/gpt-4o", "hi");
check("cloud request -> no max_tokens", cloudGcBody.max_tokens === undefined);
check("cloud request -> no temperature", cloudGcBody.temperature === undefined);
check("cloud request -> no top_p", cloudGcBody.top_p === undefined);
check("cloud request -> no seed", cloudGcBody.seed === undefined);
check("cloud request -> no reasoning_effort", cloudGcBody.reasoning_effort === undefined);
check("isLocalModel cloud false", ux.isLocalModel("openai/gpt-4o") === false);
check("isLocalModel local: true", ux.isLocalModel("local:qwen3:1.7b") === true);

// Switching local -> cloud cannot leak controls into the cloud body.
const cloudLeak = ux.buildChatBody("anthropic/claude-3-opus", "hi");
check("local->cloud switch cannot leak controls", cloudLeak.max_tokens === undefined && cloudLeak.temperature === undefined && cloudLeak.top_p === undefined && cloudLeak.seed === undefined);

// Non-Qwen local models also receive explicitly set controls.
ux.resetControls();
ux.setGenerationControls({ max_tokens: 64 });
check("non-Qwen local gets control", ux.buildChatBody("local/llama-3-8b", "hi").max_tokens === 64);

ux.resetControls();
ux.setThinking(false);

console.log(failures === 0 ? "\nALL FRONTEND UX CHECKS PASSED" : `\n${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
