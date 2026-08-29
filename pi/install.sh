#!/usr/bin/env bash
# Install vcctrld on the USB4VC Pi. Run from the repo root on the Pi, or let
# pi/deploy.sh push it from the VM.
set -euo pipefail

PREFIX=/opt/vcctrl
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------
# THE MCP SERVER -- a function, not inline, so `--mcp-only` (below, and
# pi/deploy.sh --mcp) can install/restart it WITHOUT touching vcctrld at all.
# vcctrld is the thing this whole file exists to keep running; the MCP
# server is additive and unrelated, and forcing a vcctrld restart to ship an
# unrelated change is exactly the "safe path not available, so the unsafe
# path gets used out of impatience" failure pi/deploy.sh's --page and
# --client modes already exist to avoid for the page and the client.
#
# FAULT-ISOLATED FROM vcctrld regardless of which caller runs it: every
# fallible step is wrapped in its own `if`/`|| true` rather than trusted to
# this script's top-level `set -e`, because a full install still calls this
# AFTER vcctrld is already up, and a failure here must not undo that.
#
# Runs as a SEPARATE service from vcctrld, deliberately -- see the module
# docstring in agent/vcctrl_mcp.py for why (asyncio/uvicorn vs vcctrld's
# plain threading, and blast radius: a bug here must not be able to take
# down the process that owns the uinput devices and the input lock).
install_mcp() {
  if [ ! -f "$SRC/agent/vcctrl_mcp.py" ]; then
    echo "vcctrl-mcp: agent/vcctrl_mcp.py not in this checkout, skipping" >&2
    return 0
  fi
  sudo mkdir -p "$PREFIX/agent"
  sudo install -m 0755 "$SRC/agent/vcctrl_mcp.py" "$PREFIX/agent/vcctrl_mcp.py"
  sudo install -m 0644 "$SRC/agent/requirements.txt" "$PREFIX/agent/requirements.txt"

  mcp_ready=0
  if [ -x "$PREFIX/agent/.venv/bin/python3" ] || \
     sudo python3 -m venv "$PREFIX/agent/.venv" 2>/tmp/vcctrl-mcp-venv.err; then
    if sudo "$PREFIX/agent/.venv/bin/pip" install --quiet \
         -r "$PREFIX/agent/requirements.txt" 2>/tmp/vcctrl-mcp-pip.err; then
      mcp_ready=1
    else
      echo "vcctrl-mcp: pip install failed, service not (re)installed:" >&2
      cat /tmp/vcctrl-mcp-pip.err >&2
    fi
  else
    # Seen on a fresh Debian/Ubuntu Pi image: ensurepip missing until
    # python3-venv (or the version-suffixed package it names) is installed.
    # A rig that has not run that apt command yet still gets a working
    # vcctrld from everything else in this script -- this step degrades, it
    # does not fail the deploy.
    echo "vcctrl-mcp: could not create a venv, service not (re)installed:" >&2
    cat /tmp/vcctrl-mcp-venv.err >&2
    echo "  try: sudo apt install python3-venv (or the version-suffixed" >&2
    echo "  package it names), then re-run this script." >&2
  fi

  if [ "$mcp_ready" = "1" ]; then
    # QUERIED BEFORE THE UNIT IS WRITTEN, so VCCTRL_MCP_ALLOWED_HOSTS can be
    # set correctly on first install rather than needing a second run.
    # Without this, the mcp package's own DNS-rebinding protection (which
    # agent/vcctrl_mcp.py always enables explicitly -- see its module
    # docstring) refuses every request that arrives via the tailscale proxy:
    # measured, HTTP 421 "Invalid Host header", because the Host header the
    # proxy forwards is the tailnet hostname, not 127.0.0.1:8090.
    MCP_TS_NAME=""
    if command -v tailscale >/dev/null 2>&1; then
      MCP_TS_NAME="$(tailscale status --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
    fi

    # UNQUOTED heredoc delimiter -- deliberately, so $MCP_TS_NAME expands.
    # Every other line here is a literal systemd directive with no `$` in
    # it, so this is safe; if a future edit adds one, it will need escaping.
    sudo tee /etc/systemd/system/vcctrl-mcp.service >/dev/null <<UNIT
[Unit]
Description=vcctrl MCP server (Pi-hosted, streamable-http)
# Talks to /usr/local/bin/vcctrl, which talks to vcctrld's socket -- start
# after, though it degrades to per-call connection errors rather than
# failing outright if vcctrld is not up yet or restarts later.
After=vcctrld.service
Wants=vcctrld.service

[Service]
Type=simple
ExecStart=/opt/vcctrl/agent/.venv/bin/python3 /opt/vcctrl/agent/vcctrl_mcp.py
Environment=VCCTRL_MCP_ROLE=daemon
Environment=VCCTRL_MCP_TRANSPORT=streamable-http
Environment=VCCTRL_MCP_HOST=127.0.0.1
Environment=VCCTRL_MCP_PORT=8090
Environment=VCCTRL_MCP_ALLOWED_HOSTS=${MCP_TS_NAME},${MCP_TS_NAME}:443
Restart=always
RestartSec=2
# Does not need uinput or any device access -- it only ever shells out to
# /usr/local/bin/vcctrl, the same as any other local caller. Unprivileged on
# purpose, unlike vcctrld. The \`pi\` user (usb4vc.service's own user, see
# /home/pi/usb4vc/rpi_app above) rather than \`nobody\`, which on some images
# has no home directory and can trip up a venv's own assumptions.
User=pi

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable vcctrl-mcp
    sudo systemctl restart vcctrl-mcp
    sleep 1
    sudo systemctl --no-pager --lines=10 status vcctrl-mcp || true

    # Same tailnet-only HTTPS boundary as the daemon's own web UI (below,
    # main install flow) -- a DIFFERENT PATH on the SAME hostname/port, not a
    # new port exposed on its own. `--set-path` is additive: re-running
    # neither of the two `tailscale serve --https=443` calls in this file
    # disturbs the other's path, the same "idempotent, re-running only
    # re-asserts" property the daemon's own mapping already relies on.
    # NOT fatal if it fails -- the service is still reachable at
    # 127.0.0.1:8090 on the Pi itself either way, which is what
    # --mcp-only needs to be useful even before tailscale is set up.
    if [ -n "$MCP_TS_NAME" ]; then
      if sudo tailscale serve --bg --https=443 --set-path=/mcp \
           "http://127.0.0.1:8090/mcp" >/dev/null 2>&1; then
        echo "https://${MCP_TS_NAME}/mcp  -> vcctrl-mcp (streamable-http)"
      else
        echo "note: could not add the /mcp tailscale serve path -- vcctrl-mcp is still reachable at 127.0.0.1:8090 on the Pi itself"
      fi
    fi
  fi
}

