# CS2 Agency v2 — "next level, friendly" (binding spec for builders)

Read SPEC.md too: every iron rule there still holds (money only with approval, no fake engagement,
secrets only local, fail closed and honest, offline tests). v2 changes the EXPERIENCE, adds
real connections, and moves the agency to run on the owner's Windows PC.

The owner (Dor) said: "make it friendlier, on another level, I don't want to see things that
aren't really interesting, and I want to connect everything." Design for a gamer/creator, not a
programmer. Hebrew first, RTL, short sentences, zero jargon on the main screens.

## Banned words on default (non-advanced) screens
dry-run, API, token, secret, OAuth, webhook, JSON, config, yaml, env, pipeline, status codes,
exception text, staff internal names in English, database ids like "#17" (use video titles).
Replace with: "מצב אימון" (training mode), "חיבור", "מפתח" only inside the connect wizard,
"הצוות", video title. Technical details live only behind "מצב מתקדם" (advanced mode).

## 1. Home screen (the ONLY thing most days) — agency/dashboard/
Single calm screen, dark CS2 theme (keep palette), big type, lots of space, mobile-first.
Top to bottom:
1. **Manager line** — avatar of the manager + ONE friendly first-person sentence generated from
   real state, e.g. "הכל רץ. שני סרטונים עלו היום, אחד מחכה לך." (rule-based, not LLM, so it is
   instant and truthful; GET /api/home returns it). Time-aware greeting (בוקר טוב / ערב טוב).
2. **Big drop zone** "זרוק קליפ" (also click to choose; on phone opens gallery). Optional one-line
   "מה קרה בקליפ?" field. While processing: a friendly progress timeline with the team members'
   avatars lighting up in order: "צופה בקליפ → חותך לאנכי → כותב כותרות → בוחר פלטפורמות → מוכן לאישור"
   (driven by real staff status, not fake timers).
3. **"מחכה לך"** — only rendered when something needs the owner. Each item is ONE card:
   thumbnail (served via GET /api/videos/{id}/thumbnail), video title, one line "יעלה ל:" with
   platform chips (icon + name), human time ("היום 18:00", "מחר 14:30"), main title preview.
   Buttons: big green "יאללה, תעלה" · "לא עכשיו" · "שנה משהו" (opens chat with the manager
   pre-filled "לגבי <title>: "). "פרטים" expander shows per-platform title/description/hashtags/why
   (editable inline: title + description per platform, saved via PATCH /api/approvals/{id}/plans/{platform}
   before approval only). Spend approvals show amount in ₪ in big text and the CFO's one-line reason.
4. **"איך הולך"** — results: 3 big numbers for last 7 days (צפיות, לייקים, תגובות) with a tiny
   inline-SVG sparkline, "הקליפ הכי חזק השבוע" card, and per-platform mini bars. Friendly empty state
   when there is no data yet ("ברגע שיעלה הקליפ הראשון תראה פה מספרים").
5. **Money line** — one sentence: "החודש: הוצאות 0 ₪ · הכנסות 0 ₪" + a thin budget bar. Click → finance sheet.
6. **The team** — a compact row of round avatars (manager first, bigger), each with a status dot
   (green idle / orange working / red problem). Tap → small sheet with what they're doing in one
   sentence. No long cards.
7. **Floating chat button** (bottom-left in RTL) "דבר עם המנכ"ל" → slide-up chat panel. Quick-reply
   chips: "מה המצב?", "מה עלה היום?", "תן לי רעיון לקליפ הבא", "כמה הוצאנו החודש?".
Header: logo, a small pill "מצב אימון" (when the platform is not live) or "שידור חי" (green, when ≥1
platform live), and icons: חיבורים (plug), הגדרות (gear). No tab bar on home.
Toasts for events (approval needed, video published with link, failure in plain Hebrew + "נסה שוב").
Browser Notification API when an approval is waiting (ask permission once, gracefully).

