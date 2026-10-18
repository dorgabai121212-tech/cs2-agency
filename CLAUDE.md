# CLAUDE.md: CS2 Agency ("Imagination Agency")

This is a local, self-hosted "marketing agency" of AI employees for one Counter-Strike 2 video creator, the **owner** (Dor, brand **"Imagination"**, @imaginationcs2). The owner speaks Hebrew and runs it on his Windows PC, next to his older live system `C:\CS2_STUDIO` (the "studio").

The owner drops in a clip (dashboard, inbox folder or CLI). The staff then:
1. analyses it
2. edits platform variants
3. writes copy (public copy in English, by brand rules)
4. picks platforms and times
5. waits for owner approval (dashboard, Telegram or CLI)
6. uploads
7. promotes, drafts community replies, tracks stats and tracks money

Exactly **one manager** (the CEO, "המנכ\"ל") sits above all the staff. The owner controls everything from a local Hebrew RTL dashboard (also the `agency` CLI and an optional Telegram bot).

Binding specs:
- `SPEC.md` is the v1 contract for layout, signatures and behaviour. Its iron rules still bind.
- `SPEC_V2.md` is the v2 spec. Its §11 (align with the studio) overrides earlier sections where they conflict.
- Read them before changing core code, and do not change core signatures.
- `HANDOFF.md` records what we know about the studio and the owner's PC. `HANDOFF_PC.md` is the install script for a Claude Code session on that PC.

## Iron rules (enforced in code; keep them enforced)
1. **Money never moves without owner approval.**
   - Any cost > 0 becomes a `spend` approval with status `pending`.
   - Execution happens only after the owner sets it to `approved`.
   - The hard caps `finance.monthly_budget_ils` and `finance.max_single_spend_ils` refuse over-cap spends even when approved.
   - We never call ad APIs. An approved spend records a ledger entry and gives the owner manual instructions.
2. **Training mode by default, switched per platform.**
   - `live(platform) = connected(platform) and settings.platform_live(key)`.
   - The global `dry_run: true` stays the master safety: while it is true, a platform is live only with `publishing.platforms.<key>.live: true`. `youtube_shorts` follows YouTube's switch unless it has its own.
   - Global `dry_run: false` makes every connected platform live.
   - In training mode adapters return `PostResult(dry_run=True)` without any network call.
   - Switching on is refused for a platform that is not connected (`settings_store.set_platform_live`).
3. **Publishing approval mode.** `publishing.mode: approve` is the default and makes every upload plan wait for the owner. `auto` schedules posts directly. Spends and replies always need approval. TikTok (via Zernio) refuses `auto`.
4. **No fake engagement.** No bots, no bought views, likes or followers, no spam and no vote manipulation. Respect platform ToS and rate limits. Reddit and Discord posts go only to the places the owner listed in config. Do not add such features.
5. **Secrets stay local and are never shown.**
   - `agency.config.secret(NAME)` looks them up in this order: `os.environ` (`.env` is loaded into it), then OS keyring service `cs2-agency`, then a **read-only** fallback to the studio's keyring service `cs2studio`.
   - The fallback uses aliases: `ZERNIO_API_KEY→zernio`, `META_ACCESS_TOKEN→meta_page_token`, `TELEGRAM_BOT_TOKEN→telegram_bot`, `YOUTUBE_OAUTH_JSON→youtube_oauth`, `CLAUDE_CODE_OAUTH_TOKEN→claude_token`.
   - New secrets go to keyring `cs2-agency` via `store_secret` (else `.env`).
   - **Never write to, delete from or "fix" the `cs2studio` keyring.**
   - Secrets never go into `config.yaml`, logs, the DB, events, LLM prompts or API responses. The API returns names, `connected`, `account_name`, `source` and `problem` only.
   - Never print, read aloud, copy or commit `.env`, anything in `data/secrets/` (Google client secret, YouTube token, HMAC key) or `client_secret*.json`.
6. **Offline tests.** With `AGENCY_MOCK_LLM=1`, or with no API key and no usable Claude Code, the LLM layer returns deterministic mock output. Tests need no network, no ffmpeg, no keyring, no studio and no real `claude`: `tests/conftest.py` sets `AGENCY_KEYRING=off`, `AGENCY_STUDIO_DIR=off` and `AGENCY_CLAUDE_CLI=off` (CLI tests use a fake `claude` script).
7. **Fail closed and honestly.** An adapter error sets post status `failed` with the error text. Never report success that did not happen. Zernio "still processing" is a failure until confirmed. Never hard-delete a published video: unpublishing makes it private.
8. **The manager can never approve.** It has no approve tool, by design and covered by tests. Do not add one. Telegram "אשר הכל" is the owner's own tap, signed with HMAC and bound to the paired chat.
9. **The studio is read-only.**
   - We read `C:\CS2_STUDIO\state\studio.db` with sqlite `mode=ro`, parse its config with `ast` (never executed), and read the names of its keyring entries.
   - We never change its files, its scheduled tasks ("CS2Studio Daemon"/"Watchdog") or its keyring.
   - If its daemon is active, the dashboard asks the owner and shows the disable commands. We never disable it ourselves.
   - Before a live upload the publisher refuses a clip the studio already published to the same account (`studio.double_post_guard`).

