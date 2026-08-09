/* Big Pickle — gateway frontend. All data comes from the live backend. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var ORIGIN = window.location.origin;
  var API_BASE = ORIGIN + "/v1";
  var REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* Last key created this session (never persisted, never logged). */
  var sessionKey = "";

  /* ---------- helpers ---------- */
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

  /* Client-side round-trip time measured honestly around the request. */
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

  /* ---------- nav ---------- */
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

  /* ---------- health ---------- */
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

  /* ---------- metrics ---------- */
  function setMetric(id, value, cls, sub) {
    var m = el(id);
    if (!m) return;
    m.textContent = value;
    m.className = "m-value" + (cls ? " " + cls : "");
    if (sub && m.nextElementSibling) m.nextElementSibling.textContent = sub;
  }

  /* ---------- models ---------- */
  var MODELS = [];

  async function loadModels() {
    var rail = el("model-rail");
    var errBox = el("model-rail-error");
    setError("model-rail-error", null);
    try {
      var r = await fetch(API_BASE + "/models");
      if (!r.ok) throw new Error("HTTP " + r.status);
      var d = await r.json();
      MODELS = (d.data || []).map(function (m) {
        return {
          id: m.id,
          local: !!m.local,
          providers: Array.isArray(m.providers) ? m.providers : []
        };
      });
      renderModelRail(rail);
      populateSelector();
      renderMetrics();
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

  /* ---------- model selector (combobox) ---------- */
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
    optionsBox.querySelectorAll(".select-opt").forEach(function (opt) {
      opt.addEventListener("click", function () {
        chooseModel(opt.dataset.model, opt);
        closeMenu();
      });
    });
    optionsBox.addEventListener("mousemove", function (e) {
      var opt = e.target.closest(".select-opt");
      if (opt) setActive(Number(opt.dataset.i), false);
    });
  }

  function chooseModel(id, opt) {
    selectedModel = id;
    el("pg-model-label").textContent = id;
    el("pg-model-label").title = id;
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
    optionsBox.querySelectorAll(".select-opt").forEach(function (opt) {
      opt.addEventListener("click", function () {
        chooseModel(opt.dataset.model, opt);
        closeMenu();
      });
    });
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

  /* ---------- signup ---------- */
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
      sessionKey = d.skey;
      el("signup-skey").value = d.skey;
      el("key-reveal").classList.add("on");
      el("topup-key").value = d.skey;
      signupForm.reset();
      refreshBalance(d.skey);
      if (d.stripe_enabled) {
        setError("topup-error", null);
      }
    } catch (err) {
      setError("signup-error", err.message || "Could not create the key. Try again.");
    } finally {
      setBtnLoading(signupBtn, false);
    }
  });

  el("copy-skey").addEventListener("click", function () {
    copyText(el("signup-skey").value, this);
  });

  /* ---------- balance ---------- */
  async function refreshBalance(key) {
    if (!key) return;
    try {
      var r = await fetch(API_BASE + "/usage", { headers: { Authorization: "Bearer " + key } });
      var d = await r.json().catch(function () { return {}; });
      if (r.ok && typeof d.balance_usd === "number") {
        el("balance-row").hidden = false;
        el("balance-value").textContent = "$" + d.balance_usd.toFixed(2);
      }
    } catch (e) { /* silent — balance is auxiliary */ }
  }

  /* ---------- top up (Stripe checkout) ---------- */
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

  /* ---------- playground ---------- */
  var pgRun = el("pg-run");
  var pgOutput = el("pg-output");
  var pgMeta = el("pg-meta");
  var pgStatus = el("pg-status");

  function setPgStatus(state, label) {
    if (!state) { pgStatus.hidden = true; pgStatus.className = "out-badge"; pgStatus.textContent = ""; return; }
    pgStatus.hidden = false;
    pgStatus.className = "out-badge " + state;
    pgStatus.textContent = label;
  }

  pgRun.addEventListener("click", runInference);
  el("pg-clear").addEventListener("click", function () {
    pgOutput.textContent = "Run a request to see the response here.";
    pgOutput.classList.remove("reveal", "placeholder");
    setError("pg-error", null);
    setPgStatus(null);
    pgMeta.hidden = true;
    el("pg-copy").hidden = true;
  });
  el("pg-copy").addEventListener("click", function () {
    copyText(pgOutput.textContent, this);
  });

  async function runInference() {
    setError("pg-error", null);
    var key = el("pg-key").value.trim();
    if (!key) { setError("pg-error", "Enter your API key."); el("pg-key").focus(); return; }
    if (!selectedModel) { setError("pg-error", "Select a model from the list."); return; }
    var prompt = el("pg-prompt").value.trim();
    if (!prompt) { setError("pg-error", "Write a prompt first."); return; }

    setBtnLoading(pgRun, true, "Routing request…");
    setPgStatus("busy", "Running");
    pgOutput.textContent = "";
    pgOutput.classList.add("placeholder");
    pgOutput.textContent = "Request in flight…";
    var start = performance.now();
    try {
      var r = await fetch(API_BASE + "/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + key },
        body: JSON.stringify({
          model: selectedModel,
          messages: [{ role: "user", content: prompt }]
        })
      });
      var d = await r.json().catch(function () { return {}; });
      if (!r.ok) {
        var msg = d.detail || ("HTTP " + r.status + (d.message ? " — " + d.message : ""));
        if (r.status === 402) {
          msg = "402 — Insufficient balance. Top up credits to use cloud models (local models are free).";
        } else if (r.status === 401) {
          msg = "Invalid API key. Check the key and try again.";
        }
        throw new Error(msg);
      }
      var content = (d.choices && d.choices[0] && d.choices[0].message && d.choices[0].message.content) || "";
      var usage = d.usage || {};
      var rt = roundTripMs(start);

      pgOutput.textContent = content || "(empty response)";
      pgOutput.classList.remove("placeholder");
      pgOutput.classList.add("reveal");
      setPgStatus("ready", "Complete");
      pgMeta.hidden = false;
      pgMeta.innerHTML =
        '<span><b>model</b> ' + escapeHtml(d.model || selectedModel) + "</span>" +
        '<span><b>round-trip</b> ' + rt + "</span>" +
        '<span><b>tokens</b> ' + (usage.total_tokens != null ? usage.total_tokens : "—") +
        " (" + (usage.prompt_tokens || 0) + "+" + (usage.completion_tokens || 0) + ")</span>";
      el("pg-copy").hidden = false;
      refreshBalance(key);
    } catch (err) {
      setPgStatus("err", "Error");
      setError("pg-error", err.message || "Inference failed. The gateway may be unavailable.");
      pgOutput.textContent = "Run a request to see the response here.";
      pgOutput.classList.remove("placeholder");
    } finally {
      setBtnLoading(pgRun, false);
    }
  }

  /* ---------- API tabs + copy ---------- */
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

  /* ---------- routing animation ---------- */
  if (!REDUCED_MOTION) {
    el("routing").classList.add("routing-anim");
  }

  /* ---------- boot ---------- */
  checkHealth();
  loadModels();
})();
