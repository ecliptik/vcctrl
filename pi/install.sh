#!/usr/bin/env bash
# Install vcctrld on the USB4VC Pi. Run from the repo root on the Pi, or let
# pi/deploy.sh push it from the VM.
set -euo pipefail

PREFIX=/opt/vcctrl
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

sudo mkdir -p "$PREFIX"
sudo install -m 0755 "$SRC/daemon/vcctrld.py"    "$PREFIX/vcctrld.py"
sudo install -m 0644 "$SRC/daemon/vcweb.py"      "$PREFIX/vcweb.py"
sudo install -m 0644 "$SRC/daemon/kvm.html"      "$PREFIX/kvm.html"
sudo install -m 0644 "$SRC/daemon/themes.css"    "$PREFIX/themes.css"
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

# Config. Written only if absent, so a local edit survives re-installs.
# NOTE the plug's Kasa alias is "retro-rig-plug" -- it is not renamed, so anyone
# looking at the Kasa app will not obviously connect it to the g2k. That is
# recorded here rather than fixed because the operator wants the name kept.
if [ ! -f "$PREFIX/config.json" ]; then
  sudo tee "$PREFIX/config.json" >/dev/null <<'CONF'
{
  "kasa_host": "192.0.2.46",
  "_kasa_note": "TP-Link EP10, alias 'retro-rig-plug', MAC 00:00:5E:00:53:01. Legacy port-9999 protocol; if a firmware update moves it to KLAP on port 80 this stops working and needs python-kasa."
}
CONF
  echo "wrote default $PREFIX/config.json"
fi

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
