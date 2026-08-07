# VPS Operations -- `short-lev-01`

Reference for the box that runs IB Gateway and the short_lev monitor.
Everything here was run and verified on 2026-08-06.

---

## 1. The box

| | |
|---|---|
| Provider | Hetzner Cloud, project `short-lev` |
| Server name | `short-lev-01` |
| Type | **CPX 11** -- 2 vCPU / 2 GB RAM / 40 GB disk |
| Cost | ~$21/mo (~$20.49 server + $0.60 IPv4), billed hourly |
| Location | Ashburn, VA (us-east) |
| OS | Ubuntu **22.04.5 LTS** |
| Public IPv4 | `5.161.232.45` |
| Public IPv6 | `2a01:4ff:f0:d253::1` |
| Tailscale IPv4 | `100.123.221.44` |
| Swap | 2 GB (`/swapfile`, persisted in `/etc/fstab`) |

**On sizing.** CPX 21 (4 GB, ~$38/mo) is the conservative choice. CPX 11 + swap
was taken instead because Gateway self-caps its heap at `-Xmx768m` and idles
around 270 MB. Hetzner resizes up in ~5 minutes with no reinstall, and billing
is hourly, so the downside of being wrong is small. **If Gateway starts thrashing
or gets OOM-killed, resize rather than debugging.**

**On location.** Ashburn matters for two reasons: latency to IBKR's US gateways,
and avoiding a login from an unexpected country, which can trigger IBKR security
review. Cost-optimised (CX) plans had no US stock at provisioning time, hence
the pricier CPX line.

**Do not use ARM.** IBKR ships Gateway as x86 Linux only. Avoid Hetzner's CAX
line and Raspberry Pi entirely -- ARM means unsupported jar extraction that
breaks on Gateway's automatic updates.

---

## 2. Users

| User | Login | Purpose |
|---|---|---|
| `root` | **disabled** | Reachable only via `sudo` from `owen`. |
| `owen` | SSH key | Admin. Has a password, needed for `sudo`. |
| `short-lev` | **none** (`--disabled-password`) | Service account. Owns `/opt/short_lev` and `/home/short-lev/Jts`. Reach with `sudo -u short-lev -i`. |

The service account is deliberately separate from `owen` and from the `hype_arb`
box entirely -- no shared blast radius between two trading systems.

---

## 3. Getting in and out

### Local SSH key

Per-project, matching the existing `hype_arb` convention:

```
~/.ssh/short_lev_vps        # private
~/.ssh/short_lev_vps.pub    # registered in Hetzner as "owen-laptop short_lev"
```

### `~/.ssh/config`

```
Host short-lev
    HostName 5.161.232.45
    User owen
    IdentityFile ~/.ssh/short_lev_vps
```

### Connect

```bash
ssh short-lev                    # as owen
sudo -u short-lev -i             # switch to the service account
```

### Exit

`exit` once to leave `short-lev`, again to close the SSH session.

**Anything started from an SSH shell dies when that shell closes.** Use `tmux`
for anything that must outlive the session, until the systemd units exist.

### Common failure

`Permission denied (publickey)` almost always means `~/.ssh/config` still says
`User root`. Root login is disabled.

---

## 4. Security posture

```bash
# /etc/ssh/sshd_config
PermitRootLogin no
PasswordAuthentication no
```

```bash
# ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow in on tailscale0
ufw enable
```

**Port 5900 (VNC) is deliberately not opened.** x11vnc binds to `-localhost`,
so it is reachable only through an SSH tunnel. Two independent layers: the
firewall and the bind address.

Also set: `timedatectl set-timezone America/New_York`. This is intentional --
every operational question about this box concerns the 23:45 ET Gateway reset,
the Sunday 01:00 ET re-auth, and market hours. Matching the clock removes a
conversion from every debugging session.

Unattended security upgrades are installed and enabled.

---

## 5. VNC access to Gateway

Needed whenever Gateway requires a human: weekly re-auth, or after a crash.

**Client:** TigerVNC standalone (`vncviewer64-*.exe`, no install required).

**Step 1 -- open the tunnel.** In its own terminal, left running:

