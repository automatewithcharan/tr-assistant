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

exit 0
