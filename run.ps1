<#
    run.ps1 — drive the whole OutreachFlow pipeline end to end, with logs.

      .\run.ps1                 dry: plan the cascade, spend nothing, render no email
      .\run.ps1 -Enrich         run the cascade for real (spends hunter credits)
      .\run.ps1 -Preview        render every queued email to screen + log, send nothing
      .\run.ps1 -Send           actually send
      .\run.ps1 -Send -Cap 9    send only the first 9 (the named-human list)

    Every run writes a timestamped transcript to .\logs\ .
#>
[CmdletBinding()]
param(
    [switch]$Enrich,
    [switch]$Preview,
    [switch]$Send,
    [switch]$Test,
    [string]$TestTo = "",
    [int]$Limit = 24,
    [int]$Cap = 25,
    [string]$Strategy = "scrape-first"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
if (-not (Test-Path $py)) { throw "python not found at $py" }

# ---- load .env into the process environment ----
if (-not (Test-Path ".env")) { throw ".env not found" }
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        Set-Item -Path "env:$($matches[1])" -Value $matches[2].Trim()
    }
}

if (-not (Test-Path "logs")) { New-Item -ItemType Directory logs | Out-Null }
$stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$log   = "logs\run_$stamp.log"

function Write-Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line -ForegroundColor Cyan
    Add-Content -Path $log -Value $line -Encoding utf8
}

function Invoke-Step($label, $argList) {
    Write-Log "START $label"
    Write-Log ("  python " + ($argList -join " "))
    # PowerShell 5.1 wraps a native command's stderr in ErrorRecords, which trips
    # ErrorActionPreference=Stop even on a clean exit 0. Relax it for the call and
    # judge success by the exit code instead, which is the only reliable signal.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        # Not Tee-Object: in PS 5.1 it has no -Encoding and writes UTF-16, which would
        # interleave with the UTF-8 that Write-Log appends and corrupt the transcript.
        & $py @argList 2>&1 | ForEach-Object {
            # Stringifying a wrapped stderr line directly yields the useless
            # "System.Management.Automation.RemoteException"; reach for the message.
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) {
                        $_.Exception.Message
                    } else { "$_" }
            Write-Host $line
            Add-Content -Path $log -Value $line -Encoding utf8
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) { Write-Log "FAILED $label (exit $code)"; throw "$label failed" }
    Write-Log "OK $label"
}

Write-Log "log file: $log"
Write-Log ("hunter key: " + $(if ($env:HUNTER_API_KEY) { "set" } else { "MISSING" }))
Write-Log ("smtp user : " + $(if ($env:SMTP_USER) { $env:SMTP_USER } else { "MISSING" }))
Write-Log ("smtp pass : " + $(if ($env:SMTP_PASS) { "set" } else { "MISSING - cannot send" }))

# ---- stage 1: enrichment cascade ----
$agentArgs = @("outreach_agent.py", "--limit", $Limit, "--strategy", $Strategy,
               "--out", "outreach.csv")
if ($Enrich) { $agentArgs += "--run" }
Invoke-Step "cascade" $agentArgs

if (-not $Enrich -and -not (Test-Path "outreach.csv")) {
    Write-Log "dry plan only; no outreach.csv yet. re-run with -Enrich."
    return
}

# ---- stage 2: render / send ----
if (-not ($Preview -or $Send -or $Test)) {
    Write-Log "cascade done. -Preview to see the emails, -Test to mail yourself, -Send to deliver."
    return
}

$sendArgs = @("send_campaign.py", "outreach.csv", "--template", "template.txt",
              "--custom", "custom.csv", "--cap", $Cap)

# Drop companies whose domain no longer serves them. A mailbox can verify `valid`
# long after the company behind it was acquired: the address still works, the
# company in your list does not exist any more.
if (Test-Path "research.csv") {
    $sendArgs += @("--research", "research.csv")
} else {
    Write-Log "no research.csv - liveness gate OFF. Run research_jobs.py first."
}

if ($Test) {
    # Test mode goes to your own inbox. Real recipients are never contacted and the
    # sent table is not touched, so a test cannot consume a lead.
    $dest = if ($TestTo) { $TestTo } else { $env:SMTP_USER }
    if (-not $dest) { throw "no test destination: set SMTP_USER in .env or pass -TestTo" }
    if (-not $env:SMTP_PASS) {
        throw "SMTP_PASS is empty. Generate a Google App Password at https://myaccount.google.com/apppasswords and put it in .env"
    }
    Write-Log "TEST MODE -> all mail to $dest, real recipients untouched, nothing recorded"
    $sendArgs += @("--test-to", $dest, "--send")
}
elseif ($Send) {
    if (-not $env:SMTP_PASS) {
        throw "SMTP_PASS is empty. Generate a Google App Password at https://myaccount.google.com/apppasswords and put it in .env"
    }
    Write-Log "SENDING as $env:SMTP_USER to REAL recipients - this is not reversible"
    $sendArgs += "--send"
}
else {
    Write-Log "preview only - nothing will leave this machine"
}
Invoke-Step "campaign" $sendArgs

Write-Log "done. transcript: $log"
