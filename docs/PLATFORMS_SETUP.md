<div dir="rtl">

# חיבור הרשתות החברתיות: מדריך צעד אחר צעד

המדריך מסביר איך משיגים את המפתחות לכל רשת. **כל המפתחות נכנסים רק לקובץ `.env`**, שנמצא
בתיקיית הסוכנות. פותחים אותו עם `notepad .env`. לא מכניסים מפתחות ל-`config.yaml`.

**כללים כלליים:**
- אפשר להגדיר רשת אחת בכל פעם. רשת בלי מפתחות פשוט לא מוגדרת.
- אחרי כל שינוי ב-`.env` מפעילים מחדש את הסוכנות ובודקים:
  ```powershell
  .\.venv\Scripts\agency.exe platforms
  ```
  ברשת שהגדרת צריך להופיע **מוגדר**.
- כל עוד `dry_run: true`, שום דבר לא עולה באמת. גם אחרי שהגדרת מפתחות.
- הממשקים של הרשתות (אתרי המפתחים) משתנים מדי פעם. אם כפתור נקרא קצת אחרת ממה שכתוב כאן,
  חפש את המילה הקרובה ביותר.
- **אל תשתף אף מפתח עם אף אחד.** אם מפתח דלף, בטל אותו מיד באתר של הרשת וצור חדש.

