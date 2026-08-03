param(
    [string]$Universe = "nifty50",
    [double]$Budget = 10000,
    [int]$IntervalSeconds = 30,
    [string]$Alerts = "discord",
    [switch]$Prewarm
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

$args = @(
    "-m", "pkscreener.pkscreenercli",
    "--assistantstart", $Universe,
    "--assistantautotune",
    "--assistantstrongbuyonly",
    "--assistantbudget", $Budget,
    "--assistantinterval", $IntervalSeconds,
    "--assistantalerts", $Alerts
)

if ($Prewarm) {
    $args += "--assistantprewarm"
}

python @args
