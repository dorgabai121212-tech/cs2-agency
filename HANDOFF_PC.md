# HANDOFF_PC: install the new agency on Dor's PC

**Who this is for:** the Claude Code session on Dor's Windows PC (it runs from `C:\NEXA-HANDOFF`, Windows user `dorga`, PC `DESKTOP-4P10HRU`). For background, read `HANDOFF.md` and `SPEC_V2.md` §11 in the repo.

**Goal and definition of done:**
- [ ] The private repo `dorgabai121212-tech/cs2-agency` is cloned to `C:\CS2_AGENCY`, next to `C:\CS2_STUDIO`.
- [ ] `install.ps1` finished. The Desktop and Start Menu have the shortcut "Imagination Agency".
- [ ] The studio import ran (read-only for the studio).
- [ ] The dashboard is running (started through the shortcut) and is open in Dor's browser.
- [ ] Every connection was checked. Only names and states were looked at, never values.
- [ ] Everything is still in **training mode**.
- [ ] Dor got one very short Hebrew message with exactly one yes/no question.

## 0. Rules for this whole job (non-negotiable)
1. **Talking to Dor:**
   - Hebrew only, very short and simple, zero technical words.
   - ONE yes/no question at a time.
   - Never mention ids, paths or commands to him.
2. **The console:**
   - Never print Hebrew to the Windows console; it renders reversed.
   - Everything below prints English only. Hebrew goes only in your chat reply.
   - When a result contains Hebrew, write it to a UTF-8 file and read that file with your Read tool.
3. **Secrets:**
   - Never print, echo, `cat` or `Get-Content` anything from `.env`, `data\secrets\` or Credential Manager values.
   - Never ask Dor to paste a key into this chat. He pastes keys himself into the dashboard's connect window, which has a masked field.
   - Never type passwords or create accounts for him.
4. **The old studio `C:\CS2_STUDIO` is read-only:**
   - Do not edit, move or delete anything in it.
   - Do not run its `.cmd` files or tools, and do not start its daemon.
   - Do not change its scheduled tasks.
   - Never write to the Credential Manager service `cs2studio`.
5. **Training mode stays on:**
   - Never run `agency live … on`.
   - Never change `dry_run`, `publishing.mode` or `finance.*` in `config.yaml`.
   - Switching a platform OFF is always allowed.
6. **Without Dor's explicit yes to that exact action, do not:**
   - approve or reject anything
   - upload or "prepare" any clip. `Desktop\CS2_CLIPS\SHORT_DEAGLE_MOVIE.mp4` waits for his own decision.
   - enable the clip folder, Telegram, phone access or start-with-Windows
   - disable the old studio
   - import anything beyond step 4
7. **No paid keys.** Do not add `ANTHROPIC_API_KEY` or any other paid key; Dor's budget is $0. The team writes demo texts until he decides otherwise. That is expected.
8. **Code and git:**
   - Do not edit the agency's code on the PC, and do not commit or push.
   - If something in the code is broken, stop.
   - Write the technical details (English, no secrets) to `C:\CS2_AGENCY\data\logs\handoff_pc.txt`, and tell Dor in one short line.

**How to run the blocks:**
- The blocks below are PowerShell.
- If your shell tool is bash, first save the block with your Write tool to `C:\NEXA-HANDOFF\tmp\step.ps1`.
- Then run it with `powershell -NoProfile -ExecutionPolicy Bypass -File C:\NEXA-HANDOFF\tmp\step.ps1`.

## 1. Check the PC (read-only)
```powershell
python --version                                   # expect Python 3.12.x
git --version
"studio db: "     + (Test-Path C:\CS2_STUDIO\state\studio.db)               # expect True
"studio ffmpeg: " + (Test-Path C:\CS2_STUDIO\tools\ffmpeg\ffmpeg.exe)       # True = start.ps1 uses it
Get-ScheduledTask -TaskName 'CS2Studio Daemon','CS2Studio Watchdog' -ErrorAction SilentlyContinue |
    ForEach-Object { '{0}: {1}' -f $_.TaskName, $_.State }                # expect Disabled
