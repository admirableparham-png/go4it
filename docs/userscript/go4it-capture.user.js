// ==UserScript==
// @name         go4it — go4worldbusiness lead capture
// @namespace    go4it
// @version      0.11
// @description  Reads buy-leads from your real logged-in go4world session and sends them to go4it. Auto-hunter scans the Georgia (+neighbour) tile/brick buyer pages, follows pagination through ALL pages, keeps only fresh buy-requests (2025+), skips archived/company profiles, and pushes only NEW leads. No headless bot — uses your own session.
// @match        *://*.go4worldbusiness.com/*
// @match        *://go4worldbusiness.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @connect      localhost
// @connect      127.0.0.1
// @connect      go4worldbusiness.com
// @connect      www.go4worldbusiness.com
// @connect      self
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  // =============================== CONFIG (edit to retarget) ===============================
  const CONFIG = {
    api: "http://localhost:8400",
    key: "go4it-local-key",
    // Pages the hunter scans — same-origin, fetched with your live logged-in session.
    // go4world exposes country+category buyer pages: /buyers/<country>/<category>.html
    // Each is already country-scoped, so we get Georgian buyers directly. The hunter
    // follows rel="next" pagination through each (up to maxPagesPerTarget).
    targets: [
      { url: "/buyers/georgia/tiles.html", label: "GE tiles" },
      { url: "/buyers/georgia/ceramic-tiles.html", label: "GE ceramic" },
      { url: "/buyers/georgia/bricks.html", label: "GE bricks" },
      { url: "/buyers/turkey/tiles.html", label: "TR tiles" },
      { url: "/buyers/turkey/ceramic-tiles.html", label: "TR ceramic" },
      { url: "/buyers/turkey/bricks.html", label: "TR bricks" },
      // add more neighbours the same way, e.g.:
      // { url: "/buyers/azerbaijan/tiles.html", label: "AZ tiles" },
      // { url: "/buyers/armenia/tiles.html", label: "AM tiles" },
      // { url: "/buyers/russia/tiles.html", label: "RU tiles" },
    ],
    // Safety net: keep only buyers whose country (flag OR destination) is one of these.
    // The /buyers/georgia/* pages are already GE, so this mainly guards mixed pages.
    countries: ["ge", "az", "am", "tr", "ru"],
    // A lead is kept only if it matches one of these keyword sets; the match also tags it.
    categories: {
      tiles: ["tile", "tiles", "ceramic", "porcelain", "vitrified", "porcelanato", "glazed"],
      bricks: ["brick", "bricks", "ajor", "clay block", "face brick", "refractory"],
    },
    minPostYear: 2025,       // only add buy-requests posted in this year or later (2025-2026)
    skipArchived: true,      // skip cards marked ARCHIVED (the extractor already captures only
                             // buy-requests — /member/view company profiles are never emitted)
    maxPagesPerTarget: 15,   // follow rel="next" through ALL pages (bounded for safety)
    maxFetchesPerScan: 15,   // hard cap on page fetches per scan (stay gentle vs go4world)
    scanEveryMin: 20,        // base minutes between scans
    jitterMin: 6,            // +/- randomisation on the interval
    gapMs: 8000,             // pause between individual page fetches (gentle on go4world)
    maxBackoffMin: 60,       // cap on backoff after a block
    firstScanDelayMin: 0.3,  // after enabling / a reload, wait ~18s before the first scan
  };

  const API = CONFIG.api, KEY = CONFIG.key;

  // =============================== transport ===============================
  // Works on Tampermonkey (GM_xmlhttpRequest) and, as a fallback, plain fetch.
  function xhr(opts) {
    if (typeof GM_xmlhttpRequest === "function") return GM_xmlhttpRequest(opts);
    if (typeof GM !== "undefined" && GM && typeof GM.xmlHttpRequest === "function") return GM.xmlHttpRequest(opts);
    fetch(opts.url, { method: opts.method || "GET", headers: opts.headers, body: opts.data, credentials: "include" })
      .then((r) => r.text().then((tx) => opts.onload && opts.onload({ status: r.status, responseText: tx, finalUrl: r.url })))
      .catch(() => opts.onerror && opts.onerror());
  }
  function post(path, body, cb) {
    xhr({ method: "POST", url: API + path,
      headers: { "Content-Type": "application/json", "X-API-Key": KEY },
      data: JSON.stringify(body), onload: (r) => cb && cb(r), onerror: () => cb && cb({ status: 0 }) });
  }

  // =============================== persistence (survives reloads) ===============================
  const store = {
    get(k, d) {
      try { if (typeof GM_getValue === "function") { const v = GM_getValue(k); return v === undefined ? d : v; } } catch (e) {}
      try { const v = localStorage.getItem("g4it_" + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; }
    },
    set(k, v) {
      try { if (typeof GM_setValue === "function") return GM_setValue(k, v); } catch (e) {}
      try { localStorage.setItem("g4it_" + k, JSON.stringify(v)); } catch (e) {}
    },
  };
  const todayStr = () => new Date().toISOString().slice(0, 10);

  // =============================== panel ===============================
  const panel = document.createElement("div");
  panel.style.cssText =
    "position:fixed;bottom:16px;right:16px;z-index:999999;background:#0f172a;color:#e2e8f0;" +
    "font:13px/1.4 system-ui,sans-serif;padding:10px 12px;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.4);" +
    "border:1px solid #334155;min-width:230px;max-width:300px";
  panel.innerHTML =
    '<div style="font-weight:700;margin-bottom:6px">go4it capture</div>' +
    '<div id="g4it-status" style="margin-bottom:8px;color:#94a3b8">connecting…</div>' +
    '<button id="g4it-tune" style="width:100%;margin-bottom:6px;padding:6px;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer">① Send this page to go4it (tune)</button>' +
    '<button id="g4it-cap" style="width:100%;padding:6px;border:0;border-radius:6px;background:#059669;color:#fff;cursor:pointer">② Capture leads now</button>' +
    '<div style="margin-top:10px;border-top:1px solid #334155;padding-top:8px">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">' +
        '<span style="font-weight:600">🎯 Auto-hunter</span>' +
        '<button id="g4it-agent" style="padding:3px 12px;border:0;border-radius:6px;background:#334155;color:#fff;cursor:pointer;font-weight:700">OFF</button>' +
      '</div>' +
      '<div id="g4it-agent-status" style="color:#94a3b8;font-size:12px">stopped</div>' +
    '</div>';
  (document.body || document.documentElement).appendChild(panel);
  const statusEl = panel.querySelector("#g4it-status");
  const setStatus = (t, c) => { statusEl.textContent = t; statusEl.style.color = c || "#94a3b8"; };

  // =============================== connection check ===============================
  function checkHealth(tries) {
    setStatus("connecting…");
    xhr({ method: "GET", url: API + "/api/health",
      onload: (r) => setStatus(r.status === 200 ? "✓ connected to go4it" : "✗ go4it responded " + r.status,
        r.status === 200 ? "#34d399" : "#f87171"),
      onerror: () => {
        if (tries > 0) return setTimeout(() => checkHealth(tries - 1), 1500);
        setStatus("✗ can't reach go4it — is it running on " + API + "? (use Brave/Chrome, not Safari)", "#f87171");
      } });
  }
  checkHealth(3);

  // =============================== ① tune ===============================
  panel.querySelector("#g4it-tune").onclick = function () {
    setStatus("sending page…");
    post("/api/debug/dom", { url: location.href, html: document.documentElement.outerHTML },
      (r) => setStatus(r.status === 200 ? "✓ page sent for tuning" : "✗ send failed " + r.status, r.status === 200 ? "#34d399" : "#f87171"));
  };

  // =============================== parsing helpers ===============================
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();   // \s covers nbsp + newlines
  const txt = (el) => clean(el ? el.textContent : "");

  // Known go4worldbusiness buy-lead field labels. A field's value ends at the next one of
  // these (or end of string) — the live browser collapses card line breaks into spaces, so
  // we can't rely on "\n". Multi-word labels precede their prefixes.
  const STOP_LABELS =
    "Product Name|Specifications?|Specs|Type|Grade|Brand|Model|Application|Purpose|" +
    "Preferred Incoterms|Incoterms|Trade Terms|Quantity Required|Annual Quantity|Order Quantity|Quantity|Qty|" +
    "Moisture Content|Required Documents|Documents Required|Certifications?|HS ?Code|HSN|" +
    "Packaging Terms|Packaging|Packing|Shipping Terms|Shipment|Delivery Time|Delivery|Lead Time|" +
    "Destination Port|Discharge Port|Loading Port|Payment Terms|Payment|Target Price|Price|Budget|" +
    "Looking for suppliers from|Supplier Location|Origin|Size|Colou?r|Frequency|Contact|Company|Email|Phone|Mobile|Website";

  // pull a "Label : value" (or "Label - value"); value stops at the next known label.
  // A real separator after the label is REQUIRED so "Contact" never matches "contacted".
  function fieldFrom(text, label) {
    const re = new RegExp(
      "\\b" + label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") +
      "\\s*[:：\\-–—]\\s*(.+?)\\s*(?=\\s+(?:" + STOP_LABELS + ")\\s*[:：\\-–—]|$)",
      "i");
    const m = text.match(re);
    return m ? m[1].trim() : "";
  }

  // country name -> ISO2 (backend upper-cases; corridor logic keys off GE / TR)
  const ISO2 = {
    georgia: "GE", turkey: "TR", "türkiye": "TR", turkiye: "TR", iran: "IR",
    azerbaijan: "AZ", armenia: "AM", russia: "RU", "russian federation": "RU",
    ukraine: "UA", india: "IN", china: "CN", canada: "CA", "united states": "US",
    "united states of america": "US", usa: "US", "united arab emirates": "AE", uae: "AE",
    kazakhstan: "KZ", turkmenistan: "TM", uzbekistan: "UZ", iraq: "IQ", pakistan: "PK",
    afghanistan: "AF", "united kingdom": "GB", uk: "GB", germany: "DE", spain: "ES",
    france: "FR", italy: "IT", netherlands: "NL", egypt: "EG",
  };
  const iso2 = (name) => ISO2[(name || "").toLowerCase().trim()] || (name || "");

  // parse a card's post date ("Jul-23-26" = MMM-DD-YY, or "3 days ago") -> { year, ageDays } (nulls if unknown)
  const MONTHS = { jan:0, feb:1, mar:2, apr:3, may:4, jun:5, jul:6, aug:7, sep:8, oct:9, nov:10, dec:11 };
  function parsePostDate(cardText) {
    const m = (cardText || "").match(/\b([A-Za-z]{3})-(\d{1,2})-(\d{2,4})\b/);
    if (m && MONTHS[m[1].toLowerCase()] != null) {
      let yr = parseInt(m[3], 10); if (yr < 100) yr += 2000;
      const d = new Date(yr, MONTHS[m[1].toLowerCase()], parseInt(m[2], 10));
      return { year: yr, ageDays: (Date.now() - d.getTime()) / 86400000 };
    }
    const rel = (cardText || "").match(/\b(\d+)\s+(hour|day|week|month|year)s?\s+ago\b/i);
    if (rel) {
      const n = parseInt(rel[1], 10), u = rel[2].toLowerCase();
      const ageDays = n * (u === "hour" ? 1 / 24 : u === "day" ? 1 : u === "week" ? 7 : u === "month" ? 30 : 365);
      return { year: new Date(Date.now() - ageDays * 86400000).getFullYear(), ageDays: ageDays };
    }
    return { year: null, ageDays: null };
  }

  // =============================== extractor (works on any document) ===============================
  function extractLeads(root) {
    root = root || document;
    const out = [];
    const seenIds = new Set();
    // Homepage cards are `.buyleads-row-view`; search / latest-buyleads listing cards are
    // `.entity-rows-container ... search-results-center`. Both share `.entity-rows-container`,
    // so this union covers every layout; the buylead-link guard drops nav links & widgets.
    root.querySelectorAll(".buyleads-row-view, .entity-rows-container").forEach((card) => {
      const a = card.querySelector('a[href*="/buylead/view/"]');
      if (!a) return;                                    // not a lead card
      const href = a.getAttribute("href") || "";
      const idm = href.match(/\/buylead\/view\/(\d+)/);
      const external_id = idm ? idm[1] : "";
      if (external_id) { if (seenIds.has(external_id)) return; seenIds.add(external_id); }  // de-dupe nested containers
      const source_url = href ? (href.indexOf("http") === 0 ? href : location.origin + href) : "";

      // buyer-country flag (e.g. famfamfam-flag-ge) — the most reliable country signal
      let flagCc = "";
      const flag = card.querySelector('i[class*="famfamfam-flag-"]');
      if (flag) { const fm = flag.className.match(/famfamfam-flag-([a-z]{2})\b/i); flagCc = fm ? fm[1].toLowerCase() : ""; }

      let title = txt(card.querySelector(".entity-row-title")).replace(/^\s*WANTED\s*:?\s*/i, "").trim();

      const subtitle = txt(card.querySelector(".subtitle"));            // "Buyer From Canada"
      const buyerCountry = subtitle.replace(/^Buyer\s*From\s*/i, "").trim();

      const descEl = card.querySelector('[class*="entity-row-description"]');  // matches ...-description and ...-description-search
      const desc = clean(descEl ? descEl.textContent : "");            // single-line, deterministic

      const dateStr = ((card.textContent || "").match(/\b[A-Za-z]{3}-\d{1,2}-\d{2,4}\b/) || [])[0] || "";
      const pd = parsePostDate(card.textContent || "");                // for the recency (year) filter
      const archived = /\barchived\b/i.test(card.textContent || "");   // skip ARCHIVED buy-requests

      const productName = fieldFrom(desc, "Product Name") || title;
      const hs = fieldFrom(desc, "HS Code");
      const contact = fieldFrom(desc, "Contact");
      const destPort = fieldFrom(desc, "Destination Port");
      const incoterms = fieldFrom(desc, "Preferred Incoterms") || fieldFrom(desc, "Shipping Terms");
      const qtyRaw = fieldFrom(desc, "Quantity Required") || fieldFrom(desc, "Quantity");

      let quantity = 0, unit = "";
      const qm = qtyRaw.match(/([\d][\d,.]*)\s*([A-Za-z%]+)?/);
      if (qm) { quantity = parseFloat(qm[1].replace(/,/g, "")) || 0; unit = clean(qm[2] || ""); }

      let dest_country = buyerCountry, dest_city = "";
      if (destPort) {
        const parts = destPort.split(",").map((s) => s.trim()).filter(Boolean);
        if (parts.length) { dest_country = parts[parts.length - 1]; dest_city = parts.slice(0, -1).join(", "); }
      }

      const product = (productName || title).trim();
      if (!product) return;

      const specBits = [hs ? "HS " + hs : "", incoterms, dateStr ? "posted " + dateStr : "", desc].filter(Boolean).join(" | ");

      out.push({
        product: product.slice(0, 200),
        external_id: external_id,
        category: "",
        spec: specBits.slice(0, 300),
        quantity: quantity,
        unit: unit.slice(0, 20),
        target_price: 0,
        currency: "USD",
        dest_country: iso2(dest_country),
        dest_city: dest_city.slice(0, 80),
        buyer_company: "",
        contact_name: contact.slice(0, 120),
        email: "",
        phone: "",
        source_url: source_url,
        _flagCc: flagCc,        // internal (stripped before POST) — for country filtering
        _postYear: pd.year,     // internal — for the recency (year >= minPostYear) filter
        _archived: archived,    // internal — ARCHIVED buy-requests are skipped
      });
    });
    return out;
  }

  // =============================== ② manual capture (client-side dedup) ===============================
  const sent = new Set();
  const capBtn = panel.querySelector("#g4it-cap");
  const stripInternal = (l) => { const c = {}; for (const k in l) if (k.charAt(0) !== "_") c[k] = l[k]; return c; };

  function refreshCount() {
    const n = extractLeads().length;
    capBtn.textContent = n ? ("② Capture " + n + " leads now") : "② Capture leads now";
  }
  function capture(auto) {
    const all = extractLeads();
    if (!all.length) { if (!auto) setStatus("0 leads here — open a buy-leads list", "#fbbf24"); return; }
    const toSend = (auto ? all.filter((l) => !l.external_id || !sent.has(l.external_id)) : all).map(stripInternal);
    if (!toSend.length) { if (!auto) setStatus("✓ all " + all.length + " already captured", "#34d399"); return; }
    setStatus("sending " + toSend.length + " lead(s)…");
    post("/api/leads/raw", { leads: toSend }, (r) => {
      if (r.status === 202) {
        toSend.forEach((l) => l.external_id && sent.add(l.external_id));
        setStatus("✓ captured " + toSend.length + " lead(s) → go4it", "#34d399");
      } else {
        setStatus("✗ send failed " + r.status, "#f87171");
      }
    });
  }
  capBtn.onclick = () => capture(false);

  let t = null;
  new MutationObserver(() => { clearTimeout(t); t = setTimeout(refreshCount, 1200); })
    .observe(document.body || document.documentElement, { childList: true, subtree: true });
  refreshCount();

  // =============================== 🎯 auto-hunter ===============================
  function categorize(lead) {
    const hay = ((lead.product || "") + " " + (lead.spec || "")).toLowerCase();
    for (const cat in CONFIG.categories) {
      if (CONFIG.categories[cat].some((k) => hay.indexOf(k) >= 0)) return cat;
    }
    return "";
  }
  function relevantCountry(lead) {
    const set = CONFIG.countries;
    const cc = (lead._flagCc || "").toLowerCase();
    if (cc && set.indexOf(cc) >= 0) return true;
    return set.indexOf((lead.dest_country || "").toLowerCase()) >= 0;
  }
  function recentEnough(lead) {
    return lead._postYear == null || lead._postYear >= CONFIG.minPostYear;  // keep if 2025+ (or undated)
  }
  function fetchDoc(url, cb) {
    const full = url.indexOf("http") === 0 ? url : location.origin + url;
    xhr({ method: "GET", url: full,
      onload: (r) => {
        let doc = null;
        try { doc = new DOMParser().parseFromString(r.responseText || "", "text/html"); } catch (e) {}
        cb(doc, r);
      },
      onerror: () => cb(null, { status: 0 }) });
  }
  function isBlockPage(doc, r) {
    const rt = (r && r.responseText) || "";
    const st = (r && r.status) || 0;
    if (st === 202 || st === 403 || st === 429) return true;            // go4world's throttle signatures (202 stub etc.)
    if (/too many requests|temporarily blocked|unusual traffic|are you a robot|recaptcha/i.test(rt)) return true;
    const fu = (r && (r.finalUrl || r.responseURL)) || "";
    return /\/login(\b|\/|\?)/i.test(fu);                               // bounced to login
    // NOTE: a MISSING country/category page (redirect to worldwide / 404 / tiny body) is NOT a block —
    // it just yields 0 cards and the scan moves on to the next target, so one bad URL can't halt a scan.
  }

  let agentOn = store.get("agentOn", false);
  let counts = store.get("counts", { date: todayStr(), tiles: 0, bricks: 0, sent: 0 });
  let seen = new Set(store.get("seen", []));
  let nextScanAt = store.get("nextScanAt", 0);
  let agentTimer = null, backoffMin = 0, scanning = false;

  function persistSeen() {
    let arr = Array.from(seen);
    if (arr.length > 3000) { arr = arr.slice(arr.length - 3000); seen = new Set(arr); }
    store.set("seen", arr);
  }
  function bumpCounts(leads) {
    if (counts.date !== todayStr()) counts = { date: todayStr(), tiles: 0, bricks: 0, sent: 0 };
    leads.forEach((l) => { if (l.category === "tiles") counts.tiles++; else if (l.category === "bricks") counts.bricks++; });
    counts.sent += leads.length;
    store.set("counts", counts);
  }
  function renderAgent() {
    const tgl = panel.querySelector("#g4it-agent");
    tgl.textContent = agentOn ? "ON" : "OFF";
    tgl.style.background = agentOn ? "#059669" : "#334155";
    if (counts.date !== todayStr()) counts = { date: todayStr(), tiles: 0, bricks: 0, sent: 0 };
    const tally = "today: " + counts.tiles + " tiles · " + counts.bricks + " bricks";
    let line;
    if (!agentOn) line = "stopped · " + tally;
    else if (scanning) line = "scanning… · " + tally;
    else if (backoffMin) line = "⏸ backing off " + backoffMin + "m · " + tally;
    else if (nextScanAt) line = "next scan ~" + Math.max(0, Math.round((nextScanAt - Date.now()) / 60000)) + "m · " + tally;
    else line = tally;
    panel.querySelector("#g4it-agent-status").textContent = line;
  }
  function scheduleNext(delayMin) {
    clearTimeout(agentTimer);
    const explicit = (delayMin != null);
    const base = explicit ? delayMin : CONFIG.scanEveryMin;
    const jitter = explicit ? 0 : (Math.random() * 2 - 1) * CONFIG.jitterMin;
    const min = Math.max(0.2, base + jitter);
    nextScanAt = Date.now() + min * 60000;
    store.set("nextScanAt", nextScanAt);
    agentTimer = setTimeout(() => { if (agentOn) scanOnce(false); }, min * 60000);
    renderAgent();
  }
  function finishScan(collected, blocked, cardsSeen) {
    scanning = false;
    if (blocked) {
      backoffMin = Math.min(CONFIG.maxBackoffMin, backoffMin ? backoffMin * 2 : CONFIG.scanEveryMin);
      setStatus("🎯 hunter: go4world pushed back — backing off " + backoffMin + "m", "#fbbf24");
      return scheduleNext(backoffMin);
    }
    backoffMin = 0;
    if (!collected.length) {
      // "saw N cards" distinguishes "fetched fine, nothing fresh" (N>0) from "fetch got nothing" (N=0).
      const seenN = cardsSeen || 0;
      setStatus("🎯 hunter: scan done — 0 new (saw " + seenN + " buy-request card" + (seenN === 1 ? "" : "s") + ")",
        seenN > 0 ? "#94a3b8" : "#fbbf24");
      return scheduleNext();
    }
    const payload = collected.map(stripInternal);
    setStatus("🎯 hunter: sending " + payload.length + " new lead(s)…");
    post("/api/leads/raw", { leads: payload }, (r) => {
      if (r.status === 202) {
        collected.forEach((l) => l.external_id && seen.add(l.external_id));
        persistSeen();
        bumpCounts(collected);
        setStatus("🎯 hunter: +" + payload.length + " new → go4it (today " + counts.tiles + "🔲 / " + counts.bricks + "🧱)", "#34d399");
      } else {
        setStatus("🎯 hunter: send failed " + r.status + " (is go4it running?)", "#f87171");
      }
      scheduleNext();
    });
  }
  function scanOnce() {
    if (scanning) return;
    scanning = true; renderAgent();
    const queue = CONFIG.targets.map((t) => ({ url: t.url, label: t.label, page: 1 }));
    const collected = [];
    const seenThisScan = new Set();
    let fetches = 0, cardsSeen = 0;
    (function step() {
      if (!queue.length) return finishScan(collected, false, cardsSeen);
      const job = queue.shift();
      setStatus("🎯 hunter: scanning " + job.label + (job.page > 1 ? " p" + job.page : "") + "…");
      fetchDoc(job.url, (doc, r) => {
        fetches++;
        if (doc && isBlockPage(doc, r)) return finishScan(collected, true, cardsSeen);
        if (doc) {
          const pageLeads = extractLeads(doc);
          cardsSeen += pageLeads.length;
          pageLeads.forEach((l) => {
            if (!l.external_id) return;
            if (CONFIG.skipArchived && l._archived) return;    // skip ARCHIVED buy-requests
            const cat = categorize(l); if (!cat) return;       // tile/brick only
            if (!relevantCountry(l)) return;                   // GE + neighbours only
            if (!recentEnough(l)) return;                      // posted 2025+ only
            if (seen.has(l.external_id) || seenThisScan.has(l.external_id)) return;  // new only
            seenThisScan.add(l.external_id);
            l.category = cat;
            collected.push(l);
          });
          // follow rel="next" through EVERY page (bounded by maxPagesPerTarget / maxFetchesPerScan)
          if (job.page < CONFIG.maxPagesPerTarget) {
            const nx = doc.querySelector('a[rel="next"], link[rel="next"]');
            const nextUrl = nx ? nx.getAttribute("href") : "";
            if (nextUrl) queue.push({ url: nextUrl, label: job.label, page: job.page + 1 });
          }
        }
        if (fetches >= CONFIG.maxFetchesPerScan) return finishScan(collected, false, cardsSeen);  // stay gentle
        setTimeout(step, CONFIG.gapMs);
      });
    })();
  }
  function setAgent(on) {
    agentOn = on; store.set("agentOn", on); backoffMin = 0;
    if (on) { renderAgent(); scanOnce(); } else { clearTimeout(agentTimer); scanning = false; renderAgent(); }
  }
  panel.querySelector("#g4it-agent").onclick = () => setAgent(!agentOn);

  setInterval(renderAgent, 30000);       // keep the countdown fresh
  renderAgent();
  if (agentOn) scheduleNext(CONFIG.firstScanDelayMin);   // resume if it was left ON before a reload
})();
