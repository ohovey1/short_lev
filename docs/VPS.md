# VPS Operations -- `short-lev-01`

The box that runs IB Gateway and the short_lev monitor.
Verified working 2026-08-10, after spec 007.

---

## Quick reference

Everything routine, in one place.

```bash
ssh short-lev                    # in, as owen
sudo -u short-lev -i             # switch to the service account
exit                             # back out
```

**Check everything is alive**

```bash
sudo systemctl status short-lev-xvfb short-lev-gateway short-lev-vnc short-lev-monitor --no-pager | grep -E "●|Active:"
```

**Watch the monitor**

```bash
sudo journalctl -u short-lev-monitor -f -o cat
```

**Look at Gateway's screen** (weekly login, or when something needs eyes)

```bash
ssh -L 5900:localhost:5900 short-lev      # leave this terminal open
```
Then TigerVNC -> `localhost:5900`.

**Deploy an update**

```bash
sudo -u short-lev /opt/short_lev/deploy/deploy.sh
sudo systemctl restart short-lev-monitor   # only when you choose to
```

**Restart something**

```bash
sudo systemctl restart short-lev-monitor   # safe any time
sudo systemctl restart short-lev-vnc       # safe: does not touch Gateway
sudo systemctl restart short-lev-gateway   # IBC logs back in; monitor reconnects in ~10s
```

Nothing here needs tmux. The stack is under systemd and survives reboots.

---

## 1. The box

| | |
|---|---|
| Provider | Hetzner Cloud, project `short-lev` |
| Server | `short-lev-01`, **CPX 11** -- 2 vCPU / 2 GB / 40 GB |
| Cost | ~$21/mo, billed hourly |
| Location | Ashburn, VA |
| OS | Ubuntu **22.04.5 LTS** |
| Public IPv4 | `5.161.232.45` |
| Tailscale IPv4 | `100.123.221.44` |
| Swap | 2 GB (`/swapfile`, in `/etc/fstab`) |

Gateway self-caps its heap at `-Xmx768m` and sits around 208 MB running, so 2 GB
plus swap is adequate. **If it thrashes or gets OOM-killed, resize to CPX 21
rather than debugging** -- Hetzner resizes in ~5 minutes with no reinstall.

Ashburn matters for latency to IBKR and to avoid a login from an unexpected
country triggering security review.

**Never ARM.** IBKR ships Gateway as x86 Linux only.

---

## 2. Users

| User | Login | Purpose |
|---|---|---|
| `root` | **disabled** | via `sudo` only |
| `owen` | SSH key + password for sudo | admin |
| `fiona` | SSH key + password for sudo | admin |
| `short-lev` | **none** (`--disabled-password`) | service account, owns `/opt/short_lev` and `~/Jts` |

`short-lev` has no password and is not in `sudo`. Reach it with
`sudo -u short-lev -i` from an admin account.

> **Anyone with `sudo` can read the IBKR password** in
> `/var/lib/short-lev/ibc/config.ini`. File permissions stop other users, not
> root. **Rotate the IBKR password whenever the set of admins changes.**

**Adding an operator:** own admin user (not shared `owen`), plus `adm` group so
they can read the journal without sudo, plus a Tailscale invite, plus their own
IBKR user under Users & Access Rights.

```bash
adduser <name>
usermod -aG sudo,adm <name>
mkdir -p /home/<name>/.ssh
nano /home/<name>/.ssh/authorized_keys        # paste their public key
chown -R <name>:<name> /home/<name>/.ssh
chmod 700 /home/<name>/.ssh
chmod 600 /home/<name>/.ssh/authorized_keys
```

Without `adm`, `journalctl -u ...` silently shows nothing.

---

## 3. Getting in

`~/.ssh/config` on your machine:

```
Host short-lev
    HostName 5.161.232.45
    User owen
    IdentityFile ~/.ssh/short_lev_vps
```

Key is per-project, matching the `hype_arb` convention.

### Common failures

- `Permission denied (publickey)` -- config still says `User root`. Root login is
  disabled.
- **Commands vanishing** after `ssh`, `sudo -u`, or `source` -- these change
  shell context, and anything pasted before the new prompt appears is swallowed.
  **One command at a time.**
