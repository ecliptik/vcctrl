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
  for f in kvm.html themes.css; do
    [ -f "$SRC/daemon/$f" ] || continue
    $SCP "$SRC/daemon/$f" "$HOST:/tmp/$f.new" || explain_hang
    $SSH "$HOST" "sudo sh -c 'install -m 0644 -T /tmp/$f.new /opt/vcctrl/.$f.tmp \
      && mv -f /opt/vcctrl/.$f.tmp /opt/vcctrl/$f' && rm -f /tmp/$f.new" \
      || explain_hang
    echo "installed $f (no restart)"
  done
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
# harness/ and profiles/ ship too: the cell and sweep runners moved out of
# bin/ in phase 6, and a deploy that still sent only bin/ would leave a Pi
# with the client and no runners -- working for every verb anyone tests by
# hand, and missing exactly the ones a round needs.
tar -C "$SRC" -cf - daemon bin pi tools common harness profiles | ssh "$HOST" 'tar -C ~/vcctrl-src -xf -'
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
