#!/bin/sh
# Puts this repository's hooks where git runs them. A hook in .git/hooks is
# not part of the repository and does not travel between machines, so every
# clone - the Mac, the PC, the Linux box - runs this once:
#
#   sh tools/git-hooks/install.sh
#
# commit-msg strips the Claude attribution trailers the harness asks for (the
# history carries the xsm909 signature only).
set -e
here=$(cd "$(dirname "$0")" && pwd)
# Asked from the top of the working tree, so a relative answer is relative to
# something known; `--git-path` answers relative to where git was asked.
top=$(git -C "$here" rev-parse --show-toplevel)
hooks=$(cd "$top" && git rev-parse --git-path hooks)
case "$hooks" in /*) ;; *) hooks="$top/$hooks" ;; esac
mkdir -p "$hooks"
for hook in commit-msg pre-push; do
  [ -f "$here/$hook" ] || continue
  cp "$here/$hook" "$hooks/$hook"
  chmod +x "$hooks/$hook"
  echo "installed $hooks/$hook"
done
