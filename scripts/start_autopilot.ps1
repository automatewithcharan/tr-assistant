param(
    [string]$Universe = "nifty500",
    [double]$Budget = 100000,
    [int]$IntervalSeconds = 60,
    [string]$Alerts = "discord"
)

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

$env:KITE_EXECUTION_MODE = "paper"
$env:KITE_DRY_RUN = "1"
$env:KITE_LIVE_ACKNOWLEDGEMENT = ""
$env:KITE_LIVE_CALLER_ALLOWLIST = ""
$env:KITE_LIVE_STRATEGY_ALLOWLIST = ""
$env:SIGNALMONITOR_AUTOTRADE_ENABLED = "0"
$env:SIGNALMONITOR_AUTOTRADE_REAL_ORDERS = "0"
$env:KITE_AUTO_TRADE = "0"
$env:KITE_AUTO_BUY = "0"
$env:KITE_AUTO_SELL = "0"

# Start the assistant in paper/alert-only mode.
$args = @(
    "-m", "pkscreener.pkscreenercli",
    "--assistantstart", $Universe,
    "--assistantautotune",
    "--assistantbudget", $Budget,
    "--assistantinterval", $IntervalSeconds,
    "--assistantalerts", $Alerts
)

Write-Host "Starting paper/alert-only trade assistant on $Universe..."
Write-Host "  Scan interval: ${IntervalSeconds}s"
Write-Host "  Alerts: $Alerts"
Write-Host "  Budget: $Budget"
Write-Host ""
Write-Host "You'll get Discord alerts when stocks qualify as blind-buy opportunities."
Write-Host "Buy in Groww, then /buy SYMBOL QTY PRICE in Discord to track."
Write-Host ""

python @args
