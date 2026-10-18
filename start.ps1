# ===========================================================
#  Imagination Agency (CS2 Agency) - start the dashboard
#
#  Normally started by the "Imagination Agency" Desktop / Start Menu shortcut (no window).
#  By hand, from PowerShell in any folder:
#     powershell -ExecutionPolicy Bypass -File .\start.ps1 [-NoBrowser] [-Restart] [-Stop]
#                                                          [-Foreground] [-Quiet]
#  - Already running (the port answers /api/health)? Just opens the browser.
#  - Otherwise starts the agency in the background (no window) and opens the browser
#    when it is ready. Output goes to data\logs\dashboard.log.
#  - -Restart stops the running agency first (needed after switching phone access on).
#  - -Stop only stops it (only ever OUR agency: /api/health must answer first).
#  - -Foreground runs it in this window instead (Ctrl+C stops). Other arguments go to
#    "agency dashboard", e.g.  -Foreground --interval 30
#  - -Quiet never shows a message box (scripts, Claude Code). Message boxes are also
#    skipped automatically when input is redirected.
#  If ffmpeg is not on PATH, the old studio's copy (C:\CS2_STUDIO\tools\ffmpeg) is used.
#
#  Console text is ENGLISH on purpose: Windows consoles render Hebrew reversed.
#  Hebrew messages go to a Windows message box (it renders right-to-left correctly).
#  This file is plain ASCII (plus a UTF-8 BOM); the Hebrew texts are UTF-8 Base64 so that Windows
#  PowerShell 5.1 reads them correctly whatever the file encoding becomes.
# ===========================================================
[CmdletBinding()]
param(
    [switch]$NoBrowser,    # do not open the browser (the autostart shortcut uses this)
    [switch]$Restart,      # stop the running agency, then start it again
    [switch]$Stop,         # only stop the running agency
    [switch]$Foreground,   # run in this window and show the log (Ctrl+C stops)
    [switch]$Quiet,        # never show message boxes
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DashboardArgs   # passed to "agency dashboard" (with -Foreground only)
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = $PSScriptRoot
Set-Location -LiteralPath $Root
$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'data\logs'
$LogFile = Join-Path $LogDir 'dashboard.log'
$DefaultPort = 8765
$ReadyTimeoutS = 90

# Hebrew texts (UTF-8, Base64). In English:
#   TITLE          "Imagination - the agency"
#   NOT_INSTALLED  "Not installed yet. Double-click install.cmd in the agency folder."
#   PORT_BUSY      "Another program already uses the agency's address (port {0}). Restart the PC."
#   START_FAILED   "The agency did not start. Try again in a moment. Details saved in: {0}"
#   STOPPED        "The agency stopped."
#   NOT_RUNNING    "The agency is not running right now."
$HE = @{
    TITLE = 'SW1hZ2luYXRpb24g4oCUINeU16HXldeb16DXldeq'
    NOT_INSTALLED = ('15TXodeV15vXoNeV16og16LXldeTINec15Ag157Xldeq16fXoNeqLgrXnNeX16Ug16TXotee15nXmdedINei15wg' +
        'aW5zdGFsbC5jbWQg15HXqteZ16fXmdeZ16og15TXodeV15vXoNeV16ou')
    PORT_BUSY = ('16rXldeb16DXlCDXkNeX16jXqiDXm9eR16gg16rXldek16HXqiDXkdee15fXqdeRINeQ16og15TXm9eq15XXkdeq' +
        'INep15wg15TXodeV15vXoNeV16ogKNek15XXqNeYIHswfSkuCteU16TXotecINee15fXk9epINeQ16og15TXnteX' +
        '16nXkSDXldeg16HXlCDXqdeV15Eu')
    START_FAILED = ('15TXodeV15vXoNeV16og15zXkCDXotec16rXlC4g16DXodeUINep15XXkSDXoteV15Mg16jXkteiLgrXkNedINeW' +
        '15Qg15fXldeW16gsINeU16TXqNeY15nXnSDXoNep157XqNeVINeR16fXldeR16U6CnswfQ==')
    STOPPED = '15TXodeV15vXoNeV16og16DXotem16jXlC4='
    NOT_RUNNING = '15TXodeV15vXoNeV16og15zXkCDXqNem15Qg15vXqNeS16Iu'
}

function He([string]$Key) {
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($HE[$Key]))
}

function Test-InputRedirected {
    try { return [Console]::IsInputRedirected } catch { return $false }
}
$Interactive = (-not $Quiet) -and [Environment]::UserInteractive -and (-not (Test-InputRedirected))

function Say([string]$Message) { Write-Host $Message }
function Warn([string]$Message) { Write-Host "[!] $Message" -ForegroundColor Yellow }

