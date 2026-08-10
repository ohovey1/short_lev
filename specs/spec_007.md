# Spec 007 -- systemd, IBC, and unattended operation

**Phase:** 1d
**Depends on:** spec 006 (shipped)
**Estimated:** one session, mostly operational rather than code

## Why

Nothing on the box survives a disconnect. Today alone: an orphaned monitor held
`clientId 11` and blocked a restart, x11vnc died twice with its terminal, and
rebuilding the stack took three terminals and a checklist. None of it survives a
reboot.

The target state is that the box reboots and comes back **logged in and
monitoring, with nobody involved**. VNC becomes an escape hatch for when
something needs eyes, not a routine ritual -- which matters because a
non-technical account holder may eventually need access.

## Out of scope (do not build)

- **Dashboard unit.** `app.py` is still a backtest UI with no live data. Running
  it as a service means serving an empty page. It becomes the right stakeholder
  surface later; not now.
- **Inbound Telegram commands.** Its own spec, after this.
- **Executor**, multi-pair, order submission. Read-Only API stays on.
- Any change to `decision.py`, `band.py`, `engine.py`, or `config.py`. If
  `hash_band.py` moves, something is wrong.

---

## 1. Fix the initial connect (code, do this first)

`broker.connect()` is called outside the retry loop, so a connect-time failure
raises and the process exits with a full traceback. Observed twice: once on the
paper-disclaimer gate, once on a stale `clientId`.

This becomes a crash loop under systemd. On reboot the monitor and Gateway race,
the monitor loses, `Restart=always` restarts it, and it loses again -- until
Gateway finishes booting. It settles eventually, so it may never be noticed, but
the first minutes after every reboot are silent and the journal fills with noise
that masks real failures.

The initial connect must use the same backoff path as reconnection: log one
WARNING line, wait, retry, never exit. Spec 006 item 3 quieted mid-run
disconnects; this extends it to startup.

## 2. Fix the heartbeat trigger

The heartbeat currently fires on every restart regardless of `HEARTBEAT_HOUR`.
Observed at 15:24 and again at 15:57 with the hour set to 09:45. The condition
appears to be "none sent today" rather than "none sent today **and** we are past
the configured hour."

A heartbeat arriving at arbitrary times cannot do its job -- you can only notice
silence if you know when to expect noise.

While here, use `ZoneInfo("America/New_York")` rather than local time.
`HEARTBEAT_HOUR` defaults to 09:45 because that is just after market open, which
is an ET fact. `zoneinfo` is stdlib; there is no dependency cost. On a UTC box
the current code fires at an arbitrary hour with no error and no clue why.

Also: `1 checks` should read `1 check`.

## 3. Move the state directory out of the repo

`MONITOR_STATE_PATH` currently resolves inside the git checkout. A re-clone or a
destructive pull wipes `peak_equity`, which silently disables the drawdown stop
-- a failure with no symptom.

Move to `/var/lib/short-lev/`, owned by `short-lev`. Update `.env`,
`.env.example`, and `docs/VPS.md`. The unit's `StateDirectory=` handles creation
and ownership.

Applies to `alert_state.json` and the event logs too.

---

## 4. IBC

IBC types credentials into Gateway's login dialog on restart, so a reboot or
nightly restart needs no human.

- Install to `/opt/ibc`, config in `/var/lib/short-lev/ibc/config.ini`.
- Pin `IbDir` to `/home/short-lev/Jts` and the Gateway major version to `1045`.
- Set `IbLoginId`, `IbPassword`, `TradingMode=paper`.
- Set `AutoRestartTime` so Gateway soft-restarts nightly without
  re-authentication.

**The config file contains the IBKR password.** `chmod 600`, owned by
`short-lev`, under `/var/lib/`, never in the repo, never in a backup that leaves
the box. Until now this machine held zero credentials; that property is being
traded for unattended restarts. Note it explicitly in `docs/VPS.md` -- it is a
different security posture, and it is a live conversation to have again before
this runs on an account you do not own.

**IBC cannot approve 2FA.** Paper logins have required none across three
observations, including a genuine post-expiry weekly re-auth, so on paper this
should be fully hands-off. Do not assume it holds for live.

## 5. The units

Four files in `deploy/`, installed to `/etc/systemd/system/`.

### `short-lev-xvfb.service`

The virtual display. Everything else depends on it.