תוכן:
1. [יוטיוב ויוטיוב שורטס](#1-יוטיוב-ויוטיוב-שורטס)
2. [טיקטוק](#2-טיקטוק)
3. [אינסטגרם רילס ופייסבוק (Meta)](#3-אינסטגרם-רילס-ופייסבוק-meta)
4. [X (טוויטר)](#4-x-טוויטר)
5. [רדיט](#5-רדיט)
6. [דיסקורד](#6-דיסקורד)

---

## 1. יוטיוב ויוטיוב שורטס

אותו חיבור משמש גם ליוטיוב הרגיל וגם לשורטס.

**מפתח ב-`.env`:** `YOUTUBE_CLIENT_SECRETS_FILE`

### א. יוצרים פרויקט ב-Google Cloud
1. נכנסים ל-https://console.cloud.google.com ומתחברים **עם חשבון הגוגל של הערוץ**.
2. למעלה, ליד הלוגו, לוחצים על בורר הפרויקטים, אחר כך **New Project**.
3. נותנים שם (למשל `cs2-agency`) ולוחצים **Create**. מוודאים שהפרויקט החדש נבחר.

### ב. מפעילים את YouTube Data API v3
1. בתפריט: **APIs & Services**, ואז **Library**.
2. מחפשים **YouTube Data API v3**, לוחצים עליו, ואז **Enable**.

### ג. מגדירים את מסך ההסכמה (OAuth consent screen)
1. **APIs & Services**, ואז **OAuth consent screen**. בממשק החדש זה נקרא **Google Auth Platform**.
2. סוג משתמש: **External**. ממלאים שם אפליקציה (למשל `CS2 Agency`) ואימייל.
3. במסך **Audience** או **Test users** מוסיפים את **האימייל של חשבון הערוץ** כמשתמש בדיקה.
4. שומרים. אין צורך לפרסם את האפליקציה (Publish), כי היא לשימוש שלך בלבד.

> **שים לב:** כשהאפליקציה במצב **Testing**, גוגל עלולה לבקש התחברות מחדש בערך פעם בשבוע.
> אם ההעלאה נכשלת עם שגיאת הרשאה, מחק את הקובץ `data\youtube_token.json` והתחבר שוב.

### ד. יוצרים OAuth Client מסוג Desktop
1. **APIs & Services**, ואז **Credentials**, ואז **Create Credentials**, ואז **OAuth client ID**.
2. Application type: **Desktop app**. נותנים שם ולוחצים **Create**.
3. לוחצים **Download JSON**.
4. שומרים את הקובץ בתיקיית הנתונים של הסוכנות, למשל `C:\cs2-agency\data\client_secret.json`.
   התיקייה `data` לא עולה ל-git.
5. ב-`.env`:
   ```
   YOUTUBE_CLIENT_SECRETS_FILE=C:\cs2-agency\data\client_secret.json
   ```

### ה. החיבור הראשון
בהעלאה האמיתית הראשונה (אחרי `dry_run: false`) **ייפתח דפדפן**. בוחרים את חשבון הערוץ ומאשרים.
אם מופיעה האזהרה "Google hasn't verified this app", לוחצים **Continue**, כי זו האפליקציה שלך.
האסימון נשמר ב-`data\youtube_token.json` ומתחדש לבד. **אל תשתף את הקובץ הזה.**

### ו. מכסה (Quota)
- מכסת ברירת המחדל היא **10,000 יחידות ביום**. העלאת סרטון עולה בערך **1,600** יחידות,
  כלומר **בערך 6 העלאות ביום** (יוטיוב ושורטס ביחד).
- המכסה מתאפסת בחצות לפי שעון האוקיינוס השקט (בערך 10:00 בבוקר בישראל).
- אם צריך יותר: **APIs & Services**, ואז **YouTube Data API v3**, ואז **Quotas**, ומגישים בקשת
  הגדלה (צריך למלא טופס של גוגל).
- גוגל עלולה להגביל סרטונים שהועלו דרך אפליקציה שלא עברה בדיקה (audit) ולהשאיר אותם פרטיים.
  אם זה קורה, הגש בקשה לבדיקה דרך טופס ה-API Audit של יוטיוב.

### ז. ב-`config.yaml`
```yaml
publishing:
  platforms:
    youtube: {enabled: true}
    youtube_shorts: {enabled: true}
```

---

## 2. טיקטוק

**מפתח ב-`.env`:** `TIKTOK_ACCESS_TOKEN`

### א. יוצרים אפליקציה
1. נכנסים ל-https://developers.tiktok.com ומתחברים (מומלץ עם חשבון הטיקטוק של הערוץ).
2. **Manage apps**, ואז **Connect an app** (או Create app). ממלאים שם, תיאור, אייקון, קטגוריה,
   כתובת אתר וקישור למדיניות פרטיות ולתנאי שימוש. אם אין לך אתר, גם עמוד פשוט ב-GitHub Pages מספיק.
3. בהגדרות האפליקציה, תחת **Products**, מוסיפים:
   - **Login Kit**
   - **Content Posting API**, ובתוכו מפעילים **Direct Post**
4. תחת **Scopes** מסמנים: `user.info.basic`, `video.publish`, `video.upload`.
   כדי לקבל סטטיסטיקות מסמנים גם `video.list`.
5. מגישים את האפליקציה לבדיקה ראשונית (Submit for review), אם זה נדרש כדי לקבל גישה.

### ב. מקבלים Access Token
טוקן הגישה מתקבל בתהליך OAuth של Login Kit: מתחברים עם חשבון הטיקטוק, ומחליפים את הקוד
(code) שמתקבל בטוקן, דרך `https://open.tiktokapis.com/v2/oauth/token/`.
בסביבת **Sandbox** של האפליקציה אפשר להוסיף את החשבון שלך כ-**Target user** ולבדוק.
שמים את הטוקן ב-`.env`:
```
TIKTOK_ACCESS_TOKEN=act.xxxxxxxx
```
> טוקן גישה של טיקטוק פג אחרי כ-24 שעות. כשהוא פג, ההעלאה תיכשל עם שגיאה ברורה, וצריך לחדש
> אותו באמצעות ה-refresh token ולעדכן את `.env`.

### ג. ביקורת (Audit): חשוב!
**כל עוד האפליקציה לא עברה ביקורת (audit) של טיקטוק, כל הפוסטים יעלו כפרטיים** (`SELF_ONLY`):
רק אתה תראה אותם. זו מגבלה של טיקטוק, לא של הסוכנות.
- כדי לפרסם לכולם מגישים בקשת audit ל-Content Posting API, בפורטל המפתחים של האפליקציה.
- אחרי שהביקורת אושרה, מוסיפים ב-`config.yaml`:
  ```yaml
  tiktok: {enabled: true, privacy_level: PUBLIC_TO_EVERYONE}
  ```
- אם רמת הפרטיות שביקשת לא זמינה לחשבון, הסוכנות חוזרת אוטומטית ל-`SELF_ONLY`.
- ל-API של טיקטוק אין תזמון. המתזמן של הסוכנות מעלה בזמן שנקבע, כל עוד היא פועלת.

---

## 3. אינסטגרם רילס ופייסבוק (Meta)

**מפתחות ב-`.env`:** `META_ACCESS_TOKEN`, `IG_USER_ID` (לאינסטגרם), `FB_PAGE_ID` (לפייסבוק)

### א. מכינים את החשבונות
1. **עמוד פייסבוק:** צריך עמוד (Page) של הערוץ, לא פרופיל אישי. אם אין, יוצרים עמוד ב-facebook.com/pages/create.
2. **חשבון אינסטגרם מקצועי:** באפליקציית אינסטגרם: **הגדרות**, ואז **סוג חשבון וכלים**, ואז
   **מעבר לחשבון מקצועי**, ובוחרים **יוצר** או **עסק**.
3. **מקשרים את האינסטגרם לעמוד הפייסבוק:** בעמוד הפייסבוק: **הגדרות**, ואז **חשבונות מקושרים**,
   ואז **אינסטגרם**, ואז **חיבור**. אפשר לעשות את זה גם דרך Meta Business Suite.

### ב. יוצרים אפליקציה ב-Meta for Developers
1. נכנסים ל-https://developers.facebook.com, ואז **My Apps**, ואז **Create App**.
2. בוחרים use case שמתאים לניהול תוכן, כמו "Manage everything on your Page" או
   **Instagram API with Facebook Login**. סוג האפליקציה: **Business**.
3. מוסיפים את המוצרים **Facebook Login for Business** ו-**Instagram Graph API** (אם הם לא נוספו לבד).

### ג. מקבלים טוקן עם ההרשאות הנכונות
1. פותחים את **Graph API Explorer**: https://developers.facebook.com/tools/explorer
2. בוחרים את האפליקציה שלך, ואז **User Token**, ומוסיפים את ההרשאות:
   - `instagram_basic`
   - `instagram_content_publish` (לפרסום רילס)
   - `pages_show_list`
   - `pages_read_engagement`
   - `pages_manage_posts` (לפרסום בעמוד הפייסבוק)
   - `business_management` (אם זה נדרש)
   - אופציונלי, לתגובות ולסטטיסטיקות: `instagram_manage_comments`, `instagram_manage_insights`, `read_insights`
3. לוחצים **Generate Access Token** ומאשרים לעמוד ולחשבון האינסטגרם שלך.
4. **ממירים לטוקן ארוך-טווח:** ב-**Access Token Debugger**
   (https://developers.facebook.com/tools/debug/accesstoken) מדביקים את הטוקן ולוחצים **Extend Access Token**.
5. **לפייסבוק צריך טוקן של העמוד:** ב-Explorer מריצים `me/accounts`. ברשימה מופיעים העמודים שלך,
   ולכל אחד יש `id` ו-`access_token`. טוקן עמוד שנוצר מטוקן משתמש ארוך-טווח לא פג.
   שמים אותו כ-`META_ACCESS_TOKEN`.

### ד. מוצאים את המזהים
- **FB_PAGE_ID:** ה-`id` של העמוד מהתוצאה של `me/accounts`.
- **IG_USER_ID:** ב-Explorer מריצים:
  ```
  <FB_PAGE_ID>?fields=instagram_business_account
  ```
  ה-`id` שבתוך `instagram_business_account` הוא ה-`IG_USER_ID`.

### ה. ב-`.env`
```
META_ACCESS_TOKEN=EAAG...
IG_USER_ID=17841400000000000
FB_PAGE_ID=100000000000000
```

### ו. מצב האפליקציה
- כל עוד האפליקציה במצב **Development**, היא עובדת רק לחשבונות שהם מנהלים או בודקים באפליקציה.
  זה מספיק לשימוש שלך.
- לשימוש ציבורי צריך App Review, אבל לסוכנות אישית בדרך כלל אין בזה צורך.
- אינסטגרם מגביל את מספר הפרסומים דרך ה-API (בערך 50 ביממה), וזה הרבה יותר ממה שצריך.

### ז. ב-`config.yaml`
```yaml
instagram_reels: {enabled: true}
facebook: {enabled: true}
```

---

## 4. X (טוויטר)

**מפתחות ב-`.env`:** `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`

1. נכנסים ל-https://developer.x.com ונרשמים לחשבון מפתח, עם חשבון ה-X של הערוץ.
   בודקים את תוכניות הגישה העדכניות: לחלק מהיכולות יש מגבלות ולפעמים עלות.
2. ב-**Developer Portal**, ואז **Projects & Apps**, יוצרים **Project** ובתוכו **App**.
3. בהגדרות האפליקציה: **User authentication settings**, ואז **Set up**:
   - **App permissions:** בוחרים **Read and write**
   - **Type of App:** Web App, Automated App or Bot
   - Callback URL ו-Website: אפשר לשים `http://127.0.0.1` ואת אתר הערוץ
4. בלשונית **Keys and tokens**:
   - תחת **Consumer Keys** (API Key and Secret) לוחצים **Regenerate**. אלה `X_API_KEY` ו-`X_API_SECRET`.
   - תחת **Access Token and Secret** לוחצים **Generate**. אלה `X_ACCESS_TOKEN` ו-`X_ACCESS_SECRET`.
   - **חשוב:** יוצרים את ה-Access Token **אחרי** שמשנים את ההרשאה ל-Read and write. אם יצרת אותו
     לפני כן, צור אותו מחדש, אחרת הוא יהיה לקריאה בלבד.
5. ב-`.env`:
   ```
   X_API_KEY=...
   X_API_SECRET=...
   X_ACCESS_TOKEN=...
   X_ACCESS_SECRET=...
   ```
6. ב-`config.yaml`: `x: {enabled: true}`

---

## 5. רדיט

**מפתחות ב-`.env`:** `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`

1. מתחברים לרדיט עם החשבון שאיתו תפרסם.
2. נכנסים ל-https://www.reddit.com/prefs/apps, גוללים למטה ולוחצים **create another app...**
3. ממלאים:
   - **name:** `cs2-agency`
   - סוג: **script**
   - **redirect uri:** `http://localhost:8080`
4. לוחצים **create app**.
   - ה-**client id** הוא המחרוזת הקצרה שמופיעה מתחת לשם האפליקציה, ליד "personal use script".
   - ה-**secret** מופיע בשדה secret.
5. ב-`.env`:
   ```
   REDDIT_CLIENT_ID=...
   REDDIT_CLIENT_SECRET=...
   REDDIT_USERNAME=שם_המשתמש_שלך
   REDDIT_PASSWORD=הסיסמה_שלך
   ```
   > אם מופעל אצלך אימות דו-שלבי (2FA), אפליקציית script לא תוכל להתחבר בשם-משתמש וסיסמה.
6. ב-`config.yaml` רושמים **בדיוק** לאילו קהילות מותר לפרסם (בלי `r/`):
   ```yaml
   reddit: {enabled: true, subreddits: ["GlobalOffensive", "cs2"]}
   ```
   הסוכנות **לעולם לא תפרסם** לקהילה שלא מופיעה ברשימה.
7. **קרא את החוקים של כל קהילה.** בהרבה קהילות יש הגבלות על קידום עצמי (כלל אצבע: לא יותר
   מ-10% מהפעילות שלך). חשבון שמפרסם רק את התוכן שלו עלול להיחסם.

---

## 6. דיסקורד

**מפתח ב-`.env`:** `DISCORD_WEBHOOK_URL`

1. בשרת הדיסקורד **שלך**, לוחצים על גלגל השיניים ליד הערוץ שאליו יישלחו ההודעות (למשל `#סרטונים-חדשים`).
2. **Integrations**, ואז **Webhooks**, ואז **New Webhook**.
3. נותנים שם (למשל `CS2 Agency`), ואפשר להוסיף תמונה. מוודאים שנבחר הערוץ הנכון.
4. לוחצים **Copy Webhook URL**.
5. ב-`.env`:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/....
   ```
6. ב-`config.yaml`:
   ```yaml
   discord: {enabled: true}
   ```
   כדי שהמקדם יציע הודעה לדיסקורד על כל סרטון חדש, מוסיפים גם:
   ```yaml
   promotion:
     discord_webhook_enabled: true
   ```
- קבצים עד 25MB נשלחים כקובץ. קבצים גדולים יותר נשלחים כקישור בלבד.
- **כתובת ה-Webhook היא סוד.** כל מי שיש לו אותה יכול לשלוח הודעות לערוץ שלך. אם היא דלפה,
  מחק את ה-Webhook וצור חדש.

---

## בדיקה סופית

```powershell
.\.venv\Scripts\agency.exe platforms
```

| עמודה | משמעות |
|---|---|
| **פעילה** | `enabled: true` ב-`config.yaml` |
| **מפתחות API** | **מוגדר**, או **חסר:** עם שמות המפתחות שחסרים |
| **מצב** | **ניסיון** (dry_run פעיל) או **חי** (פרסום אמיתי) |

כשהכול מוגדר, משנים ב-`config.yaml` ל-`dry_run: false`, מפעילים מחדש, ומתחילים עם סרטון אחד
שאתה שולט בו לגמרי.

</div>
