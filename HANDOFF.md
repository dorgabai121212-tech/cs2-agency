# What we know about the owner's existing studio (C:\CS2_STUDIO)

Source: the owner's earlier Claude Code sessions on his Windows PC (read-only research, 18/10/2026).
No secret values are recorded here — only where they live.

## Machine
- New PC since ~14/10 (RTX 5070 Ti), Windows user `dorga`, home `C:\Users\dorga`.
- Claude Code on the PC runs from `C:\NEXA-HANDOFF`.
- After the PC move, the studio's scheduled tasks ("CS2Studio Daemon", "CS2Studio Watchdog") were NOT
  re-created; migration notes say "tokens sealed; tasks come back disabled". Whether the credentials
  survived the move is UNKNOWN → the importer must check and report plainly.

## Accounts (brand "Imagination", @imaginationcs2)
| Platform | State | Path |
|---|---|---|
| YouTube | live since 29/09 | uploads via **Zernio**; Google OAuth desktop client (project imagination-studio-509802) only for stats/comments/private |
| TikTok | live | via **Zernio** (personal account) |
| Instagram | live | Meta Graph API v24 direct, Meta app "Imagination Studio" (Live), never-expiring Page token |
| Facebook | live | same Meta app/token, Page "Imaginationcs2", posts Reels |
| Reddit | account only | manual comments in r/GlobalOffensive, no self-promo first weeks |
| X / Threads / Discord | not connected | planned "phase 2" |
| Telegram | control bot @imagination_control_bot | one-tap approvals |

Credentials: Windows Credential Manager, service `cs2studio`, keys `zernio`, `meta_page_token`,
`youtube_oauth`, `telegram_bot`, `claude_token`, `hmac_key`.
Meta IDs: `C:\CS2_STUDIO\state\studio.db`, table kv, key `meta_ids`.

## Studio layout
`studio\` (pipeline, gateway — the only publisher, bot, media, adapters zernio/meta/youtube/dryrun,
community, analyst, finance, manager), `tools\` (connect_web.py ← "חיבור פלטפורמות.cmd", setup_web.py ←
"הקמה.cmd", set_secret.py, install_tasks.ps1, watchdog.py, ffmpeg\ffmpeg.exe), `inbox\`, `assets\<id>\`,
`state\studio.db`, `logs\daemon.log`, `prompts\copywriter.md`, `research\BLUEPRINT.md`, `DEMAND.md`, `site\`.
Clips are produced in `C:\CS2_CINE` and finished clips land in `C:\Users\dorga\Desktop\CS2_CLIPS`.

## Pending (as of 13/10)
1. v12 in 1440p (`assets\20260926-0504-imagination-v12\yt_long.mp4`) — never uploaded, waiting for yes.
2. Two private test videos on YouTube (w-rEUe6qMN8, KVrQe_1CUws).
3. Episode 3 "Mozart phonk" (`Desktop\CS2_CLIPS\SHORT_EP3_MOZART_PHONK.mp4`) — waiting for approval.
4. YouTube phone verification (custom thumbnails, >15 min videos).

See SPEC_V2.md §11 for how the agency aligns with all of this.