if [ "${1:-}" = "--mcp-only" ]; then
  install_mcp
  exit 0
fi

# ---------------------------------------------------------------------------
# THE DEDICATED USER FOR BOTH READ-ONLY-MIRROR-ADJACENT SERVICES -- neither
# vcweb_public.py nor tailscaled-ro.service needs anything `pi` carries.
#
# THIS IS NOT COSMETIC. Checked against the live Pi, not assumed: `pi` has
# no sudo and no shell/SSH access of its own, so it looked safe at a glance
# -- but `pi` is also in the `input` group, which owns
# /dev/input/eventN for the virtual keyboard/mouse vcctrld already created
# via /dev/uinput (itself root-only, mode 600, and NOT in pi's reach). A
# process with write access to one of those event nodes can inject real key
# and motion events into it -- evdev's write() path validates against the
# DEVICE'S OWN registered capability bitmap, not a fixed "LED/sound only"
# allowlist -- which means a bug in either service, running as `pi`, would
# be one step from injecting input into the DOS target directly, bypassing
# vcctrld's lock, activity log and every `confirm=` gate entirely. Neither
# service has any legitimate reason to touch that group, or `dialout`,
# `audio`, `video`, `adm`, `plugdev`, `netdev`, `spi`, `i2c`, `gpio` --
# `vcweb_public.py` only ever makes loopback HTTP calls, `tailscaled-ro`
# only ever needs ordinary network access.
#
# `useradd --system` gives this user its own private group and no
# supplementary groups at all unless told to add some -- exactly what both
# services need, which is none.
ensure_vcctrl_ro_user() {
  if ! id vcctrl-ro >/dev/null 2>&1; then
    sudo useradd --system --no-create-home --shell /usr/sbin/nologin vcctrl-ro
    echo "vcctrl-ro: created dedicated system user (no shell, no supplementary groups)"
  fi
}