```bash
ssh -L 5900:localhost:5900 short-lev
```

**Step 2 -- connect TigerVNC to:**

```
localhost:5900
```

**Not** the Tailscale IP -- x11vnc refuses connections on that interface by
design. Accept the unencrypted-connection warning; SSH already encrypts the
tunnel.

**Step 3 -- close the tunnel terminal when done.**

If the VNC window is blank grey, press Left-Alt three times to force a repaint.

### Alternative (not currently used)

Binding x11vnc to the Tailscale IP directly (`-listen 100.123.221.44`) also
works and skips the tunnel, but then any device on the tailnet can reach the
Gateway screen with no password. The tunnel is preferred.

---

## 6. IB Gateway

**Version 10.45**, `stable` channel, installed as `short-lev` in
`/home/short-lev/Jts`.

### Install (already done -- for rebuilds)

```bash
sudo -u short-lev -i
cd ~
wget https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh
chmod +x ibgateway-stable-standalone-linux-x64.sh
./ibgateway-stable-standalone-linux-x64.sh -q -dir ~/Jts
```

`-q` runs unattended -- the installer itself needs no display. Use the
**standalone** build; the regular installer requires a GUI.

Prefer `stable` over `latest`: fewer version bumps under a box meant to sit
untouched, and IBC pins to a Gateway major version.

### Start (manual)

```bash
Xvfb :10 -screen 0 1024x768x24 -nolisten tcp &
sleep 2
DISPLAY=:10 ~/Jts/ibgateway &
sleep 15
DISPLAY=:10 x11vnc -display :10 -localhost -nopw -forever &
```

The `sleep`s matter: Xvfb must exist before Gateway draws, and Gateway must
render before x11vnc attaches.

> **The executable is `~/Jts/ibgateway` -- a file, not a directory.**
> The standalone build differs from the installer layout. The
> `~/Jts/ibgateway/*/ibgateway` path in every online guide fails here with
> `Not a directory`.

### Stop

```bash
pkill -f ibgateway
pkill x11vnc
pkill Xvfb
```

### Check what is running

```bash
ps aux | grep -E "Xvfb|ibgateway|x11vnc" | grep -v grep
free -h
```

### Gateway settings (via VNC, Configure -> Settings)

**API -> Settings:**

| Setting | Value |
|---|---|
| Read-Only API | **ON** |
| Enable ActiveX and Socket Clients | **ON** |
| Socket port | **4002** (paper) / 4001 (live) |
| Trusted IPs | `127.0.0.1` |
| Download open orders on connection | **OFF** |

**Lock and Exit:** set **auto-restart**, not auto-logoff. This is what turns a
daily login into a weekly one.

Read-Only API stays ON for all of Phase 1b and 1c -- it makes "no order
submission" a property of the broker rather than a promise in a spec.

---

## 7. Authentication model

There are **no IBKR credentials anywhere in this repo or on this box's disk.**
Gateway holds the authenticated session; the API socket on localhost requires no
authentication at all. `.env` carries connection coordinates only.

| Event | Frequency | Human needed |
|---|---|---|
| Monitor process | continuous | no |
| Gateway soft restart | nightly ~23:45 ET | no |
| Full re-auth | weekly, after Sun 01:00 ET | ~2 min via VNC |
| Crash re-auth | rare, unpredictable | ~2 min via VNC |

The weekly window falls Sunday morning with markets closed until Monday 09:30 --
roughly 32 hours of slack.

**Observed 2026-08-06:** paper login required username and password only, no
2FA. One data point; do not generalise. The live account will differ, and the
Sunday expiry is the real test.

If push notifications are unreliable, Gateway's challenge/response mode is more
dependable -- it displays a challenge number, entered into IBKR Mobile's IB Key,
and the response typed back. No dependence on notification delivery.

**Only one session per credential.** Gateway on the VPS and Gateway or TWS on
the laptop cannot both be logged in. Opening TWS will disconnect the box.

---

## 8. Application layout

