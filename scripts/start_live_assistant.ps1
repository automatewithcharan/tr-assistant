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