# ---------------------------------------------------------------------------
# THE READ-ONLY MIRROR'S OWN TAILNET IDENTITY -- a second, unprivileged
# `tailscaled` instance (pi/files/tailscaled-ro.service), not a container:
# see that unit file's own comment for why. Gives the mirror a hostname of
# its own instead of living on an alternate port under the private control
# KVM's own hostname, so its ACL/exposure surface is decoupled from the
# control node's.
#
# INSTALLS AND STARTS THE DAEMON, AND -- once it reports authenticated --
# POINTS ITS OWN HOSTNAME'S :443 AT THE MIRROR. NEVER RUNS `tailscale up`:
# registering this node needs an authkey from the Tailscale admin console
# and stays a one-time, manual operator step, matching the fact that this
# script never calls `tailscale up` for the PRIMARY instance either (see the
# top of this file: it assumes that one is already authenticated
# out-of-band). A freshly installed, unauthenticated instance is a
# supported, harmless state, not a fault -- the function reports it and
# returns rather than treating it as an error.
#
# Every fallible step is `|| true`-guarded, matching install_mcp()/
# install_public(): a Pi that has never run the manual bootstrap step for
# THIS instance, or is missing /usr/sbin/tailscaled outright, must not fail
# a full install, or even a `--public-only` mirror install.
RO_SOCK=/run/tailscale-ro/tailscaled.sock
install_tailscaled_ro() {
  if [ ! -f "$SRC/pi/files/tailscaled-ro.service" ]; then
    echo "tailscaled-ro: pi/files/tailscaled-ro.service not in this checkout, skipping" >&2
    return 0
  fi
  if [ ! -x /usr/sbin/tailscaled ]; then
    echo "tailscaled-ro: /usr/sbin/tailscaled not found -- install the tailscale package first, skipping" >&2
    return 0
  fi
  ensure_vcctrl_ro_user
  sudo install -m 0644 "$SRC/pi/files/tailscaled-ro.service" \
    /etc/systemd/system/tailscaled-ro.service
  sudo systemctl daemon-reload
  sudo systemctl enable tailscaled-ro
  sudo systemctl restart tailscaled-ro
  sleep 1
  sudo systemctl --no-pager --lines=10 status tailscaled-ro || true

  # A RESTART OF AN ALREADY-REGISTERED NODE STILL NEEDS A FEW SECONDS to
  # reconnect to the control plane before `status` reports it authenticated
  # again -- measured the hard way redeploying onto an already-registered
  # instance: a single `sleep 1` before the check below reported "NOT yet
  # authenticated" for a node that was, seconds later, fine. Poll briefly
  # rather than trust one sample taken too soon after a restart.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sudo tailscale --socket=$RO_SOCK status >/dev/null 2>&1 && break
    sleep 1
  done

  # PLAIN `sudo`, NOT `sudo -u pi`, for every query below -- found the hard
  # way bringing this up for real: this Pi's sudoers only grants passwordless
  # NOPASSWD to root ("(root) NOPASSWD: ALL"), not to arbitrary target users,
  # so `sudo -u pi tailscale ...` prompts for a password non-interactively,
  # fails, and lands in the same branch as "not authenticated" regardless of
  # the real state -- a silent false negative, not a permission boundary
  # doing its job. Root can read/write the RO socket directly (root bypasses
  # the socket file's own permissions), so there is no need to impersonate
  # pi at all here. This does NOT reduce the daemon's own privilege drop --
  # tailscaled-ro.service itself still runs as User=pi throughout, per
  # tailscaled-ro.service; only these CLI queries against its socket run as
  # root, same as every other `sudo tailscale ...` call in this file.
  if sudo tailscale --socket=$RO_SOCK status >/dev/null 2>&1; then
    echo "tailscaled-ro: running and already authenticated"
  else
    echo "tailscaled-ro: running, NOT yet authenticated -- run the manual"
    echo "  'tailscale up --socket=$RO_SOCK ...' bootstrap step once (see docs)"
    return 0
  fi

  # Point the RO node's own hostname at the mirror -- needs no secret, so
  # (unlike `tailscale up`) this part is scripted, the same as the :443
  # mapping the main HTTPS block below sets up for the primary instance.
  RO_NAME="$(sudo tailscale --socket=$RO_SOCK status --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
  if [ -n "${RO_NAME:-}" ]; then
    # ONLY RUN `serve` IF THE MAPPING ISN'T ALREADY THERE -- found the hard
    # way 2026-08-28: re-running `tailscale serve --bg --https=443 <target>`
    # against a mapping that was already Funnel-enabled SILENTLY CLEARED
    # AllowFunnel, with no error and no message -- exactly the class of
    # surprise the 7473eb5 lesson is named for, just from `serve` this time
    # instead of a bad `funnel` invocation. Every later `--public`/
    # `--public-only` redeploy re-entered this function and re-ran that
    # same `serve` call unconditionally, quietly un-funneling the mirror
    # the operator had just confirmed reachable off-tailnet. `serve` itself
    # is NEVER what turns Funnel on or off here (that stays the deliberate
    # manual step, see the comment above this function) -- so once the
    # mapping already matches, there is nothing for this script to assert
    # and no reason to touch `serve` again at all.
    ALREADY="$(sudo tailscale --socket=$RO_SOCK serve status --json 2>/dev/null \
      | python3 -c "import json,sys
d = json.load(sys.stdin)
w = d.get('Web', {}).get('${RO_NAME}:443', {})
p = w.get('Handlers', {}).get('/', {}).get('Proxy')
print('yes' if p == 'http://127.0.0.1:8091' else 'no')" 2>/dev/null || echo no)"
    if [ "$ALREADY" = "yes" ]; then
      echo "https://${RO_NAME}/  -> vcctrl-web-public (mapping already present, left untouched)"
    elif sudo tailscale --socket=$RO_SOCK serve --bg --https=443 "http://127.0.0.1:8091" >/dev/null 2>&1; then
      echo "https://${RO_NAME}/  -> vcctrl-web-public (tailnet only until funneled)"
    else
      echo "note: could not configure tailscale serve on the RO instance (:443)"
    fi
    # Read back rather than trust the exit code alone -- the 7473eb5 lesson:
    # confirms the mapping actually landed, and surfaces this node's real
    # CapMap so funnel-ports coverage for :443 can be checked before anyone
    # runs `funnel` against it by hand (a FRESH node/tag's CapMap is not
    # assumed to match the primary's -- see vcctrl-web-public.service's own
    # funnel-ports-ACL-is-per-node note).
    sudo tailscale --socket=$RO_SOCK serve status || true
    sudo tailscale --socket=$RO_SOCK status --json 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin).get("Self",{}).get("CapMap",{}))' || true
  fi
}

if [ "${1:-}" = "--tailscaled-ro-only" ]; then
  install_tailscaled_ro
  exit 0
fi

