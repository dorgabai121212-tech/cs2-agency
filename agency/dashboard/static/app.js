/* Imagination agency dashboard — vanilla JS, no dependencies, no external assets.
 *
 * SECURITY: every piece of data (owner, LLM, platform, server) reaches the DOM through
 * textContent / attributes via el(). Never innerHTML with data. Links are http(s) only,
 * thumbnails only from /api/videos/<id>/thumbnail, the QR code only as a data: <img>.
 * Icons are built with createElementNS from constant path data in this file.
 *
 * Screens (hash routing): #/ home · #/connections[/<platform>] · #/settings · #/advanced.
 * Owner-facing copy is Hebrew, short, and free of technical words on the default screens.
 * Technical details live only on #/advanced ("מצב מתקדם").
 */
(function () {
  "use strict";

  // ================================================================== constants
  const POLL_MS = 3000;
  const POSTS_EVERY = 3;            // posts (publish / fail toasts) every 3rd home poll
  const HIDDEN_EVERY = 10;          // tab in the background: home + posts every 30 s
  const RECHECK_EVERY = 20;         // a tile that could not be checked: check again every 60 s
  const HEADERS = { "X-Agency-Request": "1" };
  const SVG_NS = "http://www.w3.org/2000/svg";
  const BODY = document.body;
  const ALLOWED_EXT = (BODY.dataset.ext || ".mp4 .mov .mkv .webm .avi").split(/\s+/).filter(Boolean);
  const MAX_GB = parseFloat(BODY.dataset.maxGb || "4") || 4;
  const MAX_BYTES = Math.floor(MAX_GB * 1024 ** 3);
  const REDUCED = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const IS_PC = ["127.0.0.1", "localhost", "::1", "[::1]"].includes(location.hostname);
  const LS_ADVANCED = "imagination.advanced";
  const LS_NOTIFY_ASKED = "imagination.notifyAsked";
  const LS_KEEP_OLD = "imagination.keepOldStudio";
  const LS_SECURITY_SEEN = "imagination.securitySeen";
  const PC_ONLY_HE = "את זה עושים רק מהמחשב עצמו (לא מהטלפון).";
  const OFFLINE_HE = "אין חיבור לסוכנות במחשב. בדוק שהיא פתוחה ונסה שוב.";

  const PLATFORMS = {
    youtube: "יוטיוב", youtube_shorts: "יוטיוב Shorts", tiktok: "טיקטוק",
    instagram_reels: "אינסטגרם", facebook: "פייסבוק", x: "X", reddit: "רדיט",
    discord: "דיסקורד", telegram: "טלגרם",
  };
  const TILE_ORDER = ["youtube", "youtube_shorts", "tiktok", "instagram_reels", "facebook",
    "telegram", "x", "reddit", "discord"];
  const TEAM_ICON = {
    manager: "crown", analyst: "eye", editor: "scissors", copywriter: "pen", strategist: "crosshair",
    publisher: "rocket", promoter: "megaphone", community: "chat", analytics: "chart", finance: "coin",
  };
  const STEP_ICON = { watch: "eye", cut: "scissors", write: "pen", plan: "crosshair", ready: "check" };
  const TEAM_STATUS_HE = { idle: "פנוי", working: "עובד עכשיו", problem: "נתקע" };

  // ================================================================== storage (never required)
  function store(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, String(value));
    } catch (_) { /* private mode / blocked storage: the page works without it */ }
    return null;
  }
  const advancedOn = () => store(LS_ADVANCED) === "1";

  // ================================================================== DOM helpers
  const $ = (id) => document.getElementById(id);
  const PROPS = new Set(["checked", "disabled", "value", "selected", "hidden", "indeterminate"]);

  /** Create an element. text → textContent, cls → className, on → listeners, PROPS → properties,
   * everything else → attributes ("style" and HTML-injection keys are refused). */
  function el(tag, props, children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === "text") node.textContent = String(v);
        else if (k === "cls") node.className = v;
        else if (k === "on") for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
        else if (PROPS.has(k)) node[k] = v;
        else if (k === "style" || k === "innerHTML" || k === "outerHTML" || /^on/i.test(k)) continue;
        else node.setAttribute(k, v === true ? "" : String(v));
      }
    }
    append(node, children);
    return node;
  }
  function append(node, children) {
    for (const c of [].concat(children === undefined ? [] : children)) {
      if (c === null || c === undefined || c === false || c === "") continue;
      node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    }
    return node;
  }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); return node; }
  function setText(node, text) { if (node && node.textContent !== String(text)) node.textContent = String(text); }

  function safeUrl(url) {
    try {
      const u = new URL(String(url), location.href);
      return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
    } catch (_) { return null; }
  }
  const THUMB_RE = /^\/api\/videos\/\d+\/thumbnail$/;

  // ------------------------------------------------------------------ icons (constant data)
  const GEAR = "M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z";
  const ICONS = {
    plug: { p: ["M9 2v5", "M15 2v5", "M6 7h12v3.5a6 6 0 0 1-12 0z", "M12 16.5V22"] },
    gear: { p: [GEAR], c: [[12, 12, 3]] },
    terminal: { p: ["M5 17l5-5-5-5", "M12 19h7"] },
    crown: { p: ["M3 8l4.5 4L12 5l4.5 7L21 8l-2 10H5z", "M5 21h14"] },
    eye: { p: ["M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"], c: [[12, 12, 3]] },
    scissors: { p: ["M20 4 8.1 15.9", "M14.5 14.5 20 20", "M8.1 8.1 12 12"], c: [[6, 6, 3], [6, 18, 3]] },
    pen: { p: ["M12 20h9", "M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"] },
    crosshair: { p: ["M12 2v5", "M12 17v5", "M2 12h5", "M17 12h5"], c: [[12, 12, 7.5], [12, 12, .6]] },
    rocket: { p: ["M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z", "M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z", "M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0", "M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"] },
    megaphone: { p: ["M3 10v4a1 1 0 0 0 1 1h2l5 4V5L6 9H4a1 1 0 0 0-1 1z", "M15.5 8.5a5 5 0 0 1 0 7", "M18.5 5.5a9 9 0 0 1 0 13"] },
    chat: { p: ["M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"] },
    chart: { p: ["M3 3v18h18", "M7 15l4-4 3 3 5-6"] },
    coin: { c: [[12, 12, 9]], p: ["M15 9.2a3 3 0 0 0-3-1.7c-1.6 0-2.8.9-2.8 2.1 0 2.9 5.8 1.6 5.8 4.6 0 1.2-1.2 2.1-2.9 2.1a3.2 3.2 0 0 1-3.1-1.8", "M12 6v1.5", "M12 16.5V18"] },
    upload: { p: ["M12 16V4", "M7 9l5-5 5 5", "M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"] },
    download: { p: ["M12 4v12", "M7 11l5 5 5-5", "M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"] },
    check: { p: ["M5 12.5l4.5 4.5L19 7"], sw: 2.6 },
    x: { p: ["M6 6l12 12", "M18 6 6 18"] },
    send: { p: ["M22 2 11 13", "M22 2l-7 20-4-9-9-4z"] },
    chevron: { p: ["M9 6l6 6-6 6"] },
    "chevron-down": { p: ["M6 9l6 6 6-6"] },
    arrow: { p: ["M5 12h14", "M13 6l6 6-6 6"] },
    refresh: { p: ["M21 12a9 9 0 1 1-2.64-6.36", "M21 4v5h-5"] },
    lock: { r: [[5, 11, 14, 10, 2]], p: ["M8 11V7a4 4 0 0 1 8 0v4"] },
    phone: { r: [[7, 2, 10, 20, 2.5]], p: ["M11 18h2"] },
    folder: { p: ["M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"] },
    film: { r: [[3, 3, 18, 18, 2.5]], p: ["M7 3v18", "M17 3v18", "M3 8h4", "M3 16h4", "M17 8h4", "M17 16h4", "M3 12h18"] },
    sparkles: { p: ["M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z", "M19 16l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"] },
    alert: { p: ["M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", "M12 9v4", "M12 17h.01"] },
    info: { c: [[12, 12, 9]], p: ["M12 11v5", "M12 8h.01"] },
    external: { p: ["M15 3h6v6", "M10 14 21 3", "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"] },
    copy: { r: [[9, 9, 12, 12, 2]], p: ["M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"] },
    key: { c: [[7.5, 15.5, 4.5]], p: ["M10.7 12.3 21 2", "M16 7l3 3", "M19 4l2 2"] },
    trophy: { p: ["M8 21h8", "M12 17v4", "M7 4h10v5a5 5 0 0 1-10 0z", "M17 5h3v2a3 3 0 0 1-3 3", "M7 5H4v2a3 3 0 0 0 3 3"] },
    heart: { p: ["M19 14c1.5-1.5 3-3.2 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.8 0-3 .5-4.5 2-1.5-1.5-2.7-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4 3 5.5l7 7z"] },
    comment: { p: ["M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-4-.9L3 20l1.1-4A8.4 8.4 0 1 1 21 11.5z"] },
    wallet: { p: ["M19 7V5a2 2 0 0 0-2-2H5a2 2 0 0 0 0 4h14a2 2 0 0 1 2 2v3", "M3 5v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-3", "M21 12h-4a2 2 0 0 0 0 4h4z"] },
    users: { c: [[9, 8, 4]], p: ["M2 21a7 7 0 0 1 14 0", "M16 4.2a4 4 0 0 1 0 7.6", "M22 21a7 7 0 0 0-4-6.3"] },
    bell: { p: ["M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9", "M10.3 21a1.9 1.9 0 0 0 3.4 0"] },
    shield: { p: ["M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z", "M9 12l2 2 4-4"] },
    logout: { p: ["M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4", "M16 17l5-5-5-5", "M21 12H9"] },
    unlink: { p: ["M18.8 13.4l1.8-1.8a4.9 4.9 0 0 0-7-7l-1.8 1.8", "M5.2 10.6l-1.8 1.8a4.9 4.9 0 0 0 7 7l1.8-1.8", "M8 2v3", "M2 8h3", "M16 22v-3", "M22 16h-3"] },
    link: { p: ["M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7", "M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"] },
    play: { p: ["M7 4v16l13-8z"] },
    clock: { c: [[12, 12, 9]], p: ["M12 7v5l3 2"] },
    wifi: { p: ["M5 12.5a10 10 0 0 1 14 0", "M8.5 16a5 5 0 0 1 7 0", "M2 9a15 15 0 0 1 20 0", "M12 19.5h.01"] },
    google: { p: ["M21.6 12.2c0-.7-.1-1.4-.2-2H12v3.8h5.4a4.6 4.6 0 0 1-2 3v2.5h3.2c1.9-1.7 3-4.3 3-7.3z", "M12 22c2.7 0 5-.9 6.6-2.4l-3.2-2.5c-.9.6-2 1-3.4 1-2.6 0-4.8-1.8-5.6-4.1H3.1v2.6A10 10 0 0 0 12 22z", "M6.4 14a6 6 0 0 1 0-4V7.4H3.1a10 10 0 0 0 0 9.2z", "M12 6c1.5 0 2.8.5 3.8 1.5l2.9-2.9A10 10 0 0 0 3.1 7.4L6.4 10C7.2 7.7 9.4 6 12 6z"], fillMode: true },
  };

  function svgNode(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
    return node;
  }
  function icon(name, cls) {
    const def = ICONS[name] || ICONS.info;
    const s = svgNode("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false",
      class: "ic" + (cls ? " " + cls : "") });
    if (def.fillMode) { s.setAttribute("fill", "currentColor"); }
    else {
      s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor");
      s.setAttribute("stroke-width", String(def.sw || 2));
      s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
    }
    for (const d of def.p || []) s.appendChild(svgNode("path", { d }));
    for (const [cx, cy, r] of def.c || []) s.appendChild(svgNode("circle", { cx, cy, r }));
    for (const [x, y, w, h, rx] of def.r || []) s.appendChild(svgNode("rect", { x, y, width: w, height: h, rx }));
    return s;
  }

  // Platform marks: simple white glyphs on the brand color (constant data).
  const TIKTOK_D = "M16.6 3h-3.1v11.9a2.7 2.7 0 1 1-2.7-2.7c.3 0 .5 0 .8.1V9.1a5.9 5.9 0 1 0 5 5.8V8.9a7.4 7.4 0 0 0 4.1 1.3V7.1a4.1 4.1 0 0 1-4.1-4.1z";
  const GLYPHS = {
    youtube: [["path", { d: "M9.4 7.2v9.6l7.9-4.8z", fill: "#fff" }]],
    youtube_shorts: [
      ["path", { d: "M14.9 3.4a3.8 3.8 0 0 1 3.7 6.6l-1.4.8 1.3.6a3.8 3.8 0 0 1-.3 7l-6.8 3.2a3.8 3.8 0 0 1-3.7-6.6l1.4-.8-1.3-.7a3.8 3.8 0 0 1 .3-7z", fill: "#fff" }],
      ["path", { d: "M10.3 9.2v5.6l4.7-2.8z", fill: "#ff2d55" }],
    ],
    tiktok: [
      ["path", { d: TIKTOK_D, fill: "#25f4ee", transform: "translate(-.8 -.6)" }],
      ["path", { d: TIKTOK_D, fill: "#fe2c55", transform: "translate(.8 .6)" }],
      ["path", { d: TIKTOK_D, fill: "#fff" }],
    ],
    instagram_reels: [
      ["rect", { x: 4.5, y: 4.5, width: 15, height: 15, rx: 4.6, fill: "none", stroke: "#fff", "stroke-width": 2 }],
      ["circle", { cx: 12, cy: 12, r: 3.6, fill: "none", stroke: "#fff", "stroke-width": 2 }],
      ["circle", { cx: 16.4, cy: 7.6, r: 1.1, fill: "#fff" }],
    ],
    facebook: [["path", { d: "M13.5 21v-7.7h2.6l.4-3h-3V8.4c0-.9.3-1.5 1.5-1.5h1.6V4.2a21 21 0 0 0-2.3-.1c-2.3 0-3.9 1.4-3.9 4v2.2H7.8v3h2.6V21z", fill: "#fff" }]],
    x: [["path", { d: "M17.3 3.5h2.9l-6.4 7.3 7.5 9.7h-5.9l-4.6-6-5.3 6H2.6l6.8-7.8-7.2-9.2h6l4.2 5.5zm-1 15.3h1.6L7.8 5.1H6.1z", fill: "#fff" }]],
    reddit: [
      ["ellipse", { cx: 12, cy: 14.2, rx: 7.6, ry: 5.3, fill: "#fff" }],
      ["circle", { cx: 4.9, cy: 11.4, r: 1.9, fill: "#fff" }],
      ["circle", { cx: 19.1, cy: 11.4, r: 1.9, fill: "#fff" }],
      ["path", { d: "M12 9l1.2-4.6 3.8.9", fill: "none", stroke: "#fff", "stroke-width": 1.5, "stroke-linecap": "round", "stroke-linejoin": "round" }],
      ["circle", { cx: 18.4, cy: 4.9, r: 1.6, fill: "#fff" }],
      ["circle", { cx: 9.3, cy: 13.6, r: 1.25, fill: "#ff4500" }],
      ["circle", { cx: 14.7, cy: 13.6, r: 1.25, fill: "#ff4500" }],
      ["path", { d: "M9.4 16.5c1.6 1 3.6 1 5.2 0", fill: "none", stroke: "#ff4500", "stroke-width": 1.3, "stroke-linecap": "round" }],
    ],
    discord: [["path", { d: "M19.3 5.4a16.4 16.4 0 0 0-4.1-1.3l-.5 1.1a15 15 0 0 0-5.4 0l-.5-1.1a16.4 16.4 0 0 0-4.1 1.3C2.1 9.3 1.4 13.1 1.7 16.8a16.6 16.6 0 0 0 5.1 2.6l1.1-1.8a10.7 10.7 0 0 1-1.7-.8l.4-.3a11.8 11.8 0 0 0 10.8 0l.4.3c-.5.3-1.1.6-1.7.8l1.1 1.8a16.5 16.5 0 0 0 5.1-2.6c.4-4.3-.7-8-2.9-11.4zM8.7 14.6c-1 0-1.8-.9-1.8-2.1s.8-2.1 1.8-2.1 1.8.9 1.8 2.1-.8 2.1-1.8 2.1zm6.6 0c-1 0-1.8-.9-1.8-2.1s.8-2.1 1.8-2.1 1.8.9 1.8 2.1-.8 2.1-1.8 2.1z", fill: "#fff", "fill-rule": "evenodd" }]],
    telegram: [
      ["path", { d: "M20.7 4.4 3.3 11.1c-1.2.5-1.2 1.2-.2 1.5l4.4 1.4 1.7 5.2c.2.6.1.8.7.8.5 0 .7-.2 1-.5l2.1-2.1 4.5 3.3c.8.5 1.4.2 1.6-.8l2.9-13.8c.3-1.2-.5-1.8-1.3-1.5z", fill: "#fff" }],
      ["path", { d: "M8.6 13.9l8.6-5.4c.4-.3.8-.1.5.2l-7.1 6.4-.3 3.3", fill: "none", stroke: "#229ed9", "stroke-width": 1.1, "stroke-linejoin": "round" }],
    ],
  };
  function pico(key, cls) {
    const k = GLYPHS[key] ? key : "youtube";
    const s = svgNode("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false" });
    for (const [tag, attrs] of GLYPHS[k]) s.appendChild(svgNode(tag, attrs));
    return el("span", { cls: "pico b-" + k + (cls ? " " + cls : ""), "aria-hidden": "true" }, [s]);
  }
  function pchip(p) {
    return el("span", { cls: "pchip" }, [pico(p.key || p.platform), p.name_he || PLATFORMS[p.key] || p.key]);
  }
  /** "מחובר ✓ @handle" with the account name isolated (an LTR handle inside a Hebrew line). */
  function statusNode(tile, cls) {
    const text = String(tile.status_he || "");
    const name = tile.account_name ? String(tile.account_name) : "";
    if (name && text.endsWith(name)) {
      return el("span", { cls }, [text.slice(0, text.length - name.length), el("bdi", { text: name })]);
    }
    return el("span", { cls, text });
  }
  // Static placeholders in the template: <span data-icon="name">.
  function hydrateIcons(root) {
    (root || document).querySelectorAll("[data-icon]").forEach((node) => {
      if (node.querySelector(":scope > svg.ic")) return;
      node.insertBefore(icon(node.dataset.icon), node.firstChild);
    });
  }

  // ================================================================== formatting
  const nf = new Intl.NumberFormat("he-IL");
  function fmtNum(n) { return nf.format(Math.round(Number(n) || 0)); }
  function fmtBig(n) {
    const v = Number(n) || 0;
    if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
    if (v >= 1e4) return Math.round(v / 1e3) + "K";
    return fmtNum(v);
  }
  function fmtIls(n) {
    const v = Number(n) || 0;
    return v.toLocaleString("he-IL", { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + "\u00a0₪";
  }
  function fmtBytes(n) {
    if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + "GB";
    if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + "MB";
    return Math.max(1, Math.round(n / 1024)) + "KB";
  }
  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("he-IL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  const hasHebrew = (s) => /[\u0590-\u05FF]/.test(String(s || ""));

  // ================================================================== API
  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status; this.data = data; }
  }
  const TECH_RE = /\b[a-z_]+(\.[a-z_]+)+\b|\([A-Za-z0-9_.]+\)|https?:\/\/\S+/g;

  /** One plain-Hebrew line for a failed request (never technical text or values). */
  function friendlyError(status, data) {
    const reason = data && data.reason;
    if (reason === "local_only") return PC_ONLY_HE;
    if (reason === "remote_disabled") return "הגישה מהטלפון כבויה. מדליקים אותה בהגדרות, במחשב.";
    if (reason === "confirm_required") return (data && data.confirm_he) || "צריך לאשר קודם.";
    const detail = data && typeof data.detail === "string" ? data.detail : "";
    if (detail && hasHebrew(detail)) {
      const clean = detail.replace(TECH_RE, "").replace(/\s{2,}/g, " ").replace(/\s+([:.,])/g, "$1").trim();
      if (hasHebrew(clean)) return clean;
    }
    if (status === 0) return OFFLINE_HE;
    if (status === 404) return "לא מצאתי את זה — אולי זה כבר טופל.";
    if (status === 409) return "זה כבר השתנה בינתיים. נסה שוב.";
    if (status === 413) return "הקובץ גדול מדי.";
    if (status === 403) return "אין הרשאה לפעולה הזאת.";
    if (status === 400 || status === 422) return "משהו בפרטים לא תקין. בדוק ונסה שוב.";
    return "משהו השתבש. נסה שוב עוד רגע.";
  }

  async function api(path, opts) {
    const o = opts || {};
    const init = { method: o.method || (o.json !== undefined || o.body !== undefined ? "POST" : "GET"),
      headers: Object.assign({}, HEADERS), credentials: "same-origin", cache: "no-store" };
    if (o.json !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(o.json); }
    else if (o.body !== undefined) { init.body = o.body; if (o.type) init.headers["Content-Type"] = o.type; }
    let res;
    try { res = await fetch(path, init); }
    catch (_) { throw new ApiError(OFFLINE_HE, 0, null); }
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (res.status === 401 && data && (data.login || data.reason === "login_required")) {
      location.href = "/login";
      throw new ApiError("צריך להיכנס עם הקוד.", 401, data);
    }
    if (!res.ok) throw new ApiError(friendlyError(res.status, data), res.status, data);
    return data;
  }

  // ================================================================== toasts
  function toast(text, kind, opts) {
    const o = opts || {};
    const k = kind || "info";
    const box = $("toasts");
    const ic = o.icon || { ok: "check", err: "alert", warn: "alert", info: "info" }[k] || "info";
    const node = el("div", { cls: "toast " + k, role: k === "err" ? "alert" : null }, [
      el("span", { cls: "t-ic" }, [icon(ic)]),
      el("span", { cls: "t-txt", text }),
    ]);
    let timer = null;
    const close = () => {
      clearTimeout(timer);
      if (!node.isConnected) return;
      node.classList.add("out");
      setTimeout(() => node.remove(), REDUCED ? 0 : 240);
    };
    if (o.action) {
      node.appendChild(el("button", { type: "button", cls: "btn btn-sm btn-steel t-act", text: o.action.label,
        on: { click: () => { close(); o.action.fn(); } } }));
    }
    if (o.link) {
      const href = safeUrl(o.link.url);
      if (href) node.appendChild(el("a", { cls: "btn btn-sm btn-steel t-act", href, target: "_blank",
        rel: "noopener noreferrer", text: o.link.label || "לצפייה" }));
    }
    node.appendChild(el("button", { type: "button", cls: "t-x", "aria-label": "סגור הודעה",
      on: { click: close } }, [icon("x", "ic-sm")]));
    box.appendChild(node);
    while (box.children.length > 4) box.firstChild.remove();
    const ms = o.ms || (o.action || o.link ? 9000 : 5000);
    const arm = () => { timer = setTimeout(close, ms); };
    node.addEventListener("mouseenter", () => clearTimeout(timer));
    node.addEventListener("mouseleave", arm);
    arm();
    return close;
  }

  // ================================================================== dialogs
  let dialogSeq = 0;
  /** A native <dialog> (modal; a bottom sheet on phones). Returns {dlg, body, foot, close}. */
  function openDialog(cfg) {
    const id = "dlg-" + (++dialogSeq);
    const dlg = el("dialog", { cls: "modal" + (cfg.wide ? " wide" : ""), "aria-labelledby": id + "-t" });
    const closeBtn = el("button", { type: "button", cls: "modal-close", "aria-label": "סגור" }, [icon("x")]);
    const head = el("div", { cls: "modal-head" }, [cfg.iconNode || null,
      el("h2", { id: id + "-t", text: cfg.title || "" }), closeBtn]);
    const body = el("div", { cls: "modal-body" });
    const foot = el("div", { cls: "modal-foot" });
    const inner = el("div", { cls: "modal-inner" }, [head, cfg.afterHead || null, body, foot]);
    dlg.appendChild(inner);
    document.body.appendChild(dlg);
    let closed = false;
    const opener = document.activeElement;
    function close(result) {
      if (closed) return;
      closed = true;
      dlg.classList.remove("shown");
      setTimeout(() => {
        try { dlg.close(); } catch (_) { /* already closed */ }
        dlg.remove();
        if (opener && opener.isConnected && typeof opener.focus === "function") opener.focus({ preventScroll: true });
        if (cfg.onClose) cfg.onClose(result);
      }, REDUCED ? 0 : 200);
    }
    closeBtn.addEventListener("click", () => close());
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
    dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });
    const api_ = { dlg, head, body, foot, close, title: head.querySelector("h2"),
      isOpen: () => !closed };
    if (cfg.build) cfg.build(api_);
    foot.hidden = !foot.firstChild;
    if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
    requestAnimationFrame(() => dlg.classList.add("shown"));
    return api_;
  }
  function setFoot(d, buttons) {
    clear(d.foot);
    append(d.foot, buttons);
    d.foot.hidden = !d.foot.firstChild;
  }
  /** Promise<boolean>: a calm confirm dialog (instead of the browser's confirm()). */
  function confirmDialog(cfg) {
    return new Promise((resolve) => {
      let answer = false;
      openDialog({
        title: cfg.title, iconNode: cfg.iconNode || null,
        onClose: () => resolve(answer),
        build: (d) => {
          append(d.body, [el("p", { cls: "confirm-text", text: cfg.text })].concat(cfg.extra || []));
          const yes = el("button", { type: "button", cls: "btn btn-big " + (cfg.danger ? "btn-danger" : "btn-primary"),
            text: cfg.yes || "כן", on: { click: () => { answer = true; d.close(); } } });
          const no = el("button", { type: "button", cls: "btn btn-big btn-ghost", text: cfg.no || "ביטול",
            on: { click: () => d.close() } });
          setFoot(d, [yes, no]);
          setTimeout(() => yes.focus(), 30);
        },
      });
    });
  }

  const busyKids = new WeakMap();   // button -> its original children (icon + label)
  function busy(btn, on, label) {
    if (!btn) return;
    if (on) {
      const text = label || btn.textContent;
      if (!busyKids.has(btn)) busyKids.set(btn, Array.from(btn.childNodes));
      btn.disabled = true;
      btn.classList.add("is-busy");
      btn.setAttribute("aria-busy", "true");
      clear(btn);
      append(btn, [el("span", { cls: "spin", "aria-hidden": "true" }), text]);
    } else {
      const kids = busyKids.get(btn);
      btn.disabled = false;
      btn.classList.remove("is-busy");
      btn.removeAttribute("aria-busy");
      if (kids) { clear(btn); kids.forEach((k) => btn.appendChild(k)); busyKids.delete(btn); }
    }
  }
  async function copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
      toast("הועתק", "ok", { ms: 2000 });
    } catch (_) {
      if (btn) btn.focus();
      toast("לא הצלחתי להעתיק — סמן והעתק ידנית", "warn");
    }
  }
  function copyRow(text) {
    const b = el("button", { type: "button", cls: "btn btn-sm btn-ghost", "aria-label": "העתק" }, [icon("copy", "ic-sm"), "העתק"]);
    b.addEventListener("click", () => copyText(text, b));
    return el("div", { cls: "copy-row" }, [el("code", { text }), b]);
  }

  // ================================================================== router
  const PAGES = ["home", "connections", "settings", "advanced"];
  let route = { page: "home", sub: "" };
  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, "");
    const [page, sub] = raw.split("/");
    return { page: PAGES.includes(page) ? page : "home", sub: (sub || "").replace(/[^a-z_]/g, "") };
  }
  function go() {
    const next = parseHash();
    if (next.page === "advanced" && !advancedOn()) { location.replace("#/settings"); return; }
    const changedPage = next.page !== route.page || !go.started;
    route = next;
    go.started = true;
    for (const p of PAGES) {
      const sec = $("page-" + p);
      const on = p === route.page;
      sec.classList.toggle("hidden", !on);
      if (on && changedPage && !REDUCED) { sec.classList.remove("page-enter"); void sec.offsetWidth; sec.classList.add("page-enter"); }
    }
    $("nav-connections").toggleAttribute("aria-current", route.page === "connections");
    if (route.page === "connections") $("nav-connections").setAttribute("aria-current", "page");
    $("nav-settings").toggleAttribute("aria-current", route.page === "settings");
    if (route.page === "settings") $("nav-settings").setAttribute("aria-current", "page");
    $("nav-advanced").toggleAttribute("aria-current", route.page === "advanced");
    if (route.page === "advanced") $("nav-advanced").setAttribute("aria-current", "page");
    BODY.classList.toggle("saving-open", false);
    if (changedPage) {
      window.scrollTo(0, 0);
      $("main").focus({ preventScroll: true });
    }
    updateTitle();
    if (route.page === "home") refreshHome();
    else if (route.page === "connections") loadConnections(route.sub);
    else if (route.page === "settings") loadSettings();
    else if (route.page === "advanced") loadAdvanced();
  }
  function updateTitle() {
    const n = home && home.needs_you ? home.needs_you.length : 0;
    const name = { home: "", connections: "חיבורים · ", settings: "הגדרות · ", advanced: "מצב מתקדם · " }[route.page] || "";
    document.title = (n ? "(" + n + ") " : "") + name + "Imagination · הסוכנות";
  }

  // ================================================================== HOME
  let home = null;
  let homeBusy = false;
  let online = true;
  const last = {};
  function changed(key, value) {
    const s = JSON.stringify(value === undefined ? null : value);
    if (last[key] === s) return false;
    last[key] = s;
    return true;
  }
  function setOnline(on) {
    if (on === online) return;
    online = on;
    $("offline").classList.toggle("hidden", on);
  }

  async function refreshHome() {
    if (homeBusy) return;
    homeBusy = true;
    try {
      const data = await api("/api/home");
      setOnline(true);
      home = data;
      renderHome(data);
      detectApprovals(data.needs_you || []);
    } catch (e) {
      if (e.status === 0) setOnline(false);
    } finally {
      homeBusy = false;
    }
  }

  function renderHome(d) {
    renderModePill(d);
    if (changed("channel", d.channel)) setText($("brand-name"), d.channel || "Imagination");  // sub stays "הסוכנות שלך"
    renderBanners(d);
    renderHero(d);
    if (changed("processing", d.processing)) renderTimelines(d.processing || []);
    renderNeeds(d.needs_you || []);
    renderProblems();
    if (changed("prev", d.previous_clips)) renderPrevClips(d.previous_clips || []);
    if (changed("results", d.results)) renderResults(d.results || {});
    if (changed("money", d.money)) renderMoney(d.money || {});
    if (changed("team", d.team)) renderTeam(d.team || []);
    updateTitle();
  }

  function renderModePill(d) {
    if (!changed("pill", [d.mode, d.live_platforms])) return;
    const pill = $("mode-pill");
    pill.classList.remove("hidden");
    const live = d.mode === "live";
    pill.className = "pill " + (live ? "pill-live" : "pill-training");
    setText($("mode-text"), live ? "שידור חי" : "מצב אימון");
    const names = (d.live_platforms || []).map((p) => p.name_he).join(", ");
    const tip = live ? "עולה באמת ל: " + names : "שום דבר לא עולה באמת — הצוות מתרגל. לחיבורים";
    pill.setAttribute("title", tip);
    pill.setAttribute("aria-label", (live ? "שידור חי — " : "מצב אימון — ") + tip);
  }

  function renderHero(d) {
    if (changed("greeting", d.greeting)) setText($("mgr-greeting"), (d.greeting || "שלום") + "!");
    const s = $("mgr-sentence");
    if (changed("sentence", d.sentence)) {
      setText(s, d.sentence || "");
      if (!REDUCED) { s.classList.remove("swap"); void s.offsetWidth; s.classList.add("swap"); }
    }
    const av = $("mgr-avatar");
    const mood = ["calm", "busy", "attention", "problem"].includes(d.mood) ? d.mood : "calm";
    av.className = "mgr-avatar mood-" + mood;
  }

  // ------------------------------------------------------------------ banners (old studio)
  function renderBanners(d) {
    const st = d.studio || {};
    const keepOld = store(LS_KEEP_OLD) === "1";
    const sec = IS_PC && d.security && d.security.at && store(LS_SECURITY_SEEN) !== d.security.at ? d.security : null;
    if (!changed("banners", [st, keepOld, d.mode, sec])) return;
    const box = clear($("home-banners"));
    if (sec) {  // someone tried to guess the phone PIN (the owner hears about every lockout)
      box.appendChild(el("div", { cls: "banner banner-red", role: "region", "aria-label": "אבטחה" }, [
        icon("shield", "ic-lg"),
        el("div", { cls: "banner-text" }, [el("strong", { text: "ניסיון לנחש את קוד הכניסה מהטלפון" }), el("span", { text: sec.text_he || "" })]),
        el("div", { cls: "banner-actions" }, [
          el("a", { cls: "btn btn-primary", href: "#/settings", text: sec.pin_deleted ? "ליצירת קוד חדש" : "לגישה מהטלפון",
            on: { click: () => { store(LS_SECURITY_SEEN, sec.at); last.banners = null; } } }),
          el("button", { type: "button", cls: "btn btn-ghost", text: "הבנתי", on: { click: () => { store(LS_SECURITY_SEEN, sec.at); last.banners = null; renderBanners(d); } } }),
        ]),
      ]));
    }
    if (st.daemon_active && !keepOld) {
      box.appendChild(el("div", { cls: "banner", role: "region", "aria-label": "הסטודיו הישן" }, [
        icon("alert", "ic-lg"),
        el("div", { cls: "banner-text" }, [el("strong", { text: st.question_he || "הסטודיו הישן עדיין פעיל — הסוכנות החדשה תחליף אותו?" }),
          el("span", { text: "כדי שאף קליפ לא יעלה פעמיים, רק אחד מהם צריך להעלות." })]),
        el("div", { cls: "banner-actions" }, [
          el("button", { type: "button", cls: "btn btn-primary", text: "כן, תחליף", on: { click: () => openReplaceStudio(st) } }),
          el("button", { type: "button", cls: "btn btn-ghost", text: "לא, תשאיר את הישן", on: { click: keepOldStudio } }),
        ]),
      ]));
    } else if (st.daemon_active && keepOld && d.mode === "live") {
      box.appendChild(el("div", { cls: "banner banner-red", role: "region" }, [
        icon("alert", "ic-lg"),
        el("div", { cls: "banner-text" }, [el("strong", { text: "גם הסטודיו הישן וגם הסוכנות מעלים באמת" }),
          el("span", { text: "כדי שלא יעלה פעמיים, החזר את הפלטפורמות לאימון — או כבה את הישן." })]),
        el("div", { cls: "banner-actions" }, [
          el("a", { cls: "btn btn-primary", href: "#/connections", text: "לחיבורים" }),
          el("button", { type: "button", cls: "btn btn-ghost", text: "בעצם, תחליף אותו", on: { click: () => { store(LS_KEEP_OLD, null); openReplaceStudio(st); refreshHome(); } } }),
        ]),
      ]));
    } else if (st.found && !st.imported) {
      box.appendChild(el("div", { cls: "banner banner-steel", role: "region" }, [
        icon("sparkles", "ic-lg"),
        el("div", { cls: "banner-text" }, [el("strong", { text: "מצאתי את הסטודיו הקיים שלך" }),
          el("span", { text: st.keys_missing
            ? "נביא ממנו את הסגנון, השעות והקליפים — ואז חיבור קטן אחד מחזיר את יוטיוב וטיקטוק."
            : "אפשר להביא ממנו את החיבורים, הסגנון והשעות — בלחיצה." })]),
        el("div", { cls: "banner-actions" }, [
          el("a", { cls: "btn btn-steel", href: "#/connections", text: "בוא נייבא" }),
        ]),
      ]));
    } else if (st.found && st.keys_missing) {
      box.appendChild(el("div", { cls: "banner banner-steel", role: "region" }, [
        icon("plug", "ic-lg"),
        el("div", { cls: "banner-text" }, [el("strong", { text: "נשאר צעד אחד" }),
          el("span", { text: "עוד חיבור קטן — ויוטיוב וטיקטוק חוזרים לעבוד." })]),
        el("div", { cls: "banner-actions" }, [
          el("a", { cls: "btn btn-steel", href: "#/connections/youtube", text: "בוא נחבר" }),
        ]),
      ]));
    }
  }
  function keepOldStudio() {
    store(LS_KEEP_OLD, "1");
    last.banners = null;
    if (home && home.mode === "live") toast("בסדר, הסטודיו הישן ממשיך. שים לב: גם הסוכנות מעלה עכשיו באמת — כדי שלא יעלה פעמיים, החזר את הפלטפורמות לאימון בחיבורים.", "warn", { ms: 10000, action: { label: "לחיבורים", fn: () => { location.hash = "#/connections"; } } });
    else toast("בסדר. הסטודיו הישן ממשיך, והסוכנות במצב אימון — שום דבר לא יעלה פעמיים.", "info", { ms: 8000 });
    if (home) renderBanners(home);
  }
  function openReplaceStudio(st) {
    const cmds = (st.disable_commands || []).filter((c) => typeof c === "string" && c.length < 200);
    openDialog({
      title: "מחליפים את הסטודיו הישן", iconNode: el("span", { cls: "spend-icon" }, [icon("refresh")]),
      build: (d) => {
        append(d.body, [
          el("p", { cls: "confirm-text", text: "מעולה. אנחנו אף פעם לא מכבים את הסטודיו הישן בעצמנו — זה בידיים שלך, וזה לוקח דקה." }),
          el("div", { cls: "wiz-step" }, [
            el("h3", { text: "1. פותחים חלון פקודות" }),
            el("p", { text: "לוחצים על מקש Windows, כותבים cmd ולוחצים Enter." }),
            el("h3", { text: "2. מדביקים את השורות האלה" }),
          ].concat(cmds.map(copyRow)).concat([
            el("h3", { text: "3. זהו" }),
            el("p", { text: "כשהסטודיו הישן מפסיק לעבוד, ההודעה תיעלם מכאן תוך כמה דקות. אחר כך מדליקים \"העלאה אמיתית\" בחיבורים." }),
          ])),
        ]);
        setFoot(d, [el("button", { type: "button", cls: "btn btn-big btn-primary", text: "הבנתי", on: { click: () => d.close() } }),
          el("a", { cls: "btn btn-big btn-ghost", href: "#/connections", text: "לחיבורים", on: { click: () => d.close() } })]);
      },
    });
  }

  // ------------------------------------------------------------------ progress timeline
  function renderTimelines(items) {
    const box = clear($("timelines"));
    for (const it of items.slice(0, 3)) {
      const state = it.failed ? ["נתקע", "tag-red"] : it.done ? ["מוכן ✓", "tag-green"] : ["בעבודה", "tag-orange"];
      const steps = el("ol", { cls: "steps" });
      for (const s of it.steps || []) {
        const st = ["done", "active", "todo", "failed"].includes(s.state) ? s.state : "todo";
        const ic = st === "failed" ? "alert" : st === "done" && s.key !== "ready" ? "check" : STEP_ICON[s.key] || "check";
        steps.appendChild(el("li", { cls: "step " + st + (s.key === "ready" ? " ready" : ""),
          "aria-current": st === "active" ? "step" : null }, [
          el("span", { cls: "step-dot", "aria-hidden": "true" }, [icon(ic)]),
          el("span", { cls: "step-label", text: s.label_he }),
          el("span", { cls: "visually-hidden", text: { done: " — בוצע", active: " — עכשיו", todo: "", failed: " — נתקע" }[st] }),
        ]));
      }
      box.appendChild(el("div", { cls: "timeline-card", role: "group", "aria-label": "התקדמות: " + it.video_title }, [
        el("div", { cls: "timeline-title" }, [icon("film", "ic-sm"), el("span", { text: it.video_title || "קליפ" }),
          el("span", { cls: "tag " + state[1], text: state[0] })]),
        steps,
        it.failed ? el("div", { cls: "row" }, [el("p", { cls: "muted small", text: "משהו נתקע בשלב הזה." }),
          it.can_retry ? retryVideoButton(it) : null]) : null,
      ]));
    }
  }
  const retryingVideos = new Set();
  function retryVideoButton(it) {
    const id = String(it.video_id || "");
    const b = el("button", { type: "button", cls: "btn btn-sm btn-steel", disabled: retryingVideos.has(id) },
      [icon("refresh", "ic-sm"), retryingVideos.has(id) ? "מנסה…" : "נסה שוב"]);
    b.addEventListener("click", async () => {
      busy(b, true, "מנסה…");
      try {
        await api("/api/videos/" + encodeURIComponent(id) + "/retry", { method: "POST" });
        retryingVideos.add(id);
        toast("הצוות מנסה שוב להכין את \"" + (it.video_title || "הקליפ") + "\".", "info");
        setTimeout(() => { retryingVideos.delete(id); refreshHome(); }, 1500);
      } catch (e) { toast(e.message, "err"); busy(b, false); }
    });
    return b;
  }

  // ------------------------------------------------------------------ "מחכה לך"
  const cards = new Map();        // approval id -> {sig, node}
  const openDetails = new Set();  // approval ids whose "פרטים" is open
  let seenApprovals = null;

  function renderNeeds(list) {
    const sec = $("needs");
    sec.classList.toggle("hidden", !list.length);
    setText($("needs-count"), list.length ? String(list.length) : "");
    const box = $("needs-list");
    const ids = new Set(list.map((c) => String(c.id)));
    for (const [id, st] of cards) if (!ids.has(id)) { leave(st.node); cards.delete(id); }
    let prev = null;
    for (const c of list) {
      const id = String(c.id);
      const sig = JSON.stringify(c);
      let st = cards.get(id);
      if (!st) {
        st = { sig, node: buildCard(c) };
        if (!REDUCED && seenApprovals) st.node.classList.add("enter");
        cards.set(id, st);
      } else if (st.sig !== sig && !cardBusy(st.node)) {
        const node = buildCard(c);
        st.node.replaceWith(node);
        st.node = node;
        st.sig = sig;
      }
      const want = prev ? prev.nextSibling : box.firstChild;
      if (st.node !== want) box.insertBefore(st.node, want);
      prev = st.node;
    }
    renderNotifyAsk(list.length > 0);
  }
  function cardBusy(node) {
    const a = document.activeElement;
    return node.dataset.dirty === "1" || node.dataset.busy === "1" || (a && a !== document.body && node.contains(a));
  }
  function leave(node) {
    if (!node.isConnected) return;
    if (REDUCED) { node.remove(); return; }
    node.style.maxHeight = node.offsetHeight + "px";
    node.classList.add("leaving");
    requestAnimationFrame(() => { node.style.maxHeight = "0px"; node.style.marginTop = "-16px"; });
    setTimeout(() => node.remove(), 460);
  }

  function buildCard(c) {
    if (c.kind === "publish") return publishCard(c);
    if (c.kind === "spend") return spendCard(c);
    return otherCard(c);
  }

  function thumbNode(c) {
    const box = el("div", { cls: "ap-thumb" }, [icon("film")]);
    if (typeof c.thumbnail_url === "string" && THUMB_RE.test(c.thumbnail_url)) {
      const img = el("img", { src: c.thumbnail_url, alt: "", decoding: "async" });
      img.addEventListener("error", () => img.remove());
      box.appendChild(img);
    }
    return box;
  }

  function publishCard(c) {
    const tid = "ap-t-" + c.id;
    const card = el("article", { cls: "card ap-card", "data-id": c.id, "aria-labelledby": tid });
    const thumb = thumbNode(c);
    thumb.appendChild(el("span", { cls: "ap-when" }, [icon("clock"), c.when_he || "מיד כשתאשר"]));
    const showQuote = c.main_title && c.main_title !== c.title;
    const body = el("div", { cls: "ap-body" }, [
      el("span", { cls: "ap-kicker", text: "קליפ מוכן" }),
      el("h3", { cls: "ap-title", id: tid, text: c.title || "קליפ" }),
      showQuote ? el("p", { cls: "ap-quote", dir: "auto", text: c.main_title }) : null,
      el("div", { cls: "ap-line" }, [el("span", { text: "יעלה ל:" }),
        el("span", { cls: "chips" }, (c.platforms || []).map(pchip))]),
    ]);
    const go = el("button", { type: "button", cls: "btn btn-go btn-big" }, [icon(c.retry ? "refresh" : "check"), c.retry ? "המשך" : "יאללה, תעלה"]);
    const later = el("button", { type: "button", cls: "btn btn-ghost btn-big", text: "לא עכשיו", hidden: c.retry ? true : null });
    const change = el("button", { type: "button", cls: "btn btn-steel btn-big" }, [icon("chat", "ic-sm"), "שנה משהו"]);
    if (c.retry) body.appendChild(retryNote(c));
    go.addEventListener("click", () => decide(c, card, true, go));
    later.addEventListener("click", () => decide(c, card, false, later));
    change.addEventListener("click", () => openChat(c.chat_prefill || ("לגבי " + (c.title || "הקליפ") + ": ")));
    const regionId = "ap-d-" + c.id;
    const toggle = el("button", { type: "button", cls: "details-toggle", "aria-expanded": "false", "aria-controls": regionId },
      ["פרטים לכל פלטפורמה", icon("chevron-down", "ic-sm")]);
    const region = el("div", { cls: "details", id: regionId, hidden: true });
    toggle.addEventListener("click", () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(open));
      region.hidden = !open;
      if (open) { openDetails.add(String(c.id)); if (!region.firstChild) buildDetails(c, card, region); }
      else openDetails.delete(String(c.id));
    });
    if (openDetails.has(String(c.id))) {
      toggle.setAttribute("aria-expanded", "true");
      region.hidden = false;
      buildDetails(c, card, region);
    }
    append(card, [
      el("div", { cls: "ap-main" }, [thumb, body]),
      el("div", { cls: "ap-actions" }, [go, later, change]),
      el("div", { cls: "ap-foot" }, [toggle, region]),
    ]);
    return card;
  }

  function buildDetails(c, card, region) {
    const dirty = new Set();
    for (const p of c.plans || []) region.appendChild(planBlock(c, p, card, dirty));
  }

  function planBlock(c, p, card, dirtySet) {
    const key = p.platform || p.key;
    const head = el("div", { cls: "plan-head" }, [pico(key), el("span", { cls: "pname", text: p.name_he || PLATFORMS[key] || key }),
      p.edited ? el("span", { cls: "tag tag-steel", text: "ערכת" }) : null,
      el("span", { cls: "when" }, [icon("clock", "ic-sm"), p.when_he || "מיד כשתאשר"])]);
    const block = el("div", { cls: "plan" }, [head]);
    if (p.editable) {
      const tId = "pt-" + c.id + "-" + key, dId = "pd-" + c.id + "-" + key;
      const title = el("input", { cls: "input", id: tId, value: p.title || "", maxlength: "1000", dir: "auto" });
      const desc = el("textarea", { cls: "textarea", id: dId, rows: "5", maxlength: "20000", dir: "auto" });
      desc.value = p.description || "";
      const msg = el("span", { cls: "form-msg", role: "status" });
      const save = el("button", { type: "button", cls: "btn btn-sm btn-primary", text: "שמור שינוי" });
      const undo = el("button", { type: "button", cls: "btn btn-sm btn-ghost", text: "בטל" });
      const row = el("div", { cls: "plan-save", hidden: true }, [save, undo, msg]);
      let base = { title: p.title || "", description: p.description || "" };
      const sync = () => {
        const isDirty = title.value !== base.title || desc.value !== base.description;
        row.hidden = !isDirty && !msg.textContent;
        if (isDirty) dirtySet.add(key); else dirtySet.delete(key);
        card.dataset.dirty = dirtySet.size ? "1" : "";
      };
      title.addEventListener("input", () => { msg.textContent = ""; sync(); });
      desc.addEventListener("input", () => { msg.textContent = ""; sync(); });
      undo.addEventListener("click", () => { title.value = base.title; desc.value = base.description; msg.textContent = ""; sync(); });
      save.addEventListener("click", async () => {
        const patch = {};
        if (title.value !== base.title) patch.title = title.value;
        if (desc.value !== base.description) patch.description = desc.value;
        if (!Object.keys(patch).length) return;
        if (patch.title !== undefined && !patch.title.trim()) { msg.className = "form-msg err"; msg.textContent = "הכותרת לא יכולה להיות ריקה"; return; }
        busy(save, true, "שומר…");
        try {
          const res = await api("/api/approvals/" + encodeURIComponent(c.id) + "/plans/" + encodeURIComponent(key),
            { method: "PATCH", json: patch });
          const plan = (res && res.plan) || {};
          base = { title: plan.title || title.value, description: plan.description || desc.value };
          title.value = base.title;
          desc.value = base.description;
          msg.className = "form-msg ok";
          msg.textContent = "נשמר ✓";
          if (!head.querySelector(".tag")) head.insertBefore(el("span", { cls: "tag tag-steel", text: "ערכת" }), head.querySelector(".when"));
          setTimeout(() => { msg.textContent = ""; sync(); }, 2200);
        } catch (e) {
          msg.className = "form-msg err";
          msg.textContent = e.message;
        } finally {
          busy(save, false);
          sync();
        }
      });
      append(block, [
        el("label", { cls: "field", for: tId }, [el("span", { text: "כותרת" })]), title,
        el("label", { cls: "field", for: dId }, [el("span", { text: "תיאור" })]), desc,
        row,
      ]);
    } else {
      append(block, [el("b", { dir: "auto", text: p.title || "" }),
        p.description ? el("div", { cls: "plan-text", text: p.description }) : null]);
    }
    const tags = (p.hashtags || []).slice(0, 30);
    if (tags.length) block.appendChild(el("div", { cls: "hashtags" }, tags.map((h) => el("span", { text: h }))));
    if (p.why) block.appendChild(el("div", { cls: "why" }, [el("b", { text: "למה ככה: " }), p.why]));
    return block;
  }

  function spendCard(c) {
    const tid = "ap-t-" + c.id;
    const card = el("article", { cls: "card ap-card", "data-id": c.id, "aria-labelledby": tid });
    const blocked = c.cfo_ok === false && !!c.cfo_reason;
    const cfo = c.cfo_reason ? el("div", { cls: "cfo " + (c.cfo_ok ? "ok" : "warn") }, [icon(c.cfo_ok ? "shield" : "alert"),
      el("span", { text: (c.cfo_ok ? "מנהל הכספים: " : "מנהל הכספים: ") + c.cfo_reason })]) : null;
    const body = el("div", { cls: "ap-body" }, [
      el("div", { cls: "spend-head" }, [el("span", { cls: "spend-icon" }, [icon("coin", "ic-lg")]),
        el("div", {}, [el("span", { cls: "ap-kicker", text: "בקשה להוצאה" }), el("h3", { cls: "ap-title", id: tid, text: c.title || "הוצאה" })])]),
      el("div", { cls: "spend-amount", text: c.cost_he || fmtIls(c.cost_ils) }),
      c.video_title ? el("div", { cls: "ap-line" }, [icon("film", "ic-sm"), c.video_title]) : null,
      c.days ? el("div", { cls: "ap-line" }, [icon("clock", "ic-sm"), "למשך " + c.days + " ימים"]) : null,
      cfo,
      c.why ? el("div", { cls: "why" }, [el("b", { text: "למה: " }), c.why]) : null,
    ]);
    const go = el("button", { type: "button", cls: "btn btn-go btn-big", disabled: blocked }, [icon(c.retry ? "refresh" : "check"), blocked ? "מעל התקרה" : c.retry ? "נסה שוב" : "אשר הוצאה"]);
    const later = el("button", { type: "button", cls: "btn btn-ghost btn-big", text: "לא עכשיו", hidden: c.retry ? true : null });
    if (c.retry) body.appendChild(retryNote(c));
    const ask = el("button", { type: "button", cls: "btn btn-steel btn-big" }, [icon("chat", "ic-sm"), "שאל את המנכ\"ל"]);
    go.addEventListener("click", () => decide(c, card, true, go));
    later.addEventListener("click", () => decide(c, card, false, later));
    ask.addEventListener("click", () => openChat("לגבי ההוצאה \"" + (c.title || "") + "\": "));
    append(card, [body, el("div", { cls: "ap-actions" }, [go, later, ask])]);
    return card;
  }

  function otherCard(c) {
    const tid = "ap-t-" + c.id;
    const card = el("article", { cls: "card ap-card", "data-id": c.id, "aria-labelledby": tid });
    const isReply = c.kind === "reply";
    const body = el("div", { cls: "ap-body" }, [
      el("span", { cls: "ap-kicker", text: isReply ? "תגובה לצופה" : "בקשה" }),
      el("h3", { cls: "ap-title", id: tid, text: c.title || "בקשה" }),
      isReply && c.comment ? el("p", { cls: "ap-quote", dir: "auto", text: c.comment }) : null,
      isReply && c.reply ? el("div", { cls: "why" }, [el("b", { text: "התשובה שהכנו: " }), c.reply]) : null,
      !isReply && c.text ? el("p", { cls: "plan-text", text: c.text }) : null,
    ]);
    const go = el("button", { type: "button", cls: "btn btn-go btn-big" }, [icon(c.retry ? "refresh" : "check"), c.retry ? "נסה שוב" : isReply ? "שלח תשובה" : "אשר"]);
    const later = el("button", { type: "button", cls: "btn btn-ghost btn-big", text: "לא עכשיו", hidden: c.retry ? true : null });
    if (c.retry) body.appendChild(retryNote(c));
    const change = el("button", { type: "button", cls: "btn btn-steel btn-big" }, [icon("chat", "ic-sm"), "שנה משהו"]);
    go.addEventListener("click", () => decide(c, card, true, go));
    later.addEventListener("click", () => decide(c, card, false, later));
    change.addEventListener("click", () => openChat("לגבי \"" + (c.title || "") + "\": "));
    append(card, [body, el("div", { cls: "ap-actions" }, [go, later, change])]);
    return card;
  }

  function retryNote(c) {
    return el("div", { cls: "cfo warn" }, [icon("alert"), el("span", { text: c.retry_he || "אישרת, אבל זה לא בוצע. לנסות שוב?" })]);
  }

  async function decide(c, card, approve, btn) {
    if (approve && c.kind === "spend") {
      const ok = await confirmDialog({ title: "לאשר הוצאה?", text: "לאשר " + (c.cost_he || fmtIls(c.cost_ils)) + " עבור \"" + (c.title || "") + "\"?",
        yes: "כן, לאשר", iconNode: el("span", { cls: "spend-icon" }, [icon("coin")]) });
      if (!ok) return;
    }
    if (!approve) {
      const ok = await confirmDialog({
        title: "לא עכשיו?",
        text: c.kind === "publish" ? "\"" + (c.title || "הקליפ") + "\" לא יעלה עכשיו. תמיד אפשר לזרוק אותו שוב." : "הבקשה תבוטל.",
        yes: "כן, לא עכשיו", no: "רגע, לא" });
      if (!ok) return;
    }
    card.dataset.busy = "1";
    card.querySelectorAll(".ap-actions .btn").forEach((b) => { b.disabled = true; });
    busy(btn, true, approve ? (c.kind === "spend" ? "מאשר…" : "יוצא לדרך…") : "מבטל…");
    try {
      const res = await api("/api/approvals/" + encodeURIComponent(c.id) + "/" + (approve ? "approve" : "reject"), { json: { note: "" } });
      const failed = approve && res && res.ok === false && !res.executing && c.kind !== "spend";
      if (approve && !failed && !REDUCED) card.classList.add("approved-flash");
      cards.delete(String(c.id));
      setTimeout(() => leave(card), approve && !failed && !REDUCED ? 380 : 0);
      if (!approve) toast("בסדר, לא עכשיו.", "info");
      else if (failed) {
        // approved, but it did not go through (e.g. the reply was not sent): say so, offer again
        const what = c.kind === "reply" ? "התשובה לא נשלחה" : c.kind === "publish" ? "ההעלאה לא הצליחה" : "זה לא בוצע";
        toast(what + ". אפשר לנסות שוב.", "err", { ms: 10000, action: { label: "נסה שוב", fn: () => retryApproval(c) } });
      }
      else if (c.kind === "spend") afterSpend(res);
      else if (c.kind === "publish") {
        if (home && home.mode === "training") toast("אושר! אנחנו במצב אימון — הצוות מתרגל ושום דבר לא עולה באמת.", "ok", { ms: 7000 });
        else toast("יצא לדרך! אגיד לך כשזה עולה.", "ok");
      } else toast(c.kind === "reply" ? "התשובה נשלחה ✓" : "אושר ✓", "ok");
      setTimeout(refreshHome, 400);
    } catch (e) {
      card.dataset.busy = "";
      busy(btn, false);
      card.querySelectorAll(".ap-actions .btn").forEach((b) => { b.disabled = false; });
      toast(e.message, "err");
    }
  }

  async function retryApproval(c) {
    try {
      const res = await api("/api/approvals/" + encodeURIComponent(c.id) + "/approve", { json: { note: "" } });
      if (res && res.ok === false && !res.executing) toast("גם הפעם זה לא הצליח. הכרטיס מחכה לך ב\"מחכה לך\".", "err", { ms: 9000 });
      else toast(c.kind === "reply" ? "התשובה נשלחה ✓" : "בוצע ✓", "ok");
    } catch (e) { toast(e.message, "err"); }
    setTimeout(refreshHome, 400);
  }

  function afterSpend(res) {
    const r = (res && res.result) || {};
    if (r.ok === false) { toast("ההוצאה אושרה אבל נחסמה: " + friendlyError(400, { detail: r.reason || r.error || "" }), "warn", { ms: 9000 }); return; }
    const steps = Array.isArray(r.instructions) ? r.instructions.filter((s) => typeof s === "string").slice(0, 12) : [];
    if (!steps.length) { toast("ההוצאה אושרה ונרשמה.", "ok"); return; }
    openDialog({
      title: "ההוצאה אושרה — מה עושים עכשיו", iconNode: el("span", { cls: "spend-icon" }, [icon("coin")]),
      build: (d) => {
        append(d.body, [el("p", { cls: "confirm-text", text: "אנחנו לא מפעילים פרסום ממומן בעצמנו. ככה עושים את זה:" }),
          el("ol", { cls: "stack" }, steps.map((s) => el("li", { text: s })))]);
        setFoot(d, [el("button", { type: "button", cls: "btn btn-big btn-primary", text: "הבנתי", on: { click: () => d.close() } })]);
      },
    });
  }

  // new approval -> toast (+ a browser notification when the tab is hidden)
  function detectApprovals(list) {
    const ids = new Set(list.map((c) => String(c.id)));
    if (seenApprovals) {
      for (const c of list) {
        if (seenApprovals.has(String(c.id))) continue;
        const what = c.kind === "spend" ? "בקשה להוצאה" : c.kind === "publish" ? "\"" + (c.title || "קליפ") + "\" מוכן" : "בקשה חדשה";
        toast(what + " — מחכה לך", "info", { icon: "bell", action: { label: "הצג", fn: () => focusCard(c.id) } });
        notify(what + " — מחכה לאישור שלך", "approval-" + c.id);
      }
    }
    seenApprovals = ids;
  }
  function focusCard(id) {
    if (route.page !== "home") location.hash = "#/";
    setTimeout(() => {
      const st = cards.get(String(id));
      if (!st) return;
      st.node.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "center" });
      const b = st.node.querySelector(".btn-go");
      if (b) b.focus({ preventScroll: true });
    }, 120);
  }
  const canNotify = () => "Notification" in window && window.isSecureContext;
  function notify(text, tag) {
    try {
      if (!canNotify() || Notification.permission !== "granted" || !document.hidden) return;
      const n = new Notification("Imagination", { body: text, tag, lang: "he", dir: "rtl" });
      n.onclick = () => { window.focus(); n.close(); };
    } catch (_) { /* notifications are a bonus */ }
  }
  function renderNotifyAsk(show) {
    const box = $("notify-ask");
    const want = show && canNotify() && Notification.permission === "default" && store(LS_NOTIFY_ASKED) !== "1";
    if (!changed("notifyAsk", want)) return;
    clear(box);
    if (!want) return;
    const yes = el("button", { type: "button", cls: "btn btn-sm btn-steel", text: "כן, תודיע לי" });
    const no = el("button", { type: "button", cls: "btn btn-sm btn-ghost", text: "לא תודה" });
    const done = () => { store(LS_NOTIFY_ASKED, "1"); last.notifyAsk = null; renderNotifyAsk(false); clear(box); };
    yes.addEventListener("click", async () => {
      try { const p = await Notification.requestPermission(); if (p === "granted") toast("מעולה — אגיד לך כשמשהו מחכה.", "ok"); }
      catch (_) { /* ignore */ }
      done();
    });
    no.addEventListener("click", done);
    box.appendChild(el("div", { cls: "notify-ask" }, [icon("bell"), el("span", { text: "רוצה שאגיד לך כשמשהו מחכה, גם כשהחלון ברקע?" }), yes, no]));
  }

  // ------------------------------------------------------------------ failed posts ("משהו נתקע")
  let posts = null;          // last /api/posts
  const retryWatch = new Map();  // post id -> its updated_at when the owner pressed "נסה שוב"
  let postsBusy = false;
  async function refreshPosts() {
    if (postsBusy) return;
    postsBusy = true;
    try {
      const list = await api("/api/posts");
      detectPostEvents(list);
      posts = list;
      renderProblems();
    } catch (_) { /* the next poll tries again */ }
    finally { postsBusy = false; }
  }
  function detectPostEvents(list) {
    if (!posts) return;
    const before = new Map(posts.map((p) => [p.id, p.status]));
    let training = 0;
    for (const p of list) {
      const was = before.get(p.id);
      const name = PLATFORMS[p.platform] || p.platform;
      if (retryWatch.has(p.id) && p.status !== "publishing" && p.updated_at !== retryWatch.get(p.id)) {
        // a retry the owner asked for: always say how it ended, even failed -> failed
        retryWatch.delete(p.id);
        if (p.status === "failed" && was === "failed") {
          toast("גם הפעם ההעלאה ל" + name + " לא הצליחה.", "err", { ms: 9000, action: { label: "שאל את המנכ\"ל", fn: () => openChat("למה ההעלאה ל" + name + " נכשלה? ") } });
          continue;
        }
        if (p.status === "scheduled") { toast(name + ": מתוזמן — יעלה בזמן שנקבע.", "info"); continue; }
      }
      if (was === undefined || was === p.status) continue;
      if (p.status === "published") {
        if (p.dry_run === false) {
          toast("עלה ל" + name + "!", "ok", { icon: "rocket", link: p.url ? { url: p.url, label: "לצפייה" } : null });
          notify("עלה ל" + name + "!", "post-" + p.id);
        } else training += 1;
      } else if (p.status === "failed") {
        toast("ההעלאה ל" + name + " נכשלה", "err", { action: { label: "נסה שוב", fn: () => retryPost(p) } });
      }
    }
    if (training) toast(training === 1 ? "באימון: העלאה אחת עברה בהצלחה (לא עלה באמת)" : "באימון: " + training + " העלאות עברו בהצלחה (לא עלה באמת)", "info");
  }
  function renderProblems() {
    const sec = $("problems");
    const problems = home && home.counts ? home.counts.problems : 0;
    const failed = problems && posts ? posts.filter((p) => p.status === "failed").sort((a, b) => b.id - a.id).slice(0, 4) : [];
    if (!changed("problems", failed.map((p) => [p.id, p.platform, p.title]))) return;
    sec.classList.toggle("hidden", !failed.length);
    const box = clear($("problems-list"));
    for (const p of failed) {
      const b = el("button", { type: "button", cls: "btn btn-sm btn-steel" }, [icon("refresh", "ic-sm"), "נסה שוב"]);
      b.addEventListener("click", () => retryPost(p, b));
      box.appendChild(el("div", { cls: "problem-row" }, [pico(p.platform),
        el("div", { cls: "txt" }, [el("b", { text: "ההעלאה ל" + (PLATFORMS[p.platform] || p.platform) + " לא הצליחה" }),
          el("span", { cls: "muted small", dir: "auto", text: p.title || "" })]), b]));
    }
  }
  async function retryPost(p, btn) {
    const name = PLATFORMS[p.platform] || p.platform;
    const ok = await confirmDialog({ title: "לנסות שוב?", text: "לנסות להעלות שוב ל" + name + "?", yes: "כן, נסה שוב", iconNode: pico(p.platform) });
    if (!ok) return;
    busy(btn, true, "מנסה…");
    try {
      const cur = (posts || []).find((x) => x.id === p.id) || p;
      retryWatch.set(p.id, cur.updated_at || "");
      const res = await api("/api/posts/" + encodeURIComponent(p.id) + "/retry", { method: "POST" });
      if (!res.executing) retryWatch.delete(p.id);
      if (res.executing) toast("מנסה שוב… אגיד לך איך הלך.", "info");
      else if (res.ok) toast("הצליח הפעם ✓", "ok");
      else toast("גם הפעם זה לא הצליח. אפשר לשאול את המנכ\"ל מה קרה.", "err", { action: { label: "שאל", fn: () => openChat("למה ההעלאה ל" + name + " נכשלה? ") } });
      setTimeout(refreshPosts, 800);
    } catch (e) { toast(e.message, "err"); }
    finally { busy(btn, false); }
  }

  // ------------------------------------------------------------------ previous clips
  const preparing = new Set();
  let prevExpanded = false;
  function renderPrevClips(list) {
    const sec = $("prev-clips");
    sec.classList.toggle("hidden", !list.length);
    const box = clear($("prev-list"));
    const shown = prevExpanded ? list : list.slice(0, 4);
    for (const c of shown) {
      const id = String(c.id || "");
      const b = el("button", { type: "button", cls: "btn btn-sm btn-steel", disabled: preparing.has(id) },
        [preparing.has(id) ? "בהכנה…" : "תכין לפרסום"]);
      b.addEventListener("click", () => prepareClip(c, b));
      box.appendChild(el("div", { cls: "clip-row" }, [el("span", { cls: "clip-ic" }, [icon("film")]),
        el("div", { cls: "txt" }, [el("b", { text: c.name || "קליפ" }), el("span", { cls: "muted tiny", text: c.size_mb ? c.size_mb + "MB" : "" })]), b]));
    }
    if (list.length > shown.length) {
      box.appendChild(el("button", { type: "button", cls: "btn-link", text: "ועוד " + (list.length - shown.length) + " קליפים",
        on: { click: () => { prevExpanded = true; renderPrevClips(list); } } }));
    }
  }
  async function prepareClip(c, btn) {
    const id = String(c.id || "");
    busy(btn, true, "שולח…");
    try {
      await api("/api/inbox/prepare", { json: { clip_id: id } });
      preparing.add(id);
      toast("הצוות מכין את \"" + (c.name || "הקליפ") + "\". כשזה מוכן — זה יופיע ב\"מחכה לך\".", "ok", { ms: 7000 });
      setTimeout(refreshHome, 600);
    } catch (e) { toast(e.message, "err"); }
    finally {
      busy(btn, false);
      if (preparing.has(id)) { setText(btn, "בהכנה…"); btn.disabled = true; }
    }
  }

  // ------------------------------------------------------------------ results ("איך הולך")
  function sparkline(series) {
    const vals = (series || []).map((v) => Number(v) || 0);
    const n = vals.length;
    const max = Math.max(0, ...vals);
    const s = svgNode("svg", { viewBox: "0 0 100 34", preserveAspectRatio: "none", class: "spark" + (max ? "" : " flat"),
      role: "img", "aria-label": "צפיות ב-7 הימים האחרונים" });
    if (n < 2) return s;
    const defs = svgNode("defs");
    const g = svgNode("linearGradient", { id: "spark-fill", x1: 0, y1: 0, x2: 0, y2: 1 });
    g.appendChild(svgNode("stop", { offset: 0, "stop-color": "#f5a623", "stop-opacity": .35 }));
    g.appendChild(svgNode("stop", { offset: 1, "stop-color": "#f5a623", "stop-opacity": 0 }));
    defs.appendChild(g);
    s.appendChild(defs);
    const pts = vals.map((v, i) => [(i / (n - 1)) * 100, max ? 31 - (v / max) * 27 : 22]);
    const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(2) + " " + p[1].toFixed(2)).join(" ");
    if (max) s.appendChild(svgNode("path", { class: "area", d: line + " L100 34 L0 34 Z" }));
    s.appendChild(svgNode("path", { class: "line", d: line, "vector-effect": "non-scaling-stroke" }));
    return s;
  }
  function renderResults(r) {
    const box = clear($("results"));
    if (!r.has_data) {
      box.appendChild(el("div", { cls: "empty" }, [el("span", { cls: "empty-ic" }, [icon("chart")]),
        el("strong", { text: "עוד אין מספרים" }), el("span", { text: "ברגע שיעלה הקליפ הראשון תראה פה מספרים." })]));
      return;
    }
    if (r.stats_pending) {  // clips are up (e.g. from the old studio) but nothing was counted yet
      const n = Number(r.all_time && r.all_time.videos) || 0;
      box.appendChild(el("div", { cls: "empty" }, [el("span", { cls: "empty-ic" }, [icon("chart")]),
        el("strong", { text: r.stats_reason === "no_route" ? "המספרים עוד לא מגיעים לכאן" : "המספרים בדרך" }),
        el("span", { text: (n === 1 ? "קליפ אחד כבר עלה" : n > 1 ? n + " קליפים כבר עלו" : "הקליפים כבר עלו")
          + (r.stats_reason === "no_route"
            ? ". כדי לראות כאן את המספרים של יוטיוב צריך את החיבור הישיר לגוגל (בחיבורים ← יוטיוב ← \"מתקדם\"). את המספרים של טיקטוק רואים באפליקציה."
            : ". הצוות יביא את המספרים שלהם בבדיקה הבאה.") }),
        r.stats_reason === "no_route" ? el("a", { cls: "btn btn-sm btn-steel", href: "#/connections/youtube", text: "לחיבור לגוגל" }) : null]));
      return;
    }
    const stat = (cls, ic, num, label, extra) => el("div", { cls: "stat " + cls }, [icon(ic),
      el("span", { cls: "stat-num", text: fmtBig(num), title: fmtNum(num) }), el("span", { cls: "stat-label", text: label }), extra || null]);
    box.appendChild(el("div", { cls: "stats3" }, [
      stat("stat-views", "eye", r.views, "צפיות", sparkline(r.series)),
      stat("", "heart", r.likes, "לייקים"),
      stat("", "comment", r.comments, "תגובות"),
    ]));
    if (!r.views && r.all_time && r.all_time.views) {
      box.appendChild(el("p", { cls: "muted small set-sub", text: "השבוע עוד שקט. מאז ההתחלה: " + fmtNum(r.all_time.views) + " צפיות." }));
    }
    const best = r.best_video;
    if (best && best.title) {
      const href = best.url ? safeUrl(best.url) : null;
      box.appendChild(el("div", { cls: "best" }, [el("span", { cls: "best-ic" }, [icon("trophy")]),
        el("div", { cls: "txt" }, [el("small", { text: "הקליפ הכי חזק השבוע" }), el("b", { dir: "auto", text: best.title }),
          el("span", { cls: "muted small", text: fmtNum(best.views) + " צפיות" })]),
        href ? el("a", { cls: "btn btn-sm btn-ghost", href, target: "_blank", rel: "noopener noreferrer", "aria-label": "לצפייה בקליפ" }, [icon("external", "ic-sm"), el("span", { cls: "lbl", text: "לצפייה" })]) : null]));
    }
    const per = (r.per_platform || []).filter((p) => p.views > 0).slice(0, 6);
    if (per.length) {
      const max = Math.max(...per.map((p) => p.views));
      const bars = el("div", { cls: "bars", role: "list", "aria-label": "צפיות לפי פלטפורמה" });
      for (const p of per) {
        const fill = el("i");
        fill.style.width = Math.max(4, (p.views / max) * 100).toFixed(1) + "%";
        bars.appendChild(el("div", { cls: "bar-row", role: "listitem" }, [pico(p.key), el("span", { text: p.name_he || PLATFORMS[p.key] || p.key }),
          el("span", { cls: "bar", "aria-hidden": "true" }, [fill]), el("span", { cls: "bar-val", text: fmtBig(p.views) })]));
      }
      box.appendChild(bars);
    }
  }

  // ------------------------------------------------------------------ money line + sheet
  function renderMoney(m) {
    // Keep "הוצאות 0 ₪" / "הכנסות 0 ₪" whole: a narrow phone wraps at the "·" only.
    setText($("money-sentence"), String(m.sentence || "").replace(/ ₪/g, "\u00a0₪").replace(/(הוצאות|הכנסות) /g, "$1\u00a0"));
    const sub = $("money-sub");
    const bar = $("money-bar");
    if (m.budget > 0) {
      sub.hidden = false;
      setText(sub, "נשארו " + fmtIls(m.remaining) + " מתוך " + fmtIls(m.budget));
      bar.hidden = false;
      bar.classList.toggle("high", m.pct >= 85);
      bar.firstChild.style.width = Math.min(100, Math.max(0, m.pct || 0)) + "%";
    } else {
      sub.hidden = false;
      setText(sub, "שקל לא יוצא בלי אישור שלך");
      bar.hidden = true;
    }
    $("money-line").setAttribute("aria-label", (m.sentence || "") + ". לפרטים");
  }
  function openFinance() {
    openDialog({
      title: "הכסף החודש", iconNode: el("span", { cls: "money-ic" }, [icon("wallet")]),
      build: (d) => {
        const nums = el("div", { cls: "fin-nums" });
        const list = el("ul", { cls: "ledger" }, [el("li", {}, [el("span", { cls: "skel skel-line w80" })])]);
        let kind = "income";
        const amount = el("input", { cls: "input ltr", type: "number", inputmode: "decimal", min: "0", step: "0.01", placeholder: "סכום ₪", "aria-label": "סכום בשקלים" });
        const what = el("input", { cls: "input", maxlength: "100", placeholder: "ממה? (למשל: אדסנס)", "aria-label": "מקור או קטגוריה" });
        const msg = el("p", { cls: "form-msg full", role: "status" });
        const incomeB = el("button", { type: "button", "aria-pressed": "true", text: "הכנסה" });
        const expenseB = el("button", { type: "button", "aria-pressed": "false", text: "הוצאה" });
        const setKind = (k) => { kind = k; incomeB.setAttribute("aria-pressed", String(k === "income")); expenseB.setAttribute("aria-pressed", String(k === "expense"));
          what.placeholder = k === "income" ? "ממה? (למשל: אדסנס, חסות)" : "על מה? (למשל: מוזיקה)"; };
        incomeB.addEventListener("click", () => setKind("income"));
        expenseB.addEventListener("click", () => setKind("expense"));
        const add = el("button", { type: "submit", cls: "btn btn-primary full", text: "רשום" });
        const form = el("form", { cls: "money-form", novalidate: true }, [el("div", { cls: "seg full", role: "group", "aria-label": "סוג" }, [incomeB, expenseB]), amount, what, add, msg]);
        async function load() {
          try {
            const fin = await api("/api/finance");
            const b = fin.budget || {};
            clear(nums);
            for (const [label, v] of [["הוצאות", b.spent], ["הכנסות", b.income], ["תקציב לחודש", b.budget], ["נשאר", b.remaining]]) {
              nums.appendChild(el("div", {}, [el("small", { text: label }), el("b", { text: fmtIls(v) })]));
            }
            clear(list);
            const entries = fin.entries || [];
            if (!entries.length) list.appendChild(el("li", { cls: "muted", text: "עוד אין תנועות החודש." }));
            for (const e of entries.slice(0, 30)) {
              list.appendChild(el("li", {}, [el("span", { dir: "auto", text: (e.category || "כללי") + (e.description ? " · " + e.description : "") }),
                el("span", { cls: e.kind === "income" ? "pos" : "neg", text: (e.kind === "income" ? "+" : "−") + fmtIls(e.amount_ils) })]));
            }
          } catch (e) { clear(list); list.appendChild(el("li", { cls: "muted", text: e.message })); }
        }
        form.addEventListener("submit", async (ev) => {
          ev.preventDefault();
          const v = parseFloat(amount.value);
          if (!(v > 0)) { msg.className = "form-msg full err"; msg.textContent = "צריך סכום גדול מאפס"; amount.focus(); return; }
          const body = { amount: v, description: "" };
          if (kind === "income") body.source = what.value.trim(); else body.category = what.value.trim();
          busy(add, true, "רושם…");
          try {
            await api("/api/finance/" + kind, { json: body });
            msg.className = "form-msg full ok"; msg.textContent = kind === "income" ? "ההכנסה נרשמה ✓" : "ההוצאה נרשמה ✓";
            amount.value = ""; what.value = "";
            load(); refreshHome();
          } catch (e) { msg.className = "form-msg full err"; msg.textContent = e.message; }
          finally { busy(add, false); }
        });
        append(d.body, [nums,
          el("div", { cls: "note" }, [icon("shield"), el("span", { text: "כסף לא זז בלי אישור שלך. כל הוצאה מעל התקרה נחסמת — גם אם אישרת." })]),
          el("h3", { text: "תנועות החודש" }), list,
          el("h3", { text: "לרשום הכנסה או הוצאה" }), form]);
        load();
      },
    });
  }

  // ------------------------------------------------------------------ team
  let teamData = [];
  function renderTeam(team) {
    teamData = team;
    const box = clear($("team"));
    for (const m of team) {
      const st = ["idle", "working", "problem"].includes(m.status) ? m.status : "idle";
      const b = el("button", { type: "button", cls: "mate " + st + (m.name === "manager" ? " boss" : ""),
        "aria-label": m.title_he + ": " + TEAM_STATUS_HE[st], title: m.title_he, "aria-haspopup": "dialog" },
      [icon(TEAM_ICON[m.name] || "users"), el("span", { cls: "sdot", "aria-hidden": "true" })]);
      b.addEventListener("click", () => openMate(m));
      box.appendChild(b);
    }
    const working = team.filter((m) => m.status === "working");
    const problem = team.find((m) => m.status === "problem");
    let note = "כולם מוכנים לקליפ הבא.";
    if (problem) note = problem.title_he + ": " + problem.sentence;
    else if (working.length) note = working.map((m) => m.title_he + " " + m.sentence).slice(0, 2).join(" · ");
    setText($("team-note"), note);
  }
  function openMate(m) {
    const st = ["idle", "working", "problem"].includes(m.status) ? m.status : "idle";
    openDialog({
      title: m.title_he,
      build: (d) => {
        append(d.body, [el("div", { cls: "mate-sheet" }, [
          el("span", { cls: "mate " + st + (m.name === "manager" ? " boss" : ""), "aria-hidden": "true" }, [icon(TEAM_ICON[m.name] || "users"), el("span", { cls: "sdot" })]),
          el("div", {}, [el("span", { cls: "status-line " + (st === "idle" ? "ok" : st === "working" ? "warn" : "bad") }, [el("span", { cls: "sd" }), TEAM_STATUS_HE[st]]),
            el("p", { cls: "confirm-text", text: m.sentence || "" })]),
        ])]);
        if (m.name === "manager") {
          setFoot(d, [el("button", { type: "button", cls: "btn btn-big btn-primary", text: "דבר איתו", on: { click: () => { d.close(); setTimeout(() => openChat(""), 220); } } })]);
        }
      },
    });
  }

  // ================================================================== upload
  let chosen = null;
  let previewUrl = null;
  let xhr = null;
  function validateFile(f) {
    const dot = f.name.lastIndexOf(".");
    const ext = dot >= 0 ? f.name.slice(dot).toLowerCase() : "";
    if (!ALLOWED_EXT.includes(ext)) return "זה לא קובץ וידאו שאני מכיר. אפשר: " + ALLOWED_EXT.join(" ").toUpperCase().replace(/\./g, "");
    if (f.size === 0) return "הקובץ ריק.";
    if (f.size > MAX_BYTES) return "הקובץ גדול מדי (" + fmtBytes(f.size) + "). עד " + MAX_GB + "GB.";
    return null;
  }
  function showDrop(state) {
    $("drop-idle").classList.toggle("hidden", state !== "idle");
    $("drop-chosen").classList.toggle("hidden", state !== "chosen");
    $("drop-uploading").classList.toggle("hidden", state !== "uploading");
  }
  function choose(f) {
    if (!f) return;
    const err = validateFile(f);
    if (err) { toast(err, "err"); return; }
    chosen = f;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
    const prev = clear($("chosen-preview"));
    try {
      previewUrl = URL.createObjectURL(f);
      const v = el("video", { src: previewUrl, muted: true, playsinline: true, preload: "metadata", "aria-hidden": "true" });
      v.muted = true;
      v.addEventListener("loadedmetadata", () => { try { v.currentTime = Math.min(1, (v.duration || 2) / 3); } catch (_) { /* ignore */ } });
      v.addEventListener("error", () => { clear(prev); prev.appendChild(icon("film", "ic-xl")); });
      prev.appendChild(v);
    } catch (_) { prev.appendChild(icon("film", "ic-xl")); }
    setText($("chosen-name"), f.name);
    setText($("chosen-meta"), fmtBytes(f.size));
    setText($("chosen-msg"), "");
    showDrop("chosen");
    if (route.page !== "home") location.hash = "#/";
    setTimeout(() => $("clip-title").focus(), 60);
  }
  function resetDrop() {
    chosen = null;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
    clear($("chosen-preview"));
    $("file-input").value = "";
    showDrop("idle");
  }
  function sendClip() {
    if (!chosen) return;
    const f = chosen;
    const fd = new FormData();
    fd.append("title", $("clip-title").value.trim());
    fd.append("file", f, f.name);
    xhr = new XMLHttpRequest();
    showDrop("uploading");
    const bar = $("upload-bar"), pct = $("upload-pct");
    bar.style.width = "0%";
    setText(pct, "0%");
    setText($("upload-size"), f.name + " · " + fmtBytes(f.size));
    xhr.upload.addEventListener("progress", (e) => {
      if (!e.lengthComputable) return;
      const p = (e.loaded / e.total) * 100;
      bar.style.width = p.toFixed(1) + "%";
      setText(pct, Math.floor(p) + "%");
    });
    xhr.addEventListener("load", () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (_) { data = null; }
      if (xhr.status >= 200 && xhr.status < 300 && data && data.ok) {
        bar.style.width = "100%";
        setText(pct, "100%");
        $("clip-title").value = "";
        toast("הקליפ אצל הצוות. אני על זה.", "ok", { icon: "rocket" });
        resetDrop();
        refreshHome();
        setTimeout(refreshHome, 1200);
      } else {
        showDrop("chosen");
        const m = $("chosen-msg");
        m.className = "form-msg err";
        m.textContent = "השליחה נכשלה: " + friendlyError(xhr.status, data);
      }
      xhr = null;
    });
    xhr.addEventListener("error", () => { showDrop("chosen"); const m = $("chosen-msg"); m.className = "form-msg err"; m.textContent = OFFLINE_HE; xhr = null; });
    xhr.addEventListener("abort", () => { showDrop("chosen"); xhr = null; });
    xhr.open("POST", "/api/upload");
    xhr.setRequestHeader("X-Agency-Request", "1");
    xhr.send(fd);
  }
  function setupUpload() {
    const dz = $("dropzone"), input = $("file-input");
    input.addEventListener("change", () => choose(input.files && input.files[0]));
    dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "dragend", "drop"].forEach((t) => dz.addEventListener(t, () => dz.classList.remove("drag")));
    $("send-clip").addEventListener("click", sendClip);
    $("clip-title").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sendClip(); } });
    $("cancel-clip").addEventListener("click", resetDrop);
    $("upload-cancel").addEventListener("click", () => { if (xhr) xhr.abort(); });

    // A file dragged anywhere over the page: a calm full-page target (never navigate away).
    const overlay = $("drag-overlay");
    let depth = 0;
    const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
    window.addEventListener("dragenter", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth += 1;
      overlay.classList.remove("hidden");
      requestAnimationFrame(() => overlay.classList.add("on"));
    });
    window.addEventListener("dragleave", () => {
      depth = Math.max(0, depth - 1);
      if (!depth) { overlay.classList.remove("on"); overlay.classList.add("hidden"); }
    });
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => {
      e.preventDefault();
      depth = 0;
      overlay.classList.remove("on");
      overlay.classList.add("hidden");
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) choose(f);
    });
  }

  // ================================================================== chat
  let chatGreeted = false;
  let chatBusy = false;
  function openChat(prefill) {
    const panel = $("chat-panel"), fab = $("chat-fab"), input = $("chat-input");
    panel.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
    fab.setAttribute("aria-expanded", "true");
    if (!chatGreeted) {
      chatGreeted = true;
      addMsg("boss", "היי! אני המנכ\"ל. שאל אותי על הקליפים, המספרים או הכסף. אני לא מאשר כלום בעצמי — רק אתה.");
    }
    if (typeof prefill === "string" && prefill) input.value = prefill;
    setTimeout(() => { input.focus(); const n = input.value.length; try { input.setSelectionRange(n, n); } catch (_) { /* ignore */ } }, 80);
  }
  function closeChat() {
    const panel = $("chat-panel"), fab = $("chat-fab");
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
    fab.setAttribute("aria-expanded", "false");
    fab.focus({ preventScroll: true });
  }
  function addMsg(who, text) {
    const log = $("chat-log");
    const node = el("div", { cls: "msg " + who, dir: "auto" }, [String(text)]);
    log.appendChild(node);
    log.scrollTop = log.scrollHeight;
    return node;
  }
  async function sendChat(text) {
    const msg = String(text || "").trim();
    if (!msg || chatBusy) return;
    chatBusy = true;
    $("chat-input").value = "";
    $("chat-send").disabled = true;
    addMsg("me", msg);
    const typing = el("div", { cls: "msg boss", "aria-label": "המנכ\"ל כותב" }, [el("span", { cls: "typing" }, [el("i"), el("i"), el("i")])]);
    $("chat-log").appendChild(typing);
    $("chat-log").scrollTop = $("chat-log").scrollHeight;
    try {
      const res = await api("/api/chat", { json: { message: msg.slice(0, 4000) } });
      typing.remove();
      addMsg("boss", (res && res.reply) || "…");
    } catch (e) {
      typing.remove();
      addMsg("sys", e.status === 502 ? "המנכ\"ל לא זמין כרגע. נסה שוב עוד רגע." : e.message);
    } finally {
      chatBusy = false;
      $("chat-send").disabled = false;
      $("chat-input").focus();
    }
  }
  function setupChat() {
    $("chat-fab").addEventListener("click", () => openChat(""));
    $("chat-close").addEventListener("click", closeChat);
    $("chat-panel").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); closeChat(); } });
    $("chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat($("chat-input").value); });
    $("chat-quick").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => sendChat(b.textContent)));
  }

  // ================================================================== CONNECTIONS
  let conn = null;
  let connLoading = 0;
  const tileOf = (key) => (conn && conn.tiles ? conn.tiles.find((t) => t.key === key) : null);

  async function loadConnections(sub) {
    const my = ++connLoading;
    if (!conn) renderTileSkeletons();
    try {
      conn = await api("/api/connections?check=0");
      if (my !== connLoading) return;
      renderConnections();
    } catch (e) {
      toast(e.message, "err");
      return;
    }
    if (sub) {
      history.replaceState(null, "", "#/connections");
      if (sub === "import") openImport(); else openWizard(sub);
    }
    setChecking(true);
    try {
      const fresh = await api("/api/connections?check=1");
      if (my === connLoading) { conn = fresh; renderConnections(); }
    } catch (_) { /* tiles from the quick read stay */ }
    finally { if (my === connLoading) setChecking(false); }
  }
  async function reloadConnections(check) {
    try { conn = await api("/api/connections?check=" + (check ? 1 : 0)); renderConnections(); } catch (_) { /* keep */ }
  }
  let checking = false;
  function setChecking(on) { checking = on; renderSummary(); }

  function renderTileSkeletons() {
    const grid = clear($("conn-grid"));
    for (let i = 0; i < 9; i++) {
      grid.appendChild(el("div", { cls: "tile", "aria-hidden": "true" }, [
        el("div", { cls: "row" }, [el("span", { cls: "skel skel-circle skel-mate" }), el("div", { cls: "spacer" }, [el("span", { cls: "skel skel-line w50" }), el("span", { cls: "skel skel-line w80" })])]),
        el("span", { cls: "skel skel-line w100" }),
      ]));
    }
  }

  function renderConnections() {
    if (!conn) return;
    renderConnTop();
    renderSummary();
    const grid = clear($("conn-grid"));
    const tiles = (conn.tiles || []).slice().sort((a, b) => TILE_ORDER.indexOf(a.key) - TILE_ORDER.indexOf(b.key));
    for (const t of tiles) grid.appendChild(tileNode(t));
    setText($("conn-foot"), conn.keys_saved_in === "keyring"
      ? "פרטי החיבורים נשמרים בכספת של Windows, רק במחשב הזה. אף אחד לא רואה אותם — גם לא אנחנו."
      : "פרטי החיבורים נשמרים רק במחשב הזה. אף אחד לא רואה אותם — גם לא אנחנו.");
  }

  function renderSummary() {
    const box = clear($("conn-summary"));
    if (!conn) return;
    const platforms = (conn.tiles || []).filter((t) => t.key !== "telegram");
    const connected = platforms.filter(tileSaved).length;
    const unchecked = platforms.filter((t) => t.state === "unchecked").length;
    const live = platforms.filter((t) => t.connected && t.live).length;
    box.appendChild(el("span", { cls: "tag " + (connected ? "tag-green" : "") }, [icon("plug", "ic-sm"), "מחוברות " + connected + " מתוך " + platforms.length]));
    box.appendChild(el("span", { cls: "tag " + (live ? "tag-green" : "tag-orange") }, [live ? "בהעלאה אמיתית: " + live : "הכל במצב אימון"]));
    if (unchecked && !checking) box.appendChild(el("span", { cls: "tag tag-steel" }, [icon("wifi", "ic-sm"), "אין אינטרנט? אבדוק שוב לבד"]));
    if (checking) box.appendChild(el("span", { cls: "checking" }, [el("span", { cls: "spin", "aria-hidden": "true" }), "בודק שהכל עובד…"]));
  }

  function renderConnTop() {
    const box = clear($("conn-top"));
    const st = conn.studio || {};
    // the card's heading already says it found the studio: don't say it twice
    const studioLine = String(st.message_he || "").replace(/^מצאתי את הסטודיו\.\s*/, "");
    if (st.found && !st.imported) {
      box.appendChild(el("section", { cls: "card import-card", "aria-labelledby": "imp-h" }, [
        el("div", { cls: "import-head" }, [el("span", { cls: "import-ic" }, [icon("sparkles")]),
          el("div", { cls: "import-txt" }, [el("h2", { id: "imp-h", text: "מצאתי את הסטודיו הקיים שלך" }),
            el("p", { text: studioLine || "אפשר להביא ממנו את החיבורים, הסגנון ושעות ההעלאה — בלי להתחיל מאפס." })])]),
        el("div", { cls: "import-actions" }, [
          el("button", { type: "button", cls: "btn btn-primary btn-big", on: { click: () => openImport() } }, [icon("download"), "ייבוא מהסטודיו"]),
        ]),
      ]));
    } else if (st.found && st.keys_missing) {
      box.appendChild(el("section", { cls: "card import-card", "aria-labelledby": "imp-h" }, [
        el("div", { cls: "import-head" }, [el("span", { cls: "import-ic" }, [icon("plug")]),
          el("div", { cls: "import-txt" }, [el("h2", { id: "imp-h", text: "הסטודיו מקושר. נשאר צעד אחד" }),
            el("p", { text: studioLine || "חיבור אחד ל-Zernio מחזיר את יוטיוב וטיקטוק." })])]),
        el("div", { cls: "import-actions" }, [
          el("button", { type: "button", cls: "btn btn-primary btn-big", on: { click: () => startChain(["youtube", "facebook", "telegram"]) } }, [icon("plug"), "להחזיר את יוטיוב וטיקטוק"]),
        ]),
      ]));
    }
    if (st.daemon_active && store(LS_KEEP_OLD) !== "1") {
      box.appendChild(el("div", { cls: "banner" }, [icon("alert", "ic-lg"),
        el("div", { cls: "banner-text" }, [el("strong", { text: st.question_he || "הסטודיו הישן עדיין פעיל — הסוכנות החדשה תחליף אותו?" }),
          el("span", { text: "רק אחד מהם צריך להעלות, כדי שאף קליפ לא יעלה פעמיים." })]),
        el("div", { cls: "banner-actions" }, [
          el("button", { type: "button", cls: "btn btn-primary", text: "כן, תחליף", on: { click: () => openReplaceStudio(st) } }),
          el("button", { type: "button", cls: "btn btn-ghost", text: "לא, תשאיר את הישן", on: { click: () => { keepOldStudio(); renderConnTop(); } } }),
        ])]));
    }
  }

  const TILE_STATES = ["connected", "unchecked", "needs_fix", "not_connected"];
  /** Keys are saved (connected, or saved but not checkable right now): open the manage view. */
  const tileSaved = (t) => !!t && (t.connected || t.state === "unchecked");
  function tileNode(t) {
    const state = TILE_STATES.includes(t.state) ? t.state : "not_connected";
    const node = el("div", { cls: "tile b-" + t.key + " " + state });
    const isShorts = t.shares === "youtube";
    const ctaText = { needs_fix: "לתקן", unchecked: "לבדוק", not_connected: "לחבר" }[state];
    const cta = ctaText ? el("span", { cls: "tile-cta" + (state === "needs_fix" ? " fix" : state === "unchecked" ? " check" : ""),
      "aria-hidden": "true", text: ctaText }) : null;
    const open = el("button", { type: "button", cls: "tile-open", "aria-label": (t.name_he || t.key) + ": " + (t.status_he || "") + ". " + (cta ? cta.textContent : "לפתיחה") }, [
      pico(t.key),
      el("span", { cls: "tile-txt" }, [el("span", { cls: "tile-name", text: t.name_he || PLATFORMS[t.key] || t.key }),
        statusNode(t, "tile-status")]),
      cta,
      el("span", { cls: "chev", "aria-hidden": "true" }, [icon("chevron", "ic-sm")]),
    ]);
    open.addEventListener("click", () => openWizard(isShorts ? "youtube" : t.key));
    node.appendChild(open);
    if (isShorts) node.appendChild(el("span", { cls: "tile-shares", text: "משתמש בחיבור של יוטיוב" }));
    else {  // Shorts rides on YouTube's connection: its source / check lines live on YouTube's tile
      if (t.source_he) node.appendChild(el("span", { cls: "tile-src" }, [icon("link"), t.source_he]));
      if (t.state === "needs_fix" && t.problem) node.appendChild(el("div", { cls: "tile-problem", text: t.problem }));
      if (t.state === "unchecked" && t.note_he) node.appendChild(el("div", { cls: "tile-note" }, [icon("wifi"), el("span", { text: t.note_he })]));
    }
    if (t.warning_he) node.appendChild(el("div", { cls: "tile-warn" }, [icon("lock"), el("span", { text: t.warning_he })]));
    if (t.key === "telegram" && t.telegram && t.telegram.message_he && t.connected) {
      const ts = t.telegram.state;
      node.appendChild(el("span", { cls: "status-line " + (ts === "running" ? "ok" : ts === "busy_by_old_studio" || ts === "not_paired" ? "warn" : "") },
        [el("span", { cls: "sd" }), t.telegram.message_he]));
    }
    if (!t.connected) return node;
    const foot = el("div", { cls: "tile-foot" });
    if (t.key !== "telegram") foot.appendChild(liveSwitch(t));
    else {
      foot.appendChild(el("button", { type: "button", cls: "btn btn-sm btn-ghost", text: t.telegram && t.telegram.paired ? "ניהול" : "צימוד לטלגרם שלי",
        on: { click: () => openWizard("telegram") } }));
    }
    node.appendChild(foot);
    return node;
  }

  function liveSwitch(t) {
    const on = !!t.live_switch;
    const sw = el("button", { type: "button", cls: "switch", role: "switch", "aria-checked": String(on), disabled: t.can_go_live === false }, [
      el("span", { cls: "switch-track", "aria-hidden": "true" }),
      el("span", { cls: "switch-text" }, [el("b", { text: on ? "העלאה אמיתית" : "אימון" }),
        el("small", { text: on ? "הקליפים עולים באמת" : "מתרגל — שום דבר לא עולה" })]),
    ]);
    sw.setAttribute("aria-label", "העלאה אמיתית ל" + (t.name_he || t.key));
    sw.addEventListener("click", () => toggleLive(t, sw));
    return sw;
  }
  async function toggleLive(t, sw) {
    const on = sw.getAttribute("aria-checked") !== "true";
    if (on) {
      const ok = await confirmDialog({ title: "העלאה אמיתית", iconNode: pico(t.key),
        text: "מעכשיו הקליפים יעלו באמת ל-" + (t.name_he || t.key) + ". בטוח?", yes: "כן, להעלות באמת", no: "עוד לא" });
      if (!ok) return;
    }
    sw.disabled = true;
    try {
      const res = await api("/api/connect/" + encodeURIComponent(t.key) + "/live", { json: { live: on, confirm: on } });
      toast(on ? (t.name_he || t.key) + ": העלאה אמיתית פועלת" : (t.name_he || t.key) + ": חזר למצב אימון", on ? "ok" : "info");
      if (res && res.warning_he) toast(res.warning_he, "warn", { ms: 10000 });
      await reloadConnections(false);
      refreshHome();
    } catch (e) {
      toast(e.message, "err");
      sw.disabled = false;
    }
  }

  // ------------------------------------------------------------------ wizard engine
  /* WIZARD-COPY:BEGIN — connect-wizard text quotes the exact button labels of the platforms'
   * own sites (in .ext chips), so the owner knows what to click. Only here. */
  const ext = (label) => el("span", { cls: "ext", text: label });
  const code = (text) => el("span", { cls: "code-chip", text });
  const P = (...parts) => el("p", {}, parts);
  function extLink(href, label) {
    return el("a", { cls: "btn btn-steel link-btn", href, target: "_blank", rel: "noopener noreferrer" }, [icon("external", "ic-sm"), label]);
  }
  function note(text, warn) {
    return el("div", { cls: "note" + (warn ? " warn" : "") }, [icon(warn ? "alert" : "shield"), el("span", {}, [].concat(text))]);
  }

  /** A masked key field. Required when the name is missing; else optional ("keep the saved one"). */
  function keyField(spec, missing) {
    const id = "kf-" + spec.name.toLowerCase();
    const isSecret = spec.secret !== false;
    const required = !missing || missing.includes(spec.name);
    const input = el("input", { cls: "input input-mono", id, name: spec.name, type: isSecret ? "password" : "text",
      autocomplete: "off", spellcheck: "false", autocapitalize: "off", autocorrect: "off", dir: "ltr",
      placeholder: required ? (spec.placeholder || "") : "כבר שמור (אפשר להשאיר ריק)", "aria-describedby": spec.hint ? id + "-h" : null });
    input.dataset.required = required ? "1" : "";
    const wrap = el("span", { cls: "input-wrap" }, [input]);
    if (isSecret) {
      const peek = el("button", { type: "button", cls: "peek", "aria-label": "הצג", "aria-pressed": "false" }, [icon("eye", "ic-sm")]);
      peek.addEventListener("click", () => {
        const show = input.type === "password";
        input.type = show ? "text" : "password";
        peek.setAttribute("aria-pressed", String(show));
        peek.setAttribute("aria-label", show ? "הסתר" : "הצג");
      });
      wrap.appendChild(peek);
    }
    const field = el("div", { cls: "field" }, [el("label", { cls: "field-label", for: id }, [].concat(spec.label)), wrap,
      spec.hint ? el("small", { id: id + "-h" }, [].concat(spec.hint)) : null]);
    return { field, input, name: spec.name };
  }
  function collect(fields) {
    const values = {};
    let firstEmpty = null;
    for (const f of fields) {
      const v = f.input.value.trim();
      f.input.removeAttribute("aria-invalid");
      if (v) values[f.name] = v;
      else if (f.input.dataset.required === "1" && !firstEmpty) firstEmpty = f;
    }
    return { values, firstEmpty };
  }
  function wipe(fields) { for (const f of fields) f.input.value = ""; }

  const KEY_SPECS = {
    ZERNIO_API_KEY: { label: "המפתח של Zernio" },
    META_ACCESS_TOKEN: { label: "מפתח העמוד של מטא", hint: ["הארוך, מתוך ", ext("me/accounts"), "."] },
    FB_PAGE_ID: { label: "מספר העמוד בפייסבוק", secret: false, hint: ["ה-", ext("id"), " של העמוד באותה רשימה."], placeholder: "100000000000000" },
    IG_USER_ID: { label: "מספר חשבון האינסטגרם", secret: false, hint: ["ב-Explorer מריצים ", code("<מספר העמוד>?fields=instagram_business_account"), "."], placeholder: "17841400000000000" },
    TELEGRAM_BOT_TOKEN: { label: "המפתח של הבוט", placeholder: "123456:ABC…" },
    X_API_KEY: { label: ["מפתח 1 · ", ext("API Key")] },
    X_API_SECRET: { label: ["מפתח 2 · ", ext("API Key Secret")] },
    X_ACCESS_TOKEN: { label: ["מפתח 3 · ", ext("Access Token")] },
    X_ACCESS_SECRET: { label: ["מפתח 4 · ", ext("Access Token Secret")] },
    REDDIT_CLIENT_ID: { label: "מזהה האפליקציה", secret: false, hint: "המחרוזת הקצרה שמתחת לשם האפליקציה." },
    REDDIT_CLIENT_SECRET: { label: "הקוד הסודי של האפליקציה", hint: ["מהשדה ", ext("secret"), "."] },
    REDDIT_USERNAME: { label: "שם המשתמש שלך ברדיט", secret: false },
    REDDIT_PASSWORD: { label: "הסיסמה שלך ברדיט" },
    DISCORD_WEBHOOK_URL: { label: "הכתובת שהעתקת מדיסקורד", placeholder: "https://discord.com/…" },
    TIKTOK_ACCESS_TOKEN: { label: "מפתח הגישה של טיקטוק" },
  };
  const specOf = (name) => Object.assign({ name }, KEY_SPECS[name] || { label: name });

  function wizardKind(t) {
    if (t.key === "telegram") return "telegram";
    if ((t.required || []).includes("ZERNIO_API_KEY")) return "zernio";
    if (t.key === "instagram_reels" || t.key === "facebook") return "meta";
    if (t.key === "youtube" || t.key === "youtube_shorts") return "youtube_direct";
    if (["x", "reddit", "discord"].includes(t.key)) return t.key;
    return "generic";
  }

  let chain = [];   // remaining wizards to offer after this one (import → Zernio → Meta → Telegram)
  // One wizard connects a pair: name the pair on the "הבא:" button.
  const CHAIN_HE = { youtube: "יוטיוב וטיקטוק", tiktok: "יוטיוב וטיקטוק", facebook: "פייסבוק ואינסטגרם",
    instagram_reels: "פייסבוק ואינסטגרם", telegram: "אישורים בטלגרם" };
  function startChain(keys) {
    chain = keys.slice(1);
    openWizard(keys[0]);
  }

  function openWizard(key) {
    if (!conn) { location.hash = "#/connections/" + key; return; }
    const t = tileOf(key);
    if (!t) return;
    if (!IS_PC) { toast(PC_ONLY_HE, "warn"); return; }
    const kind = wizardKind(t);
    const def = WIZARDS[kind](t);
    runWizard(t, def, tileSaved(t) || (kind === "meta" && tileSaved(tileOf("facebook"))));
  }

  /** def = {title, pico, steps:[{title, render(wrap, ctx), action?:{label, run(ctx)}}], manage?(body, ctx)} */
  function runWizard(t, def, connected) {
    const progress = el("div", { cls: "wiz-progress", "aria-hidden": "true" });
    const count = el("p", { cls: "wiz-count" });
    const top = el("div", {}, [progress, count]);
    const d = openDialog({ title: def.title, iconNode: pico(def.pico || t.key), afterHead: top });
    const ctx = {
      tile: t, dialog: d, data: {},
      show: (i) => show(i), next: () => show(idx + 1), close: () => d.close(),
      done: (lines, ok) => done(lines, ok), manage: () => manage(),
    };
    let idx = 0;
    const steps = def.steps;
    function show(i) {
      idx = Math.max(0, Math.min(i, steps.length - 1));
      const step = steps[idx];
      top.hidden = false;
      clear(progress);
      steps.forEach((_s, j) => progress.appendChild(el("i", { cls: j <= idx ? "on" : "" })));
      setText(count, "שלב " + (idx + 1) + " מתוך " + steps.length);
      clear(d.body);
      const wrap = el("div", { cls: "wiz-step" }, [el("h3", {}, [el("span", { cls: "wiz-num", text: String(idx + 1) }), " ", step.title])]);
      d.body.appendChild(wrap);
      step.render(wrap, ctx);
      const buttons = [];
      if (step.action) {
        const b = el("button", { type: "button", cls: "btn btn-big btn-primary", text: step.action.label });
        b.addEventListener("click", async () => {
          busy(b, true, step.action.busy || "בודק…");
          try { await step.action.run(ctx, b); }
          finally { if (b.isConnected) busy(b, false); }
        });
        buttons.push(b);
      } else if (idx < steps.length - 1) {
        buttons.push(el("button", { type: "button", cls: "btn btn-big btn-primary", text: "הבא", on: { click: () => show(idx + 1) } }));
      }
      if (idx > 0) buttons.push(el("button", { type: "button", cls: "btn btn-big btn-ghost", text: "חזרה", on: { click: () => show(idx - 1) } }));
      else if (connected) buttons.push(el("button", { type: "button", cls: "btn btn-big btn-ghost", text: "חזרה", on: { click: manage } }));
      setFoot(d, buttons);
      const first = d.body.querySelector("input:not([type=hidden]), textarea");
      if (first) setTimeout(() => first.focus(), 60);
    }
    function done(lines, ok) {
      top.hidden = true;
      clear(d.body);
      const success = ok !== false;
      d.body.appendChild(el("div", { cls: "success-burst" + (success ? "" : " fail-burst") }, [
        el("span", { cls: "big-check" }, [icon(success ? "check" : "alert")]),
      ].concat((lines || []).map((l, i) => (i === 0 ? el("h3", { text: l }) : el("p", { cls: "muted", text: l }))))));
      const buttons = [];
      const nextKey = chain.find((k) => { const nt = tileOf(k); return nt && !nt.connected && k !== t.key && !(k === "facebook" && t.key === "instagram_reels"); });
      if (success && nextKey) {
        buttons.push(el("button", { type: "button", cls: "btn btn-big btn-primary", text: "הבא: " + (CHAIN_HE[nextKey] || PLATFORMS[nextKey] || nextKey),
          on: { click: () => { chain = chain.filter((k) => k !== nextKey); d.close(); setTimeout(() => openWizard(nextKey), 230); } } }));
      }
      buttons.push(el("button", { type: "button", cls: "btn btn-big " + (buttons.length ? "btn-ghost" : "btn-primary"), text: "סיום", on: { click: () => { chain = []; d.close(); } } }));
      if (!success) buttons.unshift(el("button", { type: "button", cls: "btn btn-big btn-primary", text: "לנסות שוב", on: { click: () => show(Math.max(0, steps.findIndex((s) => s.keys))) } }));
      setFoot(d, buttons);
    }
    function manage() {
      top.hidden = true;
      clear(d.body);
      const tile = tileOf(t.key) || t;
      const box = el("div", { cls: "stack" }, [
        el("div", { cls: "row" }, [pico(tile.key), el("div", { cls: "spacer" }, [el("b", {}, [statusNode(tile, "")]),
          tile.source_he ? el("div", { cls: "tile-src" }, [icon("link"), tile.source_he]) : null])]),
        tile.state === "unchecked" && tile.note_he ? el("div", { cls: "tile-note" }, [icon("wifi"), el("span", { text: tile.note_he })])
          : tile.problem && !tile.connected ? el("div", { cls: "tile-problem", text: tile.problem }) : null,
      ]);
      d.body.appendChild(box);
      if (def.manage) def.manage(d.body, ctx);
      const verifyB = el("button", { type: "button", cls: "btn btn-big btn-steel" }, [icon("refresh", "ic-sm"), "בדוק שוב"]);
      verifyB.addEventListener("click", async () => {
        busy(verifyB, true, "בודק…");
        try {
          const keys = def.verifyKeys || [tile.key];
          for (const k of keys) await api("/api/connect/" + encodeURIComponent(k) + "/verify", { method: "POST" });
          await reloadConnections(false);
          const fresh = tileOf(tile.key) || tile;
          toast(fresh.connected ? (fresh.status_he || "מחובר ✓") : (fresh.note_he || fresh.problem || "לא מחובר"),
            fresh.connected ? "ok" : fresh.state === "unchecked" ? "info" : "warn");
          if (d.isOpen()) manage();
        } catch (e) { toast(e.message, "err"); }
        finally { if (verifyB.isConnected) busy(verifyB, false); }
      });
      const keyStep = Math.max(0, steps.findIndex((s) => s.keys));
      const replace = el("button", { type: "button", cls: "btn btn-big btn-ghost", text: def.replaceLabel || "החלף מפתח", on: { click: () => show(keyStep) } });
      const disc = el("button", { type: "button", cls: "btn btn-big btn-danger" }, [icon("unlink", "ic-sm"), "נתק"]);
      disc.addEventListener("click", () => disconnect(tile, d, def));
      setFoot(d, [verifyB, replace, disc]);
    }
    if (connected) manage(); else show(0);
    return d;
  }

  async function saveKeys(platform, values) {
    const res = await api("/api/connect/" + encodeURIComponent(platform) + "/keys", { json: { values } });
    return res;
  }
  /** Saved, but the check could not reach the service (no internet): a calm "saved" ending. */
  function savedUnchecked(ctx, tile) {
    if (!tile || tile.state !== "unchecked") return false;
    ctx.done(["המפתח נשמר ✓", tile.note_he || "לא הצלחתי לבדוק אותו עכשיו. אבדוק שוב לבד."]);
    return true;
  }
  /** The standard "paste here" step: fields for `names`, save + verify on `platform`. */
  function keysStep(title, platform, names, opts) {
    const o = opts || {};
    return {
      title, keys: true,
      render: (wrap, ctx) => {
        const missing = (ctx.tile && ctx.tile.missing) || names;
        ctx.data.fields = names.map((n) => keyField(specOf(n), missing));
        for (const f of ctx.data.fields) wrap.appendChild(f.field);
        if (o.extra) o.extra(wrap, ctx);
        wrap.appendChild(note("נשמר רק במחשב הזה. אף אחד לא רואה את המפתח — גם לא אנחנו."));
        ctx.data.msg = el("p", { cls: "form-msg err", role: "alert" });
        wrap.appendChild(ctx.data.msg);
        wrap.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches("input")) { e.preventDefault(); const b = ctx.dialog.foot.querySelector(".btn-primary"); if (b) b.click(); } });
      },
      action: {
        label: o.label || "חבר ובדוק", busy: "שומר ובודק…",
        run: async (ctx) => {
          const { values, firstEmpty } = collect(ctx.data.fields);
          if (firstEmpty) { firstEmpty.input.setAttribute("aria-invalid", "true"); firstEmpty.input.focus(); ctx.data.msg.textContent = "חסר עוד שדה אחד."; return; }
          if (!Object.keys(values).length) { ctx.data.msg.textContent = "לא הודבק כלום."; return; }
          ctx.data.msg.textContent = "";
          try {
            const res = await saveKeys(platform, values);
            wipe(ctx.data.fields);
            await reloadConnections(false);
            const tile = res.tile || {};
            if (o.after) { await o.after(ctx, res); return; }
            if (tile.connected) ctx.done([tile.status_he || "מחובר ✓", o.okLine || "כרגע במצב אימון. כשתרצה — מדליקים \"העלאה אמיתית\" בכרטיס."]);
            else if (!savedUnchecked(ctx, tile)) ctx.data.msg.textContent = tile.problem || "לא הצלחתי להתחבר. בדוק שהעתקת את כל המפתח ונסה שוב.";
            reloadConnections(true);
            refreshHome();
          } catch (e) { ctx.data.msg.textContent = e.message; }
        },
      },
    };
  }

  async function disconnect(tile, d, def) {
    const name = def.disconnectName || tile.name_he || tile.key;
    const ok = await confirmDialog({ title: "לנתק את " + name + "?", text: (def.disconnectText ? def.disconnectText + " " : "") + "הצוות יפסיק להעלות לשם. אפשר לחבר שוב בכל רגע.", yes: "כן, נתק", no: "ביטול", danger: true, iconNode: pico(tile.key) });
    if (!ok) return;
    try {
      const keys = def.disconnectKeys || [tile.key];
      let studioKept = false, shared = false, system = false;
      const also = new Set();
      for (const k of keys) {
        const res = await api("/api/connect/" + encodeURIComponent(k) + "/disconnect", { method: "POST" });
        if (res.studio && res.studio.length) studioKept = true;
        if (res.system && res.system.length) system = true;
        if (res.kept_shared && res.kept_shared.length && !(res.removed || []).length) shared = true;
        for (const a of res.also_disconnected || []) if (!keys.includes(a) && PLATFORMS[a]) also.add(PLATFORMS[a]);
      }
      await reloadConnections(false);
      if (system) toast("את המפתח הזה הגדירו במחשב עצמו, מחוץ לסוכנות — אז הוא נשאר. אפשר להסיר אותו במצב מתקדם.", "warn", { ms: 10000 });
      else if (studioKept) toast("נותק אצלנו. החיבור של הסטודיו הקיים נשאר כמו שהוא — לא נגענו בו.", "info", { ms: 8000 });
      else if (also.size) toast(name + " נותק, וגם " + [...also].join(" ו") + " (הם עולים עם אותו מפתח).", "info", { ms: 8000 });
      else if (shared) toast(name + " חזר לאימון. המפתח נשאר כי פלטפורמה אחרת משתמשת בו.", "info", { ms: 8000 });
      else toast(name + " נותק.", "info");
      d.close();
      refreshHome();
    } catch (e) { toast(e.message, "err"); }
  }

  // ------------------------------------------------------------------ YouTube direct (advanced)
  function googleBox(open) {
    const box = el("details", { cls: "advanced-box", open: open ? true : null });
    box.appendChild(el("summary", {}, [icon("google"), "מתקדם: התחברות ישירה עם גוגל", icon("chevron-down", "ic-sm")]));
    const inner = el("div", { cls: "inner" });
    box.appendChild(inner);
    inner.appendChild(note(["שים לב: סרטון שעולה ישירות מפרויקט גוגל שלא אומת ננעל כ", el("b", { text: "פרטי" }),
      ". ההעלאות ממשיכות דרך Zernio — החיבור הזה רק לנתונים, לתגובות ולהפוך סרטון לפרטי."], true));
    // 1. the Google login file
    const fileIn = el("input", { type: "file", accept: ".json,application/json", cls: "file-input", id: "g-file" });
    const fileB = el("label", { cls: "btn btn-ghost", for: "g-file", tabindex: "0" }, [icon("upload", "ic-sm"), "בחר קובץ"]);
    fileB.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileIn.click(); } });
    const fileMsg = el("span", { cls: "form-msg", role: "status" });
    fileIn.addEventListener("change", async () => {
      const f = fileIn.files && fileIn.files[0];
      fileIn.value = "";
      if (!f) return;
      if (f.size > 1024 * 1024) { fileMsg.className = "form-msg err"; fileMsg.textContent = "זה לא הקובץ הנכון (גדול מדי)."; return; }
      fileMsg.className = "form-msg"; fileMsg.textContent = "שומר…";
      try {
        const text = await f.text();
        await api("/api/connect/youtube/client-secret", { body: text, type: "application/json" });
        fileMsg.className = "form-msg ok"; fileMsg.textContent = "הקובץ נשמר ✓ עכשיו אפשר להתחבר.";
        gBtn.disabled = false;
      } catch (e) { fileMsg.className = "form-msg err"; fileMsg.textContent = e.message; }
    });
    inner.appendChild(el("div", { cls: "field" }, [el("span", { cls: "field-label" }, ["1. קובץ ההתחברות של גוגל (שמו מתחיל ב-", ext("client_secret"), ")"]),
      el("div", { cls: "row" }, [fileIn, fileB, fileMsg]),
      el("small", {}, ["מורידים אותו מ-", ext("Google Cloud Console"), " ← ", ext("Credentials"), ", מהפרויקט של הערוץ."])]));
    // 2. sign in with Google (the login window opens on this PC)
    const gBtn = el("button", { type: "button", cls: "btn btn-steel" }, [icon("google", "ic-sm"), "התחבר עם גוגל"]);
    const gMsg = el("p", { cls: "form-msg", role: "status" });
    let polling = null;
    const poll = async () => {
      try {
        const s = await api("/api/connect/youtube/state");
        if (s.state === "waiting") { polling = setTimeout(poll, 1500); return; }
        busy(gBtn, false);
        if (s.state === "done") { gMsg.className = "form-msg ok"; gMsg.textContent = "מחובר ✓ " + (s.channel || ""); reloadConnections(false); }
        else if (s.state === "error") { gMsg.className = "form-msg err"; gMsg.textContent = s.problem || "ההתחברות לא הושלמה — נסה שוב."; }
      } catch (_) { polling = setTimeout(poll, 3000); }
    };
    gBtn.addEventListener("click", async () => {
      busy(gBtn, true, "מחכה לגוגל…");
      gMsg.className = "form-msg"; gMsg.textContent = "נפתח חלון של גוגל במחשב. בוחרים את החשבון של הערוץ ומאשרים.";
      try { await api("/api/connect/youtube/start", { method: "POST" }); clearTimeout(polling); polling = setTimeout(poll, 1200); }
      catch (e) { busy(gBtn, false); gMsg.className = "form-msg err"; gMsg.textContent = e.message; }
    });
    inner.appendChild(el("div", { cls: "field" }, [el("span", { cls: "field-label", text: "2. מתחברים" }), gBtn, gMsg]));
    box.addEventListener("toggle", () => { if (!box.open) clearTimeout(polling); });
    return box;
  }

  // ------------------------------------------------------------------ Telegram pairing
  function pairingBlock(ctx) {
    const wrap = el("div", { cls: "stack" });
    const status = el("p", { cls: "form-msg", role: "status" });
    const go = el("button", { type: "button", cls: "btn btn-big btn-primary" }, [icon("key", "ic-sm"), "תן לי קוד"]);
    const out = el("div", { cls: "stack" });
    let timer = null;
    let issuedAt = Infinity;  // server time of the code on screen: only a pairing after it counts
    const stop = () => clearTimeout(timer);
    const watch = async (deadline) => {
      try {
        const s = await api("/api/telegram/state");
        if (s.paired && Number(s.paired_at || 0) >= issuedAt) {
          stop(); reloadConnections(false);
          ctx.done(["הטלגרם שלך מחובר ✓" + (s.paired_name ? " — " + s.paired_name : ""), "מעכשיו כל קליפ מוכן יגיע אליך עם כפתור \"אשר הכל\"."]);
          return;
        }
        if (s.state === "busy_by_old_studio" || s.state === "bad_token") { status.className = "form-msg err"; status.textContent = s.message_he || ""; }
      } catch (_) { /* keep waiting */ }
      if (Date.now() < deadline && ctx.dialog.isOpen()) timer = setTimeout(() => watch(deadline), 2000);
    };
    go.addEventListener("click", async () => {
      busy(go, true, "מכין קוד…");
      try {
        const r = await api("/api/telegram/pair", { method: "POST" });
        issuedAt = Number(r.issued_at) || 0;
        clear(out);
        const cmd = "/start " + r.code;
        const bot = typeof r.bot === "string" ? r.bot.replace(/^@/, "") : "";
        append(out, [el("div", { cls: "pair-code", "aria-label": "קוד צימוד", text: r.code }),
          P("פותחים את הבוט בטלגרם ושולחים לו את השורה הזאת:"), copyRow(cmd),
          /^[A-Za-z0-9_]{3,64}$/.test(bot) ? extLink("https://t.me/" + bot, "פתח את הבוט בטלגרם") : null,
          el("p", { cls: "muted small", text: "הקוד תקף לכמה דקות. אני מחכה כאן לחיבור…" })]);
        if (r.status && r.status.state === "busy_by_old_studio") { status.className = "form-msg err"; status.textContent = r.status.message_he || ""; }
        stop();
        watch(Date.now() + Math.max(60, Number(r.expires_in_s) || 600) * 1000);
      } catch (e) { status.className = "form-msg err"; status.textContent = e.message; }
      finally { busy(go, false); go.textContent = ""; append(go, [icon("refresh", "ic-sm"), "קוד חדש"]); }
    });
    ctx.dialog.dlg.addEventListener("close", stop);
    const tg = (tileOf("telegram") || {}).telegram || {};
    append(wrap, [P("כדי שהבוט ידע שזה אתה — שולחים לו קוד חד-פעמי."),
      tg.paired ? note("קוד חדש מנתק מיד את הטלגרם שמחובר עכשיו" + (tg.paired_name ? " (" + tg.paired_name + ")" : "") + ", עד שתשלח את הקוד מהטלגרם החדש.", true) : null,
      go, out, status]);
    return wrap;
  }

  // ------------------------------------------------------------------ the wizards
  const WIZARDS = {
    zernio: (t) => ({
      title: "חיבור יוטיוב וטיקטוק", pico: t.key === "tiktok" ? "tiktok" : "youtube",
      verifyKeys: ["youtube", "tiktok"], disconnectName: t.name_he,
      disconnectText: "יוטיוב וטיקטוק עולים עם אותו מפתח של Zernio, אז הניתוק מנתק את שניהם.",
      steps: [
        { title: "נכנסים ל-Zernio", render: (w) => append(w, [
          P("יוטיוב וטיקטוק עולים דרך Zernio — שירות שהחשבונות שלך כבר מחוברים אליו. מפתח אחד מחזיר את שניהם, בלי להתחבר מחדש."),
          extLink("https://zernio.com", "פתח את Zernio"),
          P("נכנסים עם החשבון הרגיל שלך."),
          t.key !== "tiktok" ? googleBox(false) : null]) },
        { title: "יוצרים מפתח", render: (w) => append(w, [
          P("למעלה נכנסים ל-", ext("Settings"), " ואז ל-", ext("API Keys"), "."),
          P("יוצרים מפתח חדש ומעתיקים אותו."),
          note("אם יוטיוב או טיקטוק לא מופיעים שם כמחוברים — מחברים אותם שם פעם אחת, וזהו.")]) },
        keysStep("מדביקים כאן", t.key === "youtube_shorts" ? "youtube" : t.key, ["ZERNIO_API_KEY"], {
          after: async (ctx, res) => {
            // Only the tile in the reply was really verified; the quick read of the others is
            // optimistic until checked, so success is decided by the verified tile alone.
            const tile = res.tile || {};
            if (tile.connected) {
              ctx.done([tile.status_he || "מחובר ✓", "המפתח הזה מחזיר את יוטיוב וטיקטוק יחד.",
                "כרגע במצב אימון. כשתרצה — מדליקים \"העלאה אמיתית\" בכרטיס."]);
            } else if (!savedUnchecked(ctx, tile)) ctx.data.msg.textContent = tile.problem || "לא הצלחתי להתחבר. בדוק שהעתקת את כל המפתח ונסה שוב.";
            reloadConnections(true);
            refreshHome();
          },
        }),
      ],
      manage: (body) => { if (t.key !== "tiktok") body.appendChild(googleBox(false)); },
    }),

    youtube_direct: (t) => ({
      title: "חיבור יוטיוב ישירות", pico: "youtube",
      steps: [{ title: "מתחברים עם גוגל", render: (w) => append(w, [googleBox(true)]) }],
      manage: (body) => body.appendChild(googleBox(false)),
    }),

    meta: (t) => ({
      title: "חיבור אינסטגרם ופייסבוק", pico: t.key,
      verifyKeys: ["instagram_reels", "facebook"], disconnectKeys: ["instagram_reels", "facebook"], disconnectName: "אינסטגרם ופייסבוק",
      steps: [
        { title: "פותחים את הכלי של מטא", render: (w) => append(w, [
          P("אינסטגרם ופייסבוק עולים דרך האפליקציה שלך במטא. צריך מפתח עמוד אחד — והוא לא פג."),
          extLink("https://developers.facebook.com/tools/explorer", "פתח את Graph API Explorer"),
          P("בצד בוחרים את האפליקציה ", ext("Imagination Studio"), ".")]) },
        { title: "מבקשים מפתח לעמוד", render: (w) => append(w, [
          P("לוחצים ", ext("Get Token"), " ← ", ext("Get Page Access Token"), ", ומאשרים את העמוד ", ext("Imaginationcs2"), " ואת האינסטגרם."),
          P("מסמנים את ההרשאות האלה:"),
          el("div", { cls: "chips" }, ["instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement", "pages_manage_posts"].map(code)),
          P("ולוחצים ", ext("Generate Access Token"), ".")]) },
        { title: "הופכים אותו לקבוע", render: (w) => append(w, [
          P("פותחים את ", ext("Access Token Debugger"), ", מדביקים ולוחצים ", ext("Extend Access Token"), "."),
          extLink("https://developers.facebook.com/tools/debug/accesstoken", "פתח את Access Token Debugger"),
          P("חוזרים ל-Explorer, שמים את המפתח הארוך ומריצים ", code("me/accounts"), ". ליד העמוד שלך מופיע ", ext("access_token"), " — זה המפתח הקבוע. מעתיקים אותו.")]) },
        {
          title: "מדביקים כאן", keys: true,
          render: (w, ctx) => {
            const ig = tileOf("instagram_reels") || {}, fb = tileOf("facebook") || {};
            const missing = [...(ig.missing || []), ...(fb.missing || [])];
            const names = ["META_ACCESS_TOKEN"];
            if ((fb.missing || []).includes("FB_PAGE_ID")) names.push("FB_PAGE_ID");
            if ((ig.missing || []).includes("IG_USER_ID")) names.push("IG_USER_ID");
            ctx.data.fields = names.map((n) => keyField(specOf(n), missing));
            for (const f of ctx.data.fields) w.appendChild(f.field);
            if (names.length === 1) w.appendChild(note("מספרי העמוד והאינסטגרם כבר שמורים ✓"));
            w.appendChild(note("נשמר רק במחשב הזה. אף אחד לא רואה את המפתח — גם לא אנחנו."));
            ctx.data.msg = el("p", { cls: "form-msg err", role: "alert" });
            w.appendChild(ctx.data.msg);
          },
          action: {
            label: "חבר ובדוק", busy: "שומר ובודק…",
            run: async (ctx) => {
              const { values, firstEmpty } = collect(ctx.data.fields);
              if (firstEmpty) { firstEmpty.input.setAttribute("aria-invalid", "true"); firstEmpty.input.focus(); ctx.data.msg.textContent = "חסר עוד שדה אחד."; return; }
              if (!Object.keys(values).length) { ctx.data.msg.textContent = "לא הודבק כלום."; return; }
              ctx.data.msg.textContent = "";
              try {
                const igVals = {}, fbVals = {};
                if (values.META_ACCESS_TOKEN) igVals.META_ACCESS_TOKEN = values.META_ACCESS_TOKEN;
                if (values.IG_USER_ID) igVals.IG_USER_ID = values.IG_USER_ID;
                if (values.FB_PAGE_ID) fbVals.FB_PAGE_ID = values.FB_PAGE_ID;
                let igTile = null, fbTile = null;
                if (Object.keys(igVals).length) igTile = (await saveKeys("instagram_reels", igVals)).tile;
                if (Object.keys(fbVals).length) fbTile = (await saveKeys("facebook", fbVals)).tile;
                else fbTile = (await api("/api/connect/facebook/verify", { method: "POST" })).tile;
                if (!igTile) igTile = (await api("/api/connect/instagram_reels/verify", { method: "POST" })).tile;
                wipe(ctx.data.fields);
                await reloadConnections(false);
                if ((igTile && igTile.connected) || (fbTile && fbTile.connected)) {
                  ctx.done(["החיבור למטא עובד ✓", "אינסטגרם: " + ((igTile && igTile.status_he) || "") + " · פייסבוק: " + ((fbTile && fbTile.status_he) || ""),
                    "כרגע במצב אימון. כשתרצה — מדליקים \"העלאה אמיתית\" בכרטיס."]);
                } else if (!savedUnchecked(ctx, igTile) && !savedUnchecked(ctx, fbTile)) {
                  ctx.data.msg.textContent = (igTile && igTile.problem) || (fbTile && fbTile.problem) || "לא הצלחתי להתחבר. בדוק את המפתח ונסה שוב.";
                }
                reloadConnections(true);
                refreshHome();
              } catch (e) { ctx.data.msg.textContent = e.message; }
            },
          },
        },
      ],
    }),

    telegram: (t) => ({
      title: "אישורים בטלגרם", pico: "telegram", replaceLabel: "החלף את המפתח של הבוט",
      steps: [
        { title: "פותחים את BotFather", render: (w) => append(w, [
          P("הבוט שלך שולח כל קליפ מוכן עם כפתור \"אשר הכל\". צריך רק את המפתח שלו."),
          extLink("https://t.me/BotFather", "פתח את BotFather בטלגרם"),
          P("זה הבוט הרשמי של טלגרם לניהול בוטים.")]) },
        { title: "מעתיקים את המפתח של הבוט", render: (w) => append(w, [
          P("שולחים לו ", code("/mybots"), ", בוחרים את ", ext("@imagination_control_bot"), " ולוחצים ", ext("API Token"), "."),
          P("מעתיקים את השורה הארוכה שהוא שולח.")]) },
        keysStep("מדביקים כאן", "telegram", ["TELEGRAM_BOT_TOKEN"], {
          after: async (ctx, res) => {
            const tile = res.tile || {};
            if (tile.connected) ctx.next();
            else if (!savedUnchecked(ctx, tile)) ctx.data.msg.textContent = tile.problem || "המפתח של הבוט לא עבד. בדוק שהעתקת את כולו ונסה שוב.";
          },
        }),
        { title: "מצמידים את הטלגרם שלך", render: (w, ctx) => w.appendChild(pairingBlock(ctx)) },
      ],
      manage: (body, ctx) => {
        const tg = (tileOf("telegram") || {}).telegram || {};
        if (tg.message_he) body.appendChild(el("span", { cls: "status-line " + (tg.state === "running" ? "ok" : "warn") }, [el("span", { cls: "sd" }), tg.message_he]));
        if (tg.paired && tg.paired_name) body.appendChild(el("p", { cls: "muted small", text: "מחובר לטלגרם של " + tg.paired_name }));
        body.appendChild(el("button", { type: "button", cls: "btn btn-steel", on: { click: () => ctx.show(3) } }, [icon("key", "ic-sm"), tg.paired ? "צימוד מחדש" : "צימוד לטלגרם שלי"]));
      },
    }),

    x: (t) => ({
      title: "חיבור X", pico: "x",
      steps: [
        { title: "נכנסים לפורטל של X", render: (w) => append(w, [
          extLink("https://developer.x.com", "פתח את developer.x.com"),
          P("נכנסים עם חשבון ה-X של הערוץ, ופותחים את האפליקציה שלך (או יוצרים אחת).")]) },
        { title: "נותנים הרשאה לפרסם", render: (w) => append(w, [
          P(ext("User authentication settings"), " ← ", ext("Set up"), ", ובוחרים ", ext("Read and write"), "."),
          P("בשאר השדות אפשר לשים את כתובת הערוץ.")]) },
        { title: "מעתיקים 4 מפתחות", render: (w) => append(w, [
          P("בלשונית ", ext("Keys and tokens"), ": תחת ", ext("Consumer Keys"), " לוחצים ", ext("Regenerate"), ", ותחת ", ext("Access Token and Secret"), " לוחצים ", ext("Generate"), "."),
          note("חשוב: יוצרים את שני האחרונים אחרי שבחרת Read and write — אחרת אי אפשר לפרסם.", true)]) },
        keysStep("מדביקים כאן", "x", ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"]),
      ],
    }),

    reddit: (t) => ({
      title: "חיבור רדיט", pico: "reddit",
      steps: [
        { title: "יוצרים אפליקציה קטנה ברדיט", render: (w) => append(w, [
          extLink("https://www.reddit.com/prefs/apps", "פתח את הגדרות האפליקציות ברדיט"),
          P("מתחברים עם החשבון שלך, גוללים למטה ולוחצים ", ext("create another app..."), "."),
          note("ברדיט מפרסמים רק בקהילות שאישרת מראש, ובלי ספאם. ככה לא נחסמים.")]) },
        { title: "ממלאים שלושה שדות", render: (w) => append(w, [
          P("שם: ", code("imagination"), " · סוג: ", ext("script"), " · ובשדה ", ext("redirect uri"), " כותבים ", code("http://localhost:8080"), "."),
          P("לוחצים ", ext("create app"), ".")]) },
        keysStep("מדביקים כאן", "reddit", ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD"], {
          extra: (w) => w.appendChild(note("אם יש לך אימות דו-שלבי ברדיט, החיבור הזה לא יעבוד.", true)),
        }),
      ],
    }),

    discord: (t) => ({
      title: "חיבור דיסקורד", pico: "discord",
      steps: [
        { title: "פותחים את הגדרות הערוץ", render: (w) => append(w, [
          P("בשרת הדיסקורד שלך לוחצים על גלגל השיניים ליד הערוץ שאליו יגיעו ההודעות (למשל #קליפים-חדשים).")]) },
        { title: "יוצרים כתובת לערוץ", render: (w) => append(w, [
          P(ext("Integrations"), " ← ", ext("Webhooks"), " ← ", ext("New Webhook"), "."),
          P("נותנים שם (Imagination) ולוחצים ", ext("Copy Webhook URL"), ".")]) },
        keysStep("מדביקים כאן", "discord", ["DISCORD_WEBHOOK_URL"]),
      ],
    }),

    generic: (t) => ({
      title: "חיבור " + (t.name_he || t.key), pico: t.key,
      steps: [keysStep("מדביקים כאן", t.key, (t.required || []).filter((n) => n !== "YOUTUBE_CLIENT_SECRETS_FILE"))],
    }),
  };
  /* WIZARD-COPY:END */

  // ------------------------------------------------------------------ import existing connections
  const NAME_HE = {
    ZERNIO_API_KEY: "Zernio (יוטיוב וטיקטוק)", META_ACCESS_TOKEN: "מטא", IG_USER_ID: "אינסטגרם", FB_PAGE_ID: "פייסבוק",
    TELEGRAM_BOT_TOKEN: "בוט הטלגרם", TIKTOK_ACCESS_TOKEN: "טיקטוק", DISCORD_WEBHOOK_URL: "דיסקורד",
    ANTHROPIC_API_KEY: "המוח של הצוות", YOUTUBE_CLIENT_SECRETS_FILE: "יוטיוב", YOUTUBE_OAUTH_JSON: "יוטיוב",
  };
  const nameHe = (n) => NAME_HE[n] || (/^X_/.test(n) ? "X" : /^REDDIT_/.test(n) ? "רדיט" : "חיבור נוסף");
  function foundLabel(f) {
    const names = Array.from(new Set((f.names || []).map(nameHe)));
    switch (f.kind) {
      case "studio": return "הסטודיו הקיים — החיבורים, הסגנון, שעות ההעלאה והקליפים שכבר עלו";
      case "google_client_secret": return "קובץ ההתחברות של גוגל";
      case "google_token": return f.ok ? "חיבור קיים ליוטיוב" : "חיבור ישן ליוטיוב — צריך להתחבר מחדש";
      case "token_pickle": return "חיבור ישן מאוד ליוטיוב — צריך להתחבר מחדש";
      case "env_file": return "חיבורים שמורים בקובץ: " + (names.join(", ") || "—");
      default: return "חיבור שמצאתי";
    }
  }
  /** "הבאתי: ..." — what the studio import brought over, in plain words (no setting names). */
  function studioImportedHe(r) {
    const keys = (r.imported || []).map(String);
    const has = (prefix) => keys.some((k) => k === prefix || k.startsWith(prefix + "."));
    const parts = [];
    const linked = (r.linked || []).filter((k) => k !== "youtube_shorts").map((k) => PLATFORMS[k]).filter(Boolean);
    if (linked.length) parts.push("החיבורים ל" + linked.join(", "));
    if (has("meta")) parts.push("מספרי העמוד והאינסטגרם");
    if (has("brand")) parts.push("הסגנון וההאשטגים");
    if (has("publishing.default_times")) parts.push("שעות ההעלאה");
    const n = r.history && Number(r.history.imported);
    if (n > 0) parts.push(n === 1 ? "פרסום קודם אחד" : n + " פרסומים קודמים");
    return parts.length ? "הבאתי: " + parts.join(" · ") + " ✓" : "";
  }
  const NEXT_HE = {
    ZERNIO_API_KEY: ["youtube", "להדביק מפתח Zernio — מחזיר את יוטיוב וטיקטוק"],
    META_ACCESS_TOKEN: ["facebook", "להדביק את מפתח העמוד של מטא — מחזיר את פייסבוק ואינסטגרם"],
    TELEGRAM_BOT_TOKEN: ["telegram", "לחבר את בוט הטלגרם — אישורים בלחיצה אחת"],
  };

  function openImport() {
    if (!IS_PC) { toast(PC_ONLY_HE, "warn"); return; }
    const d = openDialog({ title: "ייבוא חיבורים קיימים", wide: true, iconNode: el("span", { cls: "import-ic" }, [icon("download")]) });
    let items = [];
    async function scan(extraPath) {
      clear(d.body);
      setFoot(d, []);
      d.body.appendChild(el("div", { cls: "empty" }, [el("span", { cls: "spin", "aria-hidden": "true" }), el("strong", { text: "מחפש במחשב חיבורים קיימים…" }),
        el("span", { text: "זה לוקח כמה שניות. שום דבר לא משתנה עד שתאשר." })]));
      try {
        const res = await api("/api/connect/import/scan", { json: extraPath ? { extra_path: extraPath } : {} });
        items = res.items || [];
        renderList(res.studio || {});
      } catch (e) {
        clear(d.body);
        d.body.appendChild(note(e.message, true));
        setFoot(d, [el("button", { type: "button", cls: "btn btn-big btn-primary", text: "לנסות שוב", on: { click: () => scan(extraPath) } })]);
      }
    }
    function renderList(studio) {
      clear(d.body);
      if (studio.found) {
        const msg = String(studio.message_he || "");
        d.body.appendChild(note(msg.startsWith("מצאתי") ? msg : [el("b", { text: "מצאתי את הסטודיו. " }), msg]));
      }
      const checks = [];
      if (!items.length) {
        d.body.appendChild(el("div", { cls: "empty" }, [el("span", { cls: "empty-ic" }, [icon("folder")]), el("strong", { text: "לא מצאתי חיבורים קיימים" }),
          el("span", { text: "אפשר לחפש בתיקייה מסוימת, או פשוט לחבר כל פלטפורמה בכרטיס שלה." })]));
      } else {
        const list = el("div", { cls: "found-list", role: "group", "aria-label": "מה מצאתי" });
        for (const f of items) {
          const cb = el("input", { type: "checkbox", checked: !!f.selected && !!f.ok, disabled: !f.ok, "aria-describedby": "fp-" + f.id });
          cb.dataset.id = f.id;
          checks.push(cb);
          list.appendChild(el("label", { cls: "found" + (f.ok ? "" : " disabled") }, [cb, el("span", { cls: "txt" }, [
            el("b", { text: foundLabel(f) }),
            f.problem ? el("span", { cls: "prob", text: f.problem }) : null,
            el("span", { cls: "path", id: "fp-" + f.id, text: f.path || "" })])]));
        }
        d.body.appendChild(list);
      }
      const pathIn = el("input", { cls: "input ltr", placeholder: "C:\\CS2_STUDIO", "aria-label": "תיקייה לחיפוש", maxlength: "1000" });
      const more = el("details", { cls: "advanced-box" }, [el("summary", {}, [icon("folder"), "לחפש גם בתיקייה אחרת", icon("chevron-down", "ic-sm")]),
        el("div", { cls: "inner" }, [el("div", { cls: "row" }, [pathIn, el("button", { type: "button", cls: "btn btn-ghost", text: "חפש",
          on: { click: () => { const p = pathIn.value.trim(); if (p) scan(p); } } })])])]);
      d.body.appendChild(more);
      const go = el("button", { type: "button", cls: "btn btn-big btn-primary", text: "ייבא את מה שסימנתי" });
      const sync = () => { const n = checks.filter((c) => c.checked).length; go.disabled = !n; setText(go, n ? "ייבא את מה שסימנתי (" + n + ")" : "סמן מה לייבא"); };
      checks.forEach((c) => c.addEventListener("change", sync));
      sync();
      go.addEventListener("click", () => apply(checks.filter((c) => c.checked).map((c) => c.dataset.id), go));
      setFoot(d, items.length ? [go, el("button", { type: "button", cls: "btn btn-big btn-ghost", text: "ביטול", on: { click: () => d.close() } })]
        : [el("button", { type: "button", cls: "btn btn-big btn-primary", text: "סגור", on: { click: () => d.close() } })]);
    }
    async function apply(ids, btn) {
      busy(btn, true, "מייבא…");
      try {
        const res = await api("/api/connect/import/apply", { json: { ids } });
        renderResults(res);
        await reloadConnections(false);
        refreshHome();
        reloadConnections(true);
      } catch (e) { busy(btn, false); toast(e.message, "err"); }
    }
    function renderResults(res) {
      clear(d.body);
      const byId = new Map(items.map((f) => [f.id, f]));
      const rows = el("div", {});
      for (const r of res.results || []) {
        const f = byId.get(r.id) || { kind: r.kind, names: [] };
        const ok = (r.imported || []).length > 0 && !r.problem;
        let text = r.problem ? friendlyError(400, { detail: String(r.problem) }) : ok ? "יובא ✓" : (r.skipped || []).length ? "כבר היה שמור — לא שיניתי" : "לא היה מה לייבא";
        if (ok && (r.kind || f.kind) === "studio") text = studioImportedHe(r) || text;
        rows.appendChild(el("div", { cls: "result-row " + (ok ? "ok" : "bad") }, [icon(ok ? "check" : "info"),
          el("div", {}, [el("b", { text: foundLabel(f) }), el("div", { cls: "muted small", text }),
            r.path || f.path ? el("span", { cls: "path", text: r.path || f.path }) : null])]));
      }
      d.body.appendChild(el("h3", { text: "הנה מה שקרה" }));
      d.body.appendChild(rows);
      const steps = (res.next_connect || []).filter((s) => NEXT_HE[s.name]);
      const buttons = [];
      if (steps.length) {
        d.body.appendChild(el("h3", { text: "נשאר רק:" }));
        const list = el("div", { cls: "next-steps" });
        const keys = steps.map((s) => NEXT_HE[s.name][0]);
        steps.forEach((s, i) => {
          list.appendChild(el("button", { type: "button", cls: "next-step", on: { click: () => { d.close(); setTimeout(() => startChain(keys.slice(i)), 230); } } },
            [el("span", { cls: "num", text: String(i + 1) }), NEXT_HE[s.name][1], el("span", { cls: "chev", "aria-hidden": "true" }, [icon("chevron", "ic-sm")])]));
        });
        d.body.appendChild(list);
        buttons.push(el("button", { type: "button", cls: "btn btn-big btn-primary", text: "בוא נתחיל",
          on: { click: () => { d.close(); setTimeout(() => startChain(keys), 230); } } }));
      } else {
        d.body.appendChild(note("זהו, הכל מקושר. כשתרצה — מדליקים \"העלאה אמיתית\" בכרטיסים."));
      }
      buttons.push(el("button", { type: "button", cls: "btn btn-big " + (buttons.length ? "btn-ghost" : "btn-primary"), text: "סיום", on: { click: () => d.close() } }));
      setFoot(d, buttons);
    }
    scan("");
  }

  // ================================================================== SETTINGS
  let settings = null;
  let base = {};       // flattened values as loaded
  const FORM_FIELDS = ["channel.name", "channel.handle", "brand.bio", "brand.hashtags", "brand.blocked_words",
    "brand.style_notes", "brand.ai_label", "brand.music_credit", "publishing.mode", "publishing.quiet_hours.enabled",
    "publishing.quiet_hours.start", "publishing.quiet_hours.end", "finance.monthly_budget_ils", "finance.max_single_spend_ils"];
  const getPath = (obj, dotted) => dotted.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
  function nest(pairs) {
    const out = {};
    for (const [dotted, v] of pairs) {
      const parts = dotted.split(".");
      let node = out;
      parts.slice(0, -1).forEach((p) => { node = node[p] = node[p] || {}; });
      node[parts[parts.length - 1]] = v;
    }
    return out;
  }
  const form = () => $("settings-form");
  const fieldEl = (name) => form().querySelector("[name=\"" + name + "\"]");
  const switchEl = (name) => form().querySelector(".switch[data-name=\"" + name + "\"]");

  function readForm() {
    const out = {};
    for (const name of FORM_FIELDS) {
      const sw = switchEl(name);
      if (sw) { out[name] = sw.getAttribute("aria-checked") === "true"; continue; }
      if (name === "publishing.mode") { const r = form().querySelector("[name=\"publishing.mode\"]:checked"); out[name] = r ? r.value : "approve"; continue; }
      const f = fieldEl(name);
      if (!f) continue;
      const v = f.value;
      if (name === "brand.hashtags") out[name] = v.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean).map((s) => (s.startsWith("#") ? s : "#" + s));
      else if (name === "brand.blocked_words") out[name] = v.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
      else if (name === "brand.ai_label") out[name] = v.trim() ? v.trim() : false;
      else if (f.type === "number") out[name] = v === "" ? null : Number(v);
      else out[name] = v.trim();
    }
    return out;
  }
  function fillForm(s) {
    for (const name of FORM_FIELDS) {
      const v = getPath(s, name);
      const sw = switchEl(name);
      if (sw) { sw.setAttribute("aria-checked", String(v === true || (typeof v === "string" && v !== ""))); continue; }
      if (name === "publishing.mode") {
        form().querySelectorAll("[name=\"publishing.mode\"]").forEach((r) => { r.checked = r.value === (v === "auto" ? "auto" : "approve"); });
        continue;
      }
      const f = fieldEl(name);
      if (!f) continue;
      if (Array.isArray(v)) f.value = v.join(name === "brand.blocked_words" ? "\n" : " ");
      else if (v === false || v === null || v === undefined) f.value = "";
      else f.value = String(v);
    }
    base = readForm();
    syncQuiet();
    syncDirty();
  }
  function syncQuiet() {
    const on = switchEl("publishing.quiet_hours.enabled").getAttribute("aria-checked") === "true";
    $("quiet-times").classList.toggle("hidden", !on);
  }
  function dirtyPairs() {
    const now = readForm();
    return FORM_FIELDS.filter((k) => JSON.stringify(now[k]) !== JSON.stringify(base[k])).map((k) => [k, now[k]]);
  }
  function syncDirty() {
    const n = dirtyPairs().length;
    const bar = $("save-bar");
    bar.classList.toggle("on", n > 0 && route.page === "settings");
    bar.setAttribute("aria-hidden", String(!(n > 0)));
    BODY.classList.toggle("saving-open", n > 0 && route.page === "settings");
    setText($("save-bar-text"), n === 1 ? "שינוי אחד שלא נשמר" : n + " שינויים שלא נשמרו");
  }

  async function loadSettings() {
    renderLocalNote();
    syncAdvanced();
    try {
      settings = await api("/api/settings");
      fillForm(settings);
      renderLiveSwitches(settings);
    } catch (e) { toast(e.message, "err"); }
    loadTelegramState();
    if (settings && getPath(settings, "dashboard.remote_access")) loadRemote(false);
    else clear($("phone-panel"));
  }
  function renderLocalNote() {
    const box = clear($("set-local-note"));
    if (!IS_PC) box.appendChild(el("div", { cls: "banner banner-steel" }, [icon("phone", "ic-lg"),
      el("div", { cls: "banner-text" }, [el("strong", { text: "אתה בטלפון" }), el("span", { text: "גישה מהטלפון ותיקיית הקליפים משנים רק מהמחשב." })])]));
  }
  function renderLiveSwitches(s) {
    switchEl("inbox.enabled").setAttribute("aria-checked", String(!!getPath(s, "inbox.enabled")));
    switchEl("notify.telegram.enabled").setAttribute("aria-checked", String(!!getPath(s, "notify.telegram.enabled")));
    const remote = !!getPath(s, "dashboard.remote_access");
    const rs = switchEl("dashboard.remote_access");
    rs.setAttribute("aria-checked", String(remote));
    rs.disabled = !IS_PC;
    const info = s.info || {};
    setText($("phone-sub"), remote ? "פעיל · " + (info.remote_devices === 1 ? "מכשיר אחד מחובר" : (info.remote_devices || 0) + " מכשירים מחוברים") : "כבוי");
    const folders = info.inbox_folders_in_use || [];
    setText($("inbox-folder-now"), folders.length ? folders[0] : "");
    $("inbox-folder").value = (getPath(s, "inbox.folders") || [])[0] || "";
    $("inbox-folder").disabled = !IS_PC;
    $("inbox-folder-save").disabled = !IS_PC;
  }
  async function loadTelegramState() {
    const line = $("tg-status");
    try {
      const s = await api("/api/telegram/state");
      clear(line);
      line.className = "status-line " + (s.state === "running" ? "ok" : s.state === "off" ? "" : s.state === "bad_token" || s.state === "error" ? "bad" : "warn");
      append(line, [el("span", { cls: "sd" }), s.message_he || ""]);
    } catch (_) { /* optional */ }
  }

  async function putSettings(patchPairs) {
    const res = await api("/api/settings", { method: "PUT", json: nest(patchPairs) });
    settings = res.settings || settings;
    return res;
  }

  async function saveForm() {
    const pairs = dirtyPairs();
    if (!pairs.length) return;
    for (const [k, v] of pairs) {
      if ((k === "finance.monthly_budget_ils" || k === "finance.max_single_spend_ils") && (v === null || !isFinite(v) || v < 0)) {
        toast("הסכום צריך להיות מספר, אפס ומעלה.", "err"); fieldEl(k).focus(); return;
      }
    }
    const mode = pairs.find(([k]) => k === "publishing.mode");
    if (mode && mode[1] === "auto") {
      const ok = await confirmDialog({ title: "שהצוות יעלה לבד?", text: "מעכשיו הצוות יתזמן העלאות בלי לשאול אותך. הוצאות כסף תמיד יחכו לאישור שלך.", yes: "כן, תעלה לבד", no: "לא, אני מאשר" });
      if (!ok) return;
    }
    const b = $("save-go");
    busy(b, true, "שומר…");
    try {
      const cleanPairs = pairs.map(([k, v]) => [k, v === null ? 0 : v]);
      await putSettings(cleanPairs);
      fillForm(settings);
      renderLiveSwitches(settings);
      toast("נשמר ✓", "ok", { ms: 2500 });
      refreshHome();
    } catch (e) { toast(e.message, "err"); }
    finally { busy(b, false); syncDirty(); }
  }

  async function liveSet(name, on, sw) {
    if (name === "dashboard.remote_access") {
      if (!IS_PC) { toast(PC_ONLY_HE, "warn"); return; }
      if (!on) {
        const ok = await confirmDialog({ title: "לכבות את הגישה מהטלפון?", text: "כל הטלפונים יתנתקו, והקוד יימחק.", yes: "כן, לכבות", no: "ביטול", danger: true });
        if (!ok) return;
      }
    }
    sw.disabled = true;
    try {
      await putSettings([[name, on]]);
      renderLiveSwitches(settings);
      if (name === "dashboard.remote_access") {
        if (on) await loadRemote(true); else { clear($("phone-panel")); toast("הגישה מהטלפון כבויה. כל הטלפונים נותקו.", "info"); }
      } else if (name === "inbox.enabled") {
        toast(on ? "התיקייה פעילה. קליפים שכבר היו בה מחכים ב\"קליפים קודמים\"." : "הצוות כבר לא לוקח קליפים מהתיקייה.", on ? "ok" : "info", { ms: 7000 });
        refreshHome();
      } else if (name === "notify.telegram.enabled") {
        await loadTelegramState();
        const tile = conn ? tileOf("telegram") : null;
        if (on && (!tile || !tile.connected)) toast("צריך עוד לחבר את הבוט.", "info", { action: { label: "לחיבור", fn: () => { location.hash = "#/connections/telegram"; } } });
      }
    } catch (e) { toast(e.message, "err"); renderLiveSwitches(settings || {}); }
    finally { sw.disabled = name === "dashboard.remote_access" && !IS_PC; }
  }

  async function loadRemote(justTurnedOn) {
    if (!IS_PC) return;
    const panel = $("phone-panel");
    try {
      let r = await api("/api/remote");
      if (r.remote_enabled && !r.pin_set) {  // a PIN is made only by an explicit POST
        r = await api("/api/remote/pin", { method: "POST" });
        justTurnedOn = true;
      }
      renderPhone(r, justTurnedOn);
    } catch (e) { clear(panel); panel.appendChild(note(e.message, true)); }
  }
  function qrImg(svg) {
    if (typeof svg !== "string" || !/^<svg[\s>]/.test(svg) || svg.length > 200000) return null;
    const withNs = /xmlns=/.test(svg.slice(0, 300)) ? svg : svg.replace(/^<svg/, "<svg xmlns=\"" + SVG_NS + "\"");
    return el("img", { src: "data:image/svg+xml;charset=utf-8," + encodeURIComponent(withNs), alt: "קוד QR לפתיחה בטלפון", width: "160", height: "160" });
  }
  function renderPhone(r, fresh) {
    const panel = clear($("phone-panel"));
    if (!r.remote_enabled) return;
    const img = qrImg(r.qr_svg);
    const side = el("div", { cls: "stack" });
    if (r.pin) {
      append(side, [el("span", { cls: "field-label", text: "הקוד לטלפון" }), el("div", { cls: "pin", text: r.pin, "aria-label": "קוד: " + String(r.pin).split("").join(" ") }),
        el("p", { cls: "pin-note", text: "מוצג רק עכשיו. סורקים את הריבוע, ומקלידים את הקוד בטלפון. אחרי פעם אחת הטלפון זוכר לחודש." })]);
    } else if (r.pin_set) {
      append(side, [el("span", { cls: "field-label", text: "הקוד כבר הוצג פעם אחת" }), el("p", { cls: "pin-note", text: "שכחת אותו? קוד חדש — והטלפונים שכבר מחוברים נשארים מחוברים." })]);
    }
    if (r.url) side.appendChild(el("div", { cls: "ltr muted small", text: r.url }));
    if (!r.url) side.appendChild(note("לא מצאתי רשת Wi-Fi במחשב. בדוק שהמחשב מחובר לרשת של הבית.", true));
    if (r.restart_needed) side.appendChild(note(["כדי שהטלפון יוכל להיכנס צריך להפעיל את הסוכנות מחדש: בתפריט התחל פותחים ",
      el("b", { text: "Imagination Agency" }), " ולוחצים ", el("b", { text: "Restart Imagination Agency" }), " (או פשוט מפעילים את המחשב מחדש)."], true));
    const newPin = el("button", { type: "button", cls: "btn btn-sm btn-steel" }, [icon("key", "ic-sm"), "קוד חדש"]);
    newPin.addEventListener("click", async () => {
      busy(newPin, true, "מכין…");
      try { renderPhone(await api("/api/remote/pin", { method: "POST" }), true); } catch (e) { toast(e.message, "err"); busy(newPin, false); }
    });
    const revoke = el("button", { type: "button", cls: "btn btn-sm btn-danger" }, [icon("logout", "ic-sm"), "התנתק מכל המכשירים"]);
    revoke.addEventListener("click", async () => {
      const ok = await confirmDialog({ title: "לנתק את כל הטלפונים?", text: "כל מכשיר יצטרך להקליד קוד מחדש.", yes: "כן, לנתק", danger: true });
      if (!ok) return;
      try { const res = await api("/api/auth/revoke-all", { method: "POST" }); toast(res.revoked ? "נותקו " + res.revoked + " מכשירים." : "אין מכשירים מחוברים.", "info"); loadSettings(); }
      catch (e) { toast(e.message, "err"); }
    });
    side.appendChild(el("div", { cls: "row" }, [newPin, revoke]));
    panel.appendChild(el("div", { cls: "phone-box" }, [img ? el("div", { cls: "qr" }, [img]) : null, side]));
    if (fresh && r.pin) panel.querySelector(".pin").scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "center" });
  }

  function syncAdvanced() {
    const on = advancedOn();
    $("advanced-toggle").setAttribute("aria-checked", String(on));
    $("advanced-link").classList.toggle("hidden", !on);
    $("nav-advanced").classList.toggle("hidden", !on);
  }

  function setupSettings() {
    const f = form();
    f.addEventListener("input", syncDirty);
    f.addEventListener("change", (e) => {
      syncDirty();
      if (e.target && e.target.name === "publishing.mode") syncDirty();
    });
    f.addEventListener("submit", (e) => { e.preventDefault(); saveForm(); });
    f.querySelectorAll(".switch[data-name]").forEach((sw) => {
      sw.addEventListener("click", () => {
        const name = sw.dataset.name;
        const on = sw.getAttribute("aria-checked") !== "true";
        if (sw.classList.contains("live-set")) { liveSet(name, on, sw); return; }
        sw.setAttribute("aria-checked", String(on));
        if (name === "publishing.quiet_hours.enabled") syncQuiet();
        syncDirty();
      });
    });
    $("save-go").addEventListener("click", saveForm);
    $("save-cancel").addEventListener("click", () => { if (settings) fillForm(settings); });
    $("inbox-folder-save").addEventListener("click", async () => {
      const b = $("inbox-folder-save");
      const v = $("inbox-folder").value.trim();
      busy(b, true, "שומר…");
      try { await putSettings([["inbox.folders", v ? [v] : []]]); renderLiveSwitches(settings); toast(v ? "התיקייה נשמרה ✓" : "חוזרים לתיקייה הרגילה ✓", "ok"); }
      catch (e) { toast(e.message, "err"); }
      finally { busy(b, false); }
    });
    $("advanced-toggle").addEventListener("click", () => {
      store(LS_ADVANCED, advancedOn() ? null : "1");
      syncAdvanced();
      toast(advancedOn() ? "מצב מתקדם פועל. הפרטים הטכניים בדף המתקדם." : "מצב מתקדם כבוי.", "info", advancedOn() ? { action: { label: "לדף", fn: () => { location.hash = "#/advanced"; } } } : {});
    });
    window.addEventListener("beforeunload", (e) => { if (route.page === "settings" && dirtyPairs().length) { e.preventDefault(); e.returnValue = ""; } });
  }

  // ================================================================== ADVANCED (technical details live only here)
  /* ADVANCED:BEGIN */
  const STATUS_HE = {
    idle: "פנוי", working: "עובד", error: "שגיאה", pending: "ממתין", approved: "אושר", executing: "בביצוע",
    rejected: "נדחה", executed: "בוצע", new: "חדש", analyzed: "נותח", edited: "נערך", planned: "מתוכנן",
    awaiting_approval: "מחכה לאישור", scheduled: "מתוזמן", publishing: "בהעלאה", published: "פורסם", failed: "נכשל", cancelled: "בוטל",
  };
  const ACTOR_HE = {
    manager: "המנכ\"ל", analyst: "אנליסט וידאו", editor: "עורך", copywriter: "קופירייטר", strategist: "אסטרטג",
    publisher: "מפרסם", promoter: "מקדם", community: "קהילה", analytics: "אנליסט נתונים", finance: "כספים",
    owner: "אתה", scheduler: "מתזמן", pipeline: "צוות", inbox: "תיקייה", telegram: "טלגרם", importer: "ייבוא",
  };
  const CLI = [["מצב כללי", "agency status"], ["אישורים שמחכים", "agency approvals"], ["העלאת פוסטים שהגיע זמנם", "agency tick"],
    ["ייבוא מהסטודיו", "agency import"], ["מצב הפלטפורמות", "agency platforms"], ["דוח כספים חודשי", "agency report"]];
  let advBusy = false;
  async function loadAdvanced(quiet) {
    if (advBusy) return;
    const a = document.activeElement;
    if (quiet && a && $("adv-body").contains(a) && a.matches("input, textarea, select")) return;
    advBusy = true;
    const body = $("adv-body");
    if (!quiet && !body.firstChild) body.appendChild(el("div", { cls: "card adv-wide" }, [el("span", { cls: "skel skel-box" })]));
    try {
      const [events, videos, postsList, fin, plats] = await Promise.all([api("/api/events?limit=150"), api("/api/videos?limit=100"),
        api("/api/posts"), api("/api/finance"), api("/api/platforms")]);
      if (!changed("adv", [events, videos, postsList, fin, plats])) return;
      clear(body);
      append(body, [advEvents(events), advFinance(fin), advVideos(videos), advPosts(postsList), advPlatforms(plats), advCli()]);
    } catch (e) { if (!quiet) toast(e.message, "err"); }
    finally { advBusy = false; }
  }
  const card = (title, ic, children, wide) => el("section", { cls: "card" + (wide ? " adv-wide" : "") }, [el("h2", { cls: "sec-title" }, [icon(ic), title])].concat(children));
  function advEvents(list) {
    const ul = el("ul", { cls: "feed" });
    for (const e of list) {
      const lvl = ["info", "success", "warning", "error"].includes(e.level) ? e.level : "info";
      ul.appendChild(el("li", { cls: "lvl-" + lvl }, [el("span", { cls: "ev-dot", "aria-hidden": "true" }), el("span", { cls: "ev-msg", dir: "auto", text: e.message }),
        el("span", { cls: "ev-meta", text: (ACTOR_HE[e.actor] || e.actor) + " · " + fmtTime(e.ts) + (e.video_id ? " · סרטון #" + e.video_id : "") })]));
    }
    if (!list.length) ul.appendChild(el("li", { cls: "muted", text: "עוד אין פעילות." }));
    return card("יומן פעילות", "terminal", [ul]);
  }
  function advFinance(fin) {
    const b = fin.budget || {};
    const ul = el("ul", { cls: "ledger" });
    for (const e of fin.entries || []) {
      ul.appendChild(el("li", {}, [el("span", { dir: "auto", text: fmtTime(e.created_at) + " · " + (e.category || "כללי") + (e.description ? " · " + e.description : "") }),
        el("span", { cls: e.kind === "income" ? "pos" : "neg", text: (e.kind === "income" ? "+" : "−") + fmtIls(e.amount_ils) })]));
    }
    if (!(fin.entries || []).length) ul.appendChild(el("li", { cls: "muted", text: "אין תנועות החודש." }));
    const nums = el("div", { cls: "fin-nums" }, [["תקציב", b.budget], ["הוצאות", b.spent], ["הכנסות", b.income], ["נטו", b.net]].map(([l, v]) =>
      el("div", {}, [el("small", { text: l }), el("b", { text: fmtIls(v) })])));
    const open = el("button", { type: "button", cls: "btn btn-sm btn-steel", text: "לרשום הכנסה או הוצאה", on: { click: openFinance } });
    return card("תנועות כסף · " + (fin.month || ""), "wallet", [nums, el("p", { cls: "muted small set-sub", text: "תקרה להוצאה אחת: " + fmtIls(b.max_single) }), ul, el("div", { cls: "set-sub" }, [open])]);
  }
  function advVideos(list) {
    const tb = el("tbody");
    for (const v of list) {
      const err = v.meta && v.meta.error;
      tb.appendChild(el("tr", {}, [el("td", { "data-label": "#", text: v.id }),
        el("td", { cls: "wrap", "data-label": "סרטון" }, [el("div", { dir: "auto", text: v.title_hint || (v.meta && v.meta.original_name) || v.file_name }),
          el("div", { cls: "muted tiny ltr", text: v.file_name }), err ? el("div", { cls: "err-text", text: err }) : null]),
        el("td", { "data-label": "מצב", text: STATUS_HE[v.status] || v.status }),
        el("td", { "data-label": "פוסטים", text: (v.posts || []).map((p) => PLATFORMS[p.platform] || p.platform).join(", ") || "—" }),
        el("td", { "data-label": "נוצר", text: fmtTime(v.created_at) })]));
    }
    const table = el("div", { cls: "table-wrap" }, [el("table", { cls: "table" }, [el("thead", {}, [el("tr", {}, ["#", "סרטון", "מצב", "פוסטים", "נוצר"].map((h) => el("th", { text: h })))]), tb])]);
    return card("סרטונים", "film", list.length ? [table] : [el("p", { cls: "muted", text: "עוד אין סרטונים." })], true);
  }
  function advPosts(list) {
    const tb = el("tbody");
    for (const p of list.slice(0, 200)) {
      const href = p.url ? safeUrl(p.url) : null;
      let linkCell;
      if (href) linkCell = el("a", { href, target: "_blank", rel: "noopener noreferrer", text: "פתח" });
      else if (p.dry_run) linkCell = el("span", { cls: "muted small", text: "אימון — לא עלה" });
      else linkCell = el("span", { cls: "muted", text: "—" });
      const retry = p.status === "failed" ? el("button", { type: "button", cls: "btn btn-sm btn-steel", text: "נסה שוב" }) : null;
      if (retry) retry.addEventListener("click", () => retryPost(p, retry));
      tb.appendChild(el("tr", {}, [el("td", { "data-label": "#", text: p.id }), el("td", { "data-label": "פלטפורמה", text: PLATFORMS[p.platform] || p.platform }),
        el("td", { cls: "wrap", "data-label": "כותרת" }, [el("div", { dir: "auto", text: p.title || "—" }), p.error ? el("div", { cls: "err-text", text: p.error }) : null]),
        el("td", { "data-label": "מצב", text: STATUS_HE[p.status] || p.status }), el("td", { "data-label": "מתי", text: fmtTime(p.scheduled_at) }),
        el("td", { "data-label": "קישור" }, [linkCell, retry])]));
    }
    const table = el("div", { cls: "table-wrap" }, [el("table", { cls: "table" }, [el("thead", {}, [el("tr", {}, ["#", "פלטפורמה", "כותרת", "מצב", "מתי", "קישור"].map((h) => el("th", { text: h })))]), tb])]);
    return card("פוסטים", "rocket", list.length ? [table] : [el("p", { cls: "muted", text: "עוד אין פוסטים." })], true);
  }
  function advPlatforms(data) {
    const tb = el("tbody");
    for (const p of data.platforms || []) {
      tb.appendChild(el("tr", {}, [el("td", { "data-label": "פלטפורמה" }, [el("span", { cls: "row" }, [pico(p.key), p.display_he || p.key])]),
        el("td", { "data-label": "פעיל", text: p.enabled ? "כן" : "לא" }),
        el("td", { cls: "wrap", "data-label": "מפתחות" }, [p.configured ? el("span", { cls: "pos", text: "קיימים" }) : el("span", { cls: "ltr muted tiny", text: (p.missing_secrets || []).join(", ") || "—" })]),
        el("td", { "data-label": "דרך", cls: "ltr", text: p.via || "—" }),
        el("td", { "data-label": "מתג", text: p.live_switch ? "חי" : "אימון" }),
        el("td", { "data-label": "בפועל", text: p.live ? "מעלה באמת" : "אימון" }),
        el("td", { cls: "wrap", "data-label": "תקלה" }, [p.error ? el("span", { cls: "err-text", text: p.error }) : "—"])]));
    }
    const table = el("div", { cls: "table-wrap" }, [el("table", { cls: "table" }, [el("thead", {}, [el("tr", {}, ["פלטפורמה", "פעיל", "מפתחות", "דרך", "מתג", "בפועל", "תקלה"].map((h) => el("th", { text: h })))]), tb])]);
    return card("מצב טכני של הפלטפורמות" + (data.dry_run ? " · dry_run: true" : ""), "plug", [table], true);
  }
  function advCli() {
    return card("פקודות שימושיות (בחלון הפקודות, בתיקיית הסוכנות)", "terminal", [el("div", { cls: "cli" }, CLI.map(([label, cmd]) =>
      el("div", {}, [el("small", { cls: "muted", text: label }), copyRow(cmd)])))], true);
  }
  /* ADVANCED:END */

  // ================================================================== boot + polling
  function boot() {
    hydrateIcons(document);
    setupUpload();
    setupChat();
    setupSettings();
    syncAdvanced();
    $("money-line").addEventListener("click", openFinance);
    $("import-open").addEventListener("click", () => openImport());
    window.addEventListener("hashchange", go);
    go();
    if (route.page !== "home") refreshHome();
    refreshPosts();
    let tick = 0;
    setInterval(() => {
      tick += 1;
      if (document.hidden) {
        // in the background: slower, but still polling — the browser notification ("something
        // is waiting", "it went up") can only fire while the tab is hidden
        if (tick % HIDDEN_EVERY === 0) { refreshHome(); refreshPosts(); }
        return;
      }
      refreshHome();
      if (tick % POSTS_EVERY === 0) refreshPosts();
      if (route.page === "advanced" && tick % 2 === 0) loadAdvanced(true);
      if (route.page === "connections" && tick % RECHECK_EVERY === 0 && conn && !checking
          && (conn.tiles || []).some((t) => t.state === "unchecked")) reloadConnections(true);
    }, POLL_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { refreshHome(); refreshPosts(); } });
  }
  boot();
})();
