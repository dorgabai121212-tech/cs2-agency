# ===========================================================
#  Imagination Agency (CS2 Agency) - Windows installer
#
#  Easiest: double-click install.cmd in the agency folder.
#  From PowerShell (any folder; paths with spaces are fine):
#     powershell -ExecutionPolicy Bypass -File .\install.ps1 [-Unattended] [-NoStart]
#                                    [-NoBrowser] [-Autostart | -NoAutostart] [-NoShortcuts]
#  (-ExecutionPolicy Bypass allows this one run only; it changes no system setting.)
#
#  What it does (safe to run again; it is also the way to update):
#    1. checks Python >= 3.10 (prints the winget command when it is missing)
#    2. checks ffmpeg (on PATH, or the old studio's copy in C:\CS2_STUDIO\tools\ffmpeg)
#    3. stops a running agency (its files would be locked), creates .venv,
#       pip install -e ".[platforms]", agency init (config.yaml + .env + data, never overwritten)
#    4. shortcuts "Imagination Agency": Desktop + Start Menu (+ Restart / Stop), hidden start
#    5. asks once (Yes/No box) whether to start with Windows (Startup folder shortcut)
#    6. starts the agency and opens the dashboard - the "import from your studio" card
#       is waiting there
#  -Unattended: no questions and no message boxes (for scripts / Claude Code); autostart only
#  with -Autostart. Console text is ENGLISH on purpose (Windows consoles render Hebrew
#  reversed); the friendly Hebrew messages appear in Windows message boxes.
#  This file is plain ASCII (plus a UTF-8 BOM); Hebrew texts are UTF-8 Base64 (safe in Windows PowerShell 5.1).
# ===========================================================
[CmdletBinding()]
param(
    [switch]$Unattended,   # no questions, no message boxes
    [switch]$NoStart,      # do not start the agency at the end
    [switch]$NoBrowser,    # start it, but do not open the browser
    [switch]$Autostart,    # start with Windows (no question)
    [switch]$NoAutostart,  # do not ask about starting with Windows
    [switch]$NoShortcuts   # do not create Desktop / Start Menu shortcuts
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = $PSScriptRoot
Set-Location -LiteralPath $Root
$StartScript = Join-Path $Root 'start.ps1'
$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'
$AppName = 'Imagination Agency'
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

# Hebrew texts (UTF-8, Base64). In English:
#   TITLE           "Imagination - the agency"
#   NO_PYTHON       "Python is needed first: open PowerShell, paste
#                    winget install -e --id Python.Python.3.12, then double-click install.cmd again."
#   INSTALL_FAILED  "Setup failed (usually the internet). Check the connection and run install.cmd again."
#   AUTOSTART_Q     "Start the agency by itself every time the PC starts? (approved clips go out on time)"
#   DONE_START      "Ready! Desktop icon: Imagination Agency. Training mode: nothing is really published
#                    until you switch a platform on. The dashboard opens now; a card takes the
#                    connections from your studio."
#   DONE_NOSTART    "Ready! Double-click the Imagination Agency icon to open the dashboard. Training mode..."
#   DONE_START_LIVE / DONE_NOSTART_LIVE  the same, for an update while platforms already upload
#                    for real: "Note: some platforms upload for real - approved clips really go up."
#   DESC_*          shortcut descriptions (dashboard / restart / stop / starts with Windows)
$HE = @{
    TITLE = 'SW1hZ2luYXRpb24g4oCUINeU16HXldeb16DXldeq'
    NO_PYTHON = ('16bXqNeZ15og15zXlNeq16fXmdefIFB5dGhvbiDXnNek16DXmSDXlNeh15XXm9eg15XXqiAo16TXotedINeQ15fX' +
        'qikuCgoxLiDXpNeq15cg15DXqiDXqtek16jXmdeYINeU16rXl9ecLCDXl9ek16kgUG93ZXJTaGVsbCDXldek16rX' +
        'lyDXkNeV16rXlS4KMi4g15TXk9eR16cg15DXqiDXlNep15XXqNeUINeU15bXkNeqINeV15zXl9elIEVudGVyOgog' +
        'ICAgd2luZ2V0IGluc3RhbGwgLWUgLS1pZCBQeXRob24uUHl0aG9uLjMuMTIKMy4g15vXqdeW15Qg16DXktee16gs' +
        'INec15fXpSDXqdeV15Eg16TXotee15nXmdedINei15wgaW5zdGFsbC5jbWQu')
    INSTALL_FAILED = ('15TXlNeq16fXoNeUINec15Ag15TXptec15nXl9eUICjXkdeT16jXmiDXm9ec15wg15HXktec15wg15TXkNeZ16DX' +
        'mNeo16DXmCkuCteR15PXldenINep15nXqSDXl9eZ15HXldeoLCDXldec15fXpSDXqdeV15Eg16TXotee15nXmded' +
        'INei15wgaW5zdGFsbC5jbWQu')
    AUTOSTART_Q = ('15zXlNek16LXmdecINeQ16og15TXodeV15vXoNeV16og15zXkdeTINeR15vXnCDXpNei150g16nXlNee15fXqdeR' +
        'INeg15PXnNenPwoK15vXm9eUINen15zXmdek15nXnSDXqdeQ15nXqdeo16og16LXldec15nXnSDXkdeW157Xnywg' +
        '15LXnSDXkNedINep15vXl9eqINec16TXqteV15cg15DXldeq15Qu')
    DONE_START = ('15TXodeV15vXoNeV16og157Xldeb16DXlCEKCtei15wg16nXldec15fXnyDXlNei15HXldeT15Qg15nXqSDXoteb' +
        '16nXmdeVINeQ15nXmden15XXnzogSW1hZ2luYXRpb24gQWdlbmN5LgrXlNeb15wg157XqteX15nXnCDXkdee16bX' +
        'kSDXkNeZ157XldefOiDXqdeV150g15PXkdeoINec15Ag16LXldec15Qg15HXkNee16og16LXkyDXqdeq15PXnNeZ' +
        '16cg16TXnNeY16TXldeo157XlCDXkdei157XldeTINeU15fXmdeR15XXqNeZ150uCgrXnNeV15cg15TXkden16jX' +
        'lCDXoNek16rXlyDXoteb16nXmdeVINeR15PXpNeT16TXny4g15nXqSDXnNeaINeb15HXqCDXodeY15XXk9eZ15U/' +
        'INee15fXm9eUINec15og16nXnSDXm9eo15jXmdehINep15zXlden15cg157Xnteg15Ug15DXqiDXlNeX15nXkdeV' +
        '16jXmdedLg==')
    DONE_NOSTART = ('15TXodeV15vXoNeV16og157Xldeb16DXlCEKCtei15wg16nXldec15fXnyDXlNei15HXldeT15Qg15nXqSDXoteb' +
        '16nXmdeVINeQ15nXmden15XXnzogSW1hZ2luYXRpb24gQWdlbmN5LgrXnNeX15nXpteUINeb16TXldec15Qg16LX' +
        'nNeZ15Ug16TXldeq15fXqiDXkNeqINec15XXlyDXlNeR16fXqNeUINeR15PXpNeT16TXny4K15TXm9ecINee16rX' +
        'l9eZ15wg15HXntem15Eg15DXmdee15XXnzog16nXldedINeT15HXqCDXnNeQINei15XXnNeUINeR15DXnteqINei' +
        '15Mg16nXqteT15zXmdenINek15zXmNek15XXqNee15Qg15HXotee15XXkyDXlNeX15nXkdeV16jXmdedLg==')
    DONE_START_LIVE = ('15TXodeV15vXoNeV16og157Xldeb16DXlCEKCtei15wg16nXldec15fXnyDXlNei15HXldeT15Qg15nXqSDXoteb' +
        '16nXmdeVINeQ15nXmden15XXnzogSW1hZ2luYXRpb24gQWdlbmN5LgrXqdeZ150g15zXkTog15nXqSDXpNec15jX' +
        'pNeV16jXnteV16og15HXlNei15zXkNeUINeQ157Xmdeq15nXqiDigJQg16fXnNeZ16TXmdedINep15DXmdep16jX' +
        'qiDXoteV15zXmdedINeR15DXnteqLiDXkNeqINeU157XpteRINep15wg15vXnCDXpNec15jXpNeV16jXnteUINeo' +
        '15XXkNeZ150g15HXotee15XXkyDXlNeX15nXkdeV16jXmdedLgoK15zXldeXINeU15HXp9eo15Qg16DXpNeq15cg' +
        '16LXm9ep15nXlSDXkdeT16TXk9ek158u')
    DONE_NOSTART_LIVE = ('15TXodeV15vXoNeV16og157Xldeb16DXlCEKCtei15wg16nXldec15fXnyDXlNei15HXldeT15Qg15nXqSDXoteb' +
        '16nXmdeVINeQ15nXmden15XXnzogSW1hZ2luYXRpb24gQWdlbmN5LgrXnNeX15nXpteUINeb16TXldec15Qg16LX' +
        'nNeZ15Ug16TXldeq15fXqiDXkNeqINec15XXlyDXlNeR16fXqNeUINeR15PXpNeT16TXny4K16nXmdedINec15E6' +
        'INeZ16kg16TXnNeY16TXldeo157XldeqINeR15TXotec15DXlCDXkNee15nXqteZ16og4oCUINen15zXmdek15nX' +
        'nSDXqdeQ15nXqdeo16og16LXldec15nXnSDXkdeQ157Xqi4g15DXqiDXlNee16bXkSDXqdecINeb15wg16TXnNeY' +
        '16TXldeo157XlCDXqNeV15DXmdedINeR16LXnteV15Mg15TXl9eZ15HXldeo15nXnS4=')
    DESC_MAIN = ('16HXldeb16DXldeqINeU16nXmdeV15XXpyDXqdecIEltYWdpbmF0aW9uOiDXnNeV15cg15TXkden16jXlCDXkdeT' +
        '16TXk9ek158=')
    DESC_RESTART = '15TXpNei15zXlCDXnteX15PXqSDXqdecINeU16HXldeb16DXldeq'
    DESC_STOP = '16LXpteZ16jXqiDXlNeh15XXm9eg15XXqg=='
    DESC_AUTOSTART = '15TXodeV15vXoNeV16og16LXldec15Qg15zXkdeTINeb16nXlNee15fXqdeRINeg15PXnNen'
}

function He([string]$Key) {
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($HE[$Key]))
}