# ---------------------------------------------------------------------------
# THE PUBLIC READ-ONLY MIRROR -- daemon/vcweb_public.py, a separate process
# from vcctrld (see that module's own docstring for why: it is auditable, by
# grep alone, as incapable of ever sending a command to the target, and that
# property must survive vcctrld restarting, crashing, or being redeployed
# without this). A function, not inline, for the same reason install_mcp()
# is one: `--public-only` (below, and pi/deploy.sh --public) can install or
# restart the public mirror WITHOUT touching vcctrld, and a full install
# calls this same function so there is one implementation, not two.
#
# Every fallible step is `|| true`-guarded, matching install_mcp(): this Pi
# may not have `tailscale` configured yet, or the operator may simply not
# want the public mirror running, and neither should fail a full install of
# the thing this file exists for.
install_public() {
  if [ ! -f "$SRC/daemon/vcweb_public.py" ]; then
    echo "vcctrl-web-public: daemon/vcweb_public.py not in this checkout, skipping" >&2
    return 0
  fi

  # The mirror's OWN tailnet identity, before the mirror itself -- see
  # install_tailscaled_ro()'s own comment. Never fails a public-mirror
  # install: a Pi that has never run the manual `tailscale up` bootstrap
  # for this instance still gets the mirror installed and running, just
  # not yet reachable under its own hostname.
  install_tailscaled_ro

  sudo mkdir -p "$PREFIX"
  sudo install -m 0644 "$SRC/daemon/vcweb_public.py" "$PREFIX/vcweb_public.py"
  # kvm-ro.html and themes.css are read fresh from disk on every request
  # (same as kvm.html/vcweb.py), so shipping them here needs no restart to
  # take effect -- pi/deploy.sh --page already ships both for exactly that
  # reason. Installed here too so a FRESH Pi (never having run --page) still
  # has them the first time this function runs.
  #
  # kvm-ro.html IS NEVER INSTALLED RAW -- tools/strip_kvm_ro_comments.py runs
  # first, always. This used to be a one-time manual step during the
  # 2026-08-28 security audit (finding F4); every edit since then put this
  # repo's own maintainer comments (architecture detail, past incidents, at
  # least one commit hash) back into the file, served in the clear, because
  # nothing re-ran the strip. See that script's own docstring for the fuller
  # story. Refuses loudly (via `set -e`, no `|| true` here) rather than fall
  # back to installing the raw file -- an install that silently ships
  # unstripped comments because the stripper broke is a worse failure than
  # an install that stops.
  if [ -f "$SRC/tools/strip_kvm_ro_comments.py" ]; then
    python3 "$SRC/tools/strip_kvm_ro_comments.py" \
      "$SRC/daemon/kvm-ro.html" /tmp/kvm-ro.stripped.$$
    sudo install -m 0644 /tmp/kvm-ro.stripped.$$ "$PREFIX/kvm-ro.html"
    rm -f /tmp/kvm-ro.stripped.$$
  else
    echo "vcctrl-web-public: tools/strip_kvm_ro_comments.py not in this" >&2
    echo "  checkout -- refusing to install kvm-ro.html unstripped" >&2
    return 1
  fi
  sudo install -m 0644 "$SRC/daemon/themes.css"  "$PREFIX/themes.css"
  sudo install -m 0644 "$SRC/daemon/kvm-ro-share.jpg" "$PREFIX/kvm-ro-share.jpg"
  # The vendored browser-side Opus decoder kvm-ro.html loads for the
  # ?codec=opus audio stream. Flat, beside vcweb_public.py, which is the
  # first place its route looks. MISSING IS A WARNING, NOT A FAILURE: the
  # page detects the absent global and falls back to raw PCM audio, so an
  # older checkout still installs a working (just heavier) mirror.
  if [ -f "$SRC/vendor/ogg-opus-decoder-1.7.5.min.js" ]; then
    sudo install -m 0644 "$SRC/vendor/ogg-opus-decoder-1.7.5.min.js" \
      "$PREFIX/ogg-opus-decoder-1.7.5.min.js"
  else
    echo "vcctrl-web-public: vendor/ogg-opus-decoder-1.7.5.min.js not in" >&2
    echo "  this checkout -- public audio will fall back to raw PCM" >&2
  fi

  if [ ! -f "$SRC/pi/files/vcctrl-web-public.service" ]; then
    echo "vcctrl-web-public: pi/files/vcctrl-web-public.service not in this checkout, skipping the unit" >&2
    return 0
  fi
  ensure_vcctrl_ro_user
  sudo install -m 0644 "$SRC/pi/files/vcctrl-web-public.service" \
    /etc/systemd/system/vcctrl-web-public.service
  sudo systemctl daemon-reload
  sudo systemctl enable vcctrl-web-public
  sudo systemctl restart vcctrl-web-public
  sleep 1
  sudo systemctl --no-pager --lines=10 status vcctrl-web-public || true

  # NO LONGER MAPS :10000 ON THE PRIMARY INSTANCE HERE. Used to -- this
  # mirror lived at https://<control-hostname>:10000/, an alternate port on
  # the SAME Tailscale node as the private control UI -- until it got its
  # own tailnet identity (install_tailscaled_ro(), above; see
  # vcctrl-web-public.service's EXPOSURE comment for the full history).
  # Deliberately NOT idempotently re-asserted here any more: a scripted
  # block that re-creates :10000 on every `--public`/`--public-only` deploy
  # would silently undo that cutover the next time anyone redeploys.
  #
  # ROLLBACK, if the RO instance's own identity ever needs abandoning: the
  # exact commands that used to run here, unchanged, run by hand on the
  # PRIMARY socket (no --socket= flag):
  #   TS_NAME="$(tailscale status --json | python3 -c \
  #     'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
  #   sudo tailscale serve --bg --https=10000 "http://127.0.0.1:8091"
  # Verify the same way install_tailscaled_ro()'s own mapping is verified:
  # read back `tailscale serve status`, don't trust the exit code alone.
}

if [ "${1:-}" = "--public-only" ]; then
  install_public
  exit 0
fi

