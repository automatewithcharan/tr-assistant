param(
    [string]$Universe = "nifty50",
    [double]$Budget = 10000,
    [int]$IntervalSeconds = 30,
    [string]$Alerts = "discord",
    [switch]$Prewarm
)

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

$args = @(
    "-m", "pkscreener.pkscreenercli",
    "--charanstart", $Universe,
    "--charanautotune",
    "--charanstrongbuyonly",
    "--charanbudget", $Budget,
    "--charaninterval", $IntervalSeconds,
    "--charanalerts", $Alerts
)

if ($Prewarm) {
    $args += "--charanprewarm"
}

python @args
