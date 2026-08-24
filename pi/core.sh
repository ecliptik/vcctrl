#!/usr/bin/env bash
# Read the newest vcctrld core, or list what there is.
#
# The point of this script is that at the moment you need it you will not want
# to remember any of what is in it. vcctrld aborted once with "double free or
# corruption (top)" and one line of journal, and the investigation was over
# before it started because nothing had been saved.
#
#   pi/core.sh            backtrace the newest vcctrld core, all threads
#   pi/core.sh list       what cores exist, for which units, how big
#   pi/core.sh PID        backtrace that one
#
# Two things this gets right that the obvious command line does not:
#
#   -iex, not -ex, for the debuginfod setting. gdb asks whether to download
#   symbols while it is LOADING the files, which is before any -ex runs, and a
#   batch session answers no. You get a backtrace full of ?? that reads like
#   missing symbols rather than a declined download. (/etc/gdb/gdbinit on the
#   Pi now sets this too; the flag here is belt and braces, and makes the
#   script work on a host that has not been configured.)
#
#   `thread apply all bt`, not `bt`. The abort is raised on whichever thread
#   corrupted the heap, but vcctrld runs a capture reader, a watchdog, an
#   input poller and a web thread per request -- and the interesting question
#   is usually what the OTHER threads were doing at the same instant.
set -euo pipefail
# No hardcoded hostname: see bin/vcctrl. VCCTRL_PI still wins, so a
# one-off against another machine needs no config edit.
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
PI="${VCCTRL_PI:-${VCCTRL_HOST:-$(vc_cfg control.daemon_host "")}}"
if [ -z "$PI" ]; then
  echo "core.sh: no daemon host configured (control.daemon_host, or VCCTRL_PI)" >&2
  exit 3
fi
case "${1:-bt}" in
  list) ssh "$PI" "sudo coredumpctl list --no-pager" ;;
  *)
    sel="${1:-}"
    [ "$sel" = "bt" ] && sel=""
    # shellcheck disable=SC2029  -- the remote expansion is the point
    ssh "$PI" "set -e
      sel='${sel}'
      if [ -z \"\$sel\" ]; then
        # By UNIT, not by executable. vcctrld runs as
        # '/usr/bin/python3 -u /opt/vcctrl/vcctrld.py', so its EXE is python3
        # and matching the script path finds nothing -- while matching python3
        # finds every crashed python on the box, including this evening's
        # deliberate test.
        sel=\$(sudo coredumpctl list --no-pager -r -u vcctrld.service 2>/dev/null \
              | awk 'NR==2 {print \$5}')
        if [ -z \"\$sel\" ]; then
          echo '(no vcctrld core; showing the newest core of any kind)' >&2
          sel=\$(sudo coredumpctl list --no-pager -r 2>/dev/null \
                | awk 'NR==2 {print \$5}')
        fi
      fi
      if [ -z \"\$sel\" ]; then echo 'no cores stored'; exit 1; fi
      echo \"== core \$sel ==\"
      sudo coredumpctl info --no-pager \"\$sel\" | sed -n '1,14p'
      # mktemp under SUDO, so the file is root's. Debian sets
      # fs.protected_regular=1, under which root may not write to a file it
      # does not own in a sticky world-writable directory -- so `sudo
      # coredumpctl dump -o` into a user-owned /tmp file fails with
      # "Permission denied", leaves a zero-byte file behind, and gdb then
      # reports "not a core dump: file format not recognized". Which reads as
      # a corrupt core rather than a failed copy.
      tmp=\$(sudo mktemp /tmp/vccore.XXXXXX)
      sudo coredumpctl dump -o \"\$tmp\" \"\$sel\" >/dev/null 2>&1
      sudo gdb -q -batch -iex 'set debuginfod enabled on' -iex 'set confirm off' \
           -ex 'thread apply all bt' /usr/bin/python3 \"\$tmp\" 2>&1 \
        | grep -vE '^\[New LWP|^\[Thread debugging|^Using host libthread'
      sudo rm -f \"\$tmp\"" ;;
esac