function Test-InputRedirected {
    try { return [Console]::IsInputRedirected } catch { return $false }
}
$Interactive = (-not $Unattended) -and [Environment]::UserInteractive -and (-not (Test-InputRedirected))

function Say([string]$Message)  { Write-Host $Message }
function Ok([string]$Message)   { Write-Host "[OK] $Message" -ForegroundColor Green }
function Warn([string]$Message) { Write-Host "[!]  $Message" -ForegroundColor Yellow }

function Show-Box {
    # Hebrew message box (right-to-left). Returns 'Yes' / 'No' / 'OK', or $null when no box
    # could be shown (quiet / non-interactive / no Windows Forms).
    param([string]$Text, [string]$Icon = 'Information', [switch]$YesNo)
    if (-not $Interactive) { return $null }
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $owner = New-Object System.Windows.Forms.Form
        $owner.TopMost = $true
        $buttons = [System.Windows.Forms.MessageBoxButtons]::OK
        $default = [System.Windows.Forms.MessageBoxDefaultButton]::Button1
        if ($YesNo) {
            $buttons = [System.Windows.Forms.MessageBoxButtons]::YesNo
            $default = [System.Windows.Forms.MessageBoxDefaultButton]::Button2   # "No" is the default
        }
        $options = [System.Windows.Forms.MessageBoxOptions]::RtlReading -bor `
                   [System.Windows.Forms.MessageBoxOptions]::RightAlign
        $answer = [System.Windows.Forms.MessageBox]::Show($owner, $Text, (He 'TITLE'), $buttons,
            [System.Windows.Forms.MessageBoxIcon]::$Icon, $default, $options)
        $owner.Dispose()
        return [string]$answer
    } catch { return $null }
}

function Fail([string]$Message, [string]$BoxKey = 'INSTALL_FAILED') {
    Write-Host "[X]  $Message" -ForegroundColor Red
    Write-Host ""
    [void](Show-Box (He $BoxKey) 'Warning')
    exit 1
}

function Invoke-Native {
    # Runs a native command and returns its exit code. Its output is shown (not returned), and
    # its stderr never becomes a PowerShell error (Windows PowerShell 5.1 would stop on it).
    param([string]$Exe, [string[]]$Arguments)
    $oldPref = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments | Out-Host
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $oldPref }
}

Say ""
Say "==============================================="
Say "   $AppName - setup"
Say "   Folder: $Root"
Say "==============================================="
Say ""

# ---------------------------------------------------------------- 1. Python
function Get-PythonCommand {
    # Returns @(exe, args...) for a working Python >= 3.10, or $null.
    foreach ($cand in @('py -3', 'python', 'python3')) {
        $parts = @($cand -split ' ')
        $exe = $parts[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $extra = @()
        if ($parts.Count -gt 1) { $extra = @($parts[1..($parts.Count - 1)]) }
        $ver = $null
        $oldPref = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $ver = & $exe @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { $ver = $null } finally { $ErrorActionPreference = $oldPref }
        if ($LASTEXITCODE -ne 0 -or -not $ver) { continue }   # e.g. the Microsoft Store stub
        $nums = "$ver".Trim().Split('.')
        if ([int]$nums[0] -gt 3 -or ([int]$nums[0] -eq 3 -and [int]$nums[1] -ge 10)) {
            return , (@($exe) + $extra)
        }
        Warn "Found Python $ver - version 3.10 or newer is needed."
    }
    return $null
}

Say "Checking Python..."
$py = Get-PythonCommand
if (-not $py) {
    Say "Python 3.10 or newer was not found. Install it once, then run this setup again:"
    Say "    winget install -e --id Python.Python.3.12"
    Say "  (or download it from https://www.python.org/downloads/ and tick 'Add python.exe to PATH')"
    Fail "Python is missing." 'NO_PYTHON'
}
$pyExe = $py[0]
$pyArgs = @()
if ($py.Count -gt 1) { $pyArgs = @($py[1..($py.Count - 1)]) }
$oldPref = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try { $pyVersion = (& $pyExe @pyArgs --version 2>&1 | Out-String).Trim() } catch { $pyVersion = 'Python' }
finally { $ErrorActionPreference = $oldPref }
Ok "Found $pyVersion"

# ---------------------------------------------------------------- 2. ffmpeg (optional)
$studioFfmpeg = $null
foreach ($dir in @('C:\CS2_STUDIO\tools\ffmpeg', 'C:\CS2_STUDIO\tools\ffmpeg\bin')) {
    if ((Test-Path -LiteralPath (Join-Path $dir 'ffmpeg.exe')) -and
        (Test-Path -LiteralPath (Join-Path $dir 'ffprobe.exe'))) { $studioFfmpeg = $dir; break }
}
if ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Ok "ffmpeg is installed - the editor can cut vertical versions."
} elseif ($studioFfmpeg) {
    Ok "Using the studio's ffmpeg ($studioFfmpeg) - start.ps1 adds it automatically."
} else {
    Warn "ffmpeg was not found. The agency works, but without vertical cuts and thumbnails."
    Warn "To add it later:  winget install -e --id Gyan.FFmpeg   (then restart the agency)"
}

# ---------------------------------------------------------------- 3. stop a running agency
# Its files (python.exe, packages) are locked while it runs; it is started again at the end.
if ((Test-Path -LiteralPath $VenvPy) -and (Test-Path -LiteralPath $StartScript)) {
    try {
        & $StartScript -Stop -Quiet 6>$null | Out-Null   # exit code 0 = it was running and stopped
        if ($LASTEXITCODE -eq 0) { Ok "Stopped the running agency (it starts again at the end)." }
    } catch { }
}

# ---------------------------------------------------------------- 4. venv + packages
$venvOk = $false
if (Test-Path -LiteralPath $VenvPy) {
    $venvOk = ((Invoke-Native $VenvPy @('-c', 'import sys')) -eq 0)
    if ($venvOk) { Ok "The virtual environment (.venv) already exists." }
    else {
        Warn "The virtual environment (.venv) is broken - creating it again."
        Remove-Item -LiteralPath (Join-Path $Root '.venv') -Recurse -Force
    }
}
if (-not $venvOk) {
    Say "Creating the virtual environment (.venv)..."
    $code = Invoke-Native $pyExe (@($pyArgs) + @('-m', 'venv', '.venv'))
    if ($code -ne 0 -or -not (Test-Path -LiteralPath $VenvPy)) {
        Fail "Creating the virtual environment failed."
    }
    Ok "Created the virtual environment."
}

Say "Installing the agency and its libraries (this can take a few minutes)..."
$code = Invoke-Native $VenvPy @('-m', 'pip', 'install', '--upgrade', 'pip', '--disable-pip-version-check', '--quiet')
if ($code -ne 0) { Warn "Updating pip failed - continuing anyway." }
$code = Invoke-Native $VenvPy @('-m', 'pip', 'install', '--disable-pip-version-check', '-e', '.[platforms]')
if ($code -ne 0) {
    Fail "Installing the libraries failed. Check the internet connection and run the setup again."
}
Ok "All libraries are installed."

# ---------------------------------------------------------------- 5. agency init
Say "Preparing settings and the data folder (existing files are kept)..."
$code = Invoke-Native $VenvPy @('-m', 'agency.cli', 'init')
if ($code -ne 0) { Fail "agency init failed (see the message above)." }

# ---------------------------------------------------------------- 6. shortcuts
function New-AgencyIcon([string]$Path) {
    # A small crosshair icon (PNG inside an .ico file). Optional: any failure keeps the default icon.
    if (Test-Path -LiteralPath $Path) { return $true }   # keep it (Explorer may have it open)
    try {
        Add-Type -AssemblyName System.Drawing -ErrorAction Stop
        $size = 256
        $bmp = New-Object System.Drawing.Bitmap $size, $size
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $g.Clear([System.Drawing.Color]::Transparent)
        $dark = [System.Drawing.Color]::FromArgb(255, 11, 15, 20)
        $orange = [System.Drawing.Color]::FromArgb(255, 245, 166, 35)
        $bg = New-Object System.Drawing.SolidBrush $dark
        $g.FillEllipse($bg, 6, 6, 244, 244)
        $ring = New-Object System.Drawing.Pen $orange, 12
        $g.DrawEllipse($ring, 18, 18, 220, 220)
        $pen = New-Object System.Drawing.Pen $orange, 20
        $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
        $g.DrawLine($pen, 60, 128, 102, 128)
        $g.DrawLine($pen, 154, 128, 196, 128)
        $g.DrawLine($pen, 128, 60, 128, 102)
        $g.DrawLine($pen, 128, 154, 128, 196)
        $dot = New-Object System.Drawing.SolidBrush $orange
        $g.FillEllipse($dot, 116, 116, 24, 24)
        $g.Dispose()
        $ms = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
        $png = $ms.ToArray()
        $fs = [System.IO.File]::Create($Path)
        $w = New-Object System.IO.BinaryWriter $fs
        try {
            $w.Write([UInt16]0); $w.Write([UInt16]1); $w.Write([UInt16]1)   # ICONDIR: reserved, icon, 1 image
            $w.Write([Byte]0); $w.Write([Byte]0)                            # 256 x 256 (0 means 256)
            $w.Write([Byte]0); $w.Write([Byte]0)                            # no palette, reserved
            $w.Write([UInt16]1); $w.Write([UInt16]32)                       # planes, bits per pixel
            $w.Write([UInt32]$png.Length); $w.Write([UInt32]22)             # image size, offset
            $w.Write($png)
        } finally { $w.Close() }
        return $true
    } catch { return $false }
}

function New-Shortcut {
    param([string]$Path, [string]$StartArgs, [string]$Description, [string]$Icon)
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $PowerShellExe
    $lnk.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $StartScript + '"' + $StartArgs
    $lnk.WorkingDirectory = $Root
    $lnk.WindowStyle = 7          # minimized: the PowerShell window never shows
    $lnk.Description = $Description
    if ($Icon) { $lnk.IconLocation = "$Icon,0" }
    $lnk.Save()
}

$desktopLink = $null
if (-not $NoShortcuts) {
    $icon = Join-Path $Root 'data\imagination.ico'
    if (-not (New-AgencyIcon $icon)) { $icon = $null }
    try {
        $desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"
        New-Shortcut $desktopLink '' (He 'DESC_MAIN') $icon
        $menuDir = Join-Path ([Environment]::GetFolderPath('Programs')) $AppName
        New-Item -ItemType Directory -Force -Path $menuDir | Out-Null
        New-Shortcut (Join-Path $menuDir "$AppName.lnk") '' (He 'DESC_MAIN') $icon
        New-Shortcut (Join-Path $menuDir "Restart $AppName.lnk") ' -Restart' (He 'DESC_RESTART') $icon
        New-Shortcut (Join-Path $menuDir "Stop $AppName.lnk") ' -Stop' (He 'DESC_STOP') $icon
        Ok "Shortcuts created: Desktop + Start Menu ('$AppName')."
    } catch {
        Warn "Could not create the shortcuts: $($_.Exception.Message)"
        $desktopLink = $null
    }

    # ------------------------------------------------------------ 7. start with Windows?
    $startupLink = Join-Path ([Environment]::GetFolderPath('Startup')) "$AppName.lnk"
    $wantAutostart = $null
    if ($Autostart) { $wantAutostart = $true }
    elseif (-not $NoAutostart -and $Interactive) {
        $answer = Show-Box (He 'AUTOSTART_Q') 'Question' -YesNo
        if ($answer) { $wantAutostart = ($answer -eq 'Yes') }
        else {
            $typed = Read-Host "Start the agency automatically when Windows starts? [y/N]"
            $wantAutostart = ($typed -match '^(y|yes)$')
        }
    }
    try {
        if ($wantAutostart -eq $true) {
            New-Shortcut $startupLink ' -NoBrowser' (He 'DESC_AUTOSTART') $icon
            Ok "The agency will start with Windows (no browser window at login)."
        } elseif ($wantAutostart -eq $false -and (Test-Path -LiteralPath $startupLink)) {
            Remove-Item -LiteralPath $startupLink -Force
            Ok "Removed the start-with-Windows shortcut."
        }
    } catch { Warn "Could not change the start-with-Windows shortcut: $($_.Exception.Message)" }
}

# ---------------------------------------------------------------- done
# install.cmd is also the update path: platforms may already upload for real. Ask the agency.
function Get-LiveCount {
    $oldPref = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $code = 'from agency.config import load_settings; from agency.platforms import registry; ' +
                's = load_settings(); print(sum(1 for k in registry.enabled_keys(s) ' +
                'if registry.get_adapter(k, s).is_live()))'
        $out = & $VenvPy -c $code 2>$null
        if ($LASTEXITCODE -eq 0 -and "$out".Trim() -match '^\d+$') { return [int]("$out".Trim()) }
    } catch { } finally { $ErrorActionPreference = $oldPref }
    return 0
}
$LiveCount = Get-LiveCount

Say ""
Say "==============================================="
Ok  "Done. $AppName is installed in $Root"
Say "==============================================="
Say "  Open it any time: double-click '$AppName' on the Desktop."
if ($LiveCount -gt 0) {
    Say "  Real uploads are ON for $LiveCount platform(s): approved clips really go up."
    Say "  See each platform's switch on the Connections page of the dashboard."
} else {
    Say "  Training mode is ON: nothing is really published until you switch a platform"
    Say "  live on the Connections page of the dashboard."
}
Say "  Stop / restart: Start menu > $AppName."
Say ""

$liveSuffix = ''
if ($LiveCount -gt 0) { $liveSuffix = '_LIVE' }
if ($NoStart) {
    [void](Show-Box (He ('DONE_NOSTART' + $liveSuffix)))
    exit 0
}

[void](Show-Box (He ('DONE_START' + $liveSuffix)))
$startArgs = @{}
if ($NoBrowser) { $startArgs['NoBrowser'] = $true }
if (-not $Interactive) { $startArgs['Quiet'] = $true }
& $StartScript @startArgs
exit $LASTEXITCODE