- `sudo: unit: command not found` when running a repo script -- use
  `sudo bash deploy/<script>.sh`, or `chmod +x` it.

---

## 4. Security posture

```
# /etc/ssh/sshd_config
PermitRootLogin no
PasswordAuthentication no
```

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow in on tailscale0
ufw enable
```

**Port 5900 is deliberately not open.** x11vnc binds `-localhost`, reachable only
through an SSH tunnel. Two layers: firewall and bind address.

Timezone is `America/New_York` on purpose -- every operational question here is
about the 23:45 ET Gateway restart, the Sunday 01:00 ET re-auth, and market hours.

Unattended security upgrades are on.

**Open item:** public SSH could be closed (`ufw delete allow OpenSSH`) once every
operator is on Tailscale, leaving no internet-facing surface at all.

---

## 5. VNC

Needed only when Gateway needs eyes -- IBC handles routine logins.

**Client:** TigerVNC standalone (`vncviewer64-*.exe`, no install).

1. `ssh -L 5900:localhost:5900 short-lev` -- leave the terminal open
2. TigerVNC -> `localhost:5900` (**not** the Tailscale IP; x11vnc refuses that
   interface by design)
3. Accept the unencrypted warning -- SSH already encrypts it
4. Close the terminal when done

Blank grey window: press Left-Alt three times to force a repaint.

### Session collision

**Gateway, TWS, and Client Portal cannot share a login.** Client Portal refuses
with *"the production username associated with this paper-trading username has an
active session."*

Manual trades therefore need Gateway stopped first:

```bash
sudo systemctl stop short-lev-gateway
# trade via phone app or Client Portal
sudo systemctl start short-lev-gateway
```

The monitor rides the gap out on its reconnect backoff.

Two things that would remove this: a second IBKR user under Users & Access
Rights, or IBC 3.24's `PAUSE` command, which stops Gateway in a state that
resumes without re-authentication.

---

## 6. IB Gateway + IBC

**Gateway 10.45** (`stable`, standalone) at `/home/short-lev/Jts/ibgateway/1045/`.
**IBC 3.24.0** at `/opt/ibc`.

### The layout IBC requires

`ibcstart.sh` line 241 builds the path as:

```
gateway_program_path="${tws_path}/ibgateway/${tws_version}"
```

So it needs **`/home/short-lev/Jts/ibgateway/1045/jars/`** to exist. IBC does not
run Gateway's launcher -- it invokes Java directly against those jars, which is
why the folder must be exactly there and why moving the directory is safe.

> **Do not pass `-dir` to the installer.** An earlier setup used
> `-q -dir ~/Jts`, which flattened everything into `~/Jts` with no version
> subdirectory. IBC then failed with *"Offline TWS/Gateway version 1045 is not
> installed: can't find jars folder"*. Install with `-q` only, then move.

### IBC version constraints

- **3.20.0 and earlier do not work.** Gateway 1034+ on Linux moved to the Azul
  Zulu 17 JRE; support arrived in IBC 3.21.0.
- **Avoid 3.21.0** -- broken autorestart with Gateway. Fixed in 3.21.1.
- **3.24.0** is current and working here.
- Note the maintainer has said he is stepping away from IBC. Logged as a risk.

### Version pinning lives in the start script, not the config

There is no `IbGatewayVersionMajor` key in `config.ini` -- setting one does
nothing. The version comes from `TWS_MAJOR_VRSN` in `/opt/ibc/gatewaystart.sh`,
which ships defaulted to whatever was current when that IBC release was cut
(3.24.0 ships `1019`).

`gatewaystart.sh` also **overrides `IBC_INI` and `LOG_PATH` unconditionally**, so
setting them in the environment or the unit has no effect. All three are edited
in the script:

```bash
sudo sed -i 's/^TWS_MAJOR_VRSN=.*/TWS_MAJOR_VRSN=1045/' /opt/ibc/gatewaystart.sh
sudo sed -i 's|^IBC_INI=.*|IBC_INI=/var/lib/short-lev/ibc/config.ini|' /opt/ibc/gatewaystart.sh
sudo sed -i 's|^LOG_PATH=.*|LOG_PATH=/var/lib/short-lev/ibc/logs|' /opt/ibc/gatewaystart.sh
```

**These are lost on an IBC upgrade.** Re-apply after any reinstall.

### The `-inline` flag is required

By default `gatewaystart.sh` launches an `xterm` in the background and returns 0
immediately -- systemd sees the main process exit and restarts every 30 seconds
forever. `-inline` makes it `exec` in the foreground. The unit uses it.

`PrivateTmp` must also be **false** on the Gateway unit: Xvfb's socket is in
`/tmp/.X11-unix`, and a private `/tmp` namespace hides it.

### IBC config

Seed from IBC's own 975-line annotated file, **not** the repo template -- the
template is a reference for which keys matter, not a complete config.

```bash
sudo cp /opt/ibc/config.ini /var/lib/short-lev/ibc/config.ini
sudo chown short-lev:short-lev /var/lib/short-lev/ibc/config.ini
sudo chmod 600 /var/lib/short-lev/ibc/config.ini
sudo -u short-lev nano /var/lib/short-lev/ibc/config.ini
```

Set:

| Key | Value | Why |
|---|---|---|
| `IbLoginId` | paper username | |
| `IbPassword` | paper password | plaintext, on the box only |
| `TradingMode` | `paper` | must agree with `IB_PORT` in `.env` |
| `IbDir` | `/home/short-lev/Jts` | |
| `AutoRestartTime` | `11:45 PM` | soft restart, no re-auth |
| `IbAutoClosedown` | `no` | systemd owns lifecycle |
| `AcceptNonBrokerageAccountWarning` | `yes` | paper disclaimer dialog |
| `ReadOnlyLogin` | `no` | |
| `ReadOnlyApi` | `yes` | **re-asserts read-only on every start** |
| `AcceptIncomingConnectionAction` | `accept` | auto-handles the API connect dialog |
| `DismissPasswordExpiryWarning` | `yes` | otherwise blocks headless |

`ReadOnlyApi=yes` is the valuable one: it makes "the monitor cannot place orders"
a property of config rather than a checkbox someone ticked. The IBC log confirms
it each start with `Read-Only API checkbox is already set to: true`.

### Gateway API settings

These live in `~/Jts` and survive restarts, but **are lost if Gateway is
reinstalled**. Set over VNC under Configure -> Settings -> API -> Settings:

| Setting | Value |
|---|---|
| Enable ActiveX and Socket Clients | **ON** |
| Socket port | **4002** (paper) / 4001 (live) |
| Trusted IPs | `127.0.0.1` |
| Download open orders on connection | **OFF** |

Read-Only API is handled by IBC. Confirm the port is live with
`sudo ss -tlnp | grep 4002`.

**First-run gate:** the paper trading disclaimer must be accepted once, or the
API drops connections with `Error 10141` followed by a misleading
`clientId 11 already in use?`. `AcceptNonBrokerageAccountWarning=yes` handles it
going forward.

---

## 7. Authentication model

**No IBKR credential is in the repo.** One lives on the box, in IBC's config, so
that restarts are unattended. Gateway holds the session; the API socket on
localhost needs no authentication. `.env` carries connection coordinates only.

| Event | Frequency | Human |
|---|---|---|
| Monitor | continuous | no |
| Gateway soft restart | nightly ~23:45 ET | no |
| Full re-auth | weekly, after Sun 01:00 ET | none observed so far |
| Crash re-auth | rare | none observed so far |

**Observed:** three paper logins including a genuine post-expiry weekly re-auth
required **no 2FA**, and IBC drives them start to finish. **Do not assume this
holds for live**, where IB Key is likely enforced -- IBC cannot approve 2FA.

The weekly window falls Sunday morning, markets closed until Monday 09:30 --
about 32 hours of slack.

---

## 8. Application layout

```
/opt/short_lev/              repo, owned by short-lev
/opt/short_lev/.env          chmod 600, never committed
/var/lib/short-lev/          ALL runtime state -- survives a re-clone
  monitor.json               peak_equity
  alert_state.json           alert dedup
  events/                    checks.jsonl, alerts.jsonl
  ibc/config.ini             THE IBKR PASSWORD. 600.
  ibc/logs/                  IBC diagnostics
