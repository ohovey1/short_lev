#!/usr/bin/env bash
# Pull and sync the deployed checkout. Run from a laptop over Tailscale:
#
#     ssh short-lev-01 'sudo -u short-lev /opt/short_lev/deploy/deploy.sh'
#
# STOPS BEFORE RESTARTING, on purpose. Restarting interrupts monitor continuity
# -- an in-flight poll is lost and the reconnect shows up as a gap -- so it stays
# a chosen action, ideally outside market hours. This prints the command; you
# decide when to run it.
#
# Deliberately not wired to auto-deploy on push: no GitHub Actions, no deploy
# key, and specifically no `NOPASSWD: ALL` sudoers entry. That pattern exists on
# the hype_arb box and should not be copied here.
set -euo pipefail

REPO=/opt/short_lev
UV="${UV:-$HOME/.local/bin/uv}"

cd "$REPO"

if [[ -n "$(git status --porcelain)" ]]; then
    echo "working tree at $REPO is dirty -- refusing to pull:" >&2
    git status --short >&2
    exit 1
fi

before="$(git rev-parse HEAD)"

# --ff-only: a deployed checkout should never need a merge commit. If this
# fails, something committed on the box and that wants a human.
git pull --ff-only

after="$(git rev-parse HEAD)"

if [[ "$before" == "$after" ]]; then
    echo "already up to date at ${after:0:7} -- nothing to sync"
    exit 0
fi

echo
echo "$( git log --oneline "$before..$after" | wc -l ) new commit(s):"
git log --oneline "$before..$after"

echo
"$UV" sync

# If the units changed, they need reinstalling -- daemon-reload alone will not
# pick up a file that is still sitting in the repo.
if ! git diff --quiet "$before" "$after" -- deploy/; then
    echo
    echo "NOTE: deploy/ changed. Reinstall the units before restarting:"
    echo "    sudo $REPO/deploy/install-units.sh"
fi

cat <<EOF

Synced ${before:0:7} -> ${after:0:7}. NOT restarted.

The monitor is still running the OLD code. Restart when you are ready --
ideally outside market hours:

    sudo systemctl restart short-lev-monitor
    journalctl -u short-lev-monitor -f
EOF
