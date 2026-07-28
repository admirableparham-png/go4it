// ==UserScript==
// @name         go4it — go4worldbusiness lead capture
// @namespace    go4it
// @version      0.2
// @description  Reads buy-leads you're already viewing in your real logged-in session and sends them to go4it. No bot, no extra requests.
// @match        https://www.go4worldbusiness.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @connect      localhost
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  // ---- config (change if your go4it runs elsewhere) ----
  const API = "http://localhost:8400";
  const KEY = "go4it-local-key";

  // Works on Tampermonkey (GM_xmlhttpRequest) AND Safari "Userscripts" (GM.xmlHttpRequest).
  function xhr(opts) {
    if (typeof GM_xmlhttpRequest === "function") return GM_xmlhttpRequest(opts);
    if (typeof GM !== "undefined" && GM && typeof GM.xmlHttpRequest === "function") return GM.xmlHttpRequest(opts);
    // last resort (will hit mixed-content/CORS in Safari — the panel will show it failed)
    fetch(opts.url, { method: opts.method || "GET", headers: opts.headers, body: opts.data })
      .then((r) => opts.onload && opts.onload({ status: r.status }))
      .catch(() => opts.onerror && opts.onerror());
  }

  function post(path, body, cb) {
    xhr({ method: "POST", url: API + path,
      headers: { "Content-Type": "application/json", "X-API-Key": KEY },
      data: JSON.stringify(body), onload: (r) => cb && cb(r), onerror: () => cb && cb({ status: 0 }) });
  }

  // ---- floating panel ----
  const panel = document.createElement("div");
  panel.style.cssText =
    "position:fixed;bottom:16px;right:16px;z-index:999999;background:#0f172a;color:#e2e8f0;" +
    "font:13px/1.4 system-ui,sans-serif;padding:10px 12px;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.4);" +
    "border:1px solid #334155;min-width:210px";
  panel.innerHTML =
    '<div style="font-weight:700;margin-bottom:6px">go4it capture</div>' +
    '<div id="g4it-status" style="margin-bottom:8px;color:#94a3b8">connecting…</div>' +
    '<button id="g4it-tune" style="width:100%;margin-bottom:6px;padding:6px;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer">① Send this page to go4it (tune)</button>' +
    '<button id="g4it-cap" style="width:100%;padding:6px;border:0;border-radius:6px;background:#059669;color:#fff;cursor:pointer">② Capture leads now</button>';
  (document.body || document.documentElement).appendChild(panel);
  const statusEl = panel.querySelector("#g4it-status");
  const setStatus = (t, c) => { statusEl.textContent = t; statusEl.style.color = c || "#94a3b8"; };

  // ---- connection check ----
  xhr({ method: "GET", url: API + "/api/health",
    onload: (r) => setStatus(r.status === 200 ? "✓ connected to go4it" : "✗ go4it error " + r.status, r.status === 200 ? "#34d399" : "#f87171"),
    onerror: () => setStatus("✗ can't reach go4it (Safari may be blocking localhost)", "#f87171") });

  // ---- ① send the real page HTML so the extractor can be tuned ----
  panel.querySelector("#g4it-tune").onclick = function () {
    setStatus("sending page…");
    post("/api/debug/dom", { url: location.href, html: document.documentElement.outerHTML },
      (r) => setStatus(r.status === 200 ? "✓ page sent for tuning" : "✗ send failed " + r.status, r.status === 200 ? "#34d399" : "#f87171"));
  };

  // ---- lead extractor (TUNED after we see your real buy-leads DOM) ----
  function extractLeads() {
    return [];  // replaced with exact selectors once your tuning page arrives
  }

  // ---- ② capture visible leads and send to go4it ----
  function capture(auto) {
    const leads = extractLeads();
    if (!leads.length) { if (!auto) setStatus("0 leads found — click ① to tune", "#fbbf24"); return; }
    post("/api/leads/raw", { leads },
      (r) => setStatus(r.status === 202 ? "✓ sent " + leads.length + " lead(s)" : "✗ send failed " + r.status, r.status === 202 ? "#34d399" : "#f87171"));
  }
  panel.querySelector("#g4it-cap").onclick = () => capture(false);

  // ---- auto-capture new leads as the page updates (harmless until tuned) ----
  let t = null;
  new MutationObserver(() => { clearTimeout(t); t = setTimeout(() => capture(true), 1500); })
    .observe(document.body || document.documentElement, { childList: true, subtree: true });
})();