## Upload routes
- `youtube`, `youtube_shorts` and `tiktok` go through **Zernio** by default (`agency/platforms/zernio.py`, one key `ZERNIO_API_KEY`).
  - `publishing.platforms.<key>.via: direct` switches to the direct adapters.
  - Direct YouTube uploads from the owner's unverified Google project are locked private, so never make direct the default.
  - Google OAuth is for stats, comments and making videos private.
- `instagram_reels` and `facebook` use **Meta Graph API v24.0** directly. They need a never-expiring Page token `META_ACCESS_TOKEN` plus ids from env or config `meta.ig_user_id` / `meta.page_id`. Instagram goes ≥30 min after the same video's YouTube post.
- `x`, `reddit` and `discord` are direct.
- `registry.get_adapter(key, settings)` picks the route.

## Layout
- `agency/config.py`: `Settings` (`platform_live`, `platform_via`, `studio_dir`), `load_settings()`, `secret()`, `secret_source()`, `store_secret()`, `delete_secret()`.
- `agency/settings_store.py`: the schema-validated, atomic `config.yaml` writer (it keeps `config.yaml.bak`), the `.env` writer and `set_platform_live`.
- `agency/db.py`: SQLite storage for videos, posts, approvals, ledger, events, staff status and key-value data.
- `agency/models.py`: dataclasses and the `VideoStatus` enum.
- `agency/llm.py`: the team's brain (`complete`, `complete_json`, `run_tools`, `image_block`). Catch `LLMError` around it.
  - Backends via `llm.backend: auto|api|claude_cli|mock` (default auto). `LLM.backend` says which one runs; `LLM.mock` is True only when the answers are canned (mock, or the CLI resting).
  - auto order: `AGENCY_MOCK_LLM=1` → mock; `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` → api; a usable `claude` → claude_cli; else mock.
  - claude_cli has a per-day cap `llm.cli_daily_calls` (db kv `llm.cli_calls`); when used up it falls back to mock until tomorrow with one event "המוח נח להיום". Every real call is logged (backend, purpose, duration, ok), never the prompt.
  - In claude_cli mode `run_tools` runs the tools itself first (`Tool.snapshot_inputs`; tools that change something never run that way) and makes ONE call without tools.