sudo mkdir -p "$PREFIX"
sudo install -m 0755 "$SRC/daemon/vcctrld.py"    "$PREFIX/vcctrld.py"
sudo install -m 0644 "$SRC/daemon/vcweb.py"      "$PREFIX/vcweb.py"
# vcctrld.py imports this at module load, same directory, same reasoning as
# vcweb.py -- forgetting it is not a missing feature, it is vcctrld refusing
# to start at all (ImportError before a single capability is registered).
sudo install -m 0644 "$SRC/daemon/vcsysinfo.py"  "$PREFIX/vcsysinfo.py"
sudo install -m 0644 "$SRC/daemon/kvm.html"      "$PREFIX/kvm.html"
sudo install -m 0644 "$SRC/daemon/themes.css"    "$PREFIX/themes.css"
# THE MEASURED KEY COVERAGE. Named explicitly like everything else here, and
# forgetting it does not fail loudly: the daemon reports `coverage: null`,
# which is exactly what a board with no measurements looks like. A deploy gap
# and a genuine absence of data are different facts and this file made them
# the same JSON for one deploy on 2026-08-25. vcctrld now says which.
sudo install -m 0644 "$SRC/daemon/keycoverage.json" "$PREFIX/keycoverage.json"
sudo install -m 0755 "$SRC/bin/vcctrl-client"    /usr/local/bin/vcctrl

sudo tee /etc/systemd/system/vcctrld.service >/dev/null <<'UNIT'
[Unit]
Description=vcctrl virtual PS/2 input server
# USB4VC must be up first: it only discovers input devices on its 0.75s scan,
# and we want it running when our uinput devices appear.
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -u /opt/vcctrl/vcctrld.py
# vcweb lives beside vcctrld; the daemon imports it by name.
Environment=PYTHONPATH=/opt/vcctrl
# THE FTP PASSWORD REACHES THE DAEMON THROUGH HERE AND NOWHERE ELSE.
# control.fileserver.password_env NAMES a variable; nothing puts one in a
# service's environment by magic, and the operator's shell profile is not the
# daemon's. Without this the file server refuses to start -- correctly, since
# a server that fell back to a built-in login would come up working while the
# configuration it claims to follow was never read.
#
# The leading `-` makes it OPTIONAL: a rig with no file transfer configured
# must not fail to boot its KVM over a secrets file it does not need.
#
# NOT created by install.sh, deliberately. It holds a secret, so it is written
# once by hand with mode 0600 and never by a script that runs from a checkout.
EnvironmentFile=-/etc/vcctrl/secrets.env
Restart=always
RestartSec=2
# Needs root for /dev/uinput and for reading the USB4VC debug log.
User=root

[Install]
WantedBy=multi-user.target
UNIT

# The virtual keyboard is a keyboard to THIS Pi as well as to the DOS box, so
# systemd would act on chords meant for the target. Ctrl-Alt-Del is the one
# that bites: without masking, `vcctrl combo ctrl alt delete` reboots the Pi.
# Found the hard way -- see docs/FINDINGS.md.
sudo systemctl mask ctrl-alt-del.target

# ---------------------------------------------------------------------------
# HOST CONFIGURATION -- everything below was applied by hand during the Pi 5
# migration and is folded in here so the machine is reproducible from a
# checkout rather than from one session's shell history. That gap is the same
# "a synced tree is not a synced deployment" problem pointing the other way:
# the tree was right and the machine held state nothing tracked.
#
# All of it is idempotent and safe to re-run.

FILES="$SRC/pi/files"

# journald must never write to a tty. The vcctrl virtual keyboard is a keyboard
# to THIS Pi as well as to the target, so a Ctrl-S meant for the DOS box is
# XOFF on the Pi's own console -- and a blocked console write stops journald
# draining its socket, which blocks sshd, PAM and sudo behind it. Measured:
# journald wedged in writev() on /dev/console, fd 41, every sample of a stall.
# See docs/FINDINGS.md sec. 28.
if [ -f "$FILES/journald.conf" ]; then
  sudo install -m 0644 "$FILES/journald.conf" /etc/systemd/journald.conf
  sudo systemctl restart systemd-journald || true
fi

# Belt to that braces: clear ixon on tty1 so a stray Ctrl-S cannot stop console
# output in the first place. Ordered AFTER getty -- an earlier version ran in
# early boot, reported "active", and changed nothing, which is worse than not
# running at all because the banner then advertised a protection that did not
# exist.
if [ -f "$FILES/console-noixon.service" ]; then
  sudo install -m 0644 "$FILES/console-noixon.service" /etc/systemd/system/console-noixon.service
  sudo systemctl enable console-noixon.service >/dev/null 2>&1 || true
fi

# CPU governor and the tailscale UDP offload. Not cosmetic: `ondemand` ramps
# AFTER load appears and this rig's work is short and bursty, so the harness
# paid ramp latency as jitter on a machine whose own timing sits inside the
# measurement. tailscaled does WireGuard in userspace, so rx-udp-gro-forwarding
# is most of the per-packet cost of the video stream.
if [ -f "$FILES/vcctrl-tuning.service" ]; then
  sudo install -m 0644 "$FILES/vcctrl-tuning.service" /etc/systemd/system/vcctrl-tuning.service
  sudo systemctl enable vcctrl-tuning.service >/dev/null 2>&1 || true
fi

# USB4VC's own app under systemd. Wraps upstream's keep_alive.py rather than
# replacing it with Restart=always: a migration should change the hardware or
# the supervision, not both.
if [ -f "$FILES/usb4vc.service" ] && [ -d /home/pi/usb4vc/rpi_app ]; then
  sudo install -m 0644 "$FILES/usb4vc.service" /etc/systemd/system/usb4vc.service
  sudo systemctl enable usb4vc.service >/dev/null 2>&1 || true
