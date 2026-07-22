#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$REPO/.venv/bin/python3"
SCRIPT="$REPO/scripts/check_intraday_evidence_pipeline.py"
TAG="intraday_evidence_pipeline"
CURRENT="$(crontab -l 2>/dev/null || true)"
FILTERED="$(printf '%s\n' "$CURRENT" | grep -v "$TAG" || true)"
{
    printf '%s\n' "$FILTERED"
    printf '20-59/5 3 * * 1-5 %s %s --allow-issues >> /tmp/%s.log 2>&1 # %s\n' "$PYTHON" "$SCRIPT" "$TAG" "$TAG"
    printf '*/5 4-10 * * 1-5 %s %s --allow-issues >> /tmp/%s.log 2>&1 # %s\n' "$PYTHON" "$SCRIPT" "$TAG" "$TAG"
} | awk 'NF || !blank++' | crontab -
echo "Installed paper-evidence readiness checks every five minutes from 08:50 to 16:25 IST."