"daemon.log last written: " + (Get-Item C:\CS2_STUDIO\logs\daemon.log -ErrorAction SilentlyContinue).LastWriteTime
```
If Python is missing, ask Dor first, then run `winget install -e --id Python.Python.3.12`. HANDOFF.md says 3.12.10 is installed.

## 2. Get the code
```powershell
if (Test-Path C:\CS2_AGENCY\.git) { git -C C:\CS2_AGENCY pull --ff-only }
elseif (Test-Path C:\CS2_AGENCY) { 'C:\CS2_AGENCY exists but is not a clone - STOP and report' }
else { git clone https://github.com/dorgabai121212-tech/cs2-agency.git C:\CS2_AGENCY }
git -C C:\CS2_AGENCY log --oneline -1
```
Use the default branch unless the person who sent you here named another one.

Stop and report in any of these cases:
- git asks for a login. `git ls-remote` worked before, so this should not happen, and you never type credentials.
- `pull` refuses because of local changes.

## 3. Install (no questions, no pop-ups)
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\CS2_AGENCY\install.ps1 -Unattended -NoStart
"exit code: $LASTEXITCODE"                          # expect 0
```
The output should show these lines, ending with `Done.`:
- `[OK] Found Python 3.12…`
- `[OK] Using the studio's ffmpeg…`
- `[OK] All libraries are installed.`
- `[OK] Shortcuts created…`

What the installer does:
- It creates `.venv`, `config.yaml`, `.env` and `data\`. The `.env` is the empty template; do not open it.
- It creates the shortcuts "Imagination Agency" on the Desktop and in the Start Menu (with Restart and Stop).
- It does **not** turn on start-with-Windows. That is Dor's choice for later: he double-clicks `install.cmd` again, or you run `install.ps1 -Unattended -Autostart` after his yes.

## 4. Import from the studio (the one import this handoff covers)
The import:
- links the studio
- copies the brand rules, the proven posting times, the Facebook/Instagram ids (not secrets) and the published-post history into the agency
- copies **no** keys; they would be read in place, and on this PC the `cs2studio` entries are empty anyway
- opens the studio DB read-only

Do **not** use `agency import --yes`. It also runs a generic file scan that could pull unrelated keys from other projects' `.env` files.
```powershell
Set-Location C:\CS2_AGENCY
New-Item -ItemType Directory -Force data\logs | Out-Null
@'
import json
from agency.config import load_settings
from agency import importer
r = importer.import_studio(load_settings())
h = r.get("history") or {}
d = r.get("daemon") or {}
print("found:", r.get("found"), "| dir:", r.get("dir"))
print("studio keyring entries (names):", r.get("keyring"))
print("meta ids found:", bool(r.get("meta_ids")))
print("config updated:", r.get("config_updated"))
print("history imported:", h.get("imported"), "| already there:", h.get("skipped"))
print("linked via studio keys:", r.get("linked"))
print("old daemon active:", d.get("active"), "| task enabled:", d.get("task_enabled"))
print("keys still needed:", [s["name"] for s in r.get("next_connect") or []])
print("problems:", len(r.get("problems") or []), "(details in data/logs/handoff_import.json)")
safe = {k: r.get(k) for k in ("found", "dir", "keyring", "config_updated", "linked", "history",
                              "problems", "message_en")}
safe["daemon_active"] = d.get("active")
with open("data/logs/handoff_import.json", "w", encoding="utf-8") as fh:
    json.dump(safe, fh, ensure_ascii=False, indent=1)
'@ | .\.venv\Scripts\python.exe -
```
Expected on this PC:
- `found: True`
- studio keyring entries `[]`. They did not survive the PC move; this is known.
- `meta ids found: True`
- `config updated` includes `studio.dir`, `meta.page_id` and `meta.ig_user_id`, plus brand and default-time keys
- `history imported` ≥ 5
- `old daemon active: False`
- keys still needed: `ZERNIO_API_KEY`, `META_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`
- `problems: 0`

## 5. Start the agency so that it outlives this session
Launch the Desktop shortcut through Explorer. That is the same as Dor double-clicking it, and the agency keeps running after this session ends.
```powershell
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Imagination Agency.lnk'
Start-Process explorer.exe -ArgumentList ('"' + $lnk + '"')
$ok = $false
foreach ($i in 1..60) {
    try { $h = Invoke-RestMethod http://127.0.0.1:8765/api/health -TimeoutSec 3
          if ($h.app -eq 'cs2-agency') { $ok = $true; break } } catch { }
    Start-Sleep -Seconds 2
}
"healthy: $ok"
```
The browser opens by itself on Dor's screen with the dashboard. That is intended.

If it is not healthy:
- Read `C:\CS2_AGENCY\data\logs\dashboard.log`. It is English and has no secrets.
- Fix only environment problems.
- If another program holds port 8765, report it. Never kill processes you did not start.

## 6. Check every connection (names and states only)
```powershell
$c = Invoke-RestMethod 'http://127.0.0.1:8765/api/connections?check=1' -TimeoutSec 180
$c.tiles | ForEach-Object {
    '{0,-16} state={1,-14} live={2,-5} via={3,-7} source={4,-7} missing={5}' -f `
        $_.key, $_.state, $_.live, $_.via, $_.source, ($_.missing -join ',') }
"keys saved in: $($c.keys_saved_in) | training: $($c.training)"
$home_ = Invoke-RestMethod http://127.0.0.1:8765/api/home -TimeoutSec 60
"mode: $($home_.mode) | studio found: $($home_.studio.found) | imported: $($home_.studio.imported) | old daemon active: $($home_.studio.daemon_active) | keys missing: $($home_.studio.keys_missing)"
Select-String -Path C:\CS2_AGENCY\config.yaml -Pattern '^dry_run:' | ForEach-Object { $_.Line }
```
Expected now:
- All 9 tiles are `not_connected` (or `needs_fix`), and **none is `live=True`**.
- youtube, youtube_shorts and tiktok are missing `ZERNIO_API_KEY`.
- instagram_reels and facebook are missing only `META_ACCESS_TOKEN`; their ids came from the studio.
- telegram is missing `TELEGRAM_BOT_TOKEN`.
- x, reddit and discord are not connected (planned for "phase 2").
- `keys saved in: keyring`, `mode: training`, `imported: True`, `old daemon active: False`, `dry_run: true`.

If a tile is `live=True`, switch it off: `.\.venv\Scripts\agency.exe live <key> off`. Then note it in `handoff_pc.txt`.

## 7. Report to Dor (Hebrew, at most 4 short lines, one yes/no question)
Pick the question by the situation:
- **A. The old studio is active** (`old daemon active: True`): "הסטודיו הישן עדיין פעיל — הסוכנות החדשה תחליף אותו?"
- **B. Otherwise, the keys are missing** (expected): "לפתוח לך עכשיו את חלון החיבור של Zernio? מפתח אחד מחזיר את יוטיוב וטיקטוק."
- **C. Otherwise:** "רוצה לנסות עכשיו קליפ אחד במצב אימון?"

Template for B:
```
הסוכנות החדשה מותקנת ופתוחה אצלך בדפדפן, ליד הסטודיו.
לקחתי מהסטודיו את ההגדרות ואת מה שכבר עלה. בסטודיו עצמו לא נגעתי.
הכל במצב אימון, שום דבר לא עולה באמת.
לפתוח לך עכשיו את חלון החיבור של Zernio? מפתח אחד מחזיר את יוטיוב וטיקטוק.
```
If something blocked you, send one short line saying what is missing, plus one yes/no question. For example: "ההתקנה נעצרה כי חסר Python. להתקין אותו עכשיו?"

## 8. After his answer
**Yes to B:**
1. Open the dashboard: `Start-Process http://127.0.0.1:8765/`.
2. Tell him in one line: "לחץ על החיבורים (התקע למעלה) ← יוטיוב, ותדביק שם את המפתח מ-Zernio (Settings ← API Keys)."
3. He pastes it himself, and you never see it.
4. Re-run step 6.
5. Then ask the next single question: Meta ("עכשיו פייסבוק ואינסטגרם?"), then Telegram. One at a time. `docs/PLATFORMS_SETUP.md` §1–3 has the exact clicks if he gets stuck.

**Yes to A:**
1. Disable the two old tasks. This deletes nothing, and the studio's files stay as they are:
   ```powershell
   Start-Process schtasks -Verb RunAs -Wait -ArgumentList '/Change /TN "CS2Studio Daemon" /DISABLE'
   Start-Process schtasks -Verb RunAs -Wait -ArgumentList '/Change /TN "CS2Studio Watchdog" /DISABLE'
   ```
   Windows asks Dor to confirm. Tell him first: "יקפוץ חלון של Windows — לחץ 'כן'".
2. If the old daemon process is still running, do not kill it. Tell Dor that a restart of the PC finishes the switch.

**No:** stop. Nothing else changes, and training mode stays on.

**Going live is a separate decision for later.** Do not ask about it in this handoff. If Dor asks, he flips "העלאה אמיתית" on the platform's tile himself (it asks "בטוח?"). He starts with one platform.

## 9. Useful facts
- Stop or restart the agency with the Start Menu shortcuts ("Stop/Restart Imagination Agency"), or through Explorer as in step 5.
  - `start.ps1 -Stop -Quiet` works from this session too.
  - A restart started from this session ends with the session, so prefer the shortcut.
- The agency's console output and log are English. Hebrew appears only in the browser, in message boxes and in Telegram.
- Where keys go: keys entered in the wizard go to Credential Manager service `cs2-agency`, never `cs2studio`. The agency can read `cs2studio` only.
- Phone access, Telegram pairing and the clip folder are all off. Each is a separate yes/no question for Dor on another day.