fi

# Login banner. In profile.d and NOT /etc/update-motd.d, because nothing on
# this Debian regenerates /run/motd.dynamic -- a status block rendered through
# pam_motd is a snapshot of whenever that file was last written, and it showed
# vcctrld DOWN while vcctrld was running. profile.d runs per login shell and
# therefore cannot cache.
if [ -f "$FILES/motd-status.sh" ]; then
  sudo install -m 0644 "$FILES/motd-status.sh" /etc/profile.d/vcctrl-status.sh
  printf '\n' | sudo tee /etc/motd >/dev/null
fi

# SPI carries the STM32 (spidev0.0) and the ssd1306 OLED (spidev0.1); I2C is
# there for the OLED's alternate wiring. Asserted rather than assumed -- a Pi
# imaged fresh has neither.
BOOTCFG=/boot/firmware/config.txt
if [ -f "$BOOTCFG" ]; then
  grep -q '^dtparam=spi=on' "$BOOTCFG" || \
    printf '\n# vcctrl/USB4VC: STM32 on spidev0.0, ssd1306 OLED on spidev0.1\ndtparam=spi=on\ndtparam=i2c_arm=on\n' \
    | sudo tee -a "$BOOTCFG" >/dev/null
fi
echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf >/dev/null

# USB4VC loops forever trying to disable bluetooth ERTM, because upstream calls
# subprocess.call() on a shell redirection string with no shell=True: it raises,
# gets swallowed by `except Exception: continue`, and retries every 2 s. On the
# Pi 3 that burned 4h30m of CPU in 34 hours. Satisfying the check at module
# level costs nothing and needs no patch to upstream.
echo "options bluetooth disable_ertm=1" | sudo tee /etc/modprobe.d/usb4vc-ertm.conf >/dev/null

# Capture devices pinned by name. Installed ONLY IF ABSENT, like config.json,
# because the values are specific to whichever capture stick is attached and a
# swap to the Macintosh's HDMI device needs different ones. Overwriting a local
# edit here would silently repoint capture at hardware that is not there.
if [ -f "$FILES/device-pin.conf" ] && \
   [ ! -f /etc/systemd/system/vcctrld.service.d/device-pin.conf ]; then
  sudo mkdir -p /etc/systemd/system/vcctrld.service.d
  sudo install -m 0644 "$FILES/device-pin.conf" \
    /etc/systemd/system/vcctrld.service.d/device-pin.conf
  echo "pinned capture devices by name (edit the drop-in if the stick changes)"
fi

# CORES, because an abort that says one line cannot be investigated.
#
# vcctrld died once with "double free or corruption (top)" and nothing else --
# glibc's heap, so a C extension, so not something a Python traceback can
# reach. faulthandler in the daemon names the Python line each thread was on;
# only a core names the free() that did it.
#
# Two halves, and BOTH are needed. The package replaces kernel.core_pattern
# with the systemd-coredump pipe, and the drop-in raises vcctrld's SOFT core
# limit, which Debian ships at 0 -- the kernel writes nothing at all until it
# is raised, however the handler is configured.
# PyYAML, for common/vcconfig.py. It is the one runtime dependency this
# project adds beyond the stdlib, and it is load-bearing rather than
# convenient: without it vcctrld cannot read vcctrl.yaml at all and falls back
# to built-in defaults, which on a rig whose plug or capture device is not at
# the default address means power and capture silently do not work. The loader
# says so in one actionable sentence rather than raising from inside a
# capability, but the sentence is easier to never see than this line is to run.
if ! python3 -c 'import yaml' >/dev/null 2>&1; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-yaml \
    && echo "installed python3-yaml (required to read vcctrl.yaml)"
fi

# NO apt LINE FOR pyftpdlib. It is vendored under vendor/ and travels with the
# checkout, so there is nothing to install and nothing that needs a network at
# deploy time. An apt line here was written and removed deliberately: a deploy
# step that reaches the network is a step that silently does not happen, and it
# would have made a Pi built offline get a working KVM and a broken feature.
#
# What DOES have to happen is that vendor/ reaches the deployed tree. If the
# file-transfer capability reports it cannot import pyftpdlib, that is this --
# an incomplete deploy, not a missing package.

if ! dpkg -s systemd-coredump >/dev/null 2>&1; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q systemd-coredump \
    && echo "installed systemd-coredump (cores land in /var/lib/systemd/coredump)"
fi
if [ -f "$FILES/coredump-storage.conf" ]; then
  sudo mkdir -p /etc/systemd/coredump.conf.d
  sudo install -m 0644 "$FILES/coredump-storage.conf" \
    /etc/systemd/coredump.conf.d/vcctrl.conf
fi
# Unlike device-pin.conf this one IS overwritten: a core limit is a policy that
# belongs to the project, not a value that belongs to whichever stick is
# plugged in, so there is no local edit here worth preserving.
if [ -f "$FILES/coredump.conf" ]; then
  sudo mkdir -p /etc/systemd/system/vcctrld.service.d
  sudo install -m 0644 "$FILES/coredump.conf" \
    /etc/systemd/system/vcctrld.service.d/coredump.conf
  sudo systemctl daemon-reload
fi

# Two LOCAL patches to USB4VC that must not silently disappear under an
# upstream update. --check only reports; it never modifies.
if [ -f "$SRC/tools/patch-usb4vc-board.py" ] && [ -f /home/pi/usb4vc/rpi_app/usb4vc_ui.py ]; then
  sudo python3 "$SRC/tools/patch-usb4vc-board.py" --check || \
    echo "NOTE: board-identity patch is not applied; vcctrl will report board unknown."