function Show-Box {
    # A Hebrew message box (right-to-left). Does nothing in quiet / non-interactive runs.
    param([string]$Text, [string]$Icon = 'Information')
    if (-not $Interactive) { return }
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $owner = New-Object System.Windows.Forms.Form
        $owner.TopMost = $true
        $options = [System.Windows.Forms.MessageBoxOptions]::RtlReading -bor `
                   [System.Windows.Forms.MessageBoxOptions]::RightAlign
        [void][System.Windows.Forms.MessageBox]::Show($owner, $Text, (He 'TITLE'),
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::$Icon,
            [System.Windows.Forms.MessageBoxDefaultButton]::Button1, $options)
        $owner.Dispose()
    } catch { }
}

# ---------------------------------------------------------------- helpers
function Get-AgencyInfo {
    # Port and studio folder from the agency's own settings (config.yaml), via Python.
    # Prints JSON (ASCII only), never any secret. Falls back to the defaults.
    $info = @{ port = $DefaultPort; studio = '' }
    if (-not (Test-Path -LiteralPath $VenvPy)) { return $info }
    $code = "import json; from agency.config import load_settings as L; s = L(); " +
            "p = s.get('dashboard.port', $DefaultPort); d = s.studio_dir; " +
            "print(json.dumps({'port': int(float(p)), 'studio': str(d) if d else ''}))"
    $oldPref = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # Python warnings on stderr must not stop us
    try {
        $out = & $VenvPy -c $code 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $parsed = ("$out" | ConvertFrom-Json)
            if ($parsed.port) { $info.port = [int]$parsed.port }
            if ($parsed.studio) { $info.studio = [string]$parsed.studio }
        }
    } catch { } finally { $ErrorActionPreference = $oldPref }
    return $info
}

function Test-Agency([int]$Port) {
    # True only when OUR agency answers on this port.
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3 -UseBasicParsing
        return ($r.ok -eq $true -and $r.app -eq 'cs2-agency')
    } catch { return $false }
}

function Test-PortBusy([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $attempt = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $attempt.AsyncWaitHandle.WaitOne(800)) { return $false }
        $client.EndConnect($attempt)
        return $true
    } catch { return $false } finally { $client.Close() }
}

function Get-ListenerProcessId([int]$Port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
    } catch { }
    try {   # fallback for systems without the NetTCPIP module
        foreach ($line in (netstat -ano -p tcp)) {
            if ($line -match "^\s*TCP\s+\S+:$Port\s+\S+\s+\S+\s+(\d+)\s*$") { return [int]$Matches[1] }
        }
    } catch { }
    return $null
}

function Stop-Agency([int]$Port) {
    # Stops the agency listening on $Port. Only when /api/health says it is ours.
    if (-not (Test-Agency $Port)) { return $false }
    $procId = Get-ListenerProcessId $Port
    if (-not $procId) { Warn "The agency answers on port $Port but its process was not found."; return $false }
    Say "Stopping the agency (process $procId)..."
    try { Stop-Process -Id $procId -Force -ErrorAction Stop } catch { Warn "Could not stop it: $($_.Exception.Message)"; return $false }
    for ($i = 0; $i -lt 30; $i++) {
        if (-not (Test-PortBusy $Port)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return (-not (Test-PortBusy $Port))
}

function Find-FfmpegDir([string]$Studio) {
    # Folder with ffmpeg.exe + ffprobe.exe when they are not on PATH (the old studio ships them).
    if ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
        return $null
    }
    $candidates = @()
    foreach ($base in @($Studio, 'C:\CS2_STUDIO')) {
        if (-not $base) { continue }
        $candidates += (Join-Path $base 'tools\ffmpeg')
        $candidates += (Join-Path $base 'tools\ffmpeg\bin')
    }
    foreach ($dir in $candidates) {
        if ((Test-Path -LiteralPath (Join-Path $dir 'ffmpeg.exe')) -and
            (Test-Path -LiteralPath (Join-Path $dir 'ffprobe.exe'))) { return $dir }
    }
    return $null
}

function Open-Dashboard([int]$Port) {
    if ($NoBrowser) { return }
    try { Start-Process "http://127.0.0.1:$Port/" } catch { Warn "Open http://127.0.0.1:$Port/ in your browser." }
}

function Show-LogTail {
    # Only this run's lines (after the last start marker), and only plain ASCII ones: Hebrew
    # renders reversed in a Windows console (the message box explains the problem anyway).
    if (Test-Path -LiteralPath $LogFile) {
        Say "---- last lines of $LogFile ----"
        try {
            $lines = @(Get-Content -LiteralPath $LogFile -Tail 200 -Encoding UTF8)
            if ($lines.Count -eq 0) { return }
            $from = 0
            for ($i = $lines.Count - 1; $i -ge 0; $i--) { if ($lines[$i] -like '---- start *') { $from = $i; break } }
            $mine = @($lines[$from..($lines.Count - 1)] | Where-Object { $_ -notmatch '[^\x00-\x7F]' })
            $mine | Select-Object -Last 15 | ForEach-Object { Say $_ }
        } catch { }
    }
}

function Start-AgencyHidden([string]$FfmpegDir) {
    # cmd.exe (no window) runs Python and appends its output to the log file.
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    try {
        if ((Test-Path -LiteralPath $LogFile) -and ((Get-Item -LiteralPath $LogFile).Length -gt 5MB)) {
            Move-Item -LiteralPath $LogFile -Destination (Join-Path $LogDir 'dashboard.1.log') -Force
        }
        Add-Content -LiteralPath $LogFile -Value ("---- start {0} ----" -f (Get-Date -Format 's'))
    } catch { }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = Join-Path $env:SystemRoot 'System32\cmd.exe'
    $psi.Arguments = '/d /s /c ""' + $VenvPy + '" -m agency.cli dashboard --no-browser 1>>"' +
                     $LogFile + '" 2>&1"'
    $psi.WorkingDirectory = $Root
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $psi.EnvironmentVariables['PYTHONUNBUFFERED'] = '1'
    if ($FfmpegDir) {
        $psi.EnvironmentVariables['PATH'] = $FfmpegDir + ';' + $psi.EnvironmentVariables['PATH']
    }
    return [System.Diagnostics.Process]::Start($psi)
}

# ---------------------------------------------------------------- main
if (-not (Test-Path -LiteralPath $VenvPy)) {
    Write-Host "[X] The agency is not installed yet. Double-click install.cmd (or run install.ps1)." -ForegroundColor Red
    Show-Box (He 'NOT_INSTALLED') 'Warning'
    exit 1
}

$info = Get-AgencyInfo
$port = [int]$info.port

if ($Stop -or $Restart) {
    if (Stop-Agency $port) {
        Say "The agency stopped."
        if ($Stop) { Show-Box (He 'STOPPED'); exit 0 }
    } elseif ($Stop) {
        Say "The agency is not running (nothing answers as the agency on port $port)."
        Show-Box (He 'NOT_RUNNING')
        exit 3   # 3 = there was nothing to stop
    }
}

if (Test-Agency $port) {
    Say "The agency is already running: http://127.0.0.1:$port/"
    Open-Dashboard $port
    exit 0
}

if (Test-PortBusy $port) {
    Write-Host "[X] Port $port is used by another program. Close it, restart the PC, or change" -ForegroundColor Red
    Write-Host "    dashboard.port in config.yaml." -ForegroundColor Red
    Show-Box ((He 'PORT_BUSY') -f $port) 'Warning'
    exit 1
}

if (-not (Test-Path -LiteralPath (Join-Path $Root 'config.yaml'))) {
    Say "No config.yaml yet - creating it from the example..."
    $oldPref = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $VenvPy -m agency.cli init | Out-Null } catch { } finally { $ErrorActionPreference = $oldPref }
}

$ffmpegDir = Find-FfmpegDir $info.studio
if ($ffmpegDir) { Say "Using ffmpeg from $ffmpegDir" }

if ($Foreground) {
    if ($ffmpegDir) { $env:PATH = $ffmpegDir + ';' + $env:PATH }
    $env:PYTHONIOENCODING = 'utf-8'
    Say "Starting the agency in this window. Keep it open; Ctrl+C stops it."
    $extra = @('dashboard')
    if ($NoBrowser) { $extra += '--no-browser' }
    if ($DashboardArgs) { $extra += $DashboardArgs }
    & $VenvPy -m agency.cli @extra
    exit $LASTEXITCODE
}

Say "Starting the agency in the background..."
$proc = Start-AgencyHidden $ffmpegDir
$deadline = (Get-Date).AddSeconds($ReadyTimeoutS)
$ready = $false
while ((Get-Date) -lt $deadline) {
    if (Test-Agency $port) { $ready = $true; break }
    if ($proc.HasExited) { break }
    Start-Sleep -Milliseconds 700
}

if (-not $ready) {
    Write-Host "[X] The agency did not start. Log: $LogFile" -ForegroundColor Red
    Show-LogTail
    Show-Box ((He 'START_FAILED') -f $LogFile) 'Warning'
    exit 1
}

Say "The agency is running: http://127.0.0.1:$port/   (log: $LogFile)"
Say "To stop it: Start menu > Imagination Agency > Stop Imagination Agency"
Open-Dashboard $port
exit 0
