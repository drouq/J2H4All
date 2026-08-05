# J2H4All home Garmin sync (HYBRID deploy).
#
# Garmin's Cloudflare 429s Render's datacenter IP on the OAuth token exchange, so
# ingestion runs HERE — on a residential IP Garmin trusts — writing to the SAME Neon
# database that Render reads. This is the only thing in J2H4All that must run from home;
# web, coaching beats, Telegram, calendar and export all stay on Render.
#
# It reads NEON_DATABASE_URL from backend\.env and runs `python -m app.jobs daily_sync`
# against it, overriding the local-Postgres DATABASE_URL for THIS process only (so the
# local dev DB is untouched). daily_sync also runs post-sync coaching (post-run reads +
# red-flag), so it needs GARTH_TOKEN / ANTHROPIC_API_KEY / TELEGRAM_* — already in .env.
#
# Registered as a Scheduled Task by register_home_sync_task.ps1. Safe to run by hand:
#   powershell -ExecutionPolicy Bypass -File .\home_sync.ps1

$ErrorActionPreference = "Stop"

$Root    = (Join-Path $PSScriptRoot "..\backend")
$Py      = Join-Path $Root ".venv\Scripts\python.exe"
$EnvFile = Join-Path $Root ".env"
$LogDir  = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$Log = Join-Path $LogDir "home_sync.log"

function Write-Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $Log -Value $line -Encoding utf8
    Write-Host $line
}

if (-not (Test-Path $Py))      { Write-Log "ERROR: venv python not found at $Py"; exit 3 }
if (-not (Test-Path $EnvFile)) { Write-Log "ERROR: .env not found at $EnvFile";   exit 3 }

# Pull the Neon connection string from backend\.env (NEON_DATABASE_URL=...).
$neon = $null
foreach ($ln in Get-Content $EnvFile -Encoding utf8) {
    if ($ln -match '^\s*NEON_DATABASE_URL\s*=\s*(.+)$') { $neon = $Matches[1].Trim() }
}
if (-not $neon) {
    Write-Log "ERROR: NEON_DATABASE_URL not set in $EnvFile. Add it (the Neon connection string from the Render/Neon dashboard) and retry."
    exit 2
}

# Override the local DATABASE_URL for this process only. OS env vars take precedence over
# the .env file in pydantic-settings, so daily_sync writes to Neon, not local Postgres.
$env:DATABASE_URL     = $neon
$env:PYTHONIOENCODING = "utf-8"   # post-sync coaching prints emoji to Telegram/logs

Push-Location $Root               # `python -m app.jobs` needs CWD=backend to resolve the package + load .env
try {
    Write-Log "Starting daily_sync -> Neon ..."
    # Python's normal logging goes to stderr. Under Windows PowerShell 5.1 a native
    # command's stderr becomes error records that, with ErrorActionPreference=Stop, abort
    # the script on the first (harmless) INFO line. Switch to Continue for the run and
    # judge success by the real process exit code, not by whether stderr had output.
    $ErrorActionPreference = "Continue"
    $out = & $Py -m app.jobs daily_sync 2>&1
    $code = $LASTEXITCODE
    $out | ForEach-Object { Write-Log $_ }
}
finally {
    Pop-Location
}
Write-Log "daily_sync exited with code $code"
exit $code
