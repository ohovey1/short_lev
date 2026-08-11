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

**Gates 1-5, 7, and 9 pass. Gates 6 and 8 deferred to the overnight window.**

This section was first written before the box work and recorded only the offline
half; everything under "The live run" below was added after the gate run.

Gate 9 passes: `verify_band.py`, `verify_engine.py`, and `verify_monitor.py` all
green, and `hash_band.py`'s GRAND digest is
`08baa20ac842502aedbb5f647f6ea24cf3128429f17f4a5a1d7d1d0cb85de942`, identical to
`baseline_005_hash.txt`. `band.py`, `decision.py`, `engine.py`, and `config.py`
were not touched.

All six items shipped. Files: `src/monitor.py`, `src/alert_state.py`,
`scripts/verify_monitor.py`, `.env.example`, `docs/VPS.md`,
`docs/AUTOMATION.md`, and seven new files in `deploy/`.

### Deviations

**1. Item 2's diagnosis was wrong.** The spec says the condition "appears to be
'none sent today' rather than 'none sent today and we are past the configured
hour'." It is not: `heartbeat_due` already gated on the hour and returned False
before HH:MM. The real defect is that the gate was **open-ended** -- 15:24 and
15:57 are both past 09:45, so a restart on a day with no recorded heartbeat fired
one immediately, twice. Fixed with a 30-minute catch-up window
(`alert_state.HEARTBEAT_WINDOW_MINUTES`); past it, the day is skipped rather than
fired late. The spec's stated fix would have changed nothing.

The double firing 33 minutes apart also means `last_heartbeat_date` was not
persisting between those two runs. The window makes that harmless, but the
underlying cause was not chased -- likely the state path, which item 3 moves
anyway. Worth confirming on the box: after gate 6, check that
`/var/lib/short-lev/alert_state.json` actually contains `last_heartbeat_date`.

**2. Item 3 names one path; there are three.** `ALERT_STATE_PATH` and
`EVENT_LOG_DIR` arrived in spec 006, after 007 was written, and both default into
the repo tree. All three now documented at `/var/lib/short-lev`. No code change
was needed -- the three resolvers already read env with an in-repo fallback,
which stays correct for local dev.

**3. The tz switch needed a migration the spec did not mention.** Making `now`
ET-aware breaks comparison against `last_sent_ts` values already on disk in naive
form: `TypeError`, caught by the loop's catch-all, becomes a backoff loop that
never clears until someone deletes the file. Added `alert_state._match_awareness`.

Implemented as **bidirectional** coercion against `now` rather than the planned
unconditional coerce-to-ET. The one-directional version broke `check_n`, which
passes a naive `now` -- and that was the useful signal: a dedup ledger must never
be able to take the monitor down, in either direction.

**4. `docs/VPS.md` scope was wider than "section 8".** The file's own durable
marker said "section 8 **onward**", and sections 9 and 10 both went stale -- 9
installed tmux and `mkdir`'d the in-repo state dir, 10 was a TODO list this spec
mostly completes. Section 7's opening claim ("There are no IBKR credentials in
this repo **or on this box's disk**") is also made false by IBC. Changed: the
header marker, one sentence in 7, all of 8, steps 9-15 of section 9, and section
10. Sections 1-6 untouched.

**5. One behavior change not in the spec.** The in-loop reconnect used to sleep
`backoff` *before* its first attempt, but the `DISCONNECT_ERRORS` handler has
already waited by the time the loop re-enters -- a double wait. The helper now
sleeps only after a *failed* attempt, so the first reconnect try is immediate.
The log line lost its "in %ds", which was describing a wait that had already
happened.

### Verified mechanically

`IB.sleep` is a genuine `staticmethod` and the same function object as
`util.sleep`; it pumps the event loop with no instance and is reusable across
calls. The plan's fallback was unnecessary. This matters because there is no live
handle before the first connect, and the handle is dead after a failed reconnect.

Also confirmed by hand: an ET-aware `last_sent_ts` round-trips through the state
file with its offset, the repeat timer honors it, the heartbeat fires once per
day, and the 2026-11-01 DST fall-back does not break the window.

---

## The live run

### Gates

