<div dir="rtl">

# מצב מתקדם: כל הפרטים הטכניים

הקובץ הזה מיועד למי שרוצה לדעת איך הסוכנות עובדת מבפנים: פקודות, הגדרות, נתיבי העלאה, איפה נשמרים
המפתחות, אבטחה ופתרון בעיות. לעבודה יומיומית לא צריך אותו. לוח הבקרה עושה הכל (ראו [README.md](../README.md)).
המפרט המחייב לבונים: `SPEC.md` (חוקי הברזל) ו-`SPEC_V2.md`. ההנחיות ל-Claude Code: `CLAUDE.md`.

תוכן:
1. [איך הסוכנות בנויה](#1-איך-הסוכנות-בנויה)
2. [התקנה והפעלה ידנית](#2-התקנה-והפעלה-ידנית)
3. [שורת הפקודה (CLI)](#3-שורת-הפקודה-cli)
4. [הגדרות (config.yaml)](#4-הגדרות-configyaml)
5. [מצב אימון והעלאה אמיתית לכל פלטפורמה](#5-מצב-אימון-והעלאה-אמיתית-לכל-פלטפורמה)
6. [נתיבי העלאה: Zernio, מטא וישיר](#6-נתיבי-העלאה-zernio-מטא-וישיר)
7. [מפתחות וסודות](#7-מפתחות-וסודות)
8. [ייבוא מהסטודיו הקיים וחיבורים קיימים](#8-ייבוא-מהסטודיו-הקיים-וחיבורים-קיימים)
9. [אישורים בטלגרם](#9-אישורים-בטלגרם)
10. [תיקיית קליפים](#10-תיקיית-קליפים)
11. [גישה מהטלפון: אבטחה](#11-גישה-מהטלפון-אבטחה)
12. [עיבוד וידאו](#12-עיבוד-וידאו)
13. [ה-API של לוח הבקרה](#13-ה-api-של-לוח-הבקרה)
14. [פיתוח ובדיקות](#14-פיתוח-ובדיקות)
15. [פתרון בעיות](#15-פתרון-בעיות)

---

## 1. איך הסוכנות בנויה

תוכנת Python אחת שרצה על המחשב שלך: שרת FastAPI (לוח הבקרה) + מתזמן + צוות "עובדים" שכל אחד מהם הוא
מודול קוד (חלקם משתמשים ב-Claude). מעל כולם יש מנכ"ל אחד. הוא מדבר איתך, מתאם את הצוות, ו**אין לו
יכולת לאשר**. זה נאכף בקוד ובבדיקות.

```
קליפ חדש  (לוח הבקרה · תיקיית קליפים · agency upload)
  → manager.handle_new_video
  → analyst      ניתוח: סוג קליפ, מפה, רגעים, חלון לשורט, ai_shots
  → editor       גרסאות לפי פלטפורמה (presets, ראו §12) + תמונה ממוזערת
  → copywriter   כותרות/תיאורים באנגלית לפי המותג (finalize_copy אוכף מילים חסומות, האשטגים, תווית AI)
  → strategist   פלטפורמות ושעות (publishing.default_times; אינסטגרם/טיקטוק ≥30 דק' אחרי יוטיוב;
                 העלאה אחת לכל ערוץ יוטיוב: קליפ אנכי או ≤60 שניות → Shorts בלבד)
  → posts (planned) + approval מסוג publish (pending)
  → אתה: "יאללה, תעלה" בלוח הבקרה · "אשר הכל" בטלגרם · agency approve
  → pipeline.execute_approval → posts scheduled
  → scheduler.tick (כל 60 שניות) → publisher (בדיקת העלאה כפולה מול הסטודיו) → adapter → תוצאה אמיתית
  → scheduler.maybe_refresh_stats (כל 6 שעות, קריאה בלבד, בלי Claude) → המספרים במסך הבית
```

**חשוב:** פוסט מתוזמן יוצא כשהגיע הזמן שלו **וכשהסוכנות רצה**. לכן יש אפשרות "להפעיל עם Windows".
אם המחשב היה כבוי, הפוסט יוצא בהפעלה הבאה.

| קובץ / תיקייה | תפקיד |
|---|---|
| `agency/config.py` | `Settings`, `load_settings()`, `secret()` (סדר החיפוש בסעיף 7), `platform_live()`, `platform_via()` |
| `agency/settings_store.py` | כתיבה בטוחה ל-`config.yaml` (סכמה, כתיבה אטומית, `config.yaml.bak`) ול-`.env` |
| `agency/db.py`, `models.py` | SQLite: סרטונים, פוסטים, אישורים, ספר הכספים, אירועים, מצב הצוות, kv |
| `agency/llm.py` | עטיפה ל-Claude + מצב הדגמה (בלי מפתח או `AGENCY_MOCK_LLM=1`) |
| `agency/pipeline.py`, `scheduler.py` | שרשרת העבודה, ביצוע אישור, `tick` |
| `agency/brand.py` | פרופיל המותג ואכיפה בקוד |
| `agency/home.py` | המשפט של המנכ"ל, התוצאות, הצוות וההתקדמות (מסך הבית) |
| `agency/connect.py` | אריחי החיבורים, בדיקת חיבור אמיתית, שמירת מפתחות, ניתוק |
| `agency/importer.py` | ייבוא מהסטודיו הקיים וסריקת קבצי חיבור |
| `agency/inbox.py` | תיקיית קליפים |
| `agency/notify/telegram.py` | בוט האישורים |
| `agency/platforms/` | adapter לכל פלטפורמה. `registry.py` בוחר נתיב (Zernio/ישיר) |
| `agency/media/ffmpeg.py` | probe, קידודים, עוצמת שמע, תמונה ממוזערת |
| `agency/staff/` | manager, analyst, editor, copywriter, strategist, publisher, promoter, community, analytics, finance |
| `agency/dashboard/` | `app.py` (שרת + אבטחה), `views.py`, `auth.py` (קוד PIN), `templates/`, `static/` |
| `agency/cli.py` | הפקודה `agency` |
| `.claude/agents/` | סוכני Claude Code שמקבילים לצוות (`claude --agent manager`) |

**תיקיית הנתונים** (`data/`, לא עולה ל-git):

| נתיב | מה יש בו |
|---|---|
| `data/agency.db` | כל המידע (כולל kv: מטמון בדיקות חיבור, חשבונות ו-uploads של Zernio, העלאות מטא באמצע, תיקיית קליפים, טלגרם, גיבובי ה-PIN והחיבורים מהטלפון) |
| `data/videos/` | עותקים של הקליפים והגרסאות שנחתכו (הקובץ המקורי שלך לא זז) |
| `data/secrets/` | `youtube_client_secret.json`, `youtube_token.json`, ו-`agency_hmac_key` כשאין כספת. הרשאות לבעלים בלבד |
| `data/logs/dashboard.log` | הפלט של הסוכנות כשהיא רצה ברקע |
| `data/inbox/` | תיקיית הקליפים, כשאין `Desktop\CS2_CLIPS` |
| `data/imagination.ico` | האייקון של קיצורי הדרך |

מחיקת `data/` מתחילה מאפס. ההגדרות (`config.yaml`), `.env` והמפתחות בכספת נשארים.

---

## 2. התקנה והפעלה ידנית

### Windows

| קובץ | מה הוא עושה |
|---|---|
| `install.cmd` | לחיצה כפולה. מריץ את `install.ps1` עם `-ExecutionPolicy Bypass` (רק להרצה הזאת) |
| `install.ps1` | בדיקת Python ≥ 3.10 (עם פקודת winget אם חסר), ffmpeg, עצירת סוכנות שרצה, `.venv`, `pip install -e ".[platforms]"`, `agency init`, קיצורי דרך, שאלת הפעלה עם Windows, הפעלה ופתיחת לוח הבקרה |
| `start.ps1` | מפעיל ברקע (בלי חלון), מחכה ל-`/api/health` ופותח דפדפן. אם הסוכנות כבר רצה, רק פותח דפדפן |

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 [-Unattended] [-NoStart] [-NoBrowser]
                                                       [-Autostart | -NoAutostart] [-NoShortcuts]
powershell -ExecutionPolicy Bypass -File .\start.ps1   [-NoBrowser] [-Restart] [-Stop] [-Foreground] [-Quiet]
```

- `-Unattended`: בלי שאלות ובלי חלונות הודעה (לסקריפטים ול-Claude Code). הפעלה עם Windows רק עם `-Autostart`.
- `-Restart`: נדרש אחרי הדלקת "גישה מהטלפון", כי הכתובת שהשרת מאזין לה נקבעת בהפעלה.
- `-Stop`: עוצר רק את הסוכנות שלנו. קודם בודק ש-`/api/health` עונה `{"ok": true, "app": "cs2-agency"}`.
- `-Foreground`: מריץ בחלון הנוכחי עם הפלט (Ctrl+C עוצר). ארגומנטים נוספים עוברים ל-`agency dashboard`,
  למשל `-Foreground --interval 30`.
- קיצורי דרך (שמות באנגלית בכוונה, עם תיאור בעברית): שולחן העבודה + תפריט התחל ← `Imagination Agency`
  (כולל Restart ו-Stop). הפעלה עם Windows = קיצור דרך בתיקיית Startup עם `-NoBrowser`.
- ffmpeg: אם הוא לא ב-PATH, `start.ps1` מוסיף את העותק של הסטודיו (`C:\CS2_STUDIO\tools\ffmpeg`).
- הפלט בחלון הפקודה באנגלית, כי חלונות פקודה ב-Windows מציגים עברית הפוכה. הודעות בעברית מופיעות בחלון
  הודעה של Windows, שמציג מימין לשמאל כמו שצריך. אותו כלל חל על ה-CLI: ב-Windows ההודעות באנגלית, אלא אם
  `AGENCY_CLI_LANG=he`.
- הקבצים `install.ps1` ו-`start.ps1` כתובים ב-ASCII בלבד. הטקסטים העבריים שמורים בהם כ-Base64 של UTF-8,
  כך ש-Windows PowerShell 5.1 קורא אותם נכון בכל קידוד.

### Linux / macOS

```bash
bash install.sh [--no-start]
bash start.sh                 # בחלון הזה (Ctrl+C עוצר)
bash start.sh --background    # ברקע, פלט ב-data/logs/dashboard.log
bash start.sh --stop          # עוצר את הסוכנות שהופעלה ברקע
```

### ידנית לגמרי

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[platforms,dev]'
.venv/bin/agency init
.venv/bin/agency              # בלי פקודה = לוח הבקרה + המתזמן + פתיחת דפדפן
```

---

## 3. שורת הפקודה (CLI)

ב-Windows: `.venv\Scripts\agency.exe <פקודה>`. בכל פקודה יש `--help` (בעברית).
אפשרות כללית: `--config PATH` (או `-c`). בלי פקודה, `agency` מפעיל את לוח הבקרה.

| פקודה | מה היא עושה |
|---|---|
| `agency init [--force]` | יוצר `config.yaml` ו-`.env` מהדוגמאות ואת `data/`. `.env` לעולם לא נדרס |
| `agency dashboard [--host H] [--port P] [--no-browser] [--interval S]` | לוח הבקרה + המתזמן + תיקיית הקליפים + בוט הטלגרם (אם הודלקו) |
| `agency upload PATH [--title T]` | קליפ חדש לצוות. נגמר באישור שמחכה לך |
| `agency status` | מצב הצוות, הסרטונים, האישורים והתקציב |
| `agency approvals [--all]` | אישורים שמחכים |
| `agency approve ID [--note N] [--now]` | אישור. **רק אתה מאשר.** Claude Code לא מריץ את זה בשמך |
| `agency reject ID [--note N]` | דחייה |
| `agency budget` · `report [--month YYYY-MM]` | תקציב ודוח חודשי |
| `agency expense AMOUNT CATEGORY DESC` · `income AMOUNT SOURCE DESC` | רישום הוצאה או הכנסה שכבר קרו |
| `agency brief` · `chat` | תדריך יומי, ושיחה עם המנכ"ל (ב-Windows הצ'אט עדיף בדפדפן) |
| `agency analytics` | משיכת נתונים ותובנות |
| `agency tick` | פרסום מה שהגיע זמנו (המתזמן עושה את זה כל דקה) |
| `agency retry POST_ID` | ניסיון חוזר לפוסט שנכשל, בלי אישור חדש |
| `agency platforms` | טבלה: פעילה, חיבור (כולל "דרך הסטודיו הקיים"), נתיב (`zernio`/`direct`), אימון/חי |
| `agency import [PATH...] [--yes] [--no-history]` | ייבוא מהסטודיו הקיים, ואחר כך סריקת קבצי חיבור (סעיף 8) |
| `agency connect PLATFORM [--reset]` | מקלידים מפתחות (מוסתרים), נשמר בכספת ונבדק חיבור אמיתי. גם `telegram` |
| `agency live PLATFORM on\|off [--yes]` | מתג ההעלאה האמיתית לפלטפורמה אחת. `on` שואל "בטוח?" ומסרב לפלטפורמה לא מחוברת |

שמות פלטפורמות: `youtube`, `youtube_shorts` (או `shorts`), `tiktok`, `instagram_reels` (או `instagram`, `ig`),
`facebook` (`fb`), `x` (`twitter`), `reddit`, `discord`, `telegram` (`tg`, רק ב-`connect`).

**משתני סביבה:**

| משתנה | משמעות |
|---|---|
| `AGENCY_ROOT` | תיקיית הסוכנות (ברירת מחדל: התיקייה הנוכחית אם יש בה `config.yaml`, אחרת תיקיית ההתקנה) |
| `AGENCY_DATA_DIR` | תיקיית נתונים אחרת (או `data_dir` ב-config) |
| `AGENCY_CLI_LANG` | `he` או `en` להודעות ה-CLI. ב-Windows ברירת המחדל `en` |
| `AGENCY_KEYRING=off` | בלי כספת: מפתחות רק מ-`.env` ומהסביבה (גם בבדיקות) |
| `AGENCY_STUDIO_DIR` | נתיב הסטודיו הקיים, או `off` לניתוק (אחרי `studio.dir` ב-config) |
| `AGENCY_MOCK_LLM=1` | מצב הדגמה בלי קריאות ל-Claude |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | המוח של הצוות. בלי מפתח = מצב הדגמה. **בתשלום**, וכרגע נקרא רק מ-`.env` או מהסביבה |

---

## 4. הגדרות (config.yaml)

הדרך המומלצת היא מסך ההגדרות בלוח הבקרה (`PUT /api/settings`). הוא בודק כל ערך מול סכמה, דוחה מפתחות לא
מוכרים וכל דבר שנראה כמו סוד, כותב בצורה אטומית ושומר את הגרסה הקודמת ב-`config.yaml.bak`. אפשר גם לערוך
ידנית ולהפעיל מחדש. **אף פעם לא שמים מפתחות ב-config.yaml.**

| חלק | מפתחות עיקריים |
|---|---|
| `channel` | `name`, `handle`, `language` (שפת הבעלים; הטקסטים לפלטפורמות באנגלית), `niche` |
| `brand` | `name`, `handle`, `bio`, `hashtags` (תמיד ראשונים), `blocked_words`, `style_notes`, `ai_label` (טקסט / `true` / `false`), `music_credit` |
| `dry_run` | המתג הראשי של מצב אימון (סעיף 5). ברירת מחדל `true` |
| `llm` | `model`, `effort` (low…max), `max_tokens`, `fallbacks` |
| `publishing` | `mode: approve\|auto`, `timezone`, `default_times.<פלטפורמה>: "HH:MM"`, `quiet_hours.{enabled,start,end}`, `stale_publishing_minutes`, `platforms.<פלטפורמה>.*` |
| `publishing.platforms.<key>` | `enabled`, `live`, `via: zernio\|direct` (יוטיוב/שורטס/טיקטוק), `privacy`/`privacy_level`, `format` (פייסבוק: `auto\|reels\|video`), `min_delay_after_youtube_min` (אינסטגרם, 30), `subreddits` (רדיט), `zernio_account_id` |
| `finance` | `monthly_budget_ils`, `max_single_spend_ils` (שניהם 0 = אין הוצאות) |
| `staff`, `community`, `promotion` | עובדים פעילים, `auto_reply: false`, הודעת דיסקורד |
| `dashboard` | `host` (127.0.0.1), `port` (8765), `remote_access` (גישה מהטלפון), `allowed_hosts`, `max_upload_gb` |
| `meta` | `page_id`, `ig_user_id` (לא סודות. הייבוא מהסטודיו ממלא אותם) |
| `studio` | `dir` (ריק = `C:\CS2_STUDIO` אם קיים, `off` = בלי קישור), `double_post_guard: true` |
| `inbox` | `enabled`, `folders`, `stable_seconds`, `interval_s` |
| `media` | `presets.{enabled,youtube_hq,loudnorm}`, `preset_speed` |
| `notify.telegram` | `enabled`, `chat_id` (מתמלא בצימוד), `send_preview`, `full_text`, `poll_timeout_s` |
| `importer` | `extra_roots` (תיקיות נוספות לסריקה) |

שעות ברירת המחדל הן השעות המוכחות מהסטודיו (שעון ישראל): יוטיוב ושורטס 17:00, פייסבוק 17:40, אינסטגרם 20:00,
טיקטוק 20:40. האסטרטג יכול לסטות מהן ביותר מ-5 דקות רק עם סיבה כתובה (`extra.deviation_reason`), והיא מוצגת
ב"פרטים" של הכרטיס.

---

## 5. מצב אימון והעלאה אמיתית לכל פלטפורמה

```
live(פלטפורמה) = מחוברת(פלטפורמה) AND settings.platform_live(key)
platform_live  = true  כש-dry_run: false (כל הפלטפורמות המחוברות חיות)
               = publishing.platforms.<key>.live == true  כש-dry_run: true (ברירת המחדל)
```

- `dry_run: true` נשאר כמתג בטיחות ראשי. כל פלטפורמה מודלקת בנפרד: במתג "העלאה אמיתית" באריח (עם חלון
  "מעכשיו הקליפים יעלו באמת ל-…. בטוח?"), או ב-`agency live youtube on`. הערך נשמר ב-`config.yaml`.
- אי אפשר להדליק פלטפורמה שלא מחוברת. פלטפורמה שחיה אבל איבדה את המפתח נכשלת עם הודעה ברורה, ולא "מצליחה".
- יוטיוב Shorts משתמש במתג ובנתיב של יוטיוב, אלא אם כתבת לו `live`/`via` משלו.
- במצב אימון ה-adapter מחזיר תוצאת אימון מסומנת (`dry_run: true`, מזהה `dryrun-…`) בלי שום קריאה לרשת.
  מסך הבית קורא לזה "מוכן באימון" ולא "עלה".
- `dry_run: false` מדליק את **כל** הפלטפורמות המחוברות בבת אחת. לא מומלץ. עדיף פלטפורמה אחת בכל פעם.
- ברירת המחדל ב-`config.example.yaml` תואמת את החשבונות שלך: יוטיוב, Shorts, טיקטוק, אינסטגרם ופייסבוק
  פעילים; X כבוי (עוד לא מחובר). מדליקים אותו ב-`publishing.platforms.x.enabled: true` אחרי החיבור.
- בדיקת חיבור שלא הגיעה לשירות (אין אינטרנט, או שהשירות לא עונה) מוצגת כ"החיבור שמור · לא הצלחתי לבדוק
  עכשיו", לא כ"צריך תיקון". היא נבדקת שוב אחרי דקה (תוצאה רגילה נשמרת 10 דקות). מפתח שגוי (401/403) כן
  מוצג כ"צריך תיקון".

```yaml
publishing:
  platforms:
    youtube: {enabled: true, live: true}      # יוטיוב + Shorts עולים באמת
    tiktok: {enabled: true}                    # טיקטוק עדיין באימון
```

---

## 6. נתיבי העלאה: Zernio, מטא וישיר

| פלטפורמה | נתיב ברירת מחדל | מה צריך | הערות |
|---|---|---|---|
| יוטיוב, Shorts | **Zernio** | `ZERNIO_API_KEY` | `via: direct` = Google ישיר (ראו בהמשך) |
| טיקטוק | **Zernio** | `ZERNIO_API_KEY` | `via: direct` = TikTok Content Posting API (`TIKTOK_ACCESS_TOKEN`) |
| אינסטגרם רילס | Meta Graph v24.0, ישיר | `META_ACCESS_TOKEN` + `IG_USER_ID` (או `meta.ig_user_id`) | לפחות 30 דק' אחרי יוטיוב |
| פייסבוק | Meta Graph v24.0, ישיר | `META_ACCESS_TOKEN` + `FB_PAGE_ID` (או `meta.page_id`) | `format: auto` = קליפ אנכי עד 90 שניות עולה כ-Reel |
| X | API v2 + OAuth1 | `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` | |
| רדיט | praw | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD` | רק לקהילות ב-`subreddits` |
| דיסקורד | Webhook | `DISCORD_WEBHOOK_URL` | רק לשרת שלך |

**Zernio** (zernio.com, לשעבר Late). זה הנתיב שהסטודיו הוכיח. התוכנית החינמית כוללת 2 חשבונות: יוטיוב וטיקטוק.
- התהליך: `POST /v1/media/presign` ← `PUT` של הקובץ (בלי כותרת הרשאה) ← `POST /v1/posts` עם פלטפורמה אחת
  בכל קריאה ← בדיקת מצב ב-`GET /v1/posts/{id}`. המפתח עובר רק בכותרת `Authorization`, ולא מופיע בלוגים
  או בהודעות שגיאה.
- מניעת כפילות: `Idempotency-Key: <sha256[:24]>:<platform>`. ניסיון חוזר אחרי כישלון ודאי מקבל `…:r<n>`.
  תשובת 409 עם `existingPostId` מאמצת את הפוסט הקיים במקום ליצור חדש.
- אם הפוסט עדיין בעיבוד כשזמן ההמתנה נגמר, הוא מסומן "נכשל" עם "still processing" ועם מזהה הפוסט ב-Zernio.
  המתזמן שואל את Zernio לבד (לכל היותר כל 5 דקות, עד 3 ימים) ומעדכן ל"עלה" עם הקישור, או לכישלון עם הסיבה.
- ניסיון חוזר לא יוצר פוסט שני: לפני `POST /v1/posts` נבדק הפוסט הקודם שלנו של אותו קובץ ואותה פלטפורמה.
  אם הוא עלה, הוא התוצאה. אם הוא עדיין בדרך, מדווחים עליו שוב. רק אחרי כישלון ודאי נוצר פוסט חדש.
- פוסט שנשלח לתזמון בענן של Zernio (`scheduledFor`) נשאר "מתוזמן" ולא "עלה". המתזמן לא מעלה אותו שוב,
  ואחרי השעה שלו בודק ב-Zernio מה קרה. "לפרסם עכשיו" על פוסט כזה מבטל קודם את התזמון שלו.
- החשבונות נשמרים במטמון ל-6 שעות (`zernio.accounts`), ו-uploads של אותו קובץ ל-20 שעות (Shorts וטיקטוק
  משתמשים באותה העלאה). חשבון שסומן "צריך חיבור מחדש" נבדק שוב לפני כל העלאה, ושמירה / בדיקה / ניתוק
  של המפתח בעמוד החיבורים מוחקים את המטמון.
- טיקטוק: לפני כל העלאה נבדק `creator-info`. דגלי ההסכמה של טיקטוק נשלחים רק לפוסט שאתה אישרת בעצמך
  (`plan.extra.owner_approved`, נרשם כשהאישור שלך מבוצע). המצב הנוכחי של `publishing.mode` לא משנה את זה:
  פוסט שתוזמן לבד במצב `auto` מסורב, ופוסט שאישרת נשאר מאושר. חיבור טיקטוק דרך Zernio מפרסם לציבור, ואין העלאה פרטית לבדיקה.
- תווית AI: `containsSyntheticMedia` (יוטיוב) ו-`videoMadeWithAi` (טיקטוק) מודלקים כשבקליפ יש שוטים של AI
  (`ai_shots` בניתוח) או כש-`brand.ai_label: true`.
- ביטול פרסום לעולם לא מוחק: פוסט שעוד לא יצא מבוטל. סרטון יוטיוב שכבר עלה **הופך לפרטי** (דרך חיבור הגוגל
  הישיר, אם קיים). בטיקטוק מקבלים הוראה למחוק ידנית.
- ה-adapter תומך ב-`scheduledFor` (תזמון בענן של Zernio), אבל כרגע המתזמן של הסוכנות מוסר את הפוסט ל-Zernio
  כשמגיע הזמן שלו. לכן המחשב צריך לפעול בשעת הפרסום.

**יוטיוב ישיר** (`via: direct`). העלאות דרך פרויקט Google Cloud שלא עבר אימות **ננעלות כפרטיות לתמיד.**
לכן זה לא ברירת המחדל, ואשף החיבור מזהיר מזה. חיבור הגוגל משמש בעיקר לנתונים, לתגובות ולהפיכת סרטון לפרטי.
- התחברות: מעלים את `client_secret*.json` באשף ← "התחבר עם גוגל" (InstalledAppFlow על המחשב, scopes
  `youtube.upload` + `youtube.readonly` + `youtube.force-ssl`).
- הקבצים: `data/secrets/youtube_client_secret.json` (או `YOUTUBE_CLIENT_SECRETS_FILE`) ו-`data/secrets/youtube_token.json`
  (מתחדש לבד). אפשרות נוספת: `YOUTUBE_OAUTH_JSON` (למשל `youtube_oauth` של הסטודיו), לקריאה בלבד ולא נכתב בחזרה.
  קובץ ישן `data/youtube_token.json` מועבר אוטומטית.

**מטא (אינסטגרם + פייסבוק):** טוקן עמוד (Page token) שלא פג, מהאפליקציה שלך במטא. פרטים ב-[PLATFORMS_SETUP.md](PLATFORMS_SETUP.md).
- אינסטגרם יוצא לפחות 30 דקות אחרי פוסט היוטיוב האמיתי של אותו סרטון (`min_delay_after_youtube_min`).
  האסטרטג מתזמן אותו כך מראש. אם בכל זאת הגיע תורו מוקדם מדי, הפרסום **לא יוצא**: הפוסט מסומן "נכשל" עם הסבר
  ("אפשר לנסות שוב אחרי 20:30"), לפני כל קריאה לרשת, ואז לוחצים "נסה שוב".
- ניסיון חוזר ממשיך את אותה העלאה (kv `meta.pending`) ולא מתחיל חדשה. לכן אין פוסט כפול.

**שמירה מפני העלאה כפולה עם הסטודיו** (`studio.double_post_guard: true`). לפני העלאה אמיתית ה-publisher פותח
את `studio.db` של הסטודיו לקריאה בלבד. אם אותו קליפ (לפי sha256 של הקובץ, או קישור לאותו asset) כבר עלה
לאותו חשבון, ההעלאה נדחית עם הודעה ברורה. יוטיוב ו-Shorts נחשבים חשבון אחד (גם בין הפוסטים שלנו: אותו קובץ
לא עולה גם כסרטון וגם כ-Short). מסד נתונים של הסטודיו שקיים אבל אי אפשר לקרוא אותו **חוסם** העלאה אמיתית
(אי אפשר לדעת אם הקליפ כבר עלה). אפשר לכבות את השמירה עם `studio.double_post_guard: false`.

---

## 7. מפתחות וסודות

**סדר החיפוש** של `agency.config.secret(NAME)`:
1. משתני הסביבה (כולל `.env`, שנטען בהפעלה ולא דורס משתנים אמיתיים)
2. הכספת של מערכת ההפעלה (Windows Credential Manager), שירות **`cs2-agency`**
3. **לקריאה בלבד**: השירות של הסטודיו הישן, **`cs2studio`**, דרך מפת כינויים:

| השם אצלנו | הרשומה ב-`cs2studio` |
|---|---|
| `ZERNIO_API_KEY` | `zernio` |
| `META_ACCESS_TOKEN` | `meta_page_token` |
| `TELEGRAM_BOT_TOKEN` | `telegram_bot` |
| `YOUTUBE_OAUTH_JSON` | `youtube_oauth` |

- **לעולם לא כותבים ל-`cs2studio`.** ניתוק פלטפורמה מוחק רק את העותקים שלנו (הכספת `cs2-agency`, `.env`,
  והתהליך הנוכחי). מפתח שמשמש פלטפורמה מחוברת אחרת נשאר רק אם היא צריכה גם משהו אחר (טוקן מטא נשאר
  לפייסבוק כשמנתקים אינסטגרם). יוטיוב וטיקטוק עולים רק עם מפתח Zernio אחד, אז ניתוק של אחד מהם מוחק את המפתח
  ומנתק את שניהם (`also_disconnected`). מפתח שהוגדר במשתני הסביבה של Windows ולא ב-`.env` לא נמחק ומדווח (`system`).
- מפתח שמוקלד באשף (או ב-`agency connect`) נשמר בכספת `cs2-agency` כשהיא עובדת, ואז עותק ישן ב-`.env` מתרוקן.
  כשאין כספת (או עם `AGENCY_KEYRING=off`), הוא נשמר ב-`.env`.
- מותר לשמור רק שמות מוכרים: ה-`required_secrets` של כל ה-adapters, ובנוסף `ANTHROPIC_API_KEY`,
  `ZERNIO_API_KEY` ו-`TELEGRAM_BOT_TOKEN`. מזהי מטא (`IG_USER_ID`, `FB_PAGE_ID`) הם לא סודות, ולכן הם נשמרים
  ב-`config.yaml` תחת `meta.*`.
- ה-API של לוח הבקרה אף פעם לא מחזיר ערכים. הוא מחזיר רק שמות, מצב חיבור, שם חשבון ו-`source`
  (`env`/`keyring`/`studio`). כשהמקור הוא `studio`, האריח מראה "מחובר דרך הסטודיו הקיים".
- מטמון בדיקות החיבור (10 דקות) שומר רק תוצאות, עם טביעת אצבע HMAC במפתח שקיים רק בזיכרון.
- מפתח ה-HMAC של כפתורי הטלגרם (`AGENCY_HMAC_KEY`) נוצר פעם אחת ונשמר בכספת, או ב-`data/secrets/agency_hmac_key`.
- `ANTHROPIC_API_KEY`: שכבת ה-LLM קוראת אותו כרגע רק מהסביבה, אז שמים אותו ב-`.env`. הוא עולה כסף,
  אז מוסיפים אותו רק בהחלטה שלך.
- סודות לא נכנסים ל-`config.yaml`, ללוגים, ל-DB או להודעות ל-Claude. לא משתפים ולא מעלים ל-git את `.env`,
  `data/secrets/` או קבצי `client_secret*.json` (כולם ב-`.gitignore`).

---

## 8. ייבוא מהסטודיו הקיים וחיבורים קיימים

**מהסטודיו** (`C:\CS2_STUDIO`, או `studio.dir` / `AGENCY_STUDIO_DIR`). הכל לקריאה בלבד. `studio.db` (וה-`-wal`
שלו) מועתקים לתיקייה זמנית פרטית ונפתחים שם ב-`mode=ro`, כך שאף קובץ לא נוצר ליד ה-DB של הסטודיו.
הקוד של הסטודיו לא מורץ (`config.py` נקרא עם `ast`).
- **מה נקרא:** אילו רשומות קיימות ב-`cs2studio` (שמות בלבד), `studio.db` kv `meta_ids` (עמוד + אינסטגרם),
  כללי המותג (`studio\config.py` + `prompts\copywriter.md`), שעות הפרסום (`SLOT` ב-`studio\package.py`),
  היסטוריית הפרסומים, והאם ה-daemon הישן פעיל.
- **מה נכתב (רק אצלנו):** `studio.dir`, `meta.page_id`, `meta.ig_user_id`, `brand.*` (רשימות מתמזגות, טקסטים
  רק כשהם ריקים), `publishing.default_times.*`, וההיסטוריה ב-DB שלנו (עם `source: studio` והתאריכים המקוריים).
- **מפתחות לא מועתקים.** הם נקראים במקום, דרך מפת הכינויים (סעיף 7). אחרי מעבר מחשב הרשומות של `cs2studio`
  עלולות להיות ריקות. במקרה כזה הכרטיס אומר את זה בפשטות ומוביל לאשף Zernio, אחר כך למטא ולטלגרם.
- **ה-daemon הישן פעיל?** (המשימה המתוזמנת "CS2Studio Daemon" מופעלת, או ש-`logs\daemon.log` השתנה ב-10
  הדקות האחרונות). לוח הבקרה שואל "הסטודיו הישן עדיין פעיל — הסוכנות החדשה תחליף אותו?" ומראה את הפקודות
  לכיבוי. **הסוכנות לעולם לא מכבה אותו בעצמה.** אתה מריץ (בחלון פקודה כמנהל):
  `schtasks /Change /TN "CS2Studio Daemon" /DISABLE` ו-`schtasks /Change /TN "CS2Studio Watchdog" /DISABLE`.
  עד אז כדאי להשאיר את הסוכנות באימון.

**סריקת קבצי חיבור** (אחרי הסטודיו, ב-`agency import` ובכפתור "ייבוא חיבורים קיימים"). הסריקה עוברת עד עומק 4
על קבצים קטנים מ-1MB, ומדלגת על `node_modules`, `.git` ו-`.venv`. איפה היא מחפשת: התיקייה שמעל הסוכנות (אבל
לעולם לא שורש כונן), הסטודיו, Documents, Desktop, OneDrive, Projects, `importer.extra_roots` וכל נתיב שנתת.
- `client_secret*.json` של Google: מועתק ל-`data/secrets/youtube_client_secret.json`.
- טוקן Google מסוג authorized-user: מועתק ל-`data/secrets/youtube_token.json`. אם אין בו `youtube.upload`,
  הוא מסומן "צריך התחברות מחדש".
- `token.pickle`: **לא נפתח לעולם.** הוא רק מדווח, וצריך להתחבר מחדש.
- קובצי `.env` עם שמות מוכרים: נשמרים בכספת (בלי לדרוס ערך קיים, אלא אם `overwrite`).
  **שים לב:** הסריקה יכולה למצוא `.env` של פרויקטים אחרים (למשל `ANTHROPIC_API_KEY` בתשלום). מסמנים רק מה שרוצים.
- ערכים לא מודפסים, לא נרשמים בלוג ולא נשלחים ל-LLM. בלוח הבקרה תוצאות הסריקה נשמרות בשרת 30 דקות,
  וה-apply מתייחס אליהן לפי מזהה בלבד.

---

## 9. אישורים בטלגרם

לא חובה. זה מחליף את הבוט של הסטודיו (למשל `@imagination_control_bot`) באותה חוויה של לחיצה אחת.
1. עמוד החיבורים ← טלגרם ← מדביקים את מפתח הבוט (מ-@BotFather ← `/mybots` ← API Token).
   אם הסטודיו עדיין מחזיק את המפתח בכספת שלו, הוא נקרא משם.
2. "צימוד" מציג קוד בן 6 ספרות. שולחים לבוט בצ'אט פרטי `/start <קוד>`. ה-chat id נשמר ב-`notify.telegram.chat_id`.
   "צימוד מחדש" מנתק **מיד** את הצ'אט הקודם (למשל טלפון שאבד) ומחכה לקוד מהצ'אט החדש. קודים שגויים נספרים
   לכל צ'אט בנפרד (5), כך שזר לא יכול לבטל את הקוד שלך; 50 שגיאות מכולם יחד מבטלות את הקוד.
   "נתק" בטלגרם מוחק גם את הצ'אט המצומד.
3. מעכשיו כל אישור פרסום או הוצאה מגיע לטלגרם: קודם הטקסטים המלאים שיתפרסמו, ואחר כך כרטיס עם שני כפתורים.
   "✅ אשר הכל" מריץ בדיוק את מה שלוח הבקרה מריץ. "⏸ לא עכשיו" משאיר את האישור מחכה. אחרי פרסום אמיתי
   מגיעה הודעת "עלה!" עם הקישור.
- כל כפתור חתום ב-HMAC-SHA256 וקשור לצ'אט המצומד, למזהה אקראי של מסד הנתונים הזה ולזמן יצירת האישור
  (כרטיס ישן לא יכול לאשר משהו אחר שקיבל אותו מספר). הודעות מכל צ'אט אחר נזרקות בשקט.
- הכרטיס זוכר אילו פלטפורמות היו "באימון" ואילו "בהעלאה אמיתית" כשנשלח. אם מתג השתנה מאז, "אשר הכל"
  לא מבצע כלום ונשלח כרטיס מעודכן.
- **שני מאזינים על אותו בוט מתנגשים.** אם ה-daemon הישן עדיין מאזין, טלגרם מחזיר 409. המצב מוצג כ"הבוט תפוס
  ע״י הסטודיו הישן", לא נשלח כלום, לוח הבקרה נשאר הערוץ היחיד, ויש ניסיון חוזר כל 60 שניות.
- המפתח הוא חלק מכל כתובת של Bot API, ולכן כתובות לא נרשמות בלוג. שורות ה-DEBUG של urllib3 מצונזרות.

---

## 10. תיקיית קליפים

כבויה עד שמדליקים אותה (הגדרות ← "תיקיית קליפים", או `inbox.enabled: true`).
- התיקיות: `inbox.folders`. כשהרשימה ריקה: `Desktop\CS2_CLIPS` (התיקייה של הסטודיו) אם קיימת, אחרת `data/inbox`.
- בכל הדלקה (גם אחרי כיבוי), הקליפים שכבר נמצאים בתיקייה נרשמים כ"בסיס" **ולא מעובדים**. הם מופיעים תחת
  "קליפים קודמים" עם כפתור "תכין לפרסום". קליפ בסיס ששינו את שמו או שהעתיקו אותו בתוך התיקייה נשאר קליפ
  בסיס (אותו גודל ותאריך שינוי, או אותו sha256).
- קליפ חדש מעובד רק אחרי שהגודל ותאריך השינוי שלו לא השתנו `stable_seconds` שניות (כלומר הוא סיים להיכתב).
  קובצי `.part`, `.tmp` ו-`.crdownload` לא נספרים. קליפ שכבר מוכר לפי sha256 לא מעובד שוב.
- הקליפ מועתק ל-`data/videos` (המקור לא זז) ועובר את כל השרשרת עד אישור. שום דבר לא מתפרסם מכאן.
  הערה בקובץ צמוד `<שם>.txt` (ההרגל מהסטודיו) משמשת כרמז לכותרת.

---

## 11. גישה מהטלפון: אבטחה

כבויה כברירת מחדל. **רק ב-Wi-Fi של הבית.** אין ואסור הוראות להפניית פורטים (port forwarding) לאינטרנט.
- הדלקה: הגדרות ← "גישה מהטלפון" ← הפעלה מחדש (תפריט התחל ← Restart Imagination Agency). אחרי ההפעלה מחדש
  השרת מאזין ב-`0.0.0.0`. נוצר קוד PIN בן 6 ספרות שמוצג **פעם אחת** על מסך המחשב, יחד עם קוד QR
  ל-`http://<IP-ברשת>:<port>/`. "קוד חדש" יוצר קוד אחר.
- ב-DB נשמר רק גיבוב scrypt עם salt של ה-PIN. חיבור מהטלפון מקבל cookie אקראי (32 בתים), מסוג `HttpOnly` ו-`SameSite=Strict`,
  לשלושים יום. ב-DB נשמר רק גיבוב sha256 שלו. "התנתק מכל המכשירים" מבטל את כולם, וכיבוי הגישה מהטלפון מבטל
  את כולם וגם מוחק את ה-PIN.
- הגבלת ניסיונות: 5 קודים שגויים מאותה כתובת (ב-IPv6 לפי רשת /64) בתוך 10 דקות נועלים אותה ל-10 דקות. 30 שגיאות
  מכל הכתובות יחד נועלות כניסה חדשה מכל הטלפונים: 10 דקות, ובכל פעם הבאה כפול (עד 24 שעות). אחרי 100 קודים
  שגויים מאז שהקוד נוצר, הקוד נמחק ויוצרים חדש במחשב. כל נעילה נרשמת ביומן ומופיעה כהודעה במסך הבית של
  המחשב. טלפונים שכבר מחוברים ממשיכים לעבוד. המחשב עצמו לא ננעל אף פעם.
- `GET /api/remote` רק קורא. קוד חדש נוצר רק ב-`POST /api/remote/pin` (בקשת GET מאתר זר לא יכולה להחליף אותו).
- הגישה מהטלפון כבויה בהפעלה (גם אם כובתה ידנית ב-`config.yaml`)? כל החיבורים מהטלפון וה-PIN נמחקים.
- המחשב עצמו (חיבור loopback אמיתי) לא צריך PIN. הכתובת נקבעת רק לפי ה-TCP peer. כותרות כמו `X-Forwarded-For`
  לא נקראות, ו-uvicorn רץ עם `proxy_headers=False`.
- רשימת Host מותרים (נגד DNS rebinding): שמות loopback, ובנוסף כתובות ה-LAN ושם המחשב כשהגישה מהטלפון פעילה.
  כל בקשה שאינה GET צריכה את הכותרת `X-Agency-Request: 1` ו-Origin תואם. גוף JSON מוגבל ל-2MB.
- פעולות **מהמחשב בלבד** (מטלפון מקבלים 403 `local_only`, גם אחרי כניסה): מפתחות, `client-secret`, התחברות
  לגוגל, ניתוק, סריקה וייבוא, צימוד טלגרם, מסך ה-PIN (`/api/remote`), "התנתק מכל המכשירים", ושינוי
  `dashboard.remote_access` או `inbox.folders`. הסיבה: הטלפון מדבר ב-HTTP רגיל בתוך הרשת הביתית.
- מצב ישן (לא מומלץ): `--host 0.0.0.0` כשהגישה מהטלפון כבויה. כל מכשיר אחר צריך את המפתח החד-פעמי `?key=…`
  שמודפס בהפעלה (ב-`start.ps1` הוא נכתב ל-`data/logs/dashboard.log`). עדיף להשתמש בגישה מהטלפון.

---

## 12. עיבוד וידאו

ההגדרות המוכחות מהסטודיו (`media.presets.enabled: true`):

| preset | מה | לאן |
|---|---|---|
| `vertical` | 1080x1920, עד 60fps, x264 High crf 17, preset slow, loudnorm דו-שלבי ל-−14 LUFS, AAC 192k | טיקטוק, אינסטגרם (`vertical_9x16.mp4`) |
| `youtube_hq` | הצלע הקצרה ל-1440 (lanczos), crf 15, AAC 256k. מעל 3 דקות: medium | יוטיוב (`youtube_1440.mp4`), Shorts (`youtube_shorts_1440.mp4`) |
| `long` | המקור כמו שהוא אם הוא MP4 תקין, אחרת crf 16 | שאר הפלטפורמות |
| `preview` | עד 45MB | תצוגה מקדימה בטלגרם |

- מחשב איטי: `media.preset_speed: "medium"`. בלי `youtube_hq`: `media.presets.youtube_hq: false`.
- ffmpeg ו-ffprobe נמצאים דרך PATH. ב-Windows, `start.ps1` מוסיף לבד את `C:\CS2_STUDIO\tools\ffmpeg` אם צריך.
  בלי ffmpeg כל הפלטפורמות מקבלות את הקובץ המקורי.

---

## 13. ה-API של לוח הבקרה

לכל בקשה שאינה GET נדרשת הכותרת `X-Agency-Request: 1`. נקודות שמסומנות ב-★ פתוחות רק מהמחשב.

| נקודה | מה |
|---|---|
| `GET /api/health` | `{"ok": true, "app": "cs2-agency"}` (גם בלי כניסה) |
| `GET /api/home` | מסך הבית: משפט המנכ"ל, "מחכה לך", תוצאות, כסף, צוות, התקדמות, הסטודיו, קליפים קודמים |
| `GET /api/videos/{id}/thumbnail` | תמונה ממוזערת. מוגש רק מתוך `data/` (גם אחרי מעקב קיצורי דרך) |
| `POST /api/approvals/{id}/approve` · `/reject` | ההחלטה שלך |
| `PATCH /api/approvals/{id}/plans/{platform}` | עריכת כותרת או תיאור לפני אישור בלבד (עובר את אכיפת המותג) |
| `POST /api/posts/{id}/retry` | ניסיון חוזר |
| `GET/PUT /api/settings` | ההגדרות (שינוי של `remote_access` או `inbox.folders` ★) |
| `GET /api/connections?check=1` | אריחי החיבורים עם בדיקה אמיתית (שמות בלבד) |
| `POST /api/connect/{p}/keys` ★ · `/verify` · `/disconnect` ★ · `/live` | חיבור, בדיקה, ניתוק, מתג אמיתי (`{"live": true, "confirm": true}`) |
| `POST /api/connect/youtube/client-secret` ★ · `/youtube/start` ★ · `GET /youtube/state` | יוטיוב ישיר |
| `POST /api/connect/import/scan` ★ · `/import/apply` ★ | ייבוא חיבורים |
| `POST /api/telegram/pair` ★ · `GET /api/telegram/state` | טלגרם |
| `GET /api/inbox` · `POST /api/inbox/prepare` | תיקיית קליפים (לפי מזהה, בלי נתיבים) |
| `GET /login` · `POST /api/auth/login` · `/logout` · `/revoke-all` ★ | כניסה מהטלפון |
| `GET /api/remote` ★ · `POST /api/remote/pin` ★ | מסך ה-PIN וה-QR |
| `POST /api/upload` · `POST /api/chat` | קליפ חדש, צ'אט עם המנכ"ל |
| `GET /api/status` · `videos` · `posts` · `approvals` · `finance` · `events` · `platforms` | מצב מתקדם |

---

## 14. פיתוח ובדיקות

```bash
.venv/bin/pip install -e '.[platforms,dev]'
AGENCY_MOCK_LLM=1 .venv/bin/pytest -q       # בלי רשת, בלי ffmpeg חובה, בלי מפתחות
```
- `tests/conftest.py` מגדיר `AGENCY_KEYRING=off` ו-`AGENCY_STUDIO_DIR=off`, כך שבדיקות לא נוגעות בכספת
  האמיתית או בסטודיו.
- כלל הקוד: Python ≥ 3.10, רמזי טיפוסים, הודעות לבעלים בעברית, prompts וקוד באנגלית. ב-Windows הפלט בחלון
  הפקודה באנגלית.
- adapter חדש: יורשים מ-`PlatformAdapter`, דורסים את `_upload`, קוראים מפתחות דרך `self.credential(NAME)`
  ומייבאים ספריות צד שלישי בעצלות. מצב אימון ושגיאות מטופלים במחלקת הבסיס.

---

## 15. פתרון בעיות

| בעיה | מה עושים |
|---|---|
| האייקון לא פותח כלום | מחכים כחצי דקה. אם לא נפתח, קוראים את `data\logs\dashboard.log`. הרצה עם פלט: `start.ps1 -Foreground` |
| "port is used by another program" | תוכנה אחרת תופסת את 8765. הפעלה מחדש של המחשב, או `dashboard.port` אחר ב-config |
| "running scripts is disabled" | מפעילים דרך `install.cmd` או האייקון (הם מריצים עם `-ExecutionPolicy Bypass`) |
| "python is not recognized" | `winget install -e --id Python.Python.3.12`, ואז שוב `install.cmd` |
| העדכון נכשל כי קבצים נעולים | `install.cmd` עוצר את הסוכנות לבד. אם זה ממשיך: תפריט התחל ← Stop Imagination Agency, ואז שוב |
| אין גרסה אנכית | ffmpeg חסר: `winget install -e --id Gyan.FFmpeg`, או שהעותק של הסטודיו קיים ב-`C:\CS2_STUDIO\tools\ffmpeg` |
| אריח "צריך תיקון: המפתח לא נכון או שפג תוקפו" | מדביקים מפתח חדש באשף. בטוקן של מטא בודקים שהוא טוקן **עמוד** ושלא פג (Access Token Debugger) |
| טלגרם: "הבוט תפוס ע״י הסטודיו הישן" | ה-daemon הישן עדיין מאזין לבוט. ראו סעיף 8 על כיבוי (אתה מחליט) |
| "still processing" (Zernio/טיקטוק) | הסוכנות בודקת לבד כל כמה דקות. `agency retry` בטוח: הוא בודק קודם את הפוסט הקודם ולא מעלה פעמיים |
| אינסטגרם "נכשל" עם "YouTube first" | הוא יוצא רק 30 דקות אחרי שהיוטיוב של אותו קליפ עלה באמת. לוחצים "נסה שוב" אחרי השעה שבהודעה |
| הטלפון לא נכנס | אותה רשת Wi-Fi? הסוכנות הופעלה מחדש אחרי ההדלקה? חומת האש של Windows צריכה לאשר את Python ברשת **פרטית** |
| הכל במצב הדגמה | אין `ANTHROPIC_API_KEY` ב-`.env`. בלי מפתח הצוות כותב טקסטים קבועים. המפתח בתשלום |

</div>