```
/opt/short_lev/                  repo, owned by short-lev
/opt/short_lev/.env              chmod 600, never committed, never deployed
/opt/short_lev/data/state/       monitor state (peak_equity)
/home/short-lev/Jts/             Gateway install and its settings
/home/short-lev/.local/bin/uv    uv, installed per-user
```

> **Known issue:** the state directory currently sits inside the git checkout. A
> re-clone would wipe `peak_equity` and silently disable the drawdown stop. Move
> it outside the repo tree before this runs unattended.

### `.env` on this box

```
POLYGON_API_KEY=<set>
IB_HOST=127.0.0.1
IB_PORT=4002
IB_CLIENT_ID=11
IB_ACCOUNT=DUXXXXXXX
MONITOR_BASE_CAPITAL=10000
MONITOR_STATE_PATH=/opt/short_lev/data/state/monitor.json
POLL_INTERVAL_SECONDS=900
```

`IB_PORT` is the only thing separating paper from live. `IB_CLIENT_ID=11` is
deliberately not 0 or 1 -- every example script uses those, and a collision with
a half-dead session fails the connect.

### Run the monitor

```bash
sudo -u short-lev -i
cd /opt/short_lev
uv run python src/monitor.py
```

### Deploy an update

```bash
sudo -u short-lev -i
cd /opt/short_lev
git pull --ff-only
uv sync
```

Deliberately **not** wired to auto-deploy on push. Restarting interrupts monitor
state continuity, so deploys are a chosen action, ideally outside market hours.
No GitHub Actions, no deploy key, and specifically no `NOPASSWD: ALL` sudoers
entry.

---

## 9. Rebuilding from scratch

Ordered. Each step assumes the previous completed.

```bash
# 1. Hetzner console: CPX 11, Ashburn, Ubuntu 22.04, SSH key added AT CREATION.
#    Skipping the key means a root password by email -- a worse starting point.

# 2. First login, as root
ssh root@<ip>

# 3. Swap
fallocate -l 2G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# 4. Admin user
adduser owen
usermod -aG sudo owen
rsync --archive --chown=owen:owen ~/.ssh /home/owen

# 5. >>> VERIFY `ssh owen@<ip>` AND `sudo -v` IN A SECOND TERMINAL <<<
#    Do not proceed until both work. The next step is what locks you out.

# 6. Harden SSH
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# 7. Firewall + clock
ufw default deny incoming && ufw default allow outgoing
ufw allow OpenSSH && ufw enable
timedatectl set-timezone America/New_York

# 8. Tailscale -- before anything else, everything after is easier with it
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh          # blocks: open the printed URL, authenticate, wait
ufw allow in on tailscale0
tailscale ip -4

# 9. Display stack + JRE
apt update
apt install -y xvfb x11vnc openjdk-17-jre unattended-upgrades

# 10. Service user
adduser --disabled-password --gecos "" short-lev
mkdir -p /opt/short_lev && chown short-lev:short-lev /opt/short_lev

# 11. As short-lev: Gateway (section 6), then repo
sudo -u short-lev -i
cd /opt/short_lev
git clone https://github.com/ohovey1/short_lev.git .
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv sync
mkdir -p /opt/short_lev/data/state
nano /opt/short_lev/.env && chmod 600 /opt/short_lev/.env

# 12. Update ~/.ssh/config locally: User root -> User owen
```

**Do not paste this as a block.** Anything following `ssh`, `sudo -u`, or
`source` will be swallowed by the shell that has not started yet. One command at
a time, waiting for each prompt.

---

## 10. Not yet built

- **systemd units.** Gateway (needs `DISPLAY`, cannot use
  `ProtectHome=read-only` since it writes to `~/Jts`, `RestartSec=30` so a
  failing 2FA login is not hammered), monitor, dashboard. Until these exist,
  nothing survives an SSH disconnect.
- **IBC** for automated credential entry on restart.
- **Dashboard**, Tailscale-bound (`--server.address=$(tailscale ip -4)`), no
  public port and no reverse proxy.
- **State file relocation** out of the repo tree.
- **Backups** -- `peak_equity` is small and irreplaceable. Nightly tar to
  `data/backups/`, optional rsync to a Hetzner Storage Box, 30-day retention.