/home/short-lev/Jts/ibgateway/1045/    Gateway
/opt/ibc/                    IBC
```

State is under `/var/lib` deliberately: a `git pull` or re-clone that wiped
`peak_equity` would silently disable the drawdown stop. **Verified** -- the repo
was deleted and re-cloned and the peak survived.

### `.env`

```
POLYGON_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ALERT_REPEAT_MINUTES=60
HEARTBEAT_HOUR=09:45
EVENT_LOG_DIR=/var/lib/short-lev/events
IB_HOST=127.0.0.1
# 4002 paper gateway, 4001 live gateway
IB_PORT=4002
IB_CLIENT_ID=11
IB_ACCOUNT=DU<redacted>
MONITOR_BASE_CAPITAL=10000
MONITOR_STATE_PATH=/var/lib/short-lev/monitor.json
ALERT_STATE_PATH=/var/lib/short-lev/alert_state.json
POLL_INTERVAL_SECONDS=900
```

> **No inline comments.** `IB_PORT=4002  # paper` is parsed as the whole string
> and fails with `invalid literal for int()`. Comments go on their own line.

`IB_PORT` is the only thing separating paper from live, which is why the monitor
logs port and account on every start. `IB_CLIENT_ID=11` is deliberately not 0 or
1 -- every example script uses those.