| Gate | Result |
|---|---|
| 1. Unattended reboot | **PASS.** Cold reboot, all four units active, IBC logged Gateway in with nobody involved. |
| 2. Monitor waits rather than crash-looping | **PASS.** One WARNING, 10s backoff, connected 11 seconds after boot. No traceback, no repeated exits. Item 1's fix is what made this work. |
| 3. VNC restarts independently | **PASS.** VNC restarted; Gateway untouched; the monitor never noticed. Vindicates the separate-unit decision. |
| 4. Gateway restart survived | **PASS.** Disconnect logged as one line, no traceback; reconnected in 10s unattended. |
| 5. State survives a re-clone | **PASS.** `/opt/short_lev` deleted and re-cloned; `peak_equity=10507.95` survived in `/var/lib/short-lev`. |
| 6. Heartbeat respects the hour | **DEFERRED** to the overnight window. |
| 7. Credentials locked down | **PASS.** `config.ini` is `600 short-lev`; no filled-in password anywhere in the repo. |
| 8. Nightly restart | **DEFERRED** to the overnight window. The real gate, still unrun. |
| 9. Offline suite + hash | **PASS.** See above. |

### Five failures during setup -- none of them in the code

1. **`-dir /home/short-lev/Jts` on the original Gateway install.** It flattened
   the version subdirectory away. IBC builds the path as
   `<tws_path>/ibgateway/<vrsn>/jars` (`ibcstart.sh` line 241) and failed with
   "can't find jars folder". **Root cause of the whole evening** -- everything
   else was found while chasing this.
2. **Missing `-inline`.** `gatewaystart.sh` launches an xterm in the background
   and returns 0 in ~34ms, so systemd saw the main process exit and restarted
   every 30s forever.
3. **`PrivateTmp=true` on the Gateway unit.** Xvfb's socket is in
   `/tmp/.X11-unix`; a private `/tmp` namespace hides it.
4. **Inline comment on `IB_PORT` in `.env`** -> `invalid literal for int()`.
   python-dotenv does not strip trailing comments from an unquoted value.
5. **`TWS_MAJOR_VRSN` shipped as `1019`**; Gateway here is 1045.

2 and 3 are fixed in `deploy/short-lev-gateway.service`. 1, 4, and 5 are
box-side and documented in `docs/VPS.md`.

### Also learned

- **IBC 3.20.0 -- the version this spec named -- cannot work with Gateway 1045.**
  Gateway 1034+ on Linux moved to the Azul Zulu 17 JRE and support arrived in IBC
  3.21.0. Avoid 3.21.0 for its autorestart bug; **3.24.0** is what works here.
- **`gatewaystart.sh` overrides `IBC_INI` and `LOG_PATH` unconditionally**, so
  the unit's `Environment=` has no effect on either. All three -- those two plus
  `TWS_MAJOR_VRSN` -- are patched directly in the script, **and all three are lost
  on an IBC upgrade.**
- **`uv` needs an absolute path under `sudo -u ... bash -c`.** Non-login shells
  do not source the profile.
- **`unzip` was not installed.** Added to the package list.
- **`ReadOnlyApi=yes` confirmed working.** IBC logs "Read-Only API checkbox is
  already set to: true" on every start, which makes the no-orders property a
  matter of configuration rather than a checkbox someone once ticked.

### Repo reconciliation

The box needed five hand-fixes that were not reflected in the repo, so a rebuild
from `main` would have failed. Reconciled afterwards: the two Gateway unit fixes,
the executable bit on both deploy scripts (committed `100644`, so
`sudo deploy/install-units.sh` failed with "command not found"), the IBC template
corrections, and a full replacement of `docs/VPS.md`.

`install-units.sh` was also changed to seed the IBC config from
`/opt/ibc/config.ini` rather than from the repo template -- it was still copying
the template into place, which is the unsupported path the template now warns
against.

### Deferred

- Gates 6 and 8, both needing the overnight window.
- Dashboard unit, inbound Telegram commands, external dead-man, backups of
  `/var/lib/short-lev` -- all still out of scope, now listed in VPS.md section 11.
- `PAIR_KEY` is still hardcoded `"TSLL"`.
- **IBC's maintainer has said he is stepping away.** Logged as a dependency risk.

### Note for the live run

Gate 7 says `grep -ri "password" /opt/short_lev` finds nothing. Taken literally it
will fail: the word appears ~20 times in the checkout -- `docs/VPS.md` (the
credential note, the SSH hardening lines, `--disabled-password`),
`deploy/ibc-config.ini.template` (header warning plus the blank `IbPassword=`),
`deploy/install-units.sh` (the operator reminder), and older specs. All of them
are prose or empty keys.

What gate 7 actually needs to establish is that no credential VALUE is in the
repo. Run this instead:

```bash
grep -rIn "IbPassword=." /opt/short_lev        # must return nothing
ls -l /var/lib/short-lev/ibc/config.ini        # must be 600, short-lev
```

The first finds a filled-in password anywhere in the checkout; the blank
`IbPassword=` in the template does not match, a real one does.