- `agency/llm_cli.py`: the claude_cli backend (the owner's own Claude Code plan, $0). Detection runs only `claude --version` / `--help` (cached; `claude.exe` preferred over `claude.cmd`). Each call is `claude -p --output-format json` with list args, the prompt on stdin, a private temp cwd, `--no-session-persistence --strict-mcp-config --setting-sources project` (+ `--safe-mode`), `--tools ""` and `--disallowedTools Bash,Edit,Write,WebFetch,WebSearch,NotebookEdit,Agent`.
  - **Never pass `ANTHROPIC_API_KEY` (or any agency secret) to the child** — it is removed from the child env; `CLAUDE_CODE_OAUTH_TOKEN` goes into the child env only, never argv.
  - **Tools stay off**, except `Read` for analyst frames copied into the temp dir (`--tools Read --add-dir <frames>`, no unscoped `--allowedTools Read`).
  - Do not add `--bare`: it makes Claude Code accept only an API key and ignore the owner's login.
- `agency/pipeline.py` and `agency/scheduler.py`:
  - `run_pipeline` chains analyst, editor, copywriter, strategist, then posts, then the approval.
  - `execute_approval` carries out an approved item.
  - `tick` publishes posts that are due; `maybe_refresh_stats` pulls stats of really published posts every 6 hours (read-only, no LLM) so the home numbers move.
  - The strategist uploads once per YouTube channel (a vertical or ≤60 s clip goes to Shorts only) and gives a new clip the next day that has no other clip queued on that platform.
- `agency/brand.py`: the brand profile (`brand:` in config) and code-level enforcement: blocked words, brand hashtags, AI label, music credit.
- `agency/home.py`: the home screen's manager sentence, results, team and progress (rule-based, not LLM).
- `agency/connect.py`: connection tiles, real read-only verification, `save_keys`, `disconnect`, and the direct YouTube OAuth.
- `agency/importer.py`: the read-only studio import and the scan for existing Google/.env connection files (pickles are never opened).
- `agency/inbox.py`: the inbox folder watcher. It is OFF until the owner enables it; existing clips are a baseline and are not processed.
- `agency/notify/telegram.py`: Telegram approval cards and pairing. A 409 means the old studio's bot is polling.
- `agency/staff/`: `manager` (CEO), `analyst`, `editor`, `copywriter`, `strategist`, `publisher`, `promoter`, `community`, `analytics` and `finance` (CFO).
- `agency/platforms/`: one adapter per platform plus `zernio.py`. Subclass `PlatformAdapter`, override `_upload` and read credentials with `self.credential(NAME)`. The base class handles training mode and errors. Third-party imports are lazy.
- `agency/media/ffmpeg.py`: probe, the studio's encoding presets, loudness, thumbnail and frames. Use subprocess with list args only.
- `agency/cli.py`: the Typer CLI. Help text is Hebrew; console messages are English on Windows unless `AGENCY_CLI_LANG=he`.
- `agency/dashboard/`:
  - `app.py`: FastAPI with a Host allowlist, the `X-Agency-Request` CSRF header, PIN auth for phones and PC-only endpoints.
  - `views.py` and `auth.py`.
  - A vanilla JS page.
  - It binds 127.0.0.1, or 0.0.0.0 only with phone access on.
- `install.cmd` / `install.ps1` / `start.ps1` (Windows) and `install.sh` / `start.sh`:
  - They are launchers.
  - Their console output is English only, because Windows consoles render Hebrew reversed. Hebrew goes to WinForms message boxes.
  - The `.ps1` files are ASCII apart from a UTF-8 BOM; Hebrew strings are UTF-8 Base64.
- `.claude/agents/*.md`: Claude Code subagents that mirror the staff. Use `manager` as the orchestrator, e.g. `claude --agent manager`.
- Owner docs, in Hebrew: `README.md` (short), `docs/PLATFORMS_SETUP.md` (connect guides, linked from the wizards) and `docs/ADVANCED.md` (CLI, config, routes, secrets, security, architecture). Update them when commands or behaviour change.

## Commands
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[platforms,dev]'   # dev setup
AGENCY_MOCK_LLM=1 .venv/bin/pytest -q                                   # tests (offline)
.venv/bin/agency                       # no command = dashboard (+ scheduler, inbox watcher, Telegram)
.venv/bin/agency init | upload PATH --title T | status | approvals | approve ID | reject ID
.venv/bin/agency budget | expense AMT CAT DESC | income AMT SRC DESC | report | brief | chat
.venv/bin/agency analytics | tick | retry POST_ID | platforms | brain | dashboard
.venv/bin/agency import [PATH...] [--yes] [--no-history] | connect PLATFORM [--reset] | live PLATFORM on|off
```
On Windows the paths are `.venv\Scripts\agency.exe`. Install with `install.cmd` (or `install.ps1 -Unattended` from scripts). Start, restart and stop with `start.ps1 [-Restart|-Stop|-Foreground]`.

## Conventions for Claude Code in this repo
- **Never act for the owner without his explicit yes to that specific action in this conversation.** That covers:
  - approving or rejecting (`agency approve` / `reject`, Telegram, the dashboard, editing approvals in the DB)
  - switching a platform live (`agency live … on`, `live: true`, `dry_run: false`)
  - running an import (`agency import`, the import endpoints)
  - enabling the inbox watcher, Telegram or phone access
  - changing `publishing.mode` or `finance.*`
  - uploading or preparing one of his clips
- Never create accounts, type passwords or paste keys for the owner, and never ask him to paste a key into a chat. Keys go into the dashboard's connect wizard, a masked field on his PC.
- Never touch the studio (rule 9) and never disable its tasks yourself.
- Do not add a paid API key (e.g. `ANTHROPIC_API_KEY`). The owner's budget is $0.
- Owner communication:
  - Hebrew only, very short and simple, zero technical words.
  - At most ONE yes/no question at a time.
  - Refer to video titles, never database ids.
  - Never print Hebrew to a Windows console.
- Do not commit unless asked.
- Use Python ≥ 3.10 with type hints and short docstrings. Owner-facing strings are in Hebrew; prompts, code and public platform copy are in English.
- Claude API usage follows the rules in SPEC.md (`llm.py`):
  - model `claude-opus-5-5`
  - `output_config` for effort and JSON format
  - no `temperature`, no thinking budgets, no prefill, no forced `tool_choice`
