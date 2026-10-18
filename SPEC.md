# CS2 Agency — Build Spec (internal contract for builders)

A local, self-hosted "marketing agency" of AI employees for a Counter-Strike 2 video creator.
The owner (Hebrew speaker, Windows PC, also Linux/macOS) drops a video in; the staff analyses it,
edits platform variants, writes copy, decides platforms, schedules, uploads, promotes, tracks stats
and money. ONE manager (the "מנכ"ל" / CEO agent) is above everyone. Owner controls everything from
one CLI and one local web dashboard (Hebrew, RTL).

## Iron rules (enforced in CODE, not only in prompts)
1. **Money never moves without owner approval.** Any action with cost > 0 creates an `approval`
   row (status `pending`). Execution only after `approved`. Plus hard caps from config:
   `finance.monthly_budget_ils`, `finance.max_single_spend_ils`. Over cap → refused even if approved.
2. **DRY_RUN by default.** `config.yaml: dry_run: true`. Adapters log what they WOULD do and return a
   fake `PostResult(dry_run=True)`. Real uploads only when `dry_run: false` AND credentials exist.
3. **Publishing approval mode**: `publishing.mode: approve | auto`. Default `approve` → every
   upload plan waits for owner approval in the dashboard/CLI.
4. No fake engagement: no bots, no buying views/likes/followers, no spam, no vote manipulation,
   respect each platform's ToS and rate limits. Reddit/Discord posts only to places the owner listed
   in config. The manager prompt states this; code has no such features.
5. Secrets only in `.env` (never in config.yaml, logs, DB, or LLM prompts). `.env` in `.gitignore`.
6. Everything works offline for tests: if no `ANTHROPIC_API_KEY` (or `AGENCY_MOCK_LLM=1`), the LLM
   layer returns deterministic mock outputs. All tests pass with no network, no ffmpeg.
7. Fail closed and honest: an adapter error → post status `failed` with the error text; never report
   success that did not happen.

## Layout (repo root = /home/user/cs2-agency)
```
pyproject.toml            # name "cs2-agency", python>=3.10, console script: agency = agency.cli:app
requirements.txt
config.example.yaml       # copied to config.yaml by `agency init`
.env.example
.gitignore
README.md                 # Hebrew, step-by-step for a non-programmer on Windows
docs/PLATFORMS_SETUP.md   # Hebrew: how to get API keys for each platform
install.ps1 start.ps1     # Windows: create venv, pip install, run dashboard
install.sh start.sh       # Linux/macOS
.claude/agents/*.md       # optional Claude Code subagents mirroring the staff (manager + team)
agency/
  __init__.py
  config.py   db.py   models.py   llm.py   events.py   pipeline.py   scheduler.py
  staff/      __init__.py base.py manager.py analyst.py editor.py copywriter.py strategist.py
              publisher.py promoter.py community.py analytics.py finance.py
  platforms/  __init__.py base.py registry.py youtube.py tiktok.py instagram.py facebook.py
              x_twitter.py reddit.py discord.py
  media/      __init__.py ffmpeg.py   # probe, vertical crop 9:16, trim, thumbnail, frame grab
  cli.py
  dashboard/  __init__.py app.py templates/index.html static/app.js static/style.css
tests/        conftest.py test_*.py
```

## Core contracts (core builder writes these; everyone else imports them — DO NOT change signatures)

### agency/config.py
```python
@dataclass class Settings: raw: dict; root: Path
def load_settings(path: Path | None = None) -> Settings   # reads config.yaml (falls back to config.example.yaml), loads .env via python-dotenv
Settings.get(dotted: str, default=None)                     # e.g. s.get("finance.monthly_budget_ils", 0)
Settings.dry_run -> bool ; Settings.data_dir -> Path (default ./data, created) ; Settings.db_path -> Path
def secret(name: str) -> str | None                          # os.environ lookup only
```
config.example.yaml keys (Hebrew comments):
```yaml
channel: {name: "", language: "he", secondary_language: "en", niche: "Counter-Strike 2", handle: ""}
dry_run: true
llm: {model: "claude-opus-5-5", effort: "medium", max_tokens: 16000}
publishing:
  mode: approve            # approve | auto
  platforms: {youtube: {enabled: true}, youtube_shorts: {enabled: true}, tiktok: {enabled: true},
              instagram_reels: {enabled: true}, facebook: {enabled: false}, x: {enabled: true},
              reddit: {enabled: false, subreddits: []}, discord: {enabled: false}}
  timezone: "Asia/Jerusalem"
finance: {currency: "ILS", monthly_budget_ils: 0, max_single_spend_ils: 0}
staff: {enabled: [analyst, editor, copywriter, strategist, publisher, promoter, community, analytics, finance]}
dashboard: {host: "127.0.0.1", port: 8765}
```

### agency/models.py  (dataclasses, all JSON-serialisable via `.to_dict()`)
```python
class VideoStatus(str, Enum): NEW, ANALYZED, EDITED, PLANNED, AWAITING_APPROVAL, SCHEDULED, PUBLISHING, PUBLISHED, FAILED
@dataclass Video: id:int|None, path:str, title_hint:str, status:VideoStatus, created_at:str, analysis:dict, meta:dict
@dataclass Variant: platform:str, path:str, aspect:str, duration_s:float|None
@dataclass PostPlan: platform:str, variant_path:str, title:str, description:str, tags:list[str], hashtags:list[str], scheduled_at:str|None, extra:dict
@dataclass PostResult: platform:str, ok:bool, dry_run:bool, external_id:str|None, url:str|None, error:str|None
@dataclass Approval: id:int|None, kind:str ("publish"|"spend"|"reply"|"other"), title:str, payload:dict, cost_ils:float, status:str ("pending"|"approved"|"rejected"|"executed"), requested_by:str, created_at:str, decided_at:str|None, note:str
@dataclass LedgerEntry: id:int|None, kind:str ("expense"|"income"), amount_ils:float, category:str, description:str, platform:str|None, video_id:int|None, created_at:str
```

### agency/db.py  (sqlite3, single file, thread-safe connection-per-call, auto-migrate)
```python
class DB:
    def __init__(self, path: Path)
    # videos
    def add_video(self, path:str, title_hint:str="") -> int
    def get_video(self, vid:int) -> Video | None
    def list_videos(self, limit:int=100) -> list[Video]
    def update_video(self, vid:int, *, status:VideoStatus|None=None, analysis:dict|None=None, meta:dict|None=None) -> None
    # posts
    def add_post(self, video_id:int, plan:PostPlan, status:str="planned") -> int
    def update_post(self, post_id:int, *, status:str|None=None, result:PostResult|None=None, stats:dict|None=None) -> None
    def list_posts(self, video_id:int|None=None) -> list[dict]
    # approvals
    def add_approval(self, a:Approval) -> int
    def get_approval(self, aid:int) -> Approval | None
    def list_approvals(self, status:str|None="pending") -> list[Approval]
    def decide_approval(self, aid:int, approved:bool, note:str="") -> Approval
    def mark_approval_executed(self, aid:int) -> None
    # ledger
    def add_ledger(self, e:LedgerEntry) -> int
    def list_ledger(self, month:str|None=None) -> list[LedgerEntry]   # month "YYYY-MM"
    def month_totals(self, month:str) -> dict  # {"expense":x,"income":y,"net":y-x}
    # events / activity feed
    def log_event(self, actor:str, message:str, level:str="info", video_id:int|None=None) -> None
    def list_events(self, limit:int=200) -> list[dict]
    # staff status
    def set_staff_status(self, name:str, status:str, detail:str="") -> None
    def list_staff_status(self) -> list[dict]
    # key-value
    def kv_get(self, key:str, default=None) ; def kv_set(self, key:str, value) -> None
```

### agency/llm.py
```python
class LLM:
    def __init__(self, settings: Settings)
    mock: bool    # True when no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN or AGENCY_MOCK_LLM=1
    def complete(self, system:str, user:str|list, *, max_tokens:int|None=None) -> str
    def complete_json(self, system:str, user:str|list, schema:dict, *, mock_value:dict) -> dict
        # real: client.messages.create(... output_config={"format": {"type":"json_schema","schema":schema}, "effort": settings effort})
        # mock: returns mock_value (deep-copied)
    def run_tools(self, system:str, user:str, tools:list[Tool], *, max_turns:int=12, mock_text:str="") -> str
        # manual agentic loop; Tool = dataclass(name, description, input_schema, fn: Callable[[dict], str])
        # tool exceptions -> tool_result is_error=True ; handles stop_reason end_turn / tool_use / pause_turn / refusal / max_tokens
    def image_block(path: Path) -> dict    # staticmethod, base64 jpeg/png block
```
Claude API usage (from official docs — follow exactly):
- `import anthropic; client = anthropic.Anthropic()` (credentials from env).
- model from config, default `"claude-opus-5-5"`. Do NOT pass `thinking` with budget_tokens, do NOT pass temperature,
  do NOT use assistant prefill, do NOT use tool_choice any/tool (400 on this model). Use `tool_choice` default (auto).
- effort: `output_config={"effort": "medium"}` (merge `format` into the same output_config dict for JSON).
- Check `response.stop_reason` before reading content; on `"refusal"` raise `LLMRefusal`.
- Iterate `response.content`, read `.text` only for `block.type == "text"`; parse tool input as dicts (already parsed).
- Append the full `response.content` as the assistant turn; return all tool_results in ONE user message.
- Error handling chain: `anthropic.RateLimitError` -> `anthropic.APIStatusError` -> `anthropic.APIConnectionError`.
- Server-side refusal fallback: use `client.beta.messages.create(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default")`
  when `llm.fallbacks: true` (default true); if that raises a 400 `anthropic.BadRequestError`, retry once with plain `client.messages.create`.

### agency/events.py
`def emit(db, actor, message, level="info", video_id=None)` → db.log_event + python logging.

### agency/staff/base.py
```python
class StaffMember:
    name: str            # e.g. "copywriter"
    title_he: str        # e.g. "קופירייטר"
    role_prompt: str     # system prompt (English instructions, outputs in channel language)
    def __init__(self, ctx: "Context")
    def status(self, s:str, detail:str="")   # db.set_staff_status + emit
@dataclass class Context: settings: Settings; db: DB; llm: LLM
def build_context(settings: Settings | None = None) -> Context
```

### Staff responsibilities (agency/staff/*.py; each class subclasses StaffMember)
- `analyst.VideoAnalyst.analyze(video) -> dict` : media.probe + grab ~6 frames (if ffmpeg) → Claude vision JSON:
  `{clip_type: ace|clutch|funny|highlight|tutorial|montage|fail|other, map, weapons:[], highlights:[{t, desc}], hook_seconds, mood, summary_he, summary_en, best_short_window:{start,end}, audience:[], score_1_10}`.
- `editor.VideoEditor.make_variants(video, analysis) -> list[Variant]` : 16:9 original for youtube/facebook/x,
  9:16 crop ≤60s from best_short_window for shorts/tiktok/reels, thumbnail jpg. Without ffmpeg: returns original path for all and logs warning.
- `copywriter.Copywriter.write(video, analysis, platforms) -> dict[platform, {title, description, tags, hashtags}]` respecting each platform's limits (YouTube title ≤100, desc ≤5000, tags total ≤500 chars; TikTok caption ≤2200; X ≤280; IG ≤2200, ≤30 hashtags; Reddit title ≤300). Enforce limits in code after LLM.
- `strategist.Strategist.plan(video, analysis, variants, copy) -> list[PostPlan]` : decides WHICH enabled platforms are relevant (e.g. tutorial → YouTube long first; funny clip → TikTok/Shorts/Reels) and best posting time in configured timezone; explains reasoning (stored in plan.extra["why"]).
- `publisher.Publisher.publish(post_id) -> PostResult` : uses platforms.registry; respects dry_run; updates post + video status.
- `promoter.Promoter.propose(video, posts) -> list[Approval]` : organic promotion (cross-posting to configured Discord/Reddit, pinned comment text, community post text) and OPTIONAL paid promo proposals (YouTube/TikTok/Meta ads) as `spend` approvals with cost_ils, never executed automatically. Paid ads execution is "manual instructions" (we do not call ad APIs) — after approval it records the expense in the ledger and gives the owner step-by-step instructions.
- `community.CommunityManager.draft_replies(post_id) -> list[dict]` : drafts replies to recent comments (YouTube adapter can fetch comments); replies require `reply` approval unless config `community.auto_reply: false` (default false = needs approval).
- `analytics.Analyst.refresh() -> dict` : pull stats from adapters for published posts, store, produce weekly insight text (best platform, best hour, best clip type).
- `finance.CFO` : `record_expense`, `record_income`, `budget_status(month) -> {budget, spent, remaining, income, net}`, `can_spend(amount) -> (bool, reason)` enforcing caps, `monthly_report() -> str` (Hebrew), income sources: AdSense/TikTok/sponsorships entered manually or via CLI.
- `manager.Manager` (the ONE boss): `handle_new_video(path, title_hint="") -> dict` runs the full pipeline via agency/pipeline.py; `chat(message) -> str` — owner talks to the manager in Hebrew; manager uses `llm.run_tools` with tools: get_status, list_approvals, budget_status, list_videos, list_posts, run_analytics, propose_promotion(video_id), schedule_reschedule(post_id, iso), explain_plan(video_id). Manager CANNOT approve things itself (no approve tool). `daily_brief() -> str`.

### agency/pipeline.py
`run_pipeline(ctx, video_id) -> dict` : analyst → editor → copywriter → strategist → create posts → if mode approve: one `publish` Approval per video listing all PostPlans (status AWAITING_APPROVAL); if auto: posts scheduled. `execute_approval(ctx, approval_id) -> dict` : publish → publisher (now or keep scheduled), spend → finance.can_spend then ledger + instructions, reply → community post reply. Each step emits events and updates staff status. A failing step marks video FAILED with error, does not crash the process.

### agency/scheduler.py
`tick(ctx) -> list[PostResult]` publishes due scheduled posts (scheduled_at <= now, status "scheduled"). `run_forever(ctx, interval_s=60)`. Dashboard starts it in a background thread.

### agency/platforms/base.py
```python
class PlatformAdapter:
    key: str ; display_he: str ; required_secrets: list[str]
    def __init__(self, settings)
    def configured(self) -> bool                     # all required secrets present
    def upload(self, plan: PostPlan) -> PostResult    # if settings.dry_run: return PostResult(ok=True, dry_run=True, ...) WITHOUT network
    def fetch_stats(self, external_id: str) -> dict   # {views, likes, comments, shares} best-effort; {} if unsupported
    def fetch_comments(self, external_id:str, limit:int=20) -> list[dict]  # default []
    def reply_comment(self, comment_id:str, text:str) -> bool  # default False
class MissingCredentials(Exception)
```
registry: `get_adapter(key, settings)`, `ALL_KEYS = ["youtube","youtube_shorts","tiktok","instagram_reels","facebook","x","reddit","discord"]`.
Real implementations (imports of 3rd-party libs are LAZY inside methods so the package imports without them):
- youtube / youtube_shorts: google-api-python-client resumable upload `videos().insert(part="snippet,status", ..., media_body=MediaFileUpload(resumable=True))`, OAuth installed-app flow token stored in data/youtube_token.json (secrets YOUTUBE_CLIENT_SECRETS_FILE). Shorts = same API, vertical ≤60s, add #Shorts. Supports `publishAt` scheduling (privacyStatus private + publishAt). Stats via videos().list(part="statistics"). Comments via commentThreads().list, reply via comments().insert.
- tiktok: Content Posting API v2 `POST https://open.tiktokapis.com/v2/post/publish/video/init/` FILE_UPLOAD then PUT chunks to upload_url; secret TIKTOK_ACCESS_TOKEN. Note unaudited apps post as private (SELF_ONLY) — document it.
- instagram_reels: Graph API `/{ig-user-id}/media` media_type=REELS with resumable upload (`upload_type=resumable`, then POST to rupload.facebook.com with file), then `/media_publish`; secrets IG_USER_ID, META_ACCESS_TOKEN.
- facebook: `/{page-id}/videos` upload; secrets FB_PAGE_ID, META_ACCESS_TOKEN.
- x: tweepy v1.1 chunked `media_upload(media_category="tweet_video", chunked=True)` + v2 `create_tweet`; secrets X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET.
- reddit: praw `subreddit.submit_video` only to `publishing.platforms.reddit.subreddits`; secrets REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD.
- discord: webhook POST with message + link (file if < 25MB); secret DISCORD_WEBHOOK_URL.
Use `requests` for HTTP. Timeouts on every request. Never log tokens.

### agency/media/ffmpeg.py
`have_ffmpeg() -> bool`, `probe(path) -> dict {duration, width, height, fps}` (ffprobe JSON; {} if none),
`vertical_clip(src, dst, start, end) -> Path` (crop center to 9:16 + scale 1080x1920, -c:v libx264 -c:a aac),
`thumbnail(src, dst, t) -> Path`, `grab_frames(src, out_dir, n=6) -> list[Path]`. subprocess with list args (no shell=True).

### agency/cli.py  (Typer + rich; Hebrew help texts)
`agency init` (copy config/.env examples, create data dir, print next steps) ·
`agency upload PATH [--title]` (copy video into data/videos, run manager.handle_new_video, print plan + approval id) ·
`agency status` · `agency approvals` · `agency approve ID [--note]` · `agency reject ID [--note]` ·
`agency budget` · `agency expense AMOUNT CATEGORY DESC` · `agency income AMOUNT SOURCE DESC` ·
`agency report` (monthly Hebrew report) · `agency brief` (manager daily brief) · `agency chat` (REPL with manager) ·
`agency analytics` · `agency tick` (publish due posts) · `agency platforms` (which are configured / dry-run) ·
`agency dashboard` (run uvicorn + scheduler thread, open browser).

### agency/dashboard/app.py  (FastAPI, served at 127.0.0.1 only by default)
`create_app(ctx=None) -> FastAPI`. JSON API: GET /api/status (staff + counts + budget + dry_run),
GET /api/videos, GET /api/posts, GET /api/approvals, POST /api/approvals/{id}/approve|reject (json {note}),
GET /api/finance, POST /api/finance/expense|income, GET /api/events, POST /api/upload (multipart file + title →
saves to data/videos, runs pipeline in background thread), POST /api/chat {message} → manager reply,
GET /api/platforms. GET / → templates/index.html: Hebrew RTL single page, dark CS2-ish theme (orange/blue accents),
sections: org chart (manager on top, staff cards with live status), drag-and-drop upload, approvals queue with big
approve/reject buttons, videos+posts table, finance card (budget bar, spent, income, net), activity feed, chat with
the manager, platforms panel. Vanilla JS polling every 3s. No external CDN required.

### Tests (pytest, run with `AGENCY_MOCK_LLM=1`, dry_run, tmp data dir; no network; no ffmpeg needed)
config load, db CRUD, finance caps (refuse over budget even if approved), full pipeline on a tiny fake video file
→ approval created → approve → dry-run posts published, copywriter limits enforced, scheduler tick,
dashboard endpoints via fastapi TestClient, CLI via typer CliRunner, adapters in dry-run never touch network
(monkeypatch requests to raise), manager has no approve tool.

Dependencies: anthropic>=1.0 (fallback: if 1.x not on PyPI use latest 0.x — check with `pip index versions anthropic`), pyyaml, python-dotenv,
typer, rich, fastapi, uvicorn, python-multipart, requests, jinja2; optional extras `[platforms]`: google-api-python-client,
google-auth-oauthlib, tweepy, praw. dev: pytest, httpx (for TestClient).