## 2. Connections page ("חיבורים") — the second screen
Grid of platform tiles with brand color + simple inline SVG icon: YouTube, YouTube Shorts (shares
YouTube's connection), TikTok, Instagram, Facebook, X, Reddit, Discord.
Each tile shows ONE of: "מחובר ✓ <channel/account name>" · "לא מחובר" · "צריך תיקון" (+ plain reason).
Each connected tile has a switch: **"העלאה אמיתית"** (live) vs "אימון" — per-platform live mode
(replaces the global dry_run for owner UX; global `dry_run: true` stays as a master safety: when
global dry_run is true, a platform is live only if `publishing.platforms.<key>.live: true`.
Effective rule: `live(platform) = connected(platform) and settings.platform_live(key)`; global
`dry_run: false` means all connected platforms live). Turning a switch ON requires a confirm dialog
"מעכשיו הקליפים יעלו באמת ל-<platform>. בטוח?". Stored in config.yaml via the settings writer.
Tile tap → **Connect wizard** (modal, step by step, plain Hebrew, 3–6 steps, each step one action,
with the exact link to open and what to click; screenshot-free but precise). Ends with:
- YouTube: button "התחבר עם גוגל" → POST /api/connect/youtube/start launches the installed-app OAuth
  flow (google-auth-oauthlib `InstalledAppFlow.run_local_server(port=0, open_browser=True)`) in a
  background thread on the PC; scopes youtube.upload + youtube.readonly + youtube.force-ssl;
  the page polls GET /api/connect/youtube/state; success shows the channel title + thumbnail.
  Requires the client secrets file: wizard step lets the owner drop the `client_secret*.json` file
  (POST /api/connect/youtube/client-secret, stored as data/secrets/youtube_client_secret.json, chmod 600 where possible).
- Others: masked input fields (type=password, autocomplete=off) for the required keys → POST
  /api/connect/{platform}/keys → saved to the local `.env` via a safe .env writer (preserves other
  lines/comments, quotes values, never logs values) → then automatic **real verification** call:
  TikTok `GET https://open.tiktokapis.com/v2/user/info/?fields=display_name,username` ;
  Instagram `GET graph.facebook.com/<ver>/<IG_USER_ID>?fields=username` ; Facebook
  `GET /<FB_PAGE_ID>?fields=name` ; X `GET https://api.x.com/2/users/me` (OAuth1 user context via
  requests-oauthlib or tweepy) ; Reddit praw `reddit.user.me()` ; Discord `GET <webhook_url>` (returns
  webhook name/channel). Show account name on success, plain-Hebrew reason on failure
  ("המפתח לא נכון או שפג תוקפו"). Verification is read-only, timeouts 15s, never echoes secrets.
- "ייבוא חיבורים קיימים" button at the top of the page → see §5.
- "נתק" (disconnect) per platform: removes its keys from .env / token file after confirm.
API never returns secret values — only `{connected, account_name, live, missing:[names], problem}`.

## 3. Settings ("הגדרות") — small
Channel name + handle, main language, posting mode ("אני מאשר כל העלאה" default / "תעלה לבד"),
monthly budget ₪ + max single spend ₪, times ("שעות שאני לא רוצה העלאות" optional quiet hours),
remote access (§6), and the **"מצב מתקדם"** toggle (remembered in localStorage) which reveals:
activity log, videos/posts table, platform technical state, ledger list, raw CLI hints.
Settings saved via PUT /api/settings → writes config.yaml safely (yaml.safe_dump, keep a .bak).
Never writes secrets to config.yaml.

## 4. Settings writer & secrets store — agency/settings_store.py (new)
`update_config(settings, patch: dict) -> Settings` (deep-merge, validate types, atomic write, backup),
`env_set(root, mapping: dict[str,str]) -> None` / `env_unset(root, names)` (atomic, preserves comments,
reloads os.environ for the current process), `secrets_dir(settings) -> Path` (data/secrets).
Allowed env names = union of all adapters' `required_secrets` (+ ANTHROPIC_API_KEY). Reject others.

## 5. Import existing connections — agency/importer.py (new) + CLI + dashboard
The owner ALREADY has YouTube connected from an earlier project on the same PC (see HANDOFF.md for
what we know). `scan(paths: list[Path]) -> list[Found]` looks (non-recursive depth ≤ 4, skip
node_modules/.git/.venv, size < 1MB) for: `client_secret*.json` / `client_secrets*.json`
(Google "installed"/"web" JSON), Google authorized-user token JSON (has refresh_token + client_id),
pickled `token.pickle` (DO NOT unpickle — only report it and ask to re-login), `.env` files containing
any allowed env names. Default scan roots: the agency root's parent folders on the PC, the user's
home Documents/Desktop/OneDrive/Projects folders, and any path passed explicitly.
`import_found(items, settings)` copies Google files into data/secrets (validating JSON shape and
that the token's scopes include youtube.upload — otherwise mark "צריך התחברות מחדש"), and merges
recognised .env keys into our .env (never overwriting existing values unless `overwrite=True`).
Returns per item: what was imported, from where (path), and the verification result.
Values are never printed, logged, or sent to the LLM. CLI: `agency import [PATH ...] [--yes]`.
Dashboard: "ייבוא חיבורים קיימים" → POST /api/connect/import/scan (optional extra path) → list with
checkboxes (shows file path + "נמצא חיבור ליוטיוב" etc.) → POST /api/connect/import/apply.
YouTube adapter must load credentials from data/secrets/youtube_token.json (authorized-user format,
refresh automatically, persist refreshed token) and client secrets from data/secrets or
YOUTUBE_CLIENT_SECRETS_FILE.

## 6. Phone access (optional, OFF by default) — approve from the couch
Settings → "גישה מהטלפון" switch. When ON: dashboard binds 0.0.0.0 on next start, a 6-digit PIN is
generated and shown ONCE on the PC screen (stored only as a salted hash in data/), plus a QR code
(pure-python, e.g. `segno` dependency, rendered as inline SVG) for http://<LAN-IP>:<port>/.
Remote (non-loopback) clients must log in with the PIN → HttpOnly, SameSite=Strict session cookie
(random 32 bytes, server-side store, 30-day expiry, revocable "התנתק מכל המכשירים").
Rate-limit PIN attempts (5/10min per IP, then lockout). Loopback clients need no PIN.
Host header allowlist = loopback names + this machine's LAN IPs/hostname (keeps DNS-rebinding
protection). Keep the existing X-Agency-Request CSRF header requirement for all POST/PUT/PATCH/DELETE.
Not exposed to the internet: document "only on your home Wi-Fi"; no port forwarding instructions.

## 7. Windows-first launcher
install.ps1: also creates a Desktop shortcut "סוכנות CS2" (and Start Menu) that runs start.ps1
hidden (pythonw / -WindowStyle Hidden) and opens the browser; optional "להפעיל עם Windows" (Startup
folder shortcut) asked with Y/N. Uses the repo folder wherever it is cloned (no hardcoded paths).
`agency` with no subcommand = start dashboard + open browser (same as `agency dashboard`).
start.ps1: if already running (port busy and /api/health answers), just open the browser.
Add GET /api/health → {"ok": true, "app": "cs2-agency"}.

## 8. Manager personality
Manager chat replies: Hebrew, short (≤4 sentences unless asked), warm, gamer slang light, first
person, never technical unless asked; ends with a concrete next step when relevant. Daily brief:
max 5 bullets. Mock-mode replies follow the same style (realistic, built from real state).

## 9. README rewrite
README.md becomes SHORT (≤120 lines): what it is (3 lines), 3-step install for Windows (download →
double-click install → desktop icon), "הקליפ הראשון שלך" (drag, approve), "חיבור פלטפורמות"
(points to the in-app wizard; ייבוא חיבורים קיימים), safety in 4 bullets, "מצב מתקדם" pointer.
All technical material moves to docs/ADVANCED.md (CLI, config, architecture). docs/PLATFORMS_SETUP.md
stays but is linked from the wizards.

## 11. ALIGN WITH THE OWNER'S EXISTING LIVE STUDIO (highest priority — overrides §2/§5 where they conflict)
Facts (from the owner's earlier sessions; see HANDOFF.md):
- The owner already runs a live system `C:\CS2_STUDIO` (Python 3.12) on his Windows PC (user `dorga`).
  Brand **"Imagination"**, handle **@imaginationcs2** on YouTube, TikTok, Instagram (Creator), Facebook
  Page "Imaginationcs2". 5 shorts published to all 4; ~7,600 views, 0 followers (13/10).
- Credentials live in **Windows Credential Manager, service `cs2studio`**, keys: `zernio`, `meta_page_token`,
  `youtube_oauth`, `telegram_bot`, `claude_token`, `hmac_key`. Meta IDs in `C:\CS2_STUDIO\state\studio.db`
  table kv, key `meta_ids` (JSON with page id and IG user id).
- **YouTube + TikTok upload through Zernio** (zernio.com social API, free plan = 2 accounts). Direct
  YouTube Data API uploads from his unverified Google Cloud project get LOCKED PRIVATE — so our direct
  YouTube upload must NOT be the default; Google OAuth (`youtube_oauth`, scopes youtube.force-ssl +
  yt-analytics.readonly) is used only for stats/comments/making videos private.
- **Instagram + Facebook publish through Meta Graph API v24 directly** with a never-expiring Page token
  (`meta_page_token`) of his Meta app "Imagination Studio". Instagram posts go ≥30 min after YouTube.
- Approvals today: **Telegram bot @imagination_control_bot** (one tap "אשר הכל"). He likes it.
- His proven publishing times (Israel): YouTube ~17:00, Facebook ~17:40, Instagram ~20:00, TikTok ~20:40.
- Encoding (studio\media.py): vertical 1080x1920 ≤60fps x264 High crf17 preset slow, 2-pass loudnorm
  −14 LUFS, AAC 192k; YouTube-only HQ: upscale short side to 1440 lanczos crf15 AAC 256k ("always").
- Brand rules: hashtags #CS2 #CounterStrike2 #CS2Edit; bio "Legendary CS2 moments. Real demo. Shot like a
  movie."; block skin-gambling and cheat words; label AI shots; credit music; funny/trolling captions (not
  dry); hook within 1 second; he rejects self-mocking "fail" gags — wants "pro + crazy AI".
- Owner communication: Hebrew only, very short, simple, ONE yes/no question at a time, zero technical
  detail. Never print Hebrew in a Windows console (it renders reversed) — Hebrew UI lives in the
  browser dashboard (and Telegram). $0 budget; ~3 hours/week. Never hard-delete videos: make private.
  Never create accounts or type passwords for him.
Required changes:
1. **Secrets backend**: `agency.config.secret(name)` resolution order: os.environ → OS keyring service
   `cs2-agency` → **read-only fallback to keyring service `cs2studio`** via an alias map
   {ZERNIO_API_KEY:'zernio', META_ACCESS_TOKEN:'meta_page_token', TELEGRAM_BOT_TOKEN:'telegram_bot',
   YOUTUBE_OAUTH_JSON:'youtube_oauth'}. Use the `keyring` package (lazy import; absent/failing keyring →
   None, never crash). New secrets entered in the wizard are stored in keyring service `cs2-agency`
   when keyring works (Windows), else .env. Never write to the `cs2studio` service.
2. **Zernio adapter** `agency/platforms/zernio.py` (exact API per HANDOFF/ZERNIO_API.md): presign → PUT →
   POST /v1/posts (one platform entry per call, idempotency key from file sha256+platform) → poll
   GET /v1/posts/{id}; supports scheduledFor (Zernio's cloud schedules, works when the PC is off);
   unpublish. Platforms `youtube`, `youtube_shorts`, `tiktok` route through Zernio when ZERNIO_API_KEY
   is available (`publishing.platforms.<key>.via: zernio|direct`, default zernio). Account ids discovered
   via GET /v1/accounts and cached. Direct YouTube upload remains available only as `via: direct` with a
   warning in the wizard that unverified projects publish as private.
3. **Meta**: Instagram/Facebook adapters read IG user id / page id from env, else from the studio DB
   (`importer` reads `C:\CS2_STUDIO\state\studio.db` kv `meta_ids` read-only, sqlite `mode=ro` URI) and
   store them in our config (ids are not secrets). Graph API version v24.0.
4. **Importer**: first-class "ייבוא מהסטודיו הקיים": detects `C:\CS2_STUDIO` (or a path given), reports
   which keyring entries exist under `cs2studio` (names only), imports Meta ids, brand rules
   (hashtags/blocked words/style from prompts\copywriter.md if present), posting times, and links our
   connections to those secrets (no copying of values needed because of the read-only fallback).
   Also offers to import the studio's published-post history (read-only) so results/analytics continue.
5. **Telegram approvals** (optional channel, `notify.telegram.enabled`): send approval cards with
   inline buttons "אשר הכל" / "לא עכשיו" + per-platform links when published; Hebrew; uses
   TELEGRAM_BOT_TOKEN; pairs to the owner's chat id (stored in config after a /start pairing code shown
   on the dashboard). Callback data HMAC-signed. IMPORTANT: two pollers on one bot conflict — if the old
   studio daemon is running, getUpdates returns 409; then show "הבוט תפוס ע״י הסטודיו הישן" and fall back
   to dashboard-only. Long-polling in a background thread; never logs the token.
6. **Brand profile** in config (`brand:` name, handle, bio, hashtags, blocked_words, style notes,
   ai_label, music_credit) consumed by copywriter/strategist/promoter prompts and enforced in code
   (blocked words removed/rejected, required hashtags appended within limits).
7. **Encoding presets** in editor: port the studio's settings above (vertical crf17 slow + loudnorm −14
   LUFS 2-pass, YouTube HQ 1440 crf15) as `media.presets`, default on.
