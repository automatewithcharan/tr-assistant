#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MISSING=0
for path in \
  "results/Indices/EQUITY_L.csv" \
  "results/Indices/NSE_BSE_COMBINED.csv"
do
  if [[ ! -s "$path" ]]; then
    echo "Required index cache missing or empty: $path"
    MISSING=1
  fi
done

if [[ "$MISSING" -ne 0 ]]; then
  echo "Index cache check found missing files; continuing so live refresh/fallback logic can recover."
fi

NSE_INDEX="results/Indices/EQUITY_L.csv"
NSE_MASTER="results/Data/kite_instruments_nse_latest.csv"
TODAY_UTC="$(date -u +%F)"
if [[ ! -s "$NSE_INDEX" || ! -s "$NSE_MASTER" \
      || "$(date -u -r "$NSE_INDEX" +%F 2>/dev/null || true)" != "$TODAY_UTC" \
      || "$(date -u -r "$NSE_MASTER" +%F 2>/dev/null || true)" != "$TODAY_UTC" ]]; then
  if ! "$REPO_ROOT/.venv/bin/python3" "$REPO_ROOT/scripts/refresh_nse_universe.py"; then
    echo "NSE universe refresh failed; live symbol coverage may be stale" >&2
  fi
fi

exit 0
