# VPS Operations -- `short-lev-01`

Reference for the box that runs IB Gateway and the short_lev monitor.
Verified 2026-08-06 through 2026-08-10.

Sections 1-7 are durable -- the box, access, Gateway, and the auth model do not
change when systemd lands. **Section 8 onward describes the pre-systemd manual
workflow and will be replaced in Phase 1d.**

---

## 1. The box

| | |
|---|---|
| Provider | Hetzner Cloud, project `short-lev` |
| Server name | `short-lev-01` |
| Type | **CPX 11** -- 2 vCPU / 2 GB RAM / 40 GB disk |
| Cost | ~$21/mo, billed hourly |
| Location | Ashburn, VA (us-east) |
| OS | Ubuntu **22.04.5 LTS** |
| Public IPv4 | `5.161.232.45` |
| Tailscale IPv4 | `100.123.221.44` |
| Swap | 2 GB (`/swapfile`, persisted in `/etc/fstab`) |

**On sizing.** CPX 21 (4 GB, ~$38/mo) is the conservative choice. CPX 11 + swap
was taken instead because Gateway self-caps its heap at `-Xmx768m` and idles
around 270 MB. Hetzner resizes up in ~5 minutes with no reinstall. **If Gateway
thrashes or is OOM-killed, resize rather than debugging.**

**On location.** Ashburn matters for latency to IBKR's US gateways, and for
avoiding a login from an unexpected country, which can trigger IBKR security
review. Cost-optimised (CX) plans had no US stock at provisioning time.

**Do not use ARM.** IBKR ships Gateway as x86 Linux only. Avoid Hetzner's CAX
line and Raspberry Pi -- ARM means unsupported jar extraction that breaks on
Gateway's automatic updates.

---

## 2. Users

| User | Login | Purpose |
|---|---|---|
| `root` | **disabled** | Reachable only via `sudo` from `owen`. |
| `owen` | SSH key | Admin. Has a password, needed for `sudo`. |
| `short-lev` | **none** (`--disabled-password`) | Service account. Owns `/opt/short_lev` and `/home/short-lev/Jts`. Reach with `sudo -u short-lev -i`. |

The service account is deliberately separate from `owen` and from the `hype_arb`
box entirely -- no shared blast radius between two trading systems.

**Onboarding another operator:** give them their own admin user rather than
sharing `owen`, so the audit trail distinguishes actions. They will also need a
Tailscale invite and their own IBKR user under Users & Access Rights.

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

`exit` once to leave `short-lev`, again to close the session.

### Common failures

- `Permission denied (publickey)` -- `~/.ssh/config` still says `User root`.
  Root login is disabled.
- Commands "disappearing" after `ssh`, `sudo -u`, or `source` -- these change
  shell context, and anything pasted before the new prompt appears is swallowed.
  **Send one command at a time and wait for each prompt.**

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

**Port 5900 (VNC) is deliberately not opened.** x11vnc binds to `-localhost`, so
it is reachable only through an SSH tunnel. Two independent layers: firewall and
bind address.

Timezone is `America/New_York`, intentionally -- every operational question about
this box concerns the 23:45 ET Gateway reset, the Sunday 01:00 ET re-auth, and
market hours.

Unattended security upgrades are installed and enabled.

---

## 5. VNC access to Gateway

Needed whenever Gateway requires a human: weekly re-auth, or after a crash.

**Client:** TigerVNC standalone (`vncviewer64-*.exe`, no install required).

**Step 1 -- open the tunnel.** Its own terminal, left running:

```bash
ssh -L 5900:localhost:5900 short-lev
```

**Step 2 -- connect TigerVNC to `localhost:5900`.** Not the Tailscale IP --
x11vnc refuses connections on that interface by design. Accept the unencrypted
warning; SSH already encrypts the tunnel.

**Step 3 -- close the tunnel terminal when done.**

Blank grey window: press Left-Alt three times to force a repaint.

### Session collision

