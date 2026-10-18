# ===========================================================
#  CS2 Agency - Windows installer
#  Run from PowerShell inside the project folder:
#     powershell -ExecutionPolicy Bypass -File .\install.ps1
#  (-ExecutionPolicy Bypass allows this one run only; it changes no system setting.)
# ===========================================================
[CmdletBinding()]
param(
    [switch]$NoStart   # do not offer to start the dashboard at the end
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -LiteralPath $PSScriptRoot

function Say([string]$msg)  { Write-Host $msg }
function Ok([string]$msg)   { Write-Host "[V] $msg" -ForegroundColor Green }
function Warn([string]$msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Fail([string]$msg) {
    Write-Host "[X] $msg" -ForegroundColor Red
    Write-Host ""
    Read-Host "לחץ Enter כדי לסגור"
    exit 1
}

Say ""
Say "==============================================="
Say "   סוכנות CS2 - התקנה"
Say "==============================================="
Say ""

# ---------------------------------------------------------------- 1. Python
function Get-PythonCommand {
    # Returns @(exe, args...) for a working Python >= 3.10, or $null.
    foreach ($cand in @('py -3', 'python', 'python3')) {
        $c = @($cand -split ' ')
        $exe = $c[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $extra = @()
        if ($c.Count -gt 1) { $extra = @($c[1..($c.Count - 1)]) }
        try {
            $ver = & $exe @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $ver) { continue }   # e.g. the Microsoft Store stub
        $parts = "$ver".Trim().Split('.')
        if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10)) {
            return , (@($exe) + $extra)
        }
        Warn "נמצא Python בגרסה $ver - צריך 3.10 ומעלה."
    }
    return $null
}

Say "בודק Python..."
$py = Get-PythonCommand
if (-not $py) {
    Fail ("לא נמצא Python 3.10 ומעלה.`n" +
          "    1. הורד מ- https://www.python.org/downloads/`n" +
          "    2. בהתקנה סמן 'Add python.exe to PATH'`n" +
          "    3. פתח PowerShell חדש והרץ את הסקריפט שוב.")
}
$pyExe = $py[0]
$pyArgs = @()
if ($py.Count -gt 1) { $pyArgs = $py[1..($py.Count - 1)] }
$pyVersion = & $pyExe @pyArgs --version
Ok "נמצא $pyVersion"

# ---------------------------------------------------------------- 2. ffmpeg (optional)
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Ok "ffmpeg מותקן - העורך יוכל לחתוך גרסאות אנכיות."
} else {
    Warn "ffmpeg לא נמצא. הסוכנות תעבוד, אבל בלי חיתוך לגרסאות אנכיות ובלי תמונות ממוזערות."
    Warn "להתקנה: winget install Gyan.FFmpeg   (ואז לפתוח PowerShell חדש)"
}

# ---------------------------------------------------------------- 3. venv
$venvPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPy) {
    Ok "סביבה וירטואלית (.venv) כבר קיימת."
} else {
    Say "יוצר סביבה וירטואלית (.venv)..."
    & $pyExe @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPy)) {
        Fail "יצירת הסביבה הווירטואלית נכשלה."
    }
    Ok "נוצרה סביבה וירטואלית."
}

# ---------------------------------------------------------------- 4. packages
Say "מתקין ספריות (זה יכול לקחת כמה דקות)..."
& $venvPy -m pip install --upgrade pip --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Warn "עדכון pip נכשל - ממשיך בכל זאת." }
& $venvPy -m pip install --disable-pip-version-check -e ".[platforms]"
if ($LASTEXITCODE -ne 0) {
    Fail "התקנת הספריות נכשלה. בדוק חיבור לאינטרנט ונסה שוב."
}
Ok "כל הספריות הותקנו."

# ---------------------------------------------------------------- 5. agency init
$agency = Join-Path $PSScriptRoot '.venv\Scripts\agency.exe'
if (-not (Test-Path -LiteralPath $agency)) { Fail "הפקודה agency לא נמצאה אחרי ההתקנה." }
Say "מכין הגדרות ותיקיית נתונים..."
& $agency init
if ($LASTEXITCODE -ne 0) { Fail "agency init נכשל." }

# ---------------------------------------------------------------- done
Say ""
Say "==============================================="
Ok  "ההתקנה הסתיימה!"
Say "==============================================="
Say "הצעדים הבאים:"
Say "  1. הדבק את מפתח Claude בקובץ .env:      notepad .env"
Say "     (בלי מפתח - מצב הדגמה)"
Say "  2. ערוך את שם הערוץ והתקציב:            notepad config.yaml"
Say "  3. הפעלה בכל פעם:"
Say "     powershell -ExecutionPolicy Bypass -File .\start.ps1"
Say ""
Say "מצב ניסיון (dry_run) פעיל - שום דבר לא יעלה באמת לרשתות עד שתשנה."
Say ""

if (-not $NoStart) {
    $answer = Read-Host "להפעיל את לוח הבקרה עכשיו? (כ/ל)  [y/n]"
    if ($answer -match '^(y|yes|כ|כן)$') {
        & (Join-Path $PSScriptRoot 'start.ps1')
    }
}
