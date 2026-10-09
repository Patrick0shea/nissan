#!/usr/bin/env bash
# Fetch the Opentrons Protocol Library at the pinned commit (docs/DECISIONS.md D-005).
# Not vendored: the repo has no LICENSE file.
set -euo pipefail

COMMIT=2f447c936b49720d68154054e94d58e1d33ccba5
DEST=${1:-data/corpus/Protocols}

if [ ! -d "$DEST/.git" ]; then
  git clone --filter=blob:none --no-checkout https://github.com/Opentrons/Protocols "$DEST"
fi
git -C "$DEST" fetch --depth 1 origin "$COMMIT"
git -C "$DEST" checkout --detach "$COMMIT"
echo "corpus at $DEST @ $(git -C "$DEST" rev-parse --short HEAD)"