fi

# This one is load-bearing on any 64-bit userland: without it USB4VC discards
# every input event before it reaches the protocol board, so NOTHING is driven
# -- no keystrokes, no mouse, no activity LEDs -- while SPI, the OLED and board
# detection all keep working and make it look like a cable fault. Cost us most
# of an afternoon on 2026-08-20. See docs/FINDINGS.md sec. 29.
if [ -f "$SRC/tools/patch-usb4vc-64bit.py" ] && [ -f /home/pi/usb4vc/rpi_app/usb4vc_usb_scan.py ]; then
  sudo python3 "$SRC/tools/patch-usb4vc-64bit.py" --check || \
    echo "WARNING: 64-bit input_event patch is NOT applied. On this kernel that means no input reaches the target at all."
fi


# Config.
#
# The heredoc that used to live here wrote one rig's smart-plug address into
# every install, which is exactly the thing docs/CONFIG-PLAN.md exists to
# remove: an installer that hardcodes an address makes a fresh clone look
# configured while pointing at hardware on somebody else's LAN.
#
# Now: install the operator's vcctrl.yaml if deploy.sh shipped one, and
# install nothing at all if it did not. Running on built-in defaults is a
# supported state and it is honest about itself -- `vcctrl config show` says
# which file the daemon resolved, or says there was none.
#
# The library goes to $PREFIX so vcctrld imports it as a sibling.
# vendor/ IS A TREE, so it needs copying rather than installing file by file.
# It carries the FTP server the target pulls from -- absent, everything else
# works and only file transfer is dead, which is the slowest possible way to
# find a deploy problem.
#
# It landed in ~/vcctrl-src and stopped there for one release: deploy.sh
# shipped it and this script never placed it, and the guard covering that only
# checked the tar payload. Transport and installation are two steps and only
# one of them was tested.
if [ -d "$SRC/vendor" ]; then
  sudo rm -rf "$PREFIX/vendor.tmp"
  sudo cp -a "$SRC/vendor" "$PREFIX/vendor.tmp"
  sudo rm -rf "$PREFIX/vendor"
  sudo mv "$PREFIX/vendor.tmp" "$PREFIX/vendor"
  echo "installed vendor/ ($(find "$SRC/vendor" -type f | wc -l) files)"
fi

sudo install -m 0644 -T "$SRC/common/vcconfig.py" "$PREFIX/vcconfig.py"
# Same flat-deploy as vcconfig.py just above, same reason: AudioCapability's
# frequency-analysis math lives in common/audio_bands.py now, shared with
# the control host, so vcctrld.py needs its own copy beside it.
sudo install -m 0644 -T "$SRC/common/audio_bands.py" "$PREFIX/audio_bands.py"
# Same again: page-level Ogg parsing for the Opus audio side-stream.
sudo install -m 0644 -T "$SRC/common/ogg_pages.py" "$PREFIX/ogg_pages.py"
if [ -f "$SRC/vcctrl.yaml" ]; then
  sudo install -m 0644 -T "$SRC/vcctrl.yaml" "$PREFIX/vcctrl.yaml"
  echo "installed $PREFIX/vcctrl.yaml"
else
  echo "no vcctrl.yaml shipped -- the daemon will run on built-in defaults."
  echo "  cp vcctrl.example.yaml vcctrl.yaml and edit it to change that."
fi

# THE CAPTURE DEVICE DROP-IN IS A SECOND COPY, AND SECOND COPIES DRIFT.
#
# device-pin.conf sets VCCTRL_ALSA and VCCTRL_VIDEO in the unit's environment.
# Env outranks the config file, so once vcctrl.yaml names the same devices the
# drop-in is not merely redundant -- it is authoritative, and it is the copy
# nobody edits. Change the device in vcctrl.yaml, forget the drop-in, and the
# daemon captures from the old one while the file you edited says otherwise.
# That failure has no symptom: capture binds SOMETHING and looks healthy.
#
# So: where the config names both devices, the drop-in is REMOVED rather than
# regenerated. Regenerating would keep two files that must agree; removing
# leaves one that cannot disagree with itself.
#
# Conditional on the config actually naming them. A rig running on built-in
# defaults still needs the pin, because the built-in default is /dev/video0 --
# an index, and this machine has vc4hdmi outputs that can take it.
if [ -f "$PREFIX/vcctrl.yaml" ] \
   && python3 - "$PREFIX/vcctrl.yaml" <<'PY'
import sys
sys.path.insert(0, "/opt/vcctrl")
import vcconfig
try:
    c = vcconfig.load(sys.argv[1])
    v = c.optional("capabilities.video.settings.device")
    a = c.optional("capabilities.audio.settings.device")
    bad = (vcconfig.ABSENT, vcconfig.NONE)
    sys.exit(0 if (v not in bad and a not in bad) else 1)
except Exception:
    sys.exit(1)
PY
then
  if [ -f /etc/systemd/system/vcctrld.service.d/device-pin.conf ]; then
    sudo rm -f /etc/systemd/system/vcctrld.service.d/device-pin.conf
    sudo systemctl daemon-reload
    echo "removed device-pin.conf -- vcctrl.yaml now names both capture devices,"
    echo "  and one source that cannot disagree with itself beats two that must agree"
  fi