**Gateway, TWS, and Client Portal cannot hold the same login simultaneously.**
Client Portal refuses with *"the production username associated with this
paper-trading username has an active session."*

Manual trades therefore require stopping Gateway first:

```bash
pkill -f ibgateway                    # as short-lev
# place trades via phone app or Client Portal
DISPLAY=:10 ~/Jts/ibgateway &
```

The mobile app may hold a separate session -- untested. A second user on the
account under Users & Access Rights would remove the constraint entirely, and is
the better long-term fix since Phase 2 is explicitly manual execution.

### Alternative (not currently used)

Binding x11vnc to the Tailscale IP (`-listen 100.123.221.44`) skips the tunnel,
but then any device on the tailnet reaches the Gateway screen with no password.
Worth reconsidering **with a VNC password set** if a non-technical person ever
has to perform the weekly login -- it reduces the ritual to opening one app.

---

## 6. IB Gateway

**Version 10.45**, `stable` channel, installed as `short-lev` in
`/home/short-lev/Jts`.

### Install (for rebuilds)

```bash
sudo -u short-lev -i
cd ~
wget https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh
chmod +x ibgateway-stable-standalone-linux-x64.sh
./ibgateway-stable-standalone-linux-x64.sh -q -dir ~/Jts
```

`-q` runs unattended -- the installer needs no display. Use the **standalone**
build; the regular installer requires a GUI.

Prefer `stable` over `latest`: fewer version bumps under a box meant to sit
untouched, and IBC pins to a Gateway major version.

> **The executable is `~/Jts/ibgateway` -- a file, not a directory.**
> The standalone build differs from the installer layout. The
> `~/Jts/ibgateway/*/ibgateway` path in every online guide fails here with
> `Not a directory`.

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

Read-Only API stays ON through Phase 1c -- it makes "no order submission" a
property of the broker rather than a promise in a spec.

### First-run gate

The **paper trading disclaimer** must be accepted once before the API will serve
position data. Until then the API connects and immediately drops with
`Error 10141: Paper trading disclaimer must first be accepted for API
connection`, followed by a misleading `Peer closed connection. clientId 11
already in use?` -- ignore the second, fix the first. Accept it under
Configure -> Settings -> API -> Precautions.

---

## 7. Authentication model

**There are no IBKR credentials in this repo or on this box's disk.** Gateway
holds the authenticated session; the API socket on localhost requires no
authentication. `.env` carries connection coordinates only.

| Event | Frequency | Human needed |
|---|---|---|
| Monitor process | continuous | no |
| Gateway soft restart | nightly ~23:45 ET | no |
| Full re-auth | weekly, after Sun 01:00 ET | ~2 min via VNC |
| Crash re-auth | rare, unpredictable | ~2 min via VNC |

The weekly window falls Sunday morning with markets closed until Monday 09:30 --
roughly 32 hours of slack.

**Observed 2026-08-10:** the first login after the Sunday 01:00 ET expiry -- a
genuine weekly re-auth -- required username and password only, **no 2FA**. Two
consecutive paper logins now with no second factor. If this holds, IBC could
automate the weekly login entirely and VNC becomes exception-only. **Do not plan
around it for live**, where IB Key is likely enforced.

If push notifications are unreliable, Gateway's challenge/response mode is more
dependable -- it displays a challenge number, entered into IBKR Mobile's IB Key,
and the response typed back. No dependence on notification delivery.

**Only one session per credential** -- see Session collision above.

---

## 8. Running things (pre-systemd -- will be replaced in Phase 1d)

### Layout

```
/opt/short_lev/                  repo, owned by short-lev
/opt/short_lev/.env              chmod 600, never committed, never deployed
/opt/short_lev/data/state/       monitor state (peak_equity)
/home/short-lev/Jts/             Gateway install and its settings
/home/short-lev/.local/bin/uv    uv, installed per-user
```

