# Run one incremental CivicPulse collection/build. Intended for Windows Task Scheduler.
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("hourly-" + (Get-Date -Format "yyyy-MM-dd") + ".log")
"[$(Get-Date -Format o)] START hourly CivicPulse run" | Out-File -FilePath $LogFile -Append -Encoding utf8
try {
    & $Python -m civicpulse data *>> $LogFile
    $Code = $LASTEXITCODE
    if ($Code -eq 0) {
        & $Python -m civicpulse forecast-model *>> $LogFile
        $Code = $LASTEXITCODE
    }
    "[$(Get-Date -Format o)] EXIT $Code" | Out-File -FilePath $LogFile -Append -Encoding utf8
    exit $Code
}
catch {
    "[$(Get-Date -Format o)] ERROR $_" | Out-File -FilePath $LogFile -Append -Encoding utf8
    exit 1
}
