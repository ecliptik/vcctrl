# vcctrl login banner -- fastfetch-style system facts plus live rig state.
#
# Lives in profile.d rather than /etc/update-motd.d because nothing on this
# Debian regenerates /run/motd.dynamic: a status block rendered through
# pam_motd is a snapshot of whenever that file was last written, and it showed
# vcctrld DOWN while vcctrld was running. A status display that can be stale is
# worse than none, because it gets read as current. profile.d runs per login
# shell and cannot cache.
#
# Must be fast and must never block -- a login that hangs is a failure this rig
# has already had once.
case $- in *i*) ;; *) return ;; esac

_k='\033[1;36m'; _y='\033[1;33m'; _g='\033[0;32m'; _r='\033[0;31m'; _d='\033[0;90m'; _0='\033[0m'
_f() { printf "  ${_k}%s:${_0} %b\n" "$1" "$2"; }
_dot() { [ "$1" = active ] && printf "${_g}●${_0}" || printf "${_r}○${_0}"; }

_os=$(. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME")
_cpu=$(lscpu 2>/dev/null | awk -F': +' '/^Model name/{print $2; exit}')
_mhz=$(awk '{printf "%.2f", $1/1000000}' /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null)
_gov=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)
read -r _l1 _l5 _l15 _ < /proc/loadavg

printf "\n"
_f "OS"       "$_os $(uname -m)"
_f "Host"     "$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)"
_f "Kernel"   "Linux $(uname -r)"
_f "Uptime"   "$(uptime -p 2>/dev/null | sed 's/^up //')"
_f "Packages" "$(dpkg-query -f '.\n' -W 2>/dev/null | wc -l) (dpkg)"
_f "CPU"      "${_cpu:-unknown} ($(nproc)) @ ${_mhz:-?} GHz  ${_d}gov:${_gov}  $(awk '{printf "%.0f", $1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null)°C${_0}"
_f "Memory"   "$(free -m 2>/dev/null | awk '/^Mem:/{printf "%s MiB / %s MiB (%d%%)", $3, $2, ($3/$2)*100}')"
_f "Disk (/)" "$(df -h / 2>/dev/null | awk 'NR==2{printf "%s / %s (%s) - %s", $3, $2, $5, $1}')"
_f "Local IP" "$(ip -4 -o addr show scope global 2>/dev/null | awk '{printf "%s ", $4}')"
_f "Loadavg"  "$_l1, $_l5, $_l15"

# ---- rig ------------------------------------------------------------------
# board.json is written by our local patch to rpi_app into /run (tmpfs), so it
# cannot outlive the boot that wrote it. Absent means unknown, never a guess:
# config.json looks authoritative and is not -- it records boards once
# CONFIGURED, and read "Mac" the whole time the old rig drove the Gateway.
_bn=$(sed -n 's/.*"name"[ :]*"\([^"]*\)".*/\1/p' /run/usb4vc/board.json 2>/dev/null)
_bi=$(sed -n 's/.*"id"[ :]*\([0-9]*\).*/\1/p' /run/usb4vc/board.json 2>/dev/null)
# Ask the daemon rather than re-deriving. The plug alias, the viewer counts and
# the lock owner all already exist in /state.json, and a second copy of any of
# them here is a table that drifts. Local loopback, hard 2s cap: this runs on
# every login and must never be the reason one hangs.
_st=$(curl -s --max-time 2 http://127.0.0.1:8080/state.json 2>/dev/null)
_pl=$(printf '%s' "$_st" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
p=d.get("power") or {}
G="\033[0;32m●\033[0m"; R="\033[0;31m●\033[0m"; Y="\033[1;33m●\033[0m"
on=p.get("on")
# Tri-state on purpose: null means the plug stopped answering, which is a
# different fact from the mains being off and must not share a colour.
dot = Y if on is None else (G if on else R)
if p.get("alias"):
    print("%s %s (%s @ %s)" % (dot, p["alias"], p.get("model") or "?", p.get("host") or "?"))
else:
    print("%s %s" % (Y, p.get("reason") or "unknown"))
' 2>/dev/null)
_se=$(printf '%s' "$_st" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
lk=(d.get("lock") or {}).get("owner")
print("%s viewers · %s listeners · input lock: %s" % (
    d.get("viewers",0), d.get("listeners",0), lk or "unheld"))
' 2>/dev/null)
_v=$(ls /dev/v4l/by-id/ 2>/dev/null | head -1)
_a=$(awk '/USB-Audio|MACROSILICON/{gsub(/[][]/,"",$2); print $2; exit}' /proc/asound/cards 2>/dev/null)
printf "\n"
_f "vcctrl"   "$(_dot "$(systemctl is-active usb4vc 2>/dev/null)") usb4vc   $(_dot "$(systemctl is-active vcctrld 2>/dev/null)") vcctrld"
_bt=$(printf '%s' "$_st" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
b=d.get("board") or {}
if b.get("id") is None: print("\033[1;33munknown\033[0m  \033[0;90m%s\033[0m" % (b.get("reason") or ""))
else: print("%s \033[0;90m(PBID %s)\033[0m" % (b.get("name") or "?", b["id"]))
' 2>/dev/null)
_f "Board" "${_bt:-${_bn:-unknown}}"
_f "Power"    "${_pl:-${_y}daemon not answering${_0}}"
_f "Sessions" "${_se:-${_y}daemon not answering${_0}}"
_f "Capture"  "video ${_v:-none} · audio ${_a:-none}"
_f "Buses"    "spi $(ls /dev/spidev* 2>/dev/null | sed 's|/dev/spidev||' | tr '\n' ' ')· i2c $(ls /dev/i2c-* 2>/dev/null | sed 's|/dev/i2c-||' | tr '\n' ' ')"
printf "\n"

unset _k _y _g _r _d _0 _f _dot _st _pl _se _bt _os _cpu _mhz _gov _l1 _l5 _l15 _bn _bi _v _a