8. **Default schedule** = the proven times above (config `publishing.default_times`), strategist may
   deviate only with a stated reason.
9. Dashboard copy: the channel is "Imagination"; the connections page shows "מחובר דרך הסטודיו הקיים"
   for platforms satisfied by the cs2studio fallback.
10. **No double posting with the old studio**: before publishing, check (read-only) the studio DB for a
   post with the same file sha256 + platform and refuse with a plain message; the importer detects
   whether the old daemon is active (scheduled task "CS2Studio Daemon" via `schtasks /Query /TN` on
   Windows, or `logs\daemon.log` modified in the last 10 min) and the dashboard shows one clear choice:
   "הסטודיו הישן עדיין פעיל — הסוכנות החדשה תחליף אותו?" (we never disable it ourselves; we show the
   owner the one-click .cmd/command to disable it, or he keeps the old one and we stay in training mode).
11. **Windows console**: install.ps1/start.ps1 and any console output on Windows must not show Hebrew
   (it renders reversed). Console text in English; Hebrew goes to the browser (or a WinForms MessageBox,
   which renders RTL correctly). The CLI keeps Hebrew help but on Windows defaults to English messages
   unless `AGENCY_CLI_LANG=he`.
12. **Inbox folder**: watch `C:\Users\<user>\Desktop\CS2_CLIPS` (config `inbox.folders`, default: the
   studio's clip folder if it exists, else `data/inbox`) — a new finished clip there becomes a video
   automatically (stable-size check before processing; ignore files already known by sha256). Files
   that already exist when the watcher is first enabled are recorded as a baseline and NOT processed
   (the dashboard lists them under "קליפים קודמים" with a per-clip "תכין לפרסום" button). The owner
   can keep his habit of dropping clips in a folder. Watcher is OFF until the owner enables it.

## 10. Tests (offline, mock LLM)
home endpoint sentence correctness for several states; per-platform live logic (global dry_run
true + platform live true → adapter used for real path with mocked network; not connected → never
live); settings writer (atomic, backup, rejects secrets/unknown keys); env writer preserves
comments and rejects unknown names; connect keys endpoint never returns values; verification calls
mocked; importer finds/validates Google client secret + authorized-user token + .env, refuses
pickles, never prints values; YouTube credential loading from data/secrets; PIN login flow, rate
limit, cookie flags, remote client without cookie → 401, loopback no PIN, Host allowlist; CSRF
header still required; plan editing only while pending; thumbnail endpoint path-safe; health.
Keep all existing tests passing (update only those whose behaviour v2 intentionally changes).
