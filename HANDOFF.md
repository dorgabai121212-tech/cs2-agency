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

## Verified on the new PC (18/10/2026, read-only check by the PC session)
- PC name DESKTOP-4P10HRU. Python 3.12.10, git 2.55, ffmpeg n9.0.1 at `C:\CS2_STUDIO\tools\ffmpeg`.
  `git ls-remote` to the private repo works; `gh` is not logged in.
- **Credential Manager service `cs2studio` is EMPTY on the new PC.** zernio, meta_page_token,
  youtube_oauth, telegram_bot and hmac_key are all missing; the credentials did not migrate.
  The accounts are still connected on the platform side: YouTube and TikTok inside Zernio, and the
  Meta app is Live. So the owner has to re-enter keys once:
  - **Zernio API key**: zernio.com → Settings → API Keys → new key. One key brings back YouTube +
    TikTok without any re-login.
  - **Meta Page token**: has to be generated again.
  - **Telegram bot token**: @BotFather → /mybots → API Token.
- `studio.db` exists and has kv `meta_ids` → Page id and IG user id can be imported.
- Scheduled tasks "CS2Studio Daemon"/"CS2Studio Watchdog" exist but are DISABLED; daemon.log last
  written 13/10. The old studio is not running.
- Last published package: Episode 2 "the 4th guy" (10/10, YT+FB+IG+TT). Episode 3 was rejected by
  the owner. A new short `Desktop\CS2_CLIPS\SHORT_DEAGLE_MOVIE.mp4` (Deagle 3K, AI) awaits his decision.
UX consequence: when the studio is found but its keys are missing, the import card must say it
plainly and positively ("מצאתי את הסטודיו. החיבורים עצמם שמורים אצל הפלטפורמות — צריך רק להדביק מפתח אחד של Zernio כדי להחזיר את יוטיוב וטיקטוק"),
then lead straight into the Zernio key wizard, followed by Meta and Telegram.

See SPEC_V2.md §11 for how the agency aligns with all of this.
