#!/usr/bin/env bash
# Fails when a change modifies, renames or deletes a Policy pack file that already
# exists on the base branch. Released packs are immutable (ADR-0002): release a new
# version instead. Usage: check-released-packs.sh <base-commit>
set -euo pipefail
base="$1"
changed=$(git diff --name-status "$base" HEAD -- 'policy_packs/*/*.yaml' | grep -v '^A' || true)
if [ -n "$changed" ]; then
  echo "::error::Released policy packs are immutable. Add a new version instead of changing these:"
  echo "$changed"
  exit 1
fi
echo "No released policy pack was changed."
