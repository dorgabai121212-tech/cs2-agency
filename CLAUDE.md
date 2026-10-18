# CLAUDE.md: CS2 Agency

This is a local, self-hosted "marketing agency" of AI employees for one Counter-Strike 2 video creator, the **owner**. The owner speaks Hebrew and mostly uses Windows.

The owner drops in a video. The staff then:
1. analyses it
2. edits platform variants
3. writes copy
4. picks platforms and times
5. waits for owner approval
6. uploads
7. promotes, drafts community replies, tracks stats and tracks money

Exactly **one manager** (the CEO, "המנכ\"ל") sits above all the staff. The owner controls everything from the `agency` CLI and a local Hebrew RTL dashboard.

`SPEC.md` is the binding contract for layout, signatures and behaviour. Read it before changing core code, and do not change core signatures.

## Iron rules (enforced in code; keep them enforced)
1. **Money never moves without owner approval.**
   - Any cost > 0 becomes a `spend` approval with status `pending`.
   - Execution happens only after the owner sets it to `approved`.
   - The hard caps `finance.monthly_budget_ils` and `finance.max_single_spend_ils` refuse over-cap spends even when approved.
   - We never call ad APIs. An approved spend records a ledger entry and gives the owner manual instructions.
2. **DRY_RUN by default.** With `dry_run: true`, adapters return `PostResult(dry_run=True)` without any network call. Real uploads happen only with `dry_run: false` **and** credentials present.
3. **Publishing approval mode.** `publishing.mode: approve` is the default and makes every upload plan wait for the owner. `auto` schedules posts directly. Spends and replies always need approval.
4. **No fake engagement.** No bots, no bought views, likes or followers, no spam and no vote manipulation. Respect platform ToS and rate limits. Reddit and Discord posts go only to the places the owner listed in config. Do not add such features.
5. **Secrets live only in `.env`**, never in `config.yaml`, logs, the DB or LLM prompts. Never print or commit `.env`, `data/youtube_token.json` or OAuth client JSON files.
6. **Offline tests.** With no `ANTHROPIC_API_KEY`, or with `AGENCY_MOCK_LLM=1`, the LLM layer returns deterministic mock output. Tests need no network and no ffmpeg.
7. **Fail closed and honestly.** An adapter error sets post status `failed` with the error text. Never report success that did not happen.
8. **The manager can never approve.** It has no approve tool, by design and covered by tests. Do not add one.

## Layout
- `agency/config.py`: `Settings`, `load_settings()` and `secret()`. Settings come from `config.yaml` and secrets from `.env`.
- `agency/db.py`: SQLite storage for videos, posts, approvals, ledger, events, staff status and key-value data.
- `agency/models.py`: dataclasses and the `VideoStatus` enum.
- `agency/llm.py`: the Claude wrapper (`complete`, `complete_json`, `run_tools`) plus mock mode. Catch `LLMError` around it.
- `agency/pipeline.py` and `agency/scheduler.py`:
  - `run_pipeline` chains analyst, editor, copywriter, strategist, then posts, then the approval.
  - `execute_approval` carries out an approved item.
  - `tick` publishes posts that are due.
- `agency/staff/`: `manager` (CEO), `analyst`, `editor`, `copywriter`, `strategist`, `publisher`, `promoter`, `community`, `analytics` and `finance` (CFO).
- `agency/platforms/`: one adapter per platform. Subclass `PlatformAdapter` and override `_upload`. The base class handles dry-run and errors. Third-party imports are lazy.
- `agency/media/ffmpeg.py`: probe, vertical 9:16 clip, trim, thumbnail and frames. Use subprocess with list args only.
- `agency/cli.py`: the Typer CLI, with Hebrew help text.
- `agency/dashboard/`: a FastAPI app bound to 127.0.0.1, with a vanilla JS page.
- `.claude/agents/*.md`: Claude Code subagents that mirror the staff. Use `manager` as the orchestrator, e.g. `claude --agent manager`.
- `README.md` and `docs/PLATFORMS_SETUP.md` are owner docs in Hebrew. Update them when commands change.

## Commands
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # dev setup (add ,platforms for real uploads)
AGENCY_MOCK_LLM=1 .venv/bin/pytest -q                         # tests (offline)
.venv/bin/agency init | upload PATH --title T | status | approvals | approve ID | reject ID
.venv/bin/agency budget | expense AMT CAT DESC | income AMT SRC DESC | report | brief | chat
.venv/bin/agency analytics | tick | platforms | dashboard
```
On Windows the paths are `.venv\Scripts\agency.exe` and `install.ps1` / `start.ps1`.

## Conventions for Claude Code in this repo
- **Never run `agency approve` or `agency reject` on the owner's behalf**, and never edit approvals in the DB. Approving is the owner's call alone.
- Do not set `dry_run: false` or change `publishing.mode` or `finance.*` unless the owner explicitly asks.
- Do not commit unless asked.
- Use Python ≥ 3.10 with type hints and short docstrings. Owner-facing strings are in Hebrew; prompts and code are in English.
- Claude API usage follows the rules in SPEC.md (`llm.py`):
  - model `claude-opus-5-5`
  - `output_config` for effort and JSON format
  - no `temperature`, no thinking budgets, no prefill, no forced `tool_choice`
