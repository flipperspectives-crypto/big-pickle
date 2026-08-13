/* Clarity — gateway frontend. All data comes from the live backend. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var ORIGIN = window.location.origin;
  var API_BASE = ORIGIN + "/v1";
  var REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* Last key created this session (in memory only — never persisted, never logged). */
  var sessionKey = "";
  var lastBalance = null;

  /* Local model discovery state (read-only panel). */
  var LOCAL_STATUS = "ok";        // "ok" | "unavailable" (from /v1/models)
  var localThinkingOn = false;    // Qwen3 Thinking toggle; default OFF (verified)

  /* Local Generation Controls (LOCAL models only). Each entry is null when the
     control is unset ("Model default" => the field is omitted from the request).
     Values are kept in memory only — never persisted to browser storage or the DB. */
  var genControls = { max_tokens: null, temperature: null, top_p: null, seed: null };

  /* =========================================================================
     Pure helpers (exposed on window.ClarityUX for verification/tests)
     ========================================================================= */
  var PG_STATE_LABELS = {
    ready: "Ready",
    busy: "Running",
    success: "Success",
    insufficient: "Insufficient balance",
    unavailable: "Unavailable",
    error: "Error"
  };

  function mapPlaygroundState(status) {
    if (status === 200) return "success";
    if (status === 402) return "insufficient";
    if (status === 401 || status === 403) return "error";
    if (status === 404 || status === 422 || status === 503 || status === 504) return "unavailable";
    if (status >= 500) return "error";
    return "error";
  }

  /* Strip anything that looks like a hostname/URL so provider probe reasons can
     never leak internal hostnames or upstream URLs into the UI. */
  function safeReason(reason) {
    if (!reason) return "";
    return String(reason)
      .replace(/https?:\/\/[^\s"'`,;<>()]+/gi, "[host]")
      .replace(/\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b/g, "[host]")
      .replace(/\b[a-z0-9_-]+(?:\.[a-z0-9_-]+){1,}\.(?:com|net|org|io|ai|sh|dev|local|internal|example)\b/gi, "[host]");
  }

  /* Validate a single Local Generation Control raw value.
     Empty / null / undefined => unset ("Model default"): { ok:true, value:null }.
     Otherwise returns { ok, value, error }. We never invent a default. */
  function validateControl(name, raw) {
    if (raw === null || raw === undefined || raw === "") return { ok: true, value: null };
    var n = Number(raw);
    if (!Number.isFinite(n)) return { ok: false, error: "Enter a number." };
    if (name === "max_tokens") {
      if (!Number.isInteger(n) || n <= 0) return { ok: false, error: "Must be a positive whole number." };
    } else if (name === "seed") {
      if (!Number.isInteger(n)) return { ok: false, error: "Must be a whole number." };
    } else if (name === "temperature") {
      if (n < 0) return { ok: false, error: "Must be 0 or greater." };
    } else if (name === "top_p") {
      if (n < 0 || n > 1) return { ok: false, error: "Must be between 0 and 1." };
    }
    return { ok: true, value: n };
  }

  /* A model is local if it uses the local:* convention or was discovered as a
     local model. Cloud models never receive generation controls. */
  function isLocalModel(m) {
    if (typeof m !== "string") return false;
    if (m.indexOf("local:") === 0) return true;
    return MODELS.some(function (x) { return x.id === m && x.local; });
  }

  /* First invalid, non-empty control => concise message, else null. Used to
     block submission of the whole request when any control is invalid. */
  function firstControlError() {
    var fields = ["max_tokens", "temperature", "top_p", "seed"];
    for (var i = 0; i < fields.length; i++) {
      var raw = genControls[fields[i]];
      if (raw === null || raw === undefined || raw === "") continue;
      var res = validateControl(fields[i], raw);
      if (!res.ok) return res.error;
    }
    return null;
  }

  /* Build the /v1/chat/completions request body. Local Qwen3 models reason by
      default in Ollama, which appends a "<think:6124c78e>…</think:6124c78e>" block; for normal
      Playground use we disable that so responses are concise. Only local Qwen3
      models get reasoning_effort="none" — cloud and other local models are left
      untouched (no blanket change to unrelated providers). */
  function buildChatBody(model, prompt) {
    var body = {
      model: model,
      messages: [{ role: "user", content: prompt }]
    };
    // Local Qwen3: disable thinking by default (verified). When the user turns
    // Thinking ON we omit reasoning_effort entirely and let Ollama/Qwen3 use its
    // default behavior. Never applied to cloud or non-Qwen models.
    if (typeof model === "string" && model.indexOf("local:qwen3:") === 0 && !localThinkingOn) {
      body.reasoning_effort = "none";
    }
    // Local Generation Controls: include only explicitly-set, valid values.
    // Cloud models and unset controls are omitted, so existing behavior for
    // cloud and untouched-local requests is unchanged.
    if (isLocalModel(model)) {
      ["max_tokens", "temperature", "top_p", "seed"].forEach(function (k) {
        var raw = genControls[k];
        if (raw === null || raw === undefined || raw === "") return;   // Model default => omit
        var res = validateControl(k, raw);
        if (res.ok && res.value !== null) body[k] = res.value;          // invalid => not submitted
      });
    }
    return body;
  }

  window.ClarityUX = {
    mapPlaygroundState: mapPlaygroundState,
    safeReason: safeReason,
    buildChatBody: buildChatBody,
    setThinking: function (on) { localThinkingOn = !!on; },
    setGenerationControls: function (obj) {
      ["max_tokens", "temperature", "top_p", "seed"].forEach(function (k) {
        genControls[k] = (obj && obj[k] !== undefined) ? obj[k] : null;
      });
    },
    getGenerationControls: function () { return genControls; },
    resetControls: function () {
      genControls = { max_tokens: null, temperature: null, top_p: null, seed: null };
    },
    validateControl: validateControl,
    isLocalModel: isLocalModel,
    refreshDiagnostics: refreshDiagnostics,
    renderDiagnostics: renderDiagnostics,
    getSelectedModel: function () { return selectedModel; },
    setLocalModels: function (list, status) {
      MODELS = (list || []).map(function (m) {
        return {
          id: m.id,
          local: !!m.local,
          providers: Array.isArray(m.providers) ? m.providers : [],
          details: (m.details && typeof m.details === "object") ? m.details : null
        };
      });
      LOCAL_STATUS = (status === "unavailable") ? "unavailable" : "ok";
      renderLocalModels();
    },
    useInPlayground: useInPlayground,
    refreshLocalModels: refreshLocalModels,
    renderLocalModels: renderLocalModels,
    setLocalRuntime: function (data) {
      RUNTIME_DATA = (data && typeof data === "object") ? data : { status: "unavailable", models: [] };
      renderLocalRuntime();
    },
    getRuntimeData: function () { return RUNTIME_DATA; },
    refreshLocalRuntime: refreshLocalRuntime,
    renderLocalRuntime: renderLocalRuntime,
    humanBytes: humanBytes,
    vramShare: vramShare,
    PG_STATE_LABELS: PG_STATE_LABELS
  };

  /* =========================================================================
     DOM helpers
     ========================================================================= */
  function el(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function setError(id, msg) {
    var box = el(id);
    if (!box) return;
    if (msg) {
      box.textContent = msg;
      box.hidden = false;
    } else {
      box.hidden = true;
      box.textContent = "";
    }
  }

  function setBtnLoading(btn, loading, label) {
    var labelEl = btn.querySelector(".btn-label");
    if (loading) {
      btn.disabled = true;
      btn.dataset.label = labelEl ? labelEl.textContent : "";
      if (labelEl) labelEl.textContent = label || "Working…";
      var spin = document.createElement("span");
      spin.className = "spin";
      spin.setAttribute("aria-hidden", "true");
      spin.dataset.spin = "1";
      btn.insertBefore(spin, btn.firstChild);
    } else {
      btn.disabled = false;
      var s = btn.querySelector('[data-spin="1"]');
      if (s) s.remove();
      if (labelEl) labelEl.textContent = btn.dataset.label || "Submit";
    }
  }

  function roundTripMs(start) {
    var ms = performance.now() - start;
    return ms < 1000 ? Math.round(ms) + " ms" : (ms / 1000).toFixed(2) + " s";
  }

  async function copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e2) { /* noop */ }
      ta.remove();
    }
    if (btn) {
      var original = btn.textContent;
      btn.textContent = "Copied";
      btn.classList.add("copied");
      setTimeout(function () { btn.textContent = original; btn.classList.remove("copied"); }, 1600);
    }
  }

  /* =========================================================================
     Nav
     ========================================================================= */
  var nav = el("nav");
  var navToggle = el("nav-toggle");
  var navLinks = el("nav-links");

  window.addEventListener("scroll", function () {
    nav.classList.toggle("scrolled", window.scrollY > 10);
  }, { passive: true });

  navToggle.addEventListener("click", function () {
    var open = navLinks.classList.toggle("open");
    navToggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
  navLinks.addEventListener("click", function (e) {
    if (e.target.tagName === "A") {
      navLinks.classList.remove("open");
      navToggle.setAttribute("aria-expanded", "false");
    }
  });

  /* =========================================================================
     Health
     ========================================================================= */
  async function checkHealth() {
    var dot = el("nav-status-dot");
    var text = el("nav-status-text");
    try {
      var r = await fetch(ORIGIN + "/health", { method: "GET" });
      var ok = r.ok && (await r.json()).status === "ok";
      if (ok) {
        dot.className = "status-dot ok";
        text.textContent = "Gateway Online";
        setMetric("m-gateway", "Connected", "ok");
      } else {
        throw new Error("bad status");
      }
    } catch (e) {
      dot.className = "status-dot err";
      text.textContent = "Gateway Offline";
      setMetric("m-gateway", "Unreachable", "err");
    }
  }

  function setMetric(id, value, cls, sub) {
    var m = el(id);
    if (!m) return;
    m.textContent = value;
    m.className = "m-value" + (cls ? " " + cls : "");
    if (sub && m.nextElementSibling) m.nextElementSibling.textContent = sub;
  }

  /* =========================================================================
     Models
     ========================================================================= */
  var MODELS = [];

  async function loadModels() {
    var rail = el("model-rail");
    var errBox = el("model-rail-error");
    setError("model-rail-error", null);
    try {
      var r = await fetch(API_BASE + "/models", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      var d = await r.json();
       MODELS = (d.data || []).map(function (m) {
         return {
           id: m.id,
           local: !!m.local,
           providers: Array.isArray(m.providers) ? m.providers : [],
           details: (m.details && typeof m.details === "object") ? m.details : null
         };
       });
       LOCAL_STATUS = (d.local_status === "unavailable") ? "unavailable" : "ok";
       renderModelRail(rail);
       populateSelector();
       renderMetrics();
       renderLocalModels();
    } catch (e) {
      renderModelsError(errBox, e);
    }
  }

  function renderModelsError(errBox, e) {
    errBox.hidden = false;
    errBox.innerHTML =
      '<div class="degraded">' +
        '<p style="color:var(--text2)">Could not load the model catalog' +
        (e && e.message ? " — " + escapeHtml(e.message) : "") + ".</p>" +
        '<button class="btn btn-sm" type="button" id="models-retry">Retry</button>' +
      "</div>";
    var retry = el("models-retry");
    if (retry) retry.addEventListener("click", loadModels);
    el("model-rail").innerHTML = "";
  }

  function renderModelRail(rail) {
    rail.innerHTML = "";
    MODELS.forEach(function (m) {
      var card = document.createElement("div");
      card.className = "model-card";
      card.setAttribute("role", "listitem");
      var providers = m.providers.length
        ? m.providers.slice(0, 3).map(function (p) {
            return '<span class="chip">' + escapeHtml(p) + "</span>";
          }).join("")
        : '<span class="chip">router</span>';
      var tag = m.local
        ? '<span class="chip local tag">LOCAL · $0</span>'
        : '<span class="chip tag">CLOUD</span>';
      card.innerHTML =
        '<div class="mc-model">' + escapeHtml(m.id) + "</div>" +
        '<div class="mc-providers">' + providers + "</div>" +
        tag;
      rail.appendChild(card);
    });
    if (!MODELS.length) {
      rail.innerHTML = '<div class="degraded">The catalog is empty.</div>';
    }
  }

  function renderMetrics() {
    var local = MODELS.filter(function (m) { return m.local; }).length;
    var cloud = MODELS.length - local;
    var providers = {};
    MODELS.forEach(function (m) {
      m.providers.forEach(function (p) { providers[p] = true; });
    });
    setMetric("m-models", String(MODELS.length), "");
    setMetric("m-local", String(local), local ? "accent" : "", "billed at $0");
    setMetric("m-cloud", cloud > 0 ? String(Object.keys(providers).length) : "—", cloud ? "" : "", "providers behind the gateway");
  }

  /* =========================================================================
     Local Models control panel (read-only; exact Ollama report)
     ========================================================================= */
  var CAP_LABELS = { completion: "Completion", tools: "Tools", thinking: "Thinking" };

  function humanBytes(b) {
    if (typeof b !== "number" || b <= 0) return "";
    if (b >= 1073741824) return (b / 1073741824).toFixed(1) + " GB";
    if (b >= 1048576) return Math.round(b / 1048576) + " MB";
    return b + " B";
  }

  function renderLocalModels() {
    var panel = el("local-models-list");
    var statusLine = el("local-models-status");
    if (!panel) return;
    var localModels = MODELS.filter(function (m) { return m.local; });

    if (LOCAL_STATUS === "unavailable") {
      if (statusLine) statusLine.textContent = "Local discovery unavailable — Ollama could not be reached.";
      panel.innerHTML = '<div class="degraded">Local discovery unavailable. Check that your Ollama host is connected to the gateway, then press <b>Refresh</b>.</div>';
      return;
    }
    if (!localModels.length) {
      if (statusLine) statusLine.textContent = "No local models discovered.";
      panel.innerHTML = '<div class="degraded">No local models installed. Pull a model in Ollama (e.g. <code>ollama pull qwen3:1.7b</code>) and press <b>Refresh</b>.</div>';
      return;
    }

    if (statusLine) statusLine.textContent = localModels.length + " local model" + (localModels.length > 1 ? "s" : "") + " discovered.";
    var html = "";
    localModels.forEach(function (m) {
      var d = m.details || {};
      var rows = "";
      function row(k, v) { return '<div class="lm-row"><span class="lm-k">' + k + '</span><span class="lm-v">' + v + "</span></div>"; }
      if (d.parameter_size) rows += row("Parameters", escapeHtml(d.parameter_size));
      if (d.quantization_level) rows += row("Quantization", escapeHtml(d.quantization_level));
      if (d.family) rows += row("Family", escapeHtml(d.family));
      if (d.context_length) rows += row("Context", escapeHtml(String(d.context_length)) + " tokens");
      var size = humanBytes(d.size_bytes);
      if (size) rows += row("Installed size", size);

      var badges = "";
      var caps = Array.isArray(d.capabilities) ? d.capabilities : [];
      caps.forEach(function (c) {
        if (CAP_LABELS[c]) badges += '<span class="chip cap">' + CAP_LABELS[c] + "</span>";
      });

      html +=
        '<div class="lm-card" role="listitem">' +
          '<div class="lm-head">' +
            '<div class="lm-name">' + escapeHtml(m.id) + "</div>" +
            '<span class="chip local tag">LOCAL · $0</span>' +
          "</div>" +
          '<div class="lm-status">Ready</div>' +
          (rows ? '<div class="lm-rows">' + rows + "</div>" : "") +
          (badges ? '<div class="lm-caps">' + badges + "</div>" : "") +
          '<button class="btn btn-sm lm-use" type="button" data-use="' + escapeHtml(m.id) + '">Use in Playground</button>' +
        "</div>";
    });
    panel.innerHTML = html;
  }

  function syncThinkingToggle() {
    var field = el("pg-thinking-field");
    if (!field) return;
    var isQwen3 = typeof selectedModel === "string" && selectedModel.indexOf("local:qwen3:") === 0;
    field.hidden = !isQwen3;
    if (!isQwen3) {
      localThinkingOn = false;
      var cb = el("pg-thinking");
      if (cb) cb.checked = false;
      var st = el("pg-thinking-state");
      if (st) st.textContent = "OFF";
    }
  }

  function syncGenControlsVisibility() {
    var field = el("pg-gen-field");
    if (!field) return;
    // Show Local Generation Controls only for local models. Selecting a cloud
    // model hides them — and buildChatBody() will omit the fields regardless, so
    // no local control values can ever reach a cloud request.
    field.hidden = !isLocalModel(selectedModel);
  }

  async function refreshLocalModels() {
    try {
      var r = await fetch(API_BASE + "/models?refresh=1", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      var d = await r.json();
      MODELS = (d.data || []).map(function (m) {
        return {
          id: m.id,
          local: !!m.local,
          providers: Array.isArray(m.providers) ? m.providers : [],
          details: (m.details && typeof m.details === "object") ? m.details : null
        };
      });
      LOCAL_STATUS = (d.local_status === "unavailable") ? "unavailable" : "ok";
    } catch (e) {
      // Keep the last good view; the backend reports local_status honestly.
    }
    renderModelRail(el("model-rail"));
    populateSelector();
    renderMetrics();
    renderLocalModels();
  }

  function useInPlayground(id) {
    chooseModel(id, null);
    var pg = el("playground");
    if (pg && pg.scrollIntoView) {
      pg.scrollIntoView({ behavior: REDUCED_MOTION ? "auto" : "smooth", block: "start" });
    }
    var prompt = el("pg-prompt");
    if (prompt && prompt.focus) prompt.focus();
  }

  (function wireLocalModels() {
    var lmRefresh = el("local-models-refresh");
    if (lmRefresh) lmRefresh.addEventListener("click", refreshLocalModels);
    var lmList = el("local-models-list");
    if (lmList) lmList.addEventListener("click", function (e) {
      var btn = e.target && e.target.closest ? e.target.closest(".lm-use") : null;
      if (btn && btn.dataset && btn.dataset.use) useInPlayground(btn.dataset.use);
    });
    var pgThinking = el("pg-thinking");
    if (pgThinking) pgThinking.addEventListener("change", function () {
      localThinkingOn = !!this.checked;
      var st = el("pg-thinking-state");
      if (st) st.textContent = localThinkingOn ? "ON" : "OFF";
      this.setAttribute("aria-checked", String(this.checked));
    });
  })();

  (function wireGenControls() {
    var defs = [
      { name: "max_tokens", id: "pg-max-tokens", err: "pg-max-tokens-err" },
      { name: "temperature", id: "pg-temperature", err: "pg-temperature-err" },
      { name: "top_p", id: "pg-top-p", err: "pg-top-p-err" },
      { name: "seed", id: "pg-seed", err: "pg-seed-err" }
    ];
    defs.forEach(function (d) {
      var input = el(d.id);
      if (!input) return;
      input.addEventListener("input", function () {
        var raw = input.value.trim();
        genControls[d.name] = raw === "" ? null : raw;
        var res = validateControl(d.name, raw);
        setError(d.err, res.ok ? null : res.error);
      });
    });
    var reset = el("pg-gen-reset");
    if (reset) reset.addEventListener("click", function () {
      defs.forEach(function (d) {
        var input = el(d.id);
        if (input) input.value = "";
        genControls[d.name] = null;
        setError(d.err, null);
      });
    });
  })();

  /* =========================================================================
      Local Runtime panel (zero-inference /api/ps surface + last request)
      ========================================================================= */
  // Last local runtime payload from GET /v1/local/runtime (in memory only).
  var RUNTIME_DATA = null;

  // VRAM share = size_vram_bytes / size_bytes. This is explicitly NOT a
  // GPU-utilization / CPU-GPU-split / GPU-usage figure; it is only the
  // proportion of the model footprint that is resident in VRAM.
  function vramShare(m) {
    if (typeof m.size_vram_bytes === "number" && m.size_vram_bytes > 0 &&
        typeof m.size_bytes === "number" && m.size_bytes > 0) {
      return m.size_vram_bytes / m.size_bytes;
    }
    return null;
  }

  function renderLocalRuntimeLast(last) {
    var box = el("local-runtime-last");
    if (!box) return;
    if (!last) {
      box.hidden = false;
      box.innerHTML =
        '<div class="lrt-last-head">Last Local Request</div>' +
        '<div class="degraded">No successful local request measured yet.</div>';
      return;
    }
    box.hidden = false;
    var rows = "";
    function row(k, v) { return '<div class="lm-row"><span class="lm-k">' + k + '</span><span class="lm-v">' + v + "</span></div>"; }
    rows += row("Model", escapeHtml(last.model));
    rows += row("Clarity upstream round-trip",
      (typeof last.gateway_upstream_round_trip_ms === "number"
        ? last.gateway_upstream_round_trip_ms.toFixed(1) : "—") + " ms");
    rows += row("Prompt tokens", String(last.prompt_tokens));
    rows += row("Completion tokens", String(last.completion_tokens));
    rows += row("Total tokens", String(last.total_tokens));
    if (last.measured_at) rows += row("Measured at", escapeHtml(String(last.measured_at)));
    box.innerHTML =
      '<div class="lrt-last-head">Last Local Request</div>' +
      '<div class="lm-rows">' + rows + "</div>";
  }

  function renderLocalRuntime() {
    var panel = el("local-runtime-list");
    var statusLine = el("local-runtime-status");
    if (!panel) return;
    renderLocalRuntimeLast(RUNTIME_DATA && RUNTIME_DATA.last_local_request);

    var data = RUNTIME_DATA || { status: "unavailable", models: [] };
    if (data.status !== "ok") {
      if (statusLine) statusLine.textContent = "Local runtime unavailable — Ollama could not be reached.";
      panel.innerHTML = '<div class="degraded">Local runtime unavailable. Check that your Ollama host is connected to the gateway, then press <b>Refresh Runtime</b>.</div>';
      return;
    }
    var models = Array.isArray(data.models) ? data.models : [];
    if (!models.length) {
      if (statusLine) statusLine.textContent = "Runtime available — no models loaded.";
      panel.innerHTML = '<div class="degraded">Runtime available, but no models are currently loaded into VRAM. Send a request to a local model to load it.</div>';
      return;
    }
    if (statusLine) statusLine.textContent = models.length + " model" + (models.length > 1 ? "s" : "") + " loaded into VRAM.";
    var html = "";
    models.forEach(function (m) {
      var rows = "";
      function row(k, v) { return '<div class="lm-row"><span class="lm-k">' + k + '</span><span class="lm-v">' + v + "</span></div>"; }
      var size = humanBytes(m.size_bytes);
      if (size) rows += row("Model size", size);
      var vram = humanBytes(m.size_vram_bytes);
      if (vram) rows += row("VRAM", vram);
      var share = vramShare(m);
      if (share !== null) rows += row("VRAM share", Math.round(share * 100) + "%");
      if (m.parameter_size) rows += row("Parameters", escapeHtml(m.parameter_size));
      if (m.quantization_level) rows += row("Quantization", escapeHtml(m.quantization_level));
      if (m.family) rows += row("Family", escapeHtml(m.family));
      if (m.context_length) rows += row("Allocated context", escapeHtml(String(m.context_length)) + " tokens");
      if (m.expires_at) rows += row("Unload at", escapeHtml(String(m.expires_at)));

      html +=
        '<div class="lm-card" role="listitem">' +
          '<div class="lm-head">' +
            '<div class="lm-name">' + escapeHtml(m.id) + "</div>" +
            '<span class="chip local tag loaded">LOADED</span>' +
          "</div>" +
          (rows ? '<div class="lm-rows">' + rows + "</div>" : "") +
        "</div>";
    });
    panel.innerHTML = html;
  }

  async function refreshLocalRuntime() {
    RUNTIME_DATA = { status: "unavailable", models: [] };
    try {
      var r = await fetch(API_BASE + "/local/runtime", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      RUNTIME_DATA = await r.json();
    } catch (e) {
      // Keep the honest unavailable view; the backend reports status honestly.
    }
    renderLocalRuntime();
  }

  (function wireLocalRuntime() {
    var rtRefresh = el("local-runtime-refresh");
    if (rtRefresh) rtRefresh.addEventListener("click", refreshLocalRuntime);
  })();

  /* =========================================================================
      Model selector (combobox)
      ========================================================================= */
  var trigger = el("pg-model-trigger");
  var menu = el("pg-model-menu");
  var search = el("pg-model-search");
  var optionsBox = el("pg-model-options");
  var selectedModel = "";
  var activeIndex = -1;
  var visibleOpts = [];

  function filteredModels(q) {
    var needle = q.trim().toLowerCase();
    if (!needle) return MODELS;
    return MODELS.filter(function (m) { return m.id.toLowerCase().indexOf(needle) !== -1; });
  }

  function groupAndRender(list) {
    var local = list.filter(function (m) { return m.local; });
    var cloud = list.filter(function (m) { return !m.local; });
    visibleOpts = [];
    optionsBox.innerHTML = "";
    var html = "";
    if (cloud.length) {
      html += '<div class="select-group-label">Cloud</div>';
      cloud.forEach(function (m) {
        var i = visibleOpts.length;
        visibleOpts.push(m.id);
        html += '<button type="button" class="select-opt" role="option" data-i="' + i + '" data-model="' + escapeHtml(m.id) + '">' +
          '<span>' + escapeHtml(m.id) + '</span>' +
          '<span class="opt-badge">CLOUD</span></button>';
      });
    }
    if (local.length) {
      html += '<div class="select-group-label">Local</div>';
      local.forEach(function (m) {
        var i = visibleOpts.length;
        visibleOpts.push(m.id);
        html += '<button type="button" class="select-opt" role="option" data-i="' + i + '" data-model="' + escapeHtml(m.id) + '">' +
          '<span>' + escapeHtml(m.id) + '</span>' +
          '<span class="opt-badge local">LOCAL · $0</span></button>';
      });
    }
    if (!html) {
      html = '<div class="select-empty">No models match.</div>';
    }
    optionsBox.innerHTML = html;
  }

  function populateSelector() {
    groupAndRender(MODELS);
    optionsBox.addEventListener("mousemove", function (e) {
      var opt = e.target.closest(".select-opt");
      if (opt) setActive(Number(opt.dataset.i), false);
    });
  }

  function chooseModel(id, opt) {
    selectedModel = id;
    el("pg-model-label").textContent = id;
    el("pg-model-label").title = id;
    syncThinkingToggle();
    syncGenControlsVisibility();
    if (opt) {
      optionsBox.querySelectorAll(".select-opt").forEach(function (o) {
        o.setAttribute("aria-selected", String(o === opt));
      });
    }
  }

  function setActive(i, scroll) {
    activeIndex = i;
    optionsBox.querySelectorAll(".select-opt").forEach(function (opt) {
      var isActive = Number(opt.dataset.i) === i;
      opt.setAttribute("aria-selected", isActive ? "true" : "false");
      if (isActive && scroll) opt.scrollIntoView({ block: "nearest" });
    });
  }

  function openMenu() {
    menu.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    search.value = "";
    groupAndRender(MODELS);
    if (visibleOpts.length) setActive(Math.max(0, visibleOpts.indexOf(selectedModel)), false);
    setTimeout(function () { search.focus(); }, 10);
  }

  function closeMenu() {
    menu.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
  }

  trigger.addEventListener("click", function () {
    if (menu.hidden) openMenu(); else closeMenu();
  });
  trigger.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
      if (menu.hidden) { e.preventDefault(); openMenu(); }
    } else if (e.key === "Escape") {
      closeMenu();
    }
  });
  search.addEventListener("input", function () {
    var list = filteredModels(search.value);
    groupAndRender(list);
    if (visibleOpts.length) setActive(0, false);
  });
  search.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(Math.min(activeIndex + 1, visibleOpts.length - 1), true); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(Math.max(activeIndex - 1, 0), true); }
    else if (e.key === "Enter") {
      if (activeIndex >= 0 && visibleOpts[activeIndex]) {
        e.preventDefault();
        chooseModel(visibleOpts[activeIndex], null);
        closeMenu();
      }
    } else if (e.key === "Escape") { closeMenu(); }
  });
  menu.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeMenu(); trigger.focus(); }
  });
  document.addEventListener("click", function (e) {
    if (!menu.hidden && !menu.contains(e.target) && e.target !== trigger) closeMenu();
  });

  optionsBox.addEventListener("click", function (e) {
    var opt = e.target.closest(".select-opt");
    if (opt) {
      chooseModel(opt.dataset.model, opt);
      closeMenu();
    }
  });

  /* =========================================================================
     Signup + key masking
     ========================================================================= */
  var signupForm = el("signup-form");
  var signupBtn = el("signup-btn");

  signupForm.addEventListener("submit", async function (e) {
    e.preventDefault();
    setError("signup-error", null);
    var name = el("signup-name").value.trim();
    if (!name) { setError("signup-error", "Please enter a name for your key."); return; }
    setBtnLoading(signupBtn, true, "Creating…");
    try {
      var r = await fetch(API_BASE + "/signup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name })
      });
      var d = await r.json().catch(function () { return {}; });
      if (!r.ok) {
        throw new Error(d.detail || ("HTTP " + r.status + (d.message ? " — " + d.message : "")));
      }
      if (!d.skey) throw new Error("Backend returned no key.");
      sessionKey = d.skey;                       // in-memory only
      var skeyInput = el("signup-skey");
      skeyInput.type = "password";               // masked by default
      skeyInput.value = d.skey;
      el("key-reveal").classList.add("on");
      el("pg-key").value = d.skey;               // convenience; field is type=password
      signupForm.reset();
      refreshBalance(d.skey);
    } catch (err) {
      setError("signup-error", err.message || "Could not create the key. Try again.");
    } finally {
      setBtnLoading(signupBtn, false);
    }
  });

  /* Show/Hide toggle for the newly created key (masked by default). */
  el("key-show-toggle").addEventListener("click", function () {
    var input = el("signup-skey");
    var btn = this;
    var show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.textContent = show ? "Hide" : "Show";
    btn.setAttribute("aria-pressed", show ? "true" : "false");
  });

  el("copy-skey").addEventListener("click", function () {
    copyText(el("signup-skey").value, this);
  });

  /* =========================================================================
     Capabilities: reflect server-side signup availability (fail closed)
     ========================================================================= */
  function applyCapabilities(caps) {
    var enabled = !!(caps && caps.public_signup_enabled);
    var x402 = !!(caps && caps.x402_enabled);
    var statusEl = el("signup-status");
    // Fail closed: if capabilities are missing/unreachable, treat signup as
    // disabled rather than assuming it is available.
    el("signup-name").disabled = !enabled;
    signupBtn.disabled = !enabled;
    if (enabled) {
      if (statusEl) { statusEl.hidden = true; statusEl.textContent = ""; }
      return;
    }
    if (statusEl) {
      statusEl.hidden = false;
      statusEl.textContent = x402
        ? "Public self-service signup is disabled. Machine access is available via x402 payments."
        : "Public self-service signup is disabled.";
    }
  }

  (function loadCapabilities() {
    fetch(API_BASE + "/capabilities", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(applyCapabilities)
      .catch(function () { applyCapabilities(null); }); // fail closed
  })();

  /* =========================================================================
     Balance + first-run hint
     ========================================================================= */
  function showFirstRun(show) {
    var b = el("pg-firstrun");
    if (b) b.hidden = !show;
  }

  async function refreshBalance(key) {
    if (!key) return;
    try {
      var r = await fetch(API_BASE + "/usage", { headers: { Authorization: "Bearer " + key } });
      var d = await r.json().catch(function () { return {}; });
      if (r.ok && typeof d.balance_usd === "number") {
        el("balance-row").hidden = false;
        el("balance-value").textContent = "$" + d.balance_usd.toFixed(2);
        lastBalance = d.balance_usd;
        showFirstRun(d.balance_usd <= 0);
      }
    } catch (e) { /* silent — balance is auxiliary */ }
  }

  /* =========================================================================
     Top up (Stripe checkout)
     ========================================================================= */
  function bestKey() {
    var fromTopup = el("topup-key").value.trim();
    var fromPg = el("pg-key").value.trim();
    return sessionKey || fromTopup || fromPg;
  }

  async function startCheckout(key) {
    setError("topup-error", null);
    try {
      var r = await fetch(API_BASE + "/checkout", {
        method: "POST",
        headers: { Authorization: "Bearer " + key }
      });
      var d = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(d.detail || "HTTP " + r.status);
      if (!d.url) throw new Error("No checkout URL returned.");
      window.location.href = d.url;
    } catch (e) {
      setError("topup-error", e.message || "Could not start checkout.");
    }
  }

  el("topup-btn").addEventListener("click", function () {
    var key = bestKey();
    if (!key) {
      setError("topup-error", "Enter (or create) an API key first.");
      el("topup-key").focus();
      return;
    }
    setBtnLoading(this, true, "Opening…");
    startCheckout(key).finally(() => setBtnLoading(el("topup-btn"), false));
  });

  /* Pricing card CTAs */
  document.querySelectorAll("[data-topup]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var key = bestKey();
      if (!key) {
        document.querySelector("#credentials").scrollIntoView({ behavior: REDUCED_MOTION ? "auto" : "smooth" });
        el("topup-key").focus();
        setError("topup-error", "Enter (or create) an API key, then top up.");
        return;
      }
      setBtnLoading(btn, true, "Opening…");
      startCheckout(key).finally(function () { setBtnLoading(btn, false); });
    });
  });

  el("pg-topup-cta").addEventListener("click", function () {
    var key = bestKey();
    if (!key) { document.querySelector("#credentials").scrollIntoView({ behavior: REDUCED_MOTION ? "auto" : "smooth" }); return; }
    setBtnLoading(this, true, "Opening…");
    startCheckout(key).finally(function () { setBtnLoading(el("pg-topup-cta"), false); });
  });

  function showTopupCta(show) {
    var b = el("pg-topup-cta");
    if (b) b.hidden = !show;
  }

  /* =========================================================================
     Playground
     ========================================================================= */
  var pgRun = el("pg-run");
  var pgOutput = el("pg-output");
  var pgMeta = el("pg-meta");
  var pgStatus = el("pg-status");

  function setPgStatus(state, label) {
    if (!state) { pgStatus.hidden = true; pgStatus.className = "out-badge"; pgStatus.textContent = ""; return; }
    pgStatus.hidden = false;
    pgStatus.className = "out-badge " + state;
    pgStatus.textContent = label || PG_STATE_LABELS[state] || "Status";
  }

  pgRun.addEventListener("click", runInference);
  el("pg-clear").addEventListener("click", function () {
    pgOutput.textContent = "Run a request to see the response here.";
    pgOutput.classList.remove("reveal", "placeholder");
    setError("pg-error", null);
    setPgStatus(null);
    pgMeta.hidden = true;
    showTopupCta(false);
    el("pg-copy").hidden = true;
  });
  el("pg-copy").addEventListener("click", function () {
    copyText(pgOutput.textContent, this);
  });

  async function runInference() {
    setError("pg-error", null);
    showTopupCta(false);
    var key = el("pg-key").value.trim();
    if (!key) { setError("pg-error", "Enter your API key."); el("pg-key").focus(); return; }
    if (!selectedModel) { setError("pg-error", "Select a model from the list."); return; }
    var prompt = el("pg-prompt").value.trim();
    if (!prompt) { setError("pg-error", "Write a prompt first."); return; }

    var genErr = firstControlError();
    if (genErr) { setError("pg-error", "Generation control: " + genErr); return; }

    setBtnLoading(pgRun, true, "Routing request…");
    setPgStatus("busy", PG_STATE_LABELS.busy);
    pgOutput.textContent = "";
    pgOutput.classList.add("placeholder");
    pgOutput.textContent = "Request in flight…";
    var start = performance.now();
    try {
      var r = await fetch(API_BASE + "/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + key },
        body: JSON.stringify(buildChatBody(selectedModel, prompt))
      });
      var d = await r.json().catch(function () { return {}; });
      if (!r.ok) {
        var status = r.status;
        var state = mapPlaygroundState(status);
        var msg;
        if (state === "insufficient") {
          msg = "402 — Insufficient balance. Local models are free when you bring your own Ollama; for cloud models, top up credits to continue.";
        } else if (status === 401) {
          msg = "Invalid API key. Check the key and try again.";
        } else if (state === "unavailable") {
          var isLocal = MODELS.some(function (m) { return m.id === selectedModel && m.local; });
          msg = isLocal
            ? "Local model unavailable — make sure your Ollama host is connected to the gateway, then retry."
            : "Model or provider unavailable right now (HTTP " + status + "). Try another model or retry.";
        } else {
          msg = d.detail || ("Request failed (HTTP " + status + ").");
        }
        throw { state: state, message: msg };
      }
      var content = (d.choices && d.choices[0] && d.choices[0].message && d.choices[0].message.content) || "";
      var usage = d.usage || {};
      var rt = roundTripMs(start);

      pgOutput.textContent = content || "(empty response)";
      pgOutput.classList.remove("placeholder");
      pgOutput.classList.add("reveal");
      setPgStatus("success", PG_STATE_LABELS.success);
      pgMeta.hidden = false;
      pgMeta.innerHTML =
        '<span><b>model</b> ' + escapeHtml(d.model || selectedModel) + "</span>" +
        '<span><b>round-trip</b> ' + rt + "</span>" +
        '<span><b>tokens</b> ' + (usage.total_tokens != null ? usage.total_tokens : "—") +
        " (" + (usage.prompt_tokens || 0) + "+" + (usage.completion_tokens || 0) + ")</span>";
      el("pg-copy").hidden = false;
      refreshBalance(key);
    } catch (err) {
      var st = (err && err.state) || "error";
      setPgStatus(st, PG_STATE_LABELS[st] || "Error");
      setError("pg-error", (err && err.message) || "Inference failed. The gateway may be unavailable.");
      pgOutput.textContent = "Run a request to see the response here.";
      pgOutput.classList.remove("placeholder");
      showTopupCta(st === "insufficient");
    } finally {
      setBtnLoading(pgRun, false);
    }
  }

  /* =========================================================================
     API tabs + copy
     ========================================================================= */
  var pyBase = el("py-base-url");
  var curlBase = el("curl-base-url");
  pyBase.textContent = API_BASE + "/";
  curlBase.textContent = API_BASE + "/chat/completions";

  function setupTabs() {
    var tabs = document.querySelectorAll('[role="tab"]');
    var panels = { python: el("panel-python"), curl: el("panel-curl") };
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        tabs.forEach(function (t) {
          var on = t === tab;
          t.setAttribute("aria-selected", on ? "true" : "false");
          t.tabIndex = on ? 0 : -1;
        });
        Object.keys(panels).forEach(function (k) {
          panels[k].hidden = k !== tab.id.replace("tab-", "");
        });
      });
    });
  }
  setupTabs();

  document.querySelectorAll(".code-copy").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var target = btn.dataset.copyTarget;
      var pre = target === "curl" ? el("panel-curl").querySelector("pre") : el("panel-python").querySelector("pre");
      copyText(pre.innerText, btn);
    });
  });

  /* =========================================================================
     Live status (/v1/status) — evidence-backed, sanitized
     ========================================================================= */
  async function loadStatus() {
    var retryBtn = el("status-retry");
    if (retryBtn) setBtnLoading(retryBtn, true, "Refreshing…");
    try {
      var r = await fetch(API_BASE.replace("/v1", "") + "/v1/status");
      var data = r.ok ? await r.json().catch(function () { return null; }) : null;
      renderStatus(data);
    } catch (e) {
      renderStatus(null);
    } finally {
      if (retryBtn) setBtnLoading(retryBtn, false);
    }
  }

  function renderStatus(data) {
    var dot = el("nav-status-dot");
    var txt = el("nav-status-text");
    if (!data) {
      if (dot) dot.className = "status-dot err";
      if (txt) txt.textContent = "Status unavailable";
      return;
    }
    if (dot) dot.className = "status-dot ok";
    if (txt) txt.textContent = "Gateway " + (data.status || "unknown");

    var g = data.gateway || {};
    var gw = (data.status || "unknown");
    if (typeof g.active_keys !== "undefined") {
      gw += " · " + g.active_keys + " keys · $" + (g.total_balance_usd || 0);
    }
    var gwEl = el("s-gateway");
    if (gwEl) gwEl.textContent = gw;

    var provs = data.providers || {};
    var names = Object.keys(provs);
    var reachable = 0;
    names.forEach(function (n) {
      var p = provs[n] || {};
      if (p.reachable === true) reachable++;
    });
    var pEl = el("s-providers");
    if (pEl) pEl.textContent = reachable + " / " + names.length;

    var tEl = el("s-timestamp");
    if (tEl) tEl.textContent = (data.timestamp || "—").replace("Z", "");

    var tbody = el("status-providers");
    if (tbody) {
      tbody.innerHTML = "";
      if (!names.length) {
        tbody.innerHTML = '<tr><td data-label="Provider" colspan="6" style="text-align:center;color:var(--muted)">No providers configured.</td></tr>';
      }
      names.forEach(function (n) {
        var p = provs[n] || {};
        var reach = p.reachable === true ? "yes"
                  : p.reachable === false ? "no"
                  : "unknown";
        var creds = p.credentials_configured ? "yes" : "no";
        var lat = (typeof p.probe_latency_ms === "number") ? p.probe_latency_ms : "—";
        var reason = safeReason(p.reason);
        var reasonHtml = reason ? ' <span class="m-sub">(' + escapeHtml(reason) + ")</span>" : "";
        var tr = document.createElement("tr");
        tr.innerHTML =
          '<td data-label="Provider"><code>' + escapeHtml(n) + "</code></td>" +
          '<td data-label="Configured">' + (p.configured ? "yes" : "no") + "</td>" +
          '<td data-label="Creds set">' + creds + "</td>" +
          '<td data-label="Reachable">' + reach + reasonHtml + "</td>" +
          '<td data-label="Latency (ms)">' + lat + "</td>" +
          '<td data-label="Models">' + (p.models_in_routes || 0) + "</td>";
        tbody.appendChild(tr);
      });
    }

    var note = (data.failover && data.failover.note) ? data.failover.note : "";
    var nEl = el("status-note");
    if (nEl) nEl.textContent = note;
  }

  var statusRetry = el("status-retry");
  if (statusRetry) statusRetry.addEventListener("click", loadStatus);

  /* =========================================================================
      System Diagnostics (read-only, zero-inference)
      ========================================================================= */
  var DIAG_BUILD_COMMIT = "";

  function capStatus(s) {
    if (s === "ok") return "Available";
    if (s === "unavailable") return "Unavailable";
    return s || "—";
  }

  function setText(id, v) {
    var n = el(id);
    if (n) n.textContent = v;
  }

  function renderDiagnostics(data) {
    if (!data) {
      setText("diag-build-commit", "—");
      setText("diag-checkpoint-tag", "—");
      setText("diag-checkpoint-sha", "—");
      setText("diag-gateway", "—");
      setText("diag-discovery", "—");
      setText("diag-discovery-count", "—");
      setText("diag-runtime", "—");
      setText("diag-runtime-count", "—");
      setText("diag-last-request", "—");
      setText("diag-build-state", "Diagnostics unavailable.");
      return;
    }
    var build = data.build || {};
    var local = data.local || {};
    var cur = typeof build.current_commit === "string" ? build.current_commit : "unknown";
    DIAG_BUILD_COMMIT = cur;
    setText("diag-build-commit", cur === "unknown" ? "unknown" : cur.slice(0, 12));
    setText("diag-build-state", cur === "unknown" ? "Commit unknown in this build." : "Full commit available via Copy.");
    setText("diag-checkpoint-tag", build.checkpoint_tag || "—");
    setText("diag-checkpoint-sha", (build.checkpoint_commit || "").slice(0, 12) || "—");
    setText("diag-gateway", (data.gateway && data.gateway.process_healthy) ? "Healthy" : "Unavailable");
    setText("diag-discovery", capStatus(local.discovery_status));
    setText("diag-discovery-count", String(local.models_discovered != null ? local.models_discovered : "—"));
    setText("diag-runtime", capStatus(local.runtime_status));
    setText("diag-runtime-count", String(local.models_loaded != null ? local.models_loaded : "—"));
    setText("diag-last-request", local.last_local_request_measured ? "Measured this process" : "Not measured yet");
  }

  async function refreshDiagnostics() {
    try {
      var r = await fetch(API_BASE.replace("/v1", "") + "/v1/diagnostics");
      var data = r.ok ? await r.json().catch(function () { return null; }) : null;
      renderDiagnostics(data);
    } catch (e) {
      // Zero-inference, read-only: never throw on a failed probe; show honest dash.
      renderDiagnostics(null);
    }
  }

  (function wireDiagnostics() {
    var refresh = el("diag-refresh");
    if (refresh) refresh.addEventListener("click", refreshDiagnostics);
    var copy = el("diag-build-copy");
    if (copy) copy.addEventListener("click", function () {
      if (DIAG_BUILD_COMMIT && DIAG_BUILD_COMMIT !== "unknown") copyText(DIAG_BUILD_COMMIT, this);
    });
  })();

  /* =========================================================================
      Routing animation
      ========================================================================= */
  if (!REDUCED_MOTION) {
    el("routing").classList.add("routing-anim");
  }

  /* =========================================================================
     Boot
     ========================================================================= */
  checkHealth();
  loadModels();
  loadStatus();
  refreshLocalRuntime();
  refreshDiagnostics();
})();
