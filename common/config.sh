# Shell-side access to vcctrl.yaml. Source this; it defines vc_cfg.
#
#   vc_cfg PATH [FALLBACK]
#
# Prints the value and returns 0; prints the fallback and returns 0 if one is
# given and the key is absent; prints nothing and returns 1 if the key is
# absent with no fallback. The RETURN CODE carries absence, because a shell
# caller cannot tell an empty string from a missing key and "" is a legitimate
# value.
#
# CACHED, and the cache cannot serve a stale answer. Every call would otherwise
# be a ~34 ms python start, and bin/vcctrl is invoked once per keystroke batch
# in a harness whose whole design is organised around per-call cost.
#
# THE KEY IS THE FILE'S CONTENT, not its mtime. The first version used
# `stat -c %Y`, which is whole seconds -- so two edits inside the same second
# produce the same key and the cache serves the OLD value. That is not
# theoretical: it was caught by editing the config and restoring it in one
# test, where the restore kept returning the edited hostname. A cache keyed on
# a one-second clock is a cache that is stale exactly while someone is
# iterating, which is the only time anybody notices.
#
# Content-keyed cannot be stale by construction. The file is a couple of
# kilobytes, so hashing it costs one fork against the ~34 ms it saves.

_vc_cfg_py() {
  # Resolve the repo root from THIS file, not from the caller's cwd: these
  # scripts are run from anywhere and sourced by things that chdir.
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  printf '%s/common/vcconfig.py' "$here"
}

_vc_cfg_file() {
  if [ -n "${VCCTRL_CONFIG:-}" ]; then printf '%s' "$VCCTRL_CONFIG"; return; fi
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  for c in "$here/vcctrl.yaml" "$HOME/.config/vcctrl/vcctrl.yaml" \
           /opt/vcctrl/vcctrl.yaml; do
    [ -f "$c" ] && { printf '%s' "$c"; return; }
  done
  printf ''
}

vc_cfg() {
  local path="$1" fallback="${2-}" cfg stamp dir key out rc
  cfg="$(_vc_cfg_file)"
  # No file at all: the fallback IS the answer, and no python is needed.
  if [ -z "$cfg" ]; then
    [ $# -ge 2 ] && { printf '%s' "$fallback"; return 0; }
    return 1
  fi
  stamp="$(cksum < "$cfg" 2>/dev/null | tr -d ' ' || echo 0)"
  dir="${VCCTRL_SSH_CTL_DIR:-${TMPDIR:-/tmp}/vcctrl-ssh-$(id -u)}"
  mkdir -p "$dir" 2>/dev/null || true
  # The content hash is in the key, so a stale entry is unreachable rather
  # than merely unlikely -- the same reason the board record lives in /run.
  key="$dir/cfg-$(printf '%s|%s|%s' "$cfg" "$stamp" "$path" | cksum | tr -d ' ')"
  if [ -f "$key" ]; then
    out="$(cat "$key")"
    [ -n "$out" ] && { printf '%s' "$out"; return 0; }
  fi
  if out="$(python3 "$(_vc_cfg_py)" get "$path" 2>/dev/null)"; then
    rc=0
  else
    rc=1
  fi
  if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
    printf '%s' "$out" > "$key" 2>/dev/null || true
    printf '%s' "$out"
    return 0
  fi
  [ $# -ge 2 ] && { printf '%s' "$fallback"; return 0; }
  return 1
}
