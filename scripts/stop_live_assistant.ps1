$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

python -m pkscreener.pkscreenercli --assistantstop
