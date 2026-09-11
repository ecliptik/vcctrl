#!/usr/bin/env bash
# Push vcctrl from the VM to the Pi and install it there.
set -euo pipefail
# SOURCING THIS MUST NEVER BE FATAL.
#
# It was, and it broke pi/core.sh at the exact moment core.sh existed for: the
# daemon aborted on 2026-08-24 and the first command run was a copy of this
# script from /tmp, where `dirname/..` resolves to `/` and the source failed
# under `set -e` before a single line of work ran. The backtrace had to be got
# by hand.
#
# A tool that is only exercised on the day it is needed has never been tested.
# So: find the library if it is there, and if it is not, define a stub that
# returns the caller's fallback. Every caller already passes one, because the
# absent-key contract required it.
_vc_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || true)/common/config.sh"
if [ -f "$_vc_lib" ]; then
  # shellcheck source=../common/config.sh
  . "$_vc_lib"
else
  vc_cfg() { [ "$#" -ge 2 ] && printf '%s' "$2"; return 0; }
fi
HOST="${VCCTRL_HOST:-$(vc_cfg control.daemon_host "")}"
if [ -z "$HOST" ]; then
  echo "deploy: no daemon host configured. Set control.daemon_host in" >&2
  echo "  vcctrl.yaml (see vcctrl.example.yaml) or export VCCTRL_HOST." >&2
  exit 3