else
  echo "NOTE: vcctrl.yaml does not name both capture devices, so the"
  echo "  device-pin.conf drop-in is being kept. The built-in fallback is"
  echo "  /dev/video0, which is an INDEX and can land on an HDMI output."
fi

# The legacy config.json is NOT written any more and NOT removed either. It is
# still read as a fallback for one release so an existing rig does not lose
# power control at the moment of upgrading, which is the moment nobody is
# reading stderr. vcctrld prints a deprecation line when it finds one.

# HTTPS over the tailnet, via Tailscale's own cert.
#
# Three features need a SECURE CONTEXT and simply do not exist on a plain-http
# origin, in any browser: AudioWorklet, navigator.clipboard.write, and
# RTCPeerConnection. Each of those reads as "broken in Safari" when the real
# cause is the scheme, which is a debugging session nobody should have to have.
#
# `tailscale serve` terminates TLS with a real Let's Encrypt cert for the
# machine's MagicDNS name and proxies to the daemon. Tailnet-only: it is not
# reachable from the internet, and Tailscale remains the authentication
# boundary. The config lives in tailscaled's state, so it survives reboots.
#
# NOTE this publishes the machine's MagicDNS name to public Certificate
# Transparency logs -- that is inherent to any publicly-trusted cert, not
# something this script chooses. The operator approved it.
#
# Idempotent: re-running only re-asserts the same mapping.
if command -v tailscale >/dev/null 2>&1; then
  TS_NAME="$(tailscale status --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
  if [ -n "${TS_NAME:-}" ]; then
    if sudo tailscale serve --bg --https=443 "http://127.0.0.1:8080" >/dev/null 2>&1; then
      echo "https://${TS_NAME}/  -> proxying to the daemon"
      # And a RAW TCP forward for the daemon's own TLS listener. Raw, not
      # --tls-terminated-tcp: if Tailscale terminated TLS here it would
      # negotiate ALPN again and could hand back HTTP/2, which is the exact
      # thing this port exists to avoid. Passthrough means the browser does its
      # handshake with the daemon, which advertises http/1.1 only.
      sudo tailscale serve --bg --tcp=8443 tcp://127.0.0.1:8443 >/dev/null 2>&1 \
        && echo "https://${TS_NAME}:8443/  -> direct to the daemon (WebSocket works here)" \
        || echo "note: could not configure the 8443 TCP forward"
    else
      # Not fatal. Plain http on the tailnet still works; only the
      # secure-context features are unavailable.
      echo "note: could not configure tailscale serve (HTTPS certs enabled for this tailnet?)"
    fi
  fi
fi

# Certificate renewal: weekly, and on boot.
#
# `tailscale serve` does renew on its own, so this is belt and braces rather
# than the primary mechanism -- but a cert that silently fails to renew takes
# the KVM offline in exactly the situation where you most want to look at the
# machine, and `tailscale cert` is idempotent: it is a no-op until the cert is
# inside its renewal window.
#
# A systemd timer rather than a cron entry, for one load-bearing reason beyond
# tidiness: at boot, cron's @reboot fires before tailscaled has finished
# connecting, so the renewal would run against a down control plane and fail
# silently. A timer can say After=tailscaled.service and add a settling delay.
# It also puts the result in journald next to everything else.
sudo mkdir -p /var/lib/vcctrl

# The renewal runs from a script rather than inline in ExecStart. systemd does
# not parse nested quoting the way a shell does -- an inline `sh -c` with a
# quoted python -c inside it is a unit that fails at start with a message about
# quoting, discovered at the worst possible time. A script file has no quoting
# problem to get wrong.
sudo tee "$PREFIX/renew-cert.sh" >/dev/null <<'RENEW'
#!/bin/sh
# Refresh the tailnet TLS cert. Idempotent: tailscale cert is a no-op until the
# certificate is inside its renewal window, so running this weekly costs
# nothing and running it on boot costs nothing.
set -eu
name="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
[ -n "$name" ] || { echo "no tailscale DNS name; is tailscaled up?" >&2; exit 1; }
# Explicit output paths: tailscale cert writes into the working directory
# otherwise, and a timer that litters the filesystem weekly is its own problem.
exec tailscale cert \
  --cert-file /var/lib/vcctrl/tls.crt \
  --key-file  /var/lib/vcctrl/tls.key \
  "$name"
RENEW
sudo chmod 0755 "$PREFIX/renew-cert.sh"

sudo tee /etc/systemd/system/vcctrl-cert.service >/dev/null <<'UNIT'
[Unit]
Description=Renew the tailnet TLS certificate for the vcctrl KVM
After=tailscaled.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/vcctrl/renew-cert.sh
User=root
UNIT

sudo tee /etc/systemd/system/vcctrl-cert.timer >/dev/null <<'UNIT'
[Unit]
Description=Weekly and on-boot renewal of the vcctrl KVM certificate

[Timer]
# Three minutes after boot, so tailscaled has connected and DNS resolves.
OnBootSec=3min
OnUnitActiveSec=1w
# If the Pi was off when a run was due, catch up rather than skipping a week.
Persistent=true

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now vcctrl-cert.timer >/dev/null 2>&1 || true

sudo systemctl daemon-reload
sudo systemctl enable vcctrld
sudo systemctl restart vcctrld
sleep 3
sudo systemctl --no-pager --lines=15 status vcctrld || true

# Same function --mcp-only uses above, run here so a full install also
# picks up the MCP server without a second code path to keep in sync.
install_mcp

# Same reasoning, same function --public-only uses above: a full install
# also brings up the public read-only mirror, one code path.
install_public