### The units

| Unit | Notes |
|---|---|
| `short-lev-xvfb` | display `:10`. Everything else `Requires=` it. |
| `short-lev-gateway` | `gatewaystart.sh -inline`. `PrivateTmp=false`, `RestartSec=30`, looser hardening -- Gateway writes to `~/Jts`. |
| `short-lev-vnc` | **separate on purpose.** Restarting VNC must not log Gateway out. |
| `short-lev-monitor` | full hardening, `StateDirectory=short-lev`, `RestartSec=10`. |

`After=` is ordering, not readiness -- systemd starts the monitor once Gateway's
*process* exists, well before it is logged in. The monitor's connect backoff is
what handles that, and it works: on a cold boot it logs one WARNING, waits 10s,
and connects. Do not try to fix this with unit ordering.

### `uv` needs an absolute path

`sudo -u short-lev bash -c 'uv sync'` fails -- non-login shells don't source the
profile. Use `/home/short-lev/.local/bin/uv`.

---

## 9. Rebuilding from scratch

**Do not paste as a block.** Anything after `ssh`, `sudo -u`, or `source` is
swallowed by a shell that hasn't started.

```bash
# 1. Hetzner: CPX 11, Ashburn, Ubuntu 22.04, SSH key added AT CREATION.

# 2. ssh root@<ip>

# 3. Swap
fallocate -l 2G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# 4. Admin user
adduser owen && usermod -aG sudo,adm owen
rsync --archive --chown=owen:owen ~/.ssh /home/owen

# 5. >>> VERIFY `ssh owen@<ip>` AND `sudo -v` IN A SECOND TERMINAL <<<
#     The next step is what locks you out.

# 6. Harden
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# 7. Firewall + clock
ufw default deny incoming && ufw default allow outgoing
ufw allow OpenSSH && ufw enable
timedatectl set-timezone America/New_York

# 8. Tailscale -- first, everything after is easier with it
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up                 # blocks: open the printed URL, authenticate
ufw allow in on tailscale0

# 9. Packages. unzip IS required.
apt update
apt install -y xvfb x11vnc openjdk-17-jre unattended-upgrades unzip

# 10. Service user
adduser --disabled-password --gecos "" short-lev
mkdir -p /opt/short_lev && chown short-lev:short-lev /opt/short_lev

# 11. Gateway -- NO -dir FLAG, then move into the layout IBC needs
sudo -u short-lev bash -c 'cd ~ && wget https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh && chmod +x ibgateway-stable-standalone-linux-x64.sh'
sudo -u short-lev bash -c 'cd ~ && ./ibgateway-stable-standalone-linux-x64.sh -q'
sudo -u short-lev bash -c 'mkdir -p ~/Jts/ibgateway && mv ~/ibgateway ~/Jts/ibgateway/1045'
sudo -u short-lev bash -c 'ls ~/Jts/ibgateway/1045/jars | head -3'    # must list jars

# 12. IBC. Check github.com/IbcAlpha/IBC/releases for current; 3.21.0+ required.
mkdir -p /opt/ibc && cd /opt/ibc
curl -LO https://github.com/IbcAlpha/IBC/releases/download/3.24.0/IBCLinux-3.24.0.zip
unzip IBCLinux-3.24.0.zip && chmod +x *.sh scripts/*.sh
chown -R short-lev:short-lev /opt/ibc

# 13. Patch gatewaystart.sh -- version, config path, log path (section 6)
sed -i 's/^TWS_MAJOR_VRSN=.*/TWS_MAJOR_VRSN=1045/' /opt/ibc/gatewaystart.sh
sed -i 's|^IBC_INI=.*|IBC_INI=/var/lib/short-lev/ibc/config.ini|' /opt/ibc/gatewaystart.sh
sed -i 's|^LOG_PATH=.*|LOG_PATH=/var/lib/short-lev/ibc/logs|' /opt/ibc/gatewaystart.sh

# 14. Repo
sudo -u short-lev git clone https://github.com/ohovey1/short_lev.git /opt/short_lev
sudo -u short-lev bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u short-lev bash -c 'cd /opt/short_lev && /home/short-lev/.local/bin/uv sync'
sudo -u short-lev nano /opt/short_lev/.env      # section 8. NO inline comments.
chmod 600 /opt/short_lev/.env

# 15. Units
cd /opt/short_lev && bash deploy/install-units.sh

# 16. IBC config -- seed from IBC's own file, not the template (section 6)
cp /opt/ibc/config.ini /var/lib/short-lev/ibc/config.ini
chown short-lev:short-lev /var/lib/short-lev/ibc/config.ini
chmod 600 /var/lib/short-lev/ibc/config.ini
sudo -u short-lev nano /var/lib/short-lev/ibc/config.ini
mkdir -p /var/lib/short-lev/ibc/logs && chown -R short-lev:short-lev /var/lib/short-lev/ibc

# 17. Start
systemctl start short-lev-xvfb short-lev-gateway short-lev-vnc
sleep 30 && tail -20 /var/lib/short-lev/ibc/logs/ibc-*.txt    # confirm IBC logged in

# 18. Over VNC: set the Gateway API options (section 6). Confirm:
ss -tlnp | grep 4002

# 19. Monitor
systemctl start short-lev-monitor
journalctl -u short-lev-monitor -f -o cat

# 20. Locally: ~/.ssh/config User root -> User owen
```

