# ===========================================================
#  CS2 Agency - start the dashboard (and the scheduler)
#     powershell -ExecutionPolicy Bypass -File .\start.ps1
#  Extra arguments are passed to `agency dashboard`, e.g.  -- --port 9000 --no-browser
# ===========================================================
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -LiteralPath $PSScriptRoot

$agency = Join-Path $PSScriptRoot '.venv\Scripts\agency.exe'
if (-not (Test-Path -LiteralPath $agency)) {
    Write-Host "[X] הסוכנות עוד לא מותקנת. הרץ קודם:" -ForegroundColor Red
    Write-Host "    powershell -ExecutionPolicy Bypass -File .\install.ps1"
    Read-Host "לחץ Enter כדי לסגור"
    exit 1
}

if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'config.yaml'))) {
    Write-Host "לא נמצא config.yaml - יוצר מהדוגמה..."
    & $agency init
}

Write-Host ""
Write-Host "מפעיל את לוח הבקרה... הדפדפן ייפתח לבד." -ForegroundColor Cyan
Write-Host "השאר את החלון הזה פתוח כדי שהמתזמן יפרסם בזמן. לעצירה: Ctrl+C"
Write-Host ""

& $agency dashboard @args
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ""
    Write-Host "[!] לוח הבקרה נעצר עם שגיאה (קוד $code). קרא את ההודעה למעלה." -ForegroundColor Yellow
    Write-Host "    אם הפורט תפוס - שנה dashboard.port ב-config.yaml."
    Read-Host "לחץ Enter כדי לסגור"
}
exit $code
