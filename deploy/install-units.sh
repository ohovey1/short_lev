#!/usr/bin/env bash
# Install the four systemd units and seed the IBC config. Run ON THE BOX as
# root, from the repo root:
#
#     sudo deploy/install-units.sh
#
# Idempotent: re-running copies the units again and reloads, but NEVER touches
# an IBC config that already exists -- that file holds the password.
#
# This enables the units but does not start them. Starting is a chosen action
# (see deploy.sh for why), and the IBC config needs filling in first anyway.
set -euo pipefail

UNIT_DIR=/etc/systemd/system
STATE_DIR=/var/lib/short-lev
IBC_CONFIG="$STATE_DIR/ibc/config.ini"
IBC_STOCK_CONFIG=/opt/ibc/config.ini
SERVICE_USER=short-lev

UNITS=(
    short-lev-xvfb.service
    short-lev-gateway.service
    short-lev-vnc.service
    short-lev-monitor.service
)

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (sudo deploy/install-units.sh)" >&2
    exit 1
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "user $SERVICE_USER does not exist -- see docs/VPS.md section 9" >&2
    exit 1
fi

echo "installing units to $UNIT_DIR"
for unit in "${UNITS[@]}"; do
    install -m 644 "$here/$unit" "$UNIT_DIR/$unit"
    echo "  $unit"
done

# StateDirectory= in the monitor unit creates this on start, but the IBC config
# has to exist before Gateway first runs, so create it here too.
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$STATE_DIR" "$STATE_DIR/ibc"

# Seed from IBC'S OWN 975-line annotated config, never from the repo template.
# The template is a reference for which keys matter, not a complete config --
# a hand-written one was tried first and is not the supported path: IBC reads
# keys the template never mentions. See deploy/ibc-config.ini.template.
seeded=false
if [[ -e "$IBC_CONFIG" ]]; then
    echo "IBC config already present at $IBC_CONFIG -- left untouched"
elif [[ -e "$IBC_STOCK_CONFIG" ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 \
        "$IBC_STOCK_CONFIG" "$IBC_CONFIG"
    seeded=true
    echo "seeded $IBC_CONFIG from $IBC_STOCK_CONFIG"
else
    echo "WARNING: $IBC_STOCK_CONFIG not found -- is IBC installed at /opt/ibc?"
    echo "         Create $IBC_CONFIG by hand from IBC's config.ini before starting Gateway."
fi

systemctl daemon-reload
systemctl enable "${UNITS[@]}"

echo
echo "enabled: ${UNITS[*]}"

if [[ "$seeded" == true ]]; then
    cat <<EOF

NEXT: that is IBC's stock config. Gateway will not log in until the keys in
deploy/ibc-config.ini.template are set in it -- credentials, TradingMode,
IbDir, AutoRestartTime, ReadOnlyApi, and the dialog-dismissal keys.

    sudo -u $SERVICE_USER nano $IBC_CONFIG

That file holds the IBKR password in plaintext at 600. It never goes in the
repo and never goes in a backup that leaves this box.

Version pinning is NOT in that file -- patch TWS_MAJOR_VRSN, IBC_INI, and
LOG_PATH in /opt/ibc/gatewaystart.sh. See docs/VPS.md section 6.
EOF
fi

cat <<EOF

Then start the stack:

    sudo systemctl start short-lev-xvfb short-lev-gateway short-lev-vnc
    sudo systemctl start short-lev-monitor
    journalctl -u short-lev-monitor -f
EOF
