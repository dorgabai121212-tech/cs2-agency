/* CS2 Agency control room — vanilla JS, polling every 3s.
 * SECURITY: every piece of data (owner, LLM, platform) goes into the DOM via textContent /
 * attributes only. Never innerHTML with data. Links are restricted to http(s).
 */
(function () {
  "use strict";

  const POLL_MS = 3000;
  const HEADERS = { "X-Agency-Request": "1" };
  const ALLOWED_EXT = (document.body.dataset.ext || ".mp4 .mov .mkv .webm .avi").split(/\s+/).filter(Boolean);
  const MAX_BYTES = Math.floor(parseFloat(document.body.dataset.maxGb || "4") * 1024 ** 3);

  const STATUS_HE = {
    idle: "פנוי", working: "עובד", error: "שגיאה", pending: "ממתין", approved: "אושר", executing: "בביצוע",
    rejected: "נדחה", executed: "בוצע", new: "חדש", analyzed: "נותח", edited: "נערך",
    planned: "מתוכנן", awaiting_approval: "מחכה לאישור", scheduled: "מתוזמן",
    publishing: "בהעלאה", published: "פורסם", failed: "נכשל", cancelled: "בוטל",
  };
  const KIND_HE = { publish: "פרסום", spend: "הוצאה כספית", reply: "תגובה לצופה", other: "אחר" };
  const PLATFORM_HE = {
    youtube: "יוטיוב", youtube_shorts: "שורטס", tiktok: "טיקטוק", instagram_reels: "רילס",
    facebook: "פייסבוק", x: "X", reddit: "רדיט", discord: "דיסקורד",
  };
  const ACTOR_HE = {
    manager: 'מנכ"ל', analyst: "אנליסט וידאו", editor: "עורך", copywriter: "קופירייטר",
    strategist: "אסטרטג", publisher: "מפרסם", promoter: "מקדם", community: "קהילה",
    analytics: "אנליטיקס", finance: "כספים", owner: "אתה", scheduler: "מתזמן", pipeline: "צינור עבודה",
  };

  // ------------------------------------------------------------------ DOM helpers
  const $ = (id) => document.getElementById(id);

  /** Create an element. `props.text` → textContent, `props.cls` → className, others → attributes. */
  function el(tag, props, children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === "text") node.textContent = String(v);
        else if (k === "cls") node.className = v;
        else if (k === "on") for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
        else node.setAttribute(k, String(v));
      }
    }
    for (const c of [].concat(children || [])) {
      if (c === null || c === undefined || c === false) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function safeUrl(url) {
    try {
      const u = new URL(String(url), location.href);
      return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
    } catch (_) { return null; }
  }

  function fmtTime(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("he-IL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function fmtIls(n) {
    const v = Number(n || 0);
    return v.toLocaleString("he-IL", { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + " ₪";
  }

  function fmtBytes(n) {
    if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + "GB";
    if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + "MB";
    return Math.max(1, Math.round(n / 1024)) + "KB";
  }

  function he(v) { return STATUS_HE[v] || KIND_HE[v] || String(v || ""); }
  function pbadge(p) { return el("span", { cls: "pbadge p-" + String(p).replace(/[^a-z_]/g, ""), text: PLATFORM_HE[p] || p }); }
  function sbadge(s) { return el("span", { cls: "sbadge s-" + String(s).replace(/[^a-z_]/g, ""), text: he(s) }); }
  function dotClass(status) {
    const s = String(status || "").toLowerCase();
    if (s.includes("error") || s.includes("fail")) return "error";
    if (s === "idle" || s === "done" || s === "") return "idle";
    return "working";
  }

  let toastTimer = null;
  function toast(msg, kind) {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast " + (kind || "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), 4500);
  }

  // ------------------------------------------------------------------ API
  async function api(path, opts) {
    const o = Object.assign({ headers: {} }, opts || {});
    o.headers = Object.assign({}, HEADERS, o.headers);
    if (o.json !== undefined) {
      o.method = o.method || "POST";
      o.headers["Content-Type"] = "application/json";
      o.body = JSON.stringify(o.json);
      delete o.json;
    }
    const res = await fetch(path, o);
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      const detail = data && data.detail;
      const msg = typeof detail === "string" ? detail
        : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : "שגיאה " + res.status;
      throw new Error(msg);
    }
    return data;
  }

  // Re-render a section only when its data changed (keeps focus / typed notes intact).
  const lastJson = {};
  function changed(key, data) {
    const s = JSON.stringify(data);
    if (lastJson[key] === s) return false;
    lastJson[key] = s;
    return true;
  }

  // ------------------------------------------------------------------ status / org chart
  function personCard(m) {
    const initials = m.is_manager ? 'מנכ"ל' : (m.title_he || m.name || "?").trim().slice(0, 2);
    return el("div", { cls: "person" + (m.is_manager ? " manager" : "") }, [
      el("div", { cls: "avatar", "aria-hidden": "true", text: initials }),
      el("div", { cls: "person-body" }, [
        el("div", { cls: "person-title", text: m.title_he || m.name }),
        el("div", { cls: "status-line" }, [
          el("span", { cls: "dot " + dotClass(m.status), "aria-hidden": "true" }),
          el("span", { text: he(m.status) || m.status }),
        ]),
        m.detail ? el("div", { cls: "person-detail", text: m.detail }) : null,
        el("div", { cls: "person-name", text: m.name }),
      ]),
    ]);
  }

  function renderStatus(s) {
    $("dryrun-banner").classList.toggle("hidden", !s.dry_run);
    const mode = $("chip-mode");
    mode.textContent = s.mode === "auto" ? "פרסום: אוטומטי" : "פרסום: באישור שלך";
    mode.className = "chip " + (s.mode === "auto" ? "chip-warn" : "chip-blue");
    const llm = $("chip-llm");
    llm.textContent = s.llm_mock ? "מוח: מצב הדגמה" : "מוח: Claude";
    llm.className = "chip " + (s.llm_mock ? "chip-warn" : "chip-ok");
    $("chat-mode").textContent = s.llm_mock ? "מצב הדגמה — אין מפתח Claude" : "";
    const pending = (s.counts && s.counts.pending_approvals) || 0;
    const nb = $("nav-pending");
    nb.textContent = String(pending);
    nb.classList.toggle("hidden", pending === 0);
    document.title = (pending ? "(" + pending + ") " : "") + "חדר הבקרה · סוכנות CS2";

    if (changed("staff", s.staff)) {
      const mgr = $("org-manager"), staff = $("org-staff");
      clear(mgr); clear(staff);
      for (const m of s.staff || []) (m.is_manager ? mgr : staff).appendChild(personCard(m));
    }
    $("org-updated").textContent = "עודכן " + fmtTime(s.server_time);
    if (s.budget) renderBudget(s.budget);
  }

  // ------------------------------------------------------------------ finance
  function renderBudget(b) {
    $("fin-month").textContent = b.month || "";
    $("fin-budget").textContent = fmtIls(b.budget);
    $("fin-spent").textContent = fmtIls(b.spent);
    $("fin-income").textContent = fmtIls(b.income);
    const net = $("fin-net");
    net.textContent = fmtIls(b.net);
    net.className = Number(b.net) >= 0 ? "pos" : "neg";
    const budget = Number(b.budget || 0), spent = Number(b.spent || 0);
    const pct = budget > 0 ? Math.min(100, (spent / budget) * 100) : (spent > 0 ? 100 : 0);
    const bar = $("fin-bar");
    bar.style.width = pct.toFixed(1) + "%";
    bar.classList.toggle("over", budget > 0 ? spent > budget * 0.9 : spent > 0);
    $("fin-used").textContent = budget > 0 ? fmtIls(spent) + " / " + fmtIls(budget) : "אין תקציב קידום (0 ₪)";
    $("fin-cap").textContent = "תקרת הוצאה בודדת: " + fmtIls(b.max_single) +
      ". הוצאה מעל התקרה נחסמת גם אם אישרת.";
  }

  function renderLedger(fin) {
    if (!changed("ledger", fin.entries)) return;
    const ul = $("ledger-list");
    clear(ul);
    if (!fin.entries || !fin.entries.length) {
      ul.appendChild(el("li", { cls: "muted", text: "אין תנועות החודש." }));
      return;
    }
    for (const e of fin.entries) {
      const sign = e.kind === "income" ? "+" : "−";
      ul.appendChild(el("li", {}, [
        el("span", { text: (e.category || "כללי") + (e.description ? " · " + e.description : "") }),
        el("strong", { cls: e.kind === "income" ? "pos" : "neg", text: sign + fmtIls(e.amount_ils) }),
      ]));
    }
  }

  let moneyKind = "expense";
  function setupMoney() {
    document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      moneyKind = tab.dataset.tab === "income" ? "income" : "expense";
      $("money-btn").textContent = moneyKind === "income" ? "הוסף הכנסה" : "הוסף הוצאה";
      $("money-cat").placeholder = moneyKind === "income" ? "מקור (adsense, tiktok, חסות...)" : "קטגוריה (ads, software...)";
    }));
    $("money-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const status = $("money-status");
      const amount = parseFloat($("money-amount").value);
      if (!(amount > 0)) { status.textContent = "יש להזין סכום חיובי"; status.className = "form-status err"; return; }
      const cat = $("money-cat").value.trim();
      const body = { amount: amount, description: $("money-desc").value.trim() };
      if (moneyKind === "income") body.source = cat; else body.category = cat;
      $("money-btn").disabled = true;
      try {
        const res = await api("/api/finance/" + moneyKind, { json: body });
        status.textContent = moneyKind === "income" ? "ההכנסה נרשמה" : "ההוצאה נרשמה";
        status.className = "form-status ok";
        $("money-amount").value = ""; $("money-desc").value = "";
        if (res && res.budget) renderBudget(res.budget);
        refreshFinance();
      } catch (e) {
        status.textContent = e.message; status.className = "form-status err";
      } finally { $("money-btn").disabled = false; }
    });
  }

  // ------------------------------------------------------------------ approvals
  function planCard(p) {
    const tags = (p.hashtags || []).concat(p.tags || []).slice(0, 30);
    return el("div", { cls: "plan" }, [
      el("div", { cls: "plan-head" }, [pbadge(p.platform), p.post_status ? sbadge(p.post_status) : null]),
      el("div", { cls: "plan-title", text: p.title || "(בלי כותרת)" }),
      p.description ? el("div", { cls: "plan-desc", text: p.description }) : null,
      el("div", { cls: "plan-row" }, [el("b", { text: "מתי: " }), fmtTime(p.scheduled_at) === "-" ? "מיד עם האישור" : fmtTime(p.scheduled_at)]),
      p.why ? el("div", { cls: "plan-row why" }, [el("b", { text: "למה: " }), String(p.why)]) : null,
      tags.length ? el("div", { cls: "plan-row" }, [el("b", { text: "תגיות: " }), tags.join(" ")]) : null,
      p.variant ? el("div", { cls: "plan-row" }, [el("b", { text: "קובץ: " }), el("span", { cls: "ltr", text: p.variant })]) : null,
    ]);
  }

  function payloadSummary(a) {
    const pl = a.payload || {};
    const parts = [];
    for (const key of ["text", "reply_text", "message", "summary", "reason", "platform", "target"]) {
      if (pl[key] && typeof pl[key] !== "object") parts.push(String(pl[key]));
    }
    if (Array.isArray(pl.instructions) && pl.instructions.length) {
      parts.push("שלבים: " + pl.instructions.map((s, i) => (i + 1) + ". " + s).join("  "));
    }
    return parts.length ? el("div", { cls: "payload-text", text: parts.join("\n") }) : null;
  }

  async function decide(a, approve, noteInput, buttons) {
    if (!approve && !confirm("לדחות את הבקשה #" + a.id + "?")) return;
    if (approve && a.status === "pending" && a.cost_ils > 0 && !confirm("לאשר הוצאה של " + fmtIls(a.cost_ils) + "?")) return;
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const res = await api("/api/approvals/" + encodeURIComponent(a.id) + "/" + (approve ? "approve" : "reject"),
        { json: { note: noteInput.value.trim() } });
      if (approve) {
        const r = res && res.result;
        if (res.executing) toast("אושר! הצוות מתחיל לעבוד (עדכונים בפעילות)", "ok");
        else if (r && r.ok === false) toast("אושר, אך לא בוצע: " + (r.error || ""), "err");
        else toast("אושר ובוצע", "ok");
      } else toast("נדחה", "ok");
      refreshAll();
    } catch (e) {
      toast(e.message, "err");
      buttons.forEach((b) => { b.disabled = false; });
    }
  }

  function approvalCard(a) {
    const note = el("input", { type: "text", maxlength: "1000", placeholder: "הערה (לא חובה)", "aria-label": "הערה" });
    const yes = el("button", { type: "button", cls: "btn btn-approve btn-big", text: a.cost_ils > 0 ? "אשר הוצאה" : "אשר" });
    const no = el("button", { type: "button", cls: "btn btn-reject btn-big", text: "דחה" });
    const resume = a.status === "approved";  // approved earlier, execution was interrupted
    if (resume) yes.textContent = "המשך ביצוע (נקטע)";
    yes.addEventListener("click", () => decide(a, true, note, [yes, no]));
    no.addEventListener("click", () => decide(a, false, note, [yes, no]));
    const v = a.video;
    return el("article", { cls: "approval kind-" + String(a.kind).replace(/[^a-z]/g, "") }, [
      el("div", { cls: "approval-head" }, [
        el("div", {}, [
          el("div", { cls: "approval-title", text: "#" + a.id + " · " + a.title }),
          el("div", { cls: "approval-meta", text: he(a.kind) + " · ביקש: " + (ACTOR_HE[a.requested_by] || a.requested_by || "-") + " · " + fmtTime(a.created_at) }),
        ]),
        a.cost_ils > 0 ? el("div", { cls: "cost", text: "עלות: " + fmtIls(a.cost_ils) }) : el("span", { cls: "sbadge", text: "ללא עלות" }),
      ]),
      v && (v.summary_he || v.title_hint) ? el("div", { cls: "plan-row", text: "סרטון #" + v.id + ": " + (v.summary_he || v.title_hint) }) : null,
      a.plans && a.plans.length ? el("div", { cls: "plans" }, a.plans.map(planCard)) : payloadSummary(a),
      el("div", { cls: "approval-actions" }, resume ? [yes] : [note, yes, no]),
    ]);
  }

  function renderApprovals(list) {
    if (!changed("approvals", list)) return;
    const box = $("approvals-list");
    clear(box);
    $("approvals-empty").classList.toggle("hidden", list.length > 0);
    for (const a of list) box.appendChild(approvalCard(a));
  }

  // ------------------------------------------------------------------ videos & posts
  function renderVideos(list) {
    if (!changed("videos", list)) return;
    const tb = $("videos-body");
    clear(tb);
    $("videos-empty").classList.toggle("hidden", list.length > 0);
    for (const v of list) {
      const analysis = v.analysis || {};
      const err = v.meta && v.meta.error;
      tb.appendChild(el("tr", {}, [
        el("td", { "data-label": "#", text: v.id }),
        el("td", { cls: "title-cell", "data-label": "סרטון" }, [
          el("div", { text: v.title_hint || analysis.summary_he || (v.meta && v.meta.original_name) || v.file_name }),
          el("div", { cls: "muted small ltr", text: v.file_name }),
          err ? el("div", { cls: "err-text", text: err }) : null,
        ]),
        el("td", { "data-label": "מצב" }, [sbadge(v.status)]),
        el("td", { "data-label": "פוסטים" }, (v.posts || []).map((p) => pbadge(p.platform))),
        el("td", { "data-label": "נוצר", text: fmtTime(v.created_at) }),
      ]));
    }
  }

  function renderPosts(list) {
    if (!changed("posts", list)) return;
    const tb = $("posts-body");
    clear(tb);
    $("posts-empty").classList.toggle("hidden", list.length > 0);
    for (const p of list.slice(0, 200)) {
      const href = p.url ? safeUrl(p.url) : null;
      let link;
      if (href) link = el("a", { href: href, target: "_blank", rel: "noopener noreferrer", text: "פתח" });
      else if (p.dry_run) link = el("span", { cls: "muted small", text: "ניסיון — לא עלה" });
      else link = el("span", { cls: "muted", text: "-" });
      tb.appendChild(el("tr", {}, [
        el("td", { "data-label": "#", text: p.id }),
        el("td", { "data-label": "פלטפורמה" }, [pbadge(p.platform)]),
        el("td", { cls: "title-cell", "data-label": "כותרת" }, [
          el("div", { text: p.title || "-" }),
          p.error ? el("div", { cls: "err-text", text: p.error }) : null,
        ]),
        el("td", { "data-label": "מצב" }, [sbadge(p.status)]),
        el("td", { "data-label": "מתי", text: fmtTime(p.scheduled_at) }),
        el("td", { "data-label": "קישור" }, [link, p.status === "failed" ? retryButton(p) : null]),
      ]));
    }
  }

  function retryButton(p) {
    const b = el("button", { type: "button", cls: "btn btn-blue", text: "נסה שוב" });
    b.addEventListener("click", async () => {
      if (!confirm("להעלות שוב את פוסט #" + p.id + " (" + p.platform + ")? בדוק קודם שהוא לא עלה כבר.")) return;
      b.disabled = true;
      try {
        const res = await api("/api/posts/" + encodeURIComponent(p.id) + "/retry", { method: "POST" });
        if (res.executing) toast("מעלה שוב… (עדכונים בפעילות)", "ok");
        else if (res.ok) toast("פורסם", "ok");
        else toast("נכשל שוב: " + ((res.result && res.result.error) || ""), "err");
        refreshAll();
      } catch (e) {
        toast(e.message, "err");
        b.disabled = false;
      }
    });
    return b;
  }

  // ------------------------------------------------------------------ events
  function renderEvents(list) {
    if (!changed("events", list)) return;
    const ul = $("events-list");
    clear(ul);
    for (const e of list) {
      const lvl = ["info", "success", "warning", "error"].includes(e.level) ? e.level : "info";
      ul.appendChild(el("li", { cls: "lvl-" + lvl }, [
        el("span", { cls: "dot ev-dot lvl-" + lvl, "aria-hidden": "true" }),
        el("span", { cls: "ev-msg", text: e.message }),
        el("span", { cls: "ev-meta", text: (ACTOR_HE[e.actor] || e.actor) + " · " + fmtTime(e.ts) + (e.video_id ? " · סרטון #" + e.video_id : "") }),
      ]));
    }
    if (!list.length) ul.appendChild(el("li", { cls: "muted", text: "עוד אין פעילות." }));
  }

  // ------------------------------------------------------------------ platforms
  function renderPlatforms(data) {
    if (!changed("platforms", data)) return;
    const box = $("platforms-list");
    clear(box);
    for (const p of data.platforms || []) {
      let state, cls;
      if (!p.enabled) { state = "כבוי"; cls = "sbadge"; }
      else if (p.live) { state = "חי — מעלה באמת"; cls = "sbadge s-published"; }
      else if (!p.configured) { state = "ניסיון — חסרים מפתחות"; cls = "sbadge s-pending"; }
      else { state = "מוגדר · מצב ניסיון"; cls = "sbadge s-scheduled"; }
      box.appendChild(el("div", { cls: "platform" + (p.enabled ? "" : " off") }, [
        el("div", { cls: "platform-head" }, [pbadge(p.key), el("span", { cls: cls, text: state })]),
        el("div", { text: p.display_he || p.key }),
        p.configured ? el("div", { cls: "muted small", text: "מפתחות API: קיימים" })
          : (p.missing_secrets && p.missing_secrets.length ? el("div", { cls: "missing", text: p.missing_secrets.join(", ") }) : null),
        p.subreddits && p.subreddits.length ? el("div", { cls: "muted small", text: "קהילות: " + p.subreddits.map((s) => "r/" + s).join(", ") }) : null,
        p.error ? el("div", { cls: "err-text", text: p.error }) : null,
      ]));
    }
  }

  // ------------------------------------------------------------------ chat
  function addMsg(who, text) {
    const log = $("chat-log");
    const cls = who === "me" ? "msg me" : who === "boss" ? "msg boss" : "msg sys";
    const label = who === "me" ? "אתה" : who === "boss" ? 'מנכ"ל' : "";
    const node = el("div", { cls: cls }, [label ? el("span", { cls: "who", text: label }) : null, String(text)]);
    log.appendChild(node);
    log.scrollTop = log.scrollHeight;
    return node;
  }

  function setupChat() {
    addMsg("sys", 'שלום! אני המנכ"ל. אפשר לשאול על מצב הסרטונים, תקציב, תוכניות פרסום ועוד. אני לא מאשר דברים בעצמי — רק אתה.');
    $("chat-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const input = $("chat-input");
      const msg = input.value.trim();
      if (!msg) return;
      input.value = "";
      addMsg("me", msg);
      const wait = addMsg("sys", "חושב…");
      $("chat-btn").disabled = true;
      try {
        const res = await api("/api/chat", { json: { message: msg } });
        wait.remove();
        addMsg("boss", (res && res.reply) || "(אין תשובה)");
      } catch (e) {
        wait.remove();
        addMsg("sys", "שגיאה: " + e.message);
      } finally {
        $("chat-btn").disabled = false;
        input.focus();
      }
    });
  }

  // ------------------------------------------------------------------ upload
  let chosenFile = null;

  function validateFile(f) {
    const dot = f.name.lastIndexOf(".");
    const ext = dot >= 0 ? f.name.slice(dot).toLowerCase() : "";
    if (!ALLOWED_EXT.includes(ext)) return "סוג קובץ לא נתמך. מותר: " + ALLOWED_EXT.join(" ");
    if (f.size > MAX_BYTES) return "הקובץ גדול מדי (" + fmtBytes(f.size) + "). מקסימום " + fmtBytes(MAX_BYTES);
    if (f.size === 0) return "הקובץ ריק";
    return null;
  }

  function choose(f) {
    const status = $("upload-status");
    const err = f ? validateFile(f) : "לא נבחר קובץ";
    chosenFile = err ? null : f;
    $("file-name").textContent = f ? f.name + " · " + fmtBytes(f.size) : "";
    status.textContent = err || "";
    status.className = "form-status" + (err ? " err" : "");
    $("upload-btn").disabled = !!err;
  }

  function setupUpload() {
    const dz = $("dropzone"), input = $("file-input");
    input.addEventListener("change", () => choose(input.files && input.files[0]));
    dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "dragend", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => { const f = e.dataTransfer && e.dataTransfer.files[0]; if (f) choose(f); });
    // Dropping a file anywhere else must not navigate away from the dashboard.
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => e.preventDefault());

    $("upload-form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      if (!chosenFile) return;
      const fd = new FormData();
      fd.append("title", $("upload-title").value.trim());
      fd.append("file", chosenFile, chosenFile.name);
      const xhr = new XMLHttpRequest();
      const bar = $("upload-progress"), fill = bar.querySelector(".progress-fill"), status = $("upload-status");
      bar.classList.remove("hidden");
      fill.style.width = "0%";
      $("upload-btn").disabled = true;
      status.className = "form-status";
      status.textContent = "מעלה…";
      xhr.upload.addEventListener("progress", (e) => {
        if (!e.lengthComputable) return;
        const pct = (e.loaded / e.total) * 100;
        fill.style.width = pct.toFixed(1) + "%";
        status.textContent = "מעלה… " + Math.floor(pct) + "% (" + fmtBytes(e.loaded) + " / " + fmtBytes(e.total) + ")";
      });
      xhr.addEventListener("load", () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300 && data && data.ok) {
          fill.style.width = "100%";
          status.textContent = "הסרטון התקבל (#" + data.video_id + "). הצוות התחיל לעבוד — התוכנית תופיע באישורים.";
          status.className = "form-status ok";
          $("upload-title").value = "";
          $("file-input").value = "";
          $("file-name").textContent = "";
          chosenFile = null;
          refreshAll();
        } else {
          status.textContent = "ההעלאה נכשלה: " + ((data && typeof data.detail === "string") ? data.detail : "שגיאה " + xhr.status);
          status.className = "form-status err";
          $("upload-btn").disabled = !chosenFile;
        }
        setTimeout(() => bar.classList.add("hidden"), 1500);
      });
      xhr.addEventListener("error", () => {
        status.textContent = "ההעלאה נכשלה: אין חיבור לשרת";
        status.className = "form-status err";
        $("upload-btn").disabled = !chosenFile;
        bar.classList.add("hidden");
      });
      xhr.open("POST", "/api/upload");
      xhr.setRequestHeader("X-Agency-Request", "1");
      xhr.send(fd);
    });
  }

  // ------------------------------------------------------------------ polling
  async function refreshFinance() {
    const fin = await api("/api/finance");
    renderBudget(fin.budget);
    renderLedger(fin);
  }

  let platformsTick = 0;
  let busy = false;
  async function refreshAll() {
    if (busy) return;
    busy = true;
    const conn = $("chip-conn");
    try {
      const jobs = [
        api("/api/status").then(renderStatus),
        api("/api/approvals?status=open").then(renderApprovals),
        api("/api/videos?limit=50").then(renderVideos),
        api("/api/posts").then(renderPosts),
        api("/api/events?limit=80").then(renderEvents),
        refreshFinance(),
      ];
      if (platformsTick++ % 10 === 0) jobs.push(api("/api/platforms").then(renderPlatforms));
      await Promise.all(jobs);
      conn.textContent = "מחובר";
      conn.className = "chip chip-ok";
    } catch (e) {
      conn.textContent = "אין חיבור לשרת";
      conn.className = "chip chip-bad";
    } finally {
      busy = false;
    }
  }

  setupUpload();
  setupMoney();
  setupChat();
  refreshAll();
  setInterval(() => { if (!document.hidden) refreshAll(); }, POLL_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(); });
})();