```
ExecStart=/usr/bin/Xvfb :10 -screen 0 1024x768x24 -nolisten tcp
```

### `short-lev-gateway.service`

`After=short-lev-xvfb.service`, `Requires=` it. `Environment=DISPLAY=:10`.
`ExecStart=/opt/ibc/gatewaystart.sh`.

**Hardening is deliberately looser than the monitor's.** Gateway writes to
`~/Jts` constantly -- settings, logs, auto-restart state -- so
`ProtectHome=read-only` breaks it.

`RestartSec=30`, not 10: a Gateway failing because it needs fresh
authentication is not helped by restarting every ten seconds, and IBKR throttles
repeated login attempts.

### `short-lev-vnc.service`

**Its own unit, not folded into Gateway.** If VNC dies, restarting it must not
restart Gateway -- that would log Gateway out and create the exact situation
this spec exists to avoid. `After=short-lev-xvfb.service`.

```
ExecStart=/usr/bin/x11vnc -display :10 -localhost -nopw -forever
```

Keep `-localhost`. VNC stays tunnel-only.

### `short-lev-monitor.service`

`After=short-lev-gateway.service`. Full hardening -- unlike Gateway, the monitor
has no reason to write outside its state directory:

```
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
NoNewPrivileges=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
StateDirectory=short-lev
ReadWritePaths=/var/lib/short-lev /opt/short_lev/.venv /home/short-lev/.local /home/short-lev/.cache
```

`Restart=always`, `RestartSec=10`, `EnvironmentFile=/opt/short_lev/.env`, output
to journal.

**`After=` is ordering, not readiness.** systemd starts the monitor once Gateway's
*process* exists, which is well before Gateway is logged in and serving the API.
Item 1's backoff is what actually handles this -- do not try to solve it with
unit ordering.

## 6. Deploy script and docs

`deploy/install-units.sh`: copy the units, `daemon-reload`, enable all four.

`deploy/deploy.sh` (run from a laptop over Tailscale): `git pull --ff-only`,
`uv sync`, and **stop before restarting**. Print the restart command rather than
running it. Restarting interrupts monitor continuity, so it stays a chosen
action, ideally outside market hours.

**Rewrite `docs/VPS.md` section 8.** Sections 1-7 are marked durable and stay;
section 8 describes the pre-systemd manual workflow and is now wrong. Replace
with: `systemctl` commands, `journalctl -u short-lev-monitor -f` for logs, the
new state paths, IBC's credential note, and the fact that tmux is no longer part
of normal operation.

Keep one line saying tmux was the old approach and why it is gone -- another
operator has the current doc and will notice the change.

---

## Session gate

1. **Reboot is fully unattended.** `sudo reboot`. Without touching anything:
   Xvfb, Gateway (logged in via IBC), VNC, and the monitor all return, and a
   check line appears in the journal.
2. **The monitor waits for Gateway rather than crash-looping.** Watch the boot
   journal: one WARNING per retry, backoff visible, no traceback, no repeated
   process exits.
3. **VNC restarts independently.** `systemctl restart short-lev-vnc` while the
   monitor runs. VNC returns; Gateway stays logged in; the monitor never
   disconnects.
4. **Gateway restart is survived.** `systemctl restart short-lev-gateway`. IBC
   logs back in unattended; the monitor reconnects on its own; one INFO alert.
5. **State survives a re-clone.** Delete and re-clone `/opt/short_lev`.
   `peak_equity` is intact because it lives in `/var/lib/short-lev`.
6. **Heartbeat respects the hour.** Restart the monitor mid-afternoon -- no
   heartbeat. Set `HEARTBEAT_HOUR` to a few minutes ahead and confirm exactly one
   arrives in that window.
7. **Credentials are locked down.** `ls -l` on the IBC config shows `600` and
   `short-lev` ownership. `grep -ri "password" /opt/short_lev` finds nothing.
8. **The nightly restart works.** Leave it running overnight. Next morning:
   Gateway restarted at its `AutoRestartTime`, the monitor reconnected without
   help, and the journal shows the gap and the recovery.
9. `verify_band.py`, `verify_engine.py`, `verify_monitor.py` pass and
   `hash_band.py` is unchanged.

Gate 8 is the real one. Everything else can be forced; that one is the system
doing what it is meant to do while nobody watches.

Separate commits per numbered item, imperative lowercase with a scope prefix. Do
not commit until I have reviewed the diff.

---

## Result

*(Fill in after the session.)*