---

## 10. Troubleshooting

| Symptom | Cause |
|---|---|
| Gateway restarts every 30s, exits 0 in 34ms | missing `-inline` |
| `can't find jars folder` | Gateway not at `~/Jts/ibgateway/<vrsn>/`; don't use `-dir` |
| `Invalid username or password` in the VNC window | wrong `IbPassword`; IBC log shows the login attempt |
| `invalid literal for int()` | inline comment in `.env` |
| `ConnectionRefusedError` on 4002 | Gateway API settings lost (reinstall) or still booting |
| `Error 10141` then `clientId already in use?` | paper disclaimer; ignore the second error |
| `-- No entries --` from journalctl | not in `adm` group; use sudo |
| `uv: command not found` under `sudo -u` | use the absolute path |
| VNC connects to nothing | tunnel terminal closed |
| x11vnc on 5901 not 5900 | stale instance holds 5900 |

---

## 11. Not yet built

- **Dashboard**, Tailscale-bound. The stakeholder-facing surface; `app.py` is
  still a backtest UI.
- **Backups** of `/var/lib/short-lev/` -- small, irreplaceable, currently none.
- **Close public SSH** once all operators are on Tailscale.
- **Multi-account.** One Gateway per IBKR login, so a second account means a
  second display, port, IBC config, and unit set -- unless the accounts sit under
  one advisor login, in which case one Gateway serves both.