> **Known issue:** the state directory sits inside the git checkout. A re-clone
> would wipe `peak_equity` and silently disable the drawdown stop. Move it
> outside the repo tree before this runs unattended.

### `.env`

```
POLYGON_API_KEY=<set>
IB_HOST=127.0.0.1
IB_PORT=4002
IB_CLIENT_ID=11
IB_ACCOUNT=DU<redacted>
MONITOR_BASE_CAPITAL=10000
MONITOR_STATE_PATH=/opt/short_lev/data/state/monitor.json
POLL_INTERVAL_SECONDS=900
```

`IB_PORT` is the only thing separating paper from live -- the monitor logs port
and account on every startup for this reason. `IB_CLIENT_ID=11` is deliberately
not 0 or 1; every example script uses those, and a collision with a half-dead
session fails the connect.

### tmux convention

The Gateway stack runs in a tmux session named `gw`, owned by `short-lev`.

```bash
sudo -u short-lev -i
tmux attach -t gw          # or: tmux new -s gw
# ... start or inspect ...
# Ctrl-B then D to detach
```

**Anything started outside tmux dies when the SSH session closes.** tmux sessions
are per-user -- `tmux ls` as `owen` will not show `short-lev`'s sessions -- and a
reboot destroys them.

### Start the Gateway stack

Inside tmux:

```bash
Xvfb :10 -screen 0 1024x768x24 -nolisten tcp &
sleep 2
DISPLAY=:10 ~/Jts/ibgateway &
sleep 15
DISPLAY=:10 x11vnc -display :10 -localhost -nopw -forever &
```

The `sleep`s matter: Xvfb must exist before Gateway draws, and Gateway must
render before x11vnc attaches. Confirm x11vnc reports `PORT=5900` -- if a stale
instance holds it, x11vnc silently auto-probes to 5901 and the tunnel will not
match.

### Check and stop

```bash
ps aux | grep -E "Xvfb|ibgateway|x11vnc" | grep -v grep
free -h

pkill -f ibgateway
pkill x11vnc
pkill Xvfb
```

### Run the monitor

```bash
sudo -u short-lev -i
cd /opt/short_lev
uv run python src/monitor.py
```

### Deploy an update

```bash
cd /opt/short_lev
git pull --ff-only
uv sync
```

Deliberately **not** wired to auto-deploy on push. Restarting interrupts monitor
state continuity, so deploys are a chosen action, ideally outside market hours.
No GitHub Actions, no deploy key, and specifically no `NOPASSWD: ALL` sudoers
entry -- that pattern exists on the `hype_arb` box and should not be copied here.

---

## 9. Rebuilding from scratch

Ordered. **Do not paste as a block** -- anything following `ssh`, `sudo -u`, or
`source` is swallowed by a shell that has not started yet.

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

# 8. Tailscale -- before anything else; everything after is easier with it
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh          # blocks: open the printed URL, authenticate, wait
ufw allow in on tailscale0
tailscale ip -4

# 9. Display stack + JRE
apt update
apt install -y xvfb x11vnc openjdk-17-jre unattended-upgrades tmux

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
# 13. Log into Gateway over VNC, accept the paper disclaimer, set API options
```

---

## 10. Not yet built

- **systemd units.** Gateway (needs `DISPLAY`, cannot use
  `ProtectHome=read-only` since it writes to `~/Jts`, `RestartSec=30` so a
  failing login is not hammered), monitor, dashboard. Until these exist, nothing
  survives an SSH disconnect. This replaces section 8.
- **IBC** for automated credential entry on restart. The 2026-08-10 no-2FA
  observation suggests this could cover the weekly login on paper.
- **Dashboard**, Tailscale-bound (`--server.address=$(tailscale ip -4)`), no
  public port and no reverse proxy.
- **State file relocation** out of the repo tree.
- **Backups** -- `peak_equity` is small and irreplaceable. Nightly tar to
  `data/backups/`, optional rsync to a Hetzner Storage Box, 30-day retention.