fi
# The daemon web base, for the busy guard and the hang explainer. An
# empty value is a supported answer: those checks then skip rather than
# curl a hostname that belongs to a different rig.
WEB="${VCCTRL_WEB:-$(vc_cfg control.web "")}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------
# REFUSE TO DEPLOY WHILE THE TARGET IS BUSY.
#
# Installing restarts vcctrld, which drops both uinput devices and the unix
# socket. To this side that is four seconds of nobody looking. To a running
# measurement cell it is fatal: the cell is 150 s of the target's time plus a
# reboot either side, it is not resumable, and a restart does not degrade it --
# it destroys it. That happened at 18:54 on 2026-08-19 and cost a real run.
#
# The social protocol ("ask for a window") had already failed twice by then, so
# this is the mechanism that replaces it. The input lock the daemon already has
# is exactly the right signal: whoever is driving the machine holds it, and a
# deploy that would interrupt them is refused rather than merely discouraged.
#
# THE GUARD IS A NET, NOT PERMISSION. Three blind spots observed in one
# evening, each of which reported "safe" during a real run:
#
#   1. in-flight only        a wait loop lives BETWEEN commands, so a 150 s
#                            cell is mostly not in flight
#   2. recent activity       a DOS-side reboot makes no daemon calls at all,
#                            so a cell mid-reboot looks completely idle
#   3. neither                a BAT running on the target never touches the
#                            daemon in any way
#
# Only the input lock spans all three, because it is held for the duration of
# the thing being protected rather than sampled from its side effects. That is
# the primary signal; everything below is a net under it. An ALLOWED here means
# "no evidence of a run", which is not the same as "no run".
#
# Override with VCCTRL_FORCE=1 when you genuinely need to deploy anyway --
# deliberately awkward, and it prints who you are interrupting.
guard_busy() {
  local state owner inflight
  state="$(curl -fsS --max-time 5 \
      "$WEB/state.json" 2>/dev/null || true)"
  [ -n "$state" ] || return 0        # daemon down: nothing to interrupt
  owner="$(printf '%s' "$state" | python3 -c \
      'import json,sys; print((json.load(sys.stdin).get("lock") or {}).get("owner") or "")' 2>/dev/null || true)"
  # Count only in-flight work that is NOT a browser's own poll. Every open
  # viewer polls status every 1.5 s, so counting those refused deploys while
  # nothing was running -- the guard cannot protect a run it cannot tell apart
  # from a page being open.
  inflight="$(printf '%s' "$state" | python3 -c \
      'import json,sys
d = json.load(sys.stdin).get("inflight") or []
print(sum(1 for i in d if i.get("by") not in ("browser", None)))' 2>/dev/null || echo 0)"
  if [ -n "$owner" ]; then
    echo "REFUSING TO DEPLOY: the input lock is held by '$owner'." >&2
    echo "A restart would kill whatever they are running. Ask them, or set" >&2
    echo "VCCTRL_FORCE=1 if you know it is safe." >&2
    return 1
  fi
  # In-flight only catches a command running *right now*. A harness wait loop
  # polls `leds` several times a second between commands, so the instantaneous
  # check sees an idle daemon while a cell is very much running. Recent
  # activity from anyone who is not a browser is the better signal.
  local busy
  busy="$(curl -fsS --max-time 5 \
      "$WEB/events?since=0" 2>/dev/null \
      | python3 -c '
import json, sys, time
try:
    evs = json.load(sys.stdin).get("events", [])
except Exception:
    print(""); raise SystemExit
now = time.time()
# NAME WHO IT WAS, OR SAY THAT YOU CANNOT.
#
# This used to print the literal string "harness" whenever there was recent
# non-browser traffic it could not name -- and `by` is None for every call
# that did not pass --as, which is most of them, including the verification
# reads a deploying session makes a minute earlier. So the guard accused a
# specific party by name on evidence that only supported "an unidentified
# client".
#
# It cost a real exchange: the refusal named harness, that session was asked,
# and it had issued no command at all. The traffic was this side own config
# show / board / power state reads. A guard that fabricates an attribution
# sends people to ask the wrong person, and the next step after being told
# "not me" is to force -- which is the guard defeating itself.
#
# NOTE the quoting: this block lives inside a single-quoted shell string, so
# it must contain no apostrophes at all. One turns the refusal into a syntax
# error at exactly the moment somebody is trying to deploy.
recent = [e for e in evs if e.get("kind") == "cmd"
          and e.get("by") != "browser" and now - e.get("t", 0) < 45]
named = sorted({str(e.get("by")) for e in recent
                if e.get("by") not in (None, "", "None")})
if named:
    print(",".join(named))
elif recent:
    print("an unidentified client (%d call(s) with no --as)" % len(recent))
else:
    print("")
' 2>/dev/null || true)"
  if [ -n "$busy" ]; then
    echo "REFUSING TO DEPLOY: the daemon has served commands from $busy" >&2
    echo "in the last 45 seconds -- something may be driving the target." >&2
    echo "If that is unidentified traffic it may well be YOUR OWN reads from" >&2
    echo "a minute ago; \`vcctrl activity\` shows whether anything is actually" >&2
    echo "in flight or holding the lock. Ask whoever it is, or set" >&2
    echo "VCCTRL_FORCE=1 if you know it is safe." >&2
    return 1
  fi

  if [ "${inflight:-0}" -gt 0 ]; then
    echo "REFUSING TO DEPLOY: $inflight command(s) in flight on the daemon." >&2
    echo "Wait for them, or set VCCTRL_FORCE=1." >&2
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# PAGE ONLY. vcweb reads kvm.html and themes.css from disk on every request, so
# changing the browser side needs no restart at all -- and a deploy that does
# not restart cannot interrupt a run, which is the whole reason the guard above
# exists. Use this for anything that is purely front-end; it is safe to run
# while the target is mid-cell, and the operator picks it up on reload.
#
# Copy to a temp name and mv into place: the mv is atomic within the
# filesystem, so no request can ever be served half a file.
#
# BOUNDED, because ssh to this host can hang forever rather than fail.
# Diagnosed by the vcctrl session 2026-08-20: systemd-journald blocks in
# file_tty_write on /dev/console, which is tty1, which has ixon enabled -- so
# a Ctrl-S aimed at the DOS box is XOFF on the PI'S OWN console. The virtual
# keyboard is a keyboard to the Pi as well as to the target, which is the same
# reason install.sh masks ctrl-alt-del.target. Output suspends, the console
# write never returns, journald stops draining its socket, and every writer
# behind it blocks -- including sshd, which completes the TCP handshake and
# then never sends a banner. There is no client-side banner timeout, so
# `timeout` is the only thing that turns that into an error.
#
# THE DISCRIMINATOR IS HTTP: the daemon does not go through sshd, so if
# /state.json answers while ssh does not, the Pi is up and this is the console
# wedge rather than anything about the deploy.
SSH="timeout 45 ssh -o BatchMode=yes -o ConnectTimeout=10"
SCP="timeout 45 scp -q -o BatchMode=yes -o ConnectTimeout=10"

explain_hang() {
  echo "ssh to $HOST did not complete." >&2
  if curl -fsS --max-time 5 \
      "$WEB/state.json" \
      >/dev/null 2>&1; then
    echo "The daemon IS answering over HTTP, so the Pi is up and the network" >&2
    echo "is fine -- this is the console wedge: journald blocked on tty1 takes" >&2
    echo "sshd with it. A Ctrl-S sent to the target does this. Nothing was" >&2
    echo "installed; the running page is unchanged." >&2
  else
    echo "HTTP is not answering either, so the Pi or the tailnet is down." >&2
  fi
  exit 1
}

if [ "${1:-}" = "--page" ]; then
  # kvm-ro.html included for the same reason kvm.html is: vcweb_public.py's
  # _serve_file() reads it fresh from disk on every request, same as
  # vcweb.py does for kvm.html, so shipping it needs no restart of anything.
  #
  # kvm-ro.html NEVER SHIPS RAW -- stripped on THIS host (the control
  # machine, where the checkout and python3 both already are) before the
  # scp below, same requirement and same reasoning as install_public()'s
  # own copy of this step in pi/install.sh: this repo's own maintainer
  # comments must not reach the public mirror, and a step that only runs
  # when a human remembers to run it is the failure this fixes, not a
  # description of one. Refuses (bash -e) rather than fall back to shipping
  # the raw file.
  if [ -f "$SRC/daemon/kvm-ro.html" ]; then
    python3 "$SRC/tools/strip_kvm_ro_comments.py" \
      "$SRC/daemon/kvm-ro.html" /tmp/kvm-ro.stripped.$$ || explain_hang
    $SCP /tmp/kvm-ro.stripped.$$ "$HOST:/tmp/kvm-ro.html.new" || explain_hang
    rm -f /tmp/kvm-ro.stripped.$$
    $SSH "$HOST" "sudo sh -c 'install -m 0644 -T /tmp/kvm-ro.html.new /opt/vcctrl/.kvm-ro.html.tmp \
      && mv -f /opt/vcctrl/.kvm-ro.html.tmp /opt/vcctrl/kvm-ro.html' && rm -f /tmp/kvm-ro.html.new" \
      || explain_hang
    echo "installed kvm-ro.html, stripped (no restart)"
  fi
  for f in kvm.html themes.css kvm-ro-share.jpg; do
    [ -f "$SRC/daemon/$f" ] || continue
    $SCP "$SRC/daemon/$f" "$HOST:/tmp/$f.new" || explain_hang
    $SSH "$HOST" "sudo sh -c 'install -m 0644 -T /tmp/$f.new /opt/vcctrl/.$f.tmp \
      && mv -f /opt/vcctrl/.$f.tmp /opt/vcctrl/$f' && rm -f /tmp/$f.new" \
      || explain_hang
    echo "installed $f (no restart)"
  done
  # Same no-restart ship for the vendored Opus decoder kvm-ro.html loads --
  # vcweb_public.py reads it fresh from disk per request like the files
  # above, and its name carries its version so the browser cache stays
  # honest (see vendor/README.md).
  f=ogg-opus-decoder-1.7.5.min.js
  if [ -f "$SRC/vendor/$f" ]; then
    $SCP "$SRC/vendor/$f" "$HOST:/tmp/$f.new" || explain_hang
    $SSH "$HOST" "sudo sh -c 'install -m 0644 -T /tmp/$f.new /opt/vcctrl/.$f.tmp \
      && mv -f /opt/vcctrl/.$f.tmp /opt/vcctrl/$f' && rm -f /tmp/$f.new" \
      || explain_hang
    echo "installed $f (no restart)"
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# CLIENT ONLY. /usr/local/bin/vcctrl is a fresh process per invocation, so
# replacing the file needs no restart for exactly the reason --page does not:
# nothing long-lived is holding the old copy. The next call picks up the new
# one and any call already in flight finishes on the old one, which is fine --
# they do not share state.
#
# This exists so CLI work can ship while somebody is mid-run. A full deploy
# runs install.sh, which restarts vcctrld and drops both uinput devices; a
# sweep cannot survive that and is not resumable. Making the safe path
# available is what stops the unsafe path being used out of impatience.
if [ "${1:-}" = "--client" ]; then
  f="$SRC/bin/vcctrl-client"
  [ -f "$f" ] || { echo "no $f" >&2; exit 1; }
  # Syntax-check BEFORE installing. A client that cannot parse takes out every
  # verb at once, including the ones a running sweep depends on -- and it would
  # do so on the next call rather than at deploy time, so the deploy would look
  # like it worked.
  python3 -m py_compile "$f" || { echo "refusing: $f does not compile" >&2; exit 1; }
  $SCP "$f" "$HOST:/tmp/vcctrl.new" || explain_hang
  $SSH "$HOST" "python3 -m py_compile /tmp/vcctrl.new && sudo install -m 0755 -T \
    /tmp/vcctrl.new /usr/local/bin/vcctrl && rm -f /tmp/vcctrl.new" || explain_hang
  echo "installed vcctrl client (no restart)"
  exit 0
fi

# ---------------------------------------------------------------------------
# MCP SERVER ONLY. Never touches vcctrld at all -- vcctrl-mcp.service is a
# separate unit (pi/install.sh's install_mcp(), see its own comment for why),
# so this needs no guard_busy check either: nothing here can interrupt a
# running cell or sweep, the same reason --page and --client don't check.
#
# Ships agent/ and pi/install.sh into a throwaway /tmp layout that preserves
# their relative positions (agent/ beside pi/), so install.sh --mcp-only's
# own $SRC auto-detection resolves correctly and runs the SAME install_mcp()
# a full deploy uses -- one implementation, not a second copy of it inlined
# into an ssh command.
if [ "${1:-}" = "--mcp" ]; then
  bash -n "$SRC/pi/install.sh" || { echo "refusing: pi/install.sh does not parse" >&2; exit 1; }
  [ -f "$SRC/agent/vcctrl_mcp.py" ] || { echo "no $SRC/agent/vcctrl_mcp.py" >&2; exit 1; }
  python3 -m py_compile "$SRC/agent/vcctrl_mcp.py" || \
    { echo "refusing: agent/vcctrl_mcp.py does not compile" >&2; exit 1; }
  rdir="/tmp/vcctrl-mcp-deploy.$$"
  $SSH "$HOST" "rm -rf $rdir && mkdir -p $rdir/agent $rdir/pi" || explain_hang
  $SCP "$SRC/agent/vcctrl_mcp.py" "$SRC/agent/requirements.txt" \
    "$HOST:$rdir/agent/" || explain_hang
  $SCP "$SRC/pi/install.sh" "$HOST:$rdir/pi/" || explain_hang
  $SSH "$HOST" "bash $rdir/pi/install.sh --mcp-only; rc=\$?; rm -rf $rdir; exit \$rc" \
    || explain_hang
  echo "installed vcctrl-mcp (vcctrld untouched)"
  exit 0
fi


# ---------------------------------------------------------------------------
# PUBLIC MIRROR ONLY. Never touches vcctrld -- vcctrl-web-public.service is
# a separate unit (pi/install.sh's install_public(), see its own comment for
# why), so this needs no guard_busy check either, same reasoning as --mcp
# above. Ships daemon/ and pi/ into a throwaway /tmp layout so install.sh
# --public-only's own $SRC auto-detection resolves correctly and runs the
# SAME install_public() a full deploy uses.
if [ "${1:-}" = "--public" ]; then
  bash -n "$SRC/pi/install.sh" || { echo "refusing: pi/install.sh does not parse" >&2; exit 1; }
  [ -f "$SRC/daemon/vcweb_public.py" ] || { echo "no $SRC/daemon/vcweb_public.py" >&2; exit 1; }
  python3 -m py_compile "$SRC/daemon/vcweb_public.py" || \
    { echo "refusing: daemon/vcweb_public.py does not compile" >&2; exit 1; }
  rdir="/tmp/vcctrl-public-deploy.$$"
  $SSH "$HOST" "rm -rf $rdir && mkdir -p $rdir/daemon $rdir/pi/files $rdir/tools $rdir/vendor" || explain_hang
  $SCP "$SRC/daemon/vcweb_public.py" "$SRC/daemon/kvm-ro.html" "$SRC/daemon/themes.css" \
    "$SRC/daemon/kvm-ro-share.jpg" "$HOST:$rdir/daemon/" || explain_hang
  # The vendored Opus decoder install_public() flat-installs beside
  # vcweb_public.py. Optional on purpose (install_public warns and the page
  # falls back to PCM audio), so an older checkout can still --public.
  [ -f "$SRC/vendor/ogg-opus-decoder-1.7.5.min.js" ] && \
    { $SCP "$SRC/vendor/ogg-opus-decoder-1.7.5.min.js" "$HOST:$rdir/vendor/" || explain_hang; }
  # install_public() (running remotely below) strips kvm-ro.html's comments
  # before installing it -- needs its own copy of the stripper script over
  # here too, since this throwaway layout is not a full checkout.
  $SCP "$SRC/tools/strip_kvm_ro_comments.py" "$HOST:$rdir/tools/" || explain_hang
  $SCP "$SRC/pi/install.sh" "$HOST:$rdir/pi/" || explain_hang
  $SCP "$SRC/pi/files/vcctrl-web-public.service" "$SRC/pi/files/tailscaled-ro.service" \
    "$HOST:$rdir/pi/files/" || explain_hang
  $SSH "$HOST" "bash $rdir/pi/install.sh --public-only; rc=\$?; rm -rf $rdir; exit \$rc" \
    || explain_hang
  echo "installed vcctrl-web-public (vcctrld untouched)"
  exit 0
fi

# ---------------------------------------------------------------------------
# HID GADGET ONLY. Never touches vcctrld -- vcctrl-hid-gadget.service is a
# separate unit (pi/install.sh's install_hid_gadget(), see its own comment
# for why), same reasoning as --mcp/--public above: no guard_busy check,
# nothing here can interrupt a running cell or sweep. May still require a
# reboot the operator has to do by hand (dtoverlay only takes effect on the
# next boot) -- install_hid_gadget() prints that plainly rather than this
# script guessing at it.
if [ "${1:-}" = "--hid-gadget" ]; then
  bash -n "$SRC/pi/install.sh" || { echo "refusing: pi/install.sh does not parse" >&2; exit 1; }
  bash -n "$SRC/pi/files/vcctrl-hid-gadget-setup.sh" || \
    { echo "refusing: pi/files/vcctrl-hid-gadget-setup.sh does not parse" >&2; exit 1; }
  rdir="/tmp/vcctrl-hid-gadget-deploy.$$"
  $SSH "$HOST" "rm -rf $rdir && mkdir -p $rdir/pi/files" || explain_hang
  $SCP "$SRC/pi/install.sh" "$HOST:$rdir/pi/" || explain_hang
  $SCP "$SRC/pi/files/vcctrl-hid-gadget-setup.sh" "$SRC/pi/files/vcctrl-hid-gadget.service" \
    "$HOST:$rdir/pi/files/" || explain_hang
  $SSH "$HOST" "bash $rdir/pi/install.sh --hid-gadget-only; rc=\$?; rm -rf $rdir; exit \$rc" \
    || explain_hang
  echo "installed vcctrl-hid-gadget (vcctrld untouched)"
  exit 0
fi

# ---------------------------------------------------------------------------
# A NAMED PROFILE ONLY (--profile <name>, e.g. --profile modernpc). Installs/
# restarts that SECOND vcctrld instance (vcctrld-<name>.service) -- the
# PRIMARY instance's own vcctrld.service is never touched by this path, so
# this needs no guard_busy check either, same reasoning as
# --mcp/--public/--hid-gadget above. Replaces the earlier modernpc-specific
# --modernpc mode -- one code path for any profile name, matching
# pi/install.sh's own install_profile() generalization.
if [ "${1:-}" = "--profile" ]; then
  name="${2:-}"
  if [ -z "$name" ]; then
    echo "usage: pi/deploy.sh --profile <name>" >&2
    exit 1
  fi
  bash -n "$SRC/pi/install.sh" || { echo "refusing: pi/install.sh does not parse" >&2; exit 1; }
  python3 -m py_compile "$SRC/daemon/vcctrld.py" || \
    { echo "refusing: daemon/vcctrld.py does not compile" >&2; exit 1; }
  rdir="/tmp/vcctrl-profile-${name}-deploy.$$"
  $SSH "$HOST" "rm -rf $rdir && mkdir -p $rdir/daemon $rdir/pi" || explain_hang
  $SCP "$SRC/daemon/vcctrld.py" "$SRC/daemon/vcweb.py" "$SRC/daemon/vcsysinfo.py" \
    "$SRC/daemon/kvm.html" "$SRC/daemon/themes.css" "$SRC/daemon/keycoverage.json" \
    "$HOST:$rdir/daemon/" || explain_hang
  $SCP "$SRC/pi/install.sh" "$HOST:$rdir/pi/" || explain_hang
  # Ship whichever of the real or example config exists -- install_profile()
  # (running remotely below) makes the same "real one already deployed?"
  # check itself before ever touching what is on the Pi; shipping both when
  # both exist locally is harmless since only the missing one is ever used.
  [ -f "$SRC/vcctrl-${name}.yaml" ] && \
    { $SCP "$SRC/vcctrl-${name}.yaml" "$HOST:$rdir/" || explain_hang; }
  [ -f "$SRC/vcctrl-${name}.example.yaml" ] && \
    { $SCP "$SRC/vcctrl-${name}.example.yaml" "$HOST:$rdir/" || explain_hang; }
  $SSH "$HOST" "bash $rdir/pi/install.sh --profile-only $name; rc=\$?; rm -rf $rdir; exit \$rc" \
    || explain_hang
  echo "installed vcctrld-${name} (primary vcctrld untouched)"
  exit 0
fi

if [ "${VCCTRL_FORCE:-0}" != "1" ]; then
  guard_busy || exit 1
else
  echo "VCCTRL_FORCE=1: deploying without checking whether anyone is mid-run." >&2
fi

ssh "$HOST" 'rm -rf ~/vcctrl-src && mkdir -p ~/vcctrl-src'
# tools/ ships too: install.sh runs patch-usb4vc-board.py --check from it,
# and a check that cannot find its own script reports a missing patch that
# is actually applied -- a false alarm is still a wrong answer.
# common/ ships because vcctrld imports vcconfig from it, and the operator's
# vcctrl.yaml ships because the daemon host is where it is read. The YAML is
# sent ONLY IF IT EXISTS: a rig configured entirely by built-in defaults is a
# supported state, and shipping the example in its place would install a
# configuration nobody wrote, pointing at hardware nobody has.
# harness/ ships too: the cell and sweep runners moved out of bin/ in
# phase 6, and a deploy that still sent only bin/ would leave a Pi with
# the client and no runners -- working for every verb anyone tests by
# hand, and missing exactly the ones a round needs.
# profiles/ does NOT ship (removed 2026-09-11): the harness generalization
# moved every port's target-software profile OUT of this repo and into
# that port's own (dosags's profiles/dosags.yaml, dossage's profiles/
# dossage.yaml, doskutsu's profiles/doskutsu.yaml) -- this repo has no
# profiles/ directory left to ship at all. The control host's own
# harness.profile (in its local vcctrl.yaml, shipped separately below)
# names wherever the active one actually lives now; the Pi never
# resolves harness.profile itself (no harness tools run in daemon mode --
# see test_harness_workflow_tools_are_absent_in_daemon_mode), so it does
# not need the file either.
# vendor/ ships for the same reason harness/ does, and it is the same mistake
# one release later: the file server the target pulls from is a vendored
# library, and a deploy that omitted it would give a Pi where everything anyone
# tests by hand works and only file transfer is dead -- reported as a broken
# feature rather than as a missing directory.
# agent/ ships as of the Pi-hosted MCP server (2026-08-25): install.sh sets
# up its own venv and systemd service from what lands here, separate from and
# fault-isolated from vcctrld's own install steps -- see the comment there.
tar -C "$SRC" -cf - daemon bin pi tools common harness vendor agent | ssh "$HOST" 'tar -C ~/vcctrl-src -xf -'
if [ -f "$SRC/vcctrl.yaml" ]; then
  # Validate BEFORE shipping. An invalid file does not stop the daemon -- it
  # degrades to built-in defaults, which on this rig means no power control and
  # capture bound to whatever /dev/video0 happens to be. That degradation is
  # deliberate and it is also silent from the outside, so the refusal belongs
  # here, where somebody is watching, rather than in the journal at 3am.
  if ! python3 "$SRC/common/vcconfig.py" check "$SRC/vcctrl.yaml" >/dev/null; then
    echo "refusing: vcctrl.yaml does not validate (run: python3 common/vcconfig.py check vcctrl.yaml)" >&2
    exit 1
  fi
  tar -C "$SRC" -cf - vcctrl.yaml | ssh "$HOST" 'tar -C ~/vcctrl-src -xf -'
fi
ssh "$HOST" 'bash ~/vcctrl-src/pi/install.sh'
