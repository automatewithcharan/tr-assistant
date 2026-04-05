param(
    [string]$Universe = "nifty500",
    [double]$Budget = 100000,
    [int]$IntervalSeconds = 60,
    [string]$Alerts = "discord"
)

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

# Start the full assistant with autopilot (blind-buy alerts) enabled
$args = @(
    "-m", "pkscreener.pkscreenercli",
    "--assistantstart", $Universe,
    "--assistantautotune",
    "--assistantbudget", $Budget,
    "--assistantinterval", $IntervalSeconds,
    "--assistantalerts", $Alerts,
    "--assistantautopilot"
)

Write-Host "Starting trade assistant with autopilot on $Universe..."
Write-Host "  Scan interval: ${IntervalSeconds}s"
Write-Host "  Alerts: $Alerts"
Write-Host "  Budget: $Budget"
Write-Host ""
Write-Host "You'll get Discord alerts when stocks qualify as blind-buy opportunities."
Write-Host "Buy in Groww, then /buy SYMBOL QTY PRICE in Discord to track."
Write-Host ""

python @args
