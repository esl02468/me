# NQ Toolkit — Windows VPS one-time setup.
#
# Run in an *elevated* PowerShell (right-click -> Run as Administrator):
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   .\windows-setup.ps1
#
# To try an unmerged branch before it lands on main:
#   .\windows-setup.ps1 -Branch claude/algobox-suite-build-vg6ujg
# and to go back to the released code afterwards:
#   .\windows-setup.ps1 -Branch main
#
# What it does (idempotent — safe to re-run; re-running updates the code):
#   1. Clones (or updates) the repo into C:\nq-toolkit, on -Branch
#   2. Generates a private access token (kept in C:\nq-toolkit\token.txt)
#   3. Registers a scheduled task that starts the dashboard at boot and
#      restarts it if it ever dies
#   4. Opens the firewall port
#   5. Prints the URL to use from any device
#
# No pip installs, ever — the toolkit is Python standard library only.
#
# Requirements: Python 3.9+ and Git on PATH.
#   winget install -e --id Python.Python.3.12
#   winget install -e --id Git.Git

param(
    [string]$InstallDir = "C:\nq-toolkit",
    [string]$RepoUrl    = "https://github.com/esl02468/me.git",
    [string]$Branch     = "main",
    [int]$Port          = 8787,
    [string]$Token      = ""   # blank = generate once and reuse
)

$ErrorActionPreference = "Stop"
$TaskName = "NQDashboard"

foreach ($tool in "python", "git") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Error "'$tool' not found on PATH. Install it (see header) and re-run."
    }
}

# 1. Code
if (Test-Path (Join-Path $InstallDir ".git")) {
    Write-Host "Updating existing install in $InstallDir to '$Branch' ..."
    git -C $InstallDir fetch origin $Branch
    # Switching branches, not just fast-forwarding: a re-run with a
    # different -Branch has to actually move, and --ff-only would refuse
    # once the two histories have diverged.
    git -C $InstallDir checkout -B $Branch "origin/$Branch"
    # Safe for your data: reset only touches tracked files, and token.txt
    # and journal.db are both gitignored — the access token stays the same
    # and the signal journal's accumulated track record survives updates.
    git -C $InstallDir reset --hard "origin/$Branch"
} else {
    Write-Host "Cloning '$Branch' into $InstallDir ..."
    git clone --branch $Branch $RepoUrl $InstallDir
}

# 2. Token (generate once, reuse on re-runs so the URL never changes)
$TokenFile = Join-Path $InstallDir "token.txt"
if (-not $Token) {
    if (Test-Path $TokenFile) {
        $Token = (Get-Content $TokenFile -Raw).Trim()
    } else {
        $Token = [guid]::NewGuid().ToString("N")
    }
}
Set-Content -Path $TokenFile -Value $Token -NoNewline

# 3. Scheduled task: start at boot, restart forever if it dies
$PythonExe = (Get-Command python).Source
$TaskArgs = "dashboard.py --host 0.0.0.0 --port $Port --token $Token"
$Action   = New-ScheduledTaskAction -Execute $PythonExe -Argument $TaskArgs -WorkingDirectory $InstallDir
$Trigger  = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal | Out-Null
Start-ScheduledTask -TaskName $TaskName

# 4. Firewall
if (-not (Get-NetFirewallRule -DisplayName "NQ Dashboard $Port" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "NQ Dashboard $Port" -Direction Inbound `
        -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
}

# 5. URLs
Start-Sleep -Seconds 2
try { $PublicIp = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 10) } catch { $PublicIp = "<this-vps-ip>" }
Write-Host ""
$Commit = (git -C $InstallDir rev-parse --short HEAD)
Write-Host "=========================================================="
Write-Host " NQ dashboard is running and will survive reboots/crashes."
Write-Host " Running branch '$Branch' at $Commit."
Write-Host ""
Write-Host " Open from ANY device (keep this URL private — the token"
Write-Host " in it is the password):"
Write-Host ""
Write-Host "   http://${PublicIp}:${Port}/?token=${Token}"
Write-Host ""
Write-Host " AlgoBox in the terminal on this machine:"
Write-Host "   cd $InstallDir; python -m algobox"
Write-Host ""
Write-Host " Update later: re-run this script (keeps the same URL)."
Write-Host " Stop:  Stop-ScheduledTask  -TaskName $TaskName"
Write-Host " Start: Start-ScheduledTask -TaskName $TaskName"
Write-Host "=========================================================="
