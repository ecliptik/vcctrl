# vcctrl

Claude-driven control of the g2k DOS test machine: keyboard and mouse over
USB4VC's PS/2 bridge, screen over a USB VGA capture stick, files over the CF
card and later over mTCP, power over a smart plug.

**[PLAN.md](./PLAN.md)** is the design. **[docs/FINDINGS.md](./docs/FINDINGS.md)**
is what measurement changed.

## Layout

```
bin/vcctrl          VM-side wrapper -- ssh's to the Pi, holds no logic
bin/vcctrl-client   the real CLI; installed on the Pi as /usr/local/bin/vcctrl
daemon/vcctrld.py   input server; owns the uinput devices, listens on a socket
pi/install.sh       systemd unit + masks ctrl-alt-del.target
pi/deploy.sh        push from the VM and install
```

All logic lives on the Pi. The VM wrapper is deliberately thin so that timing
(key dwell, event pacing) never crosses the network, and so moving Claude Code
onto the Pi later changes nothing but which wrapper gets called.

## Install

    ./pi/deploy.sh          # from the VM; VCCTRL_HOST=usb4vc by default

## Use

    vcctrl status                    # devices, USB4VC hold state, LED state
    vcctrl type 'CD \DOSKUTSU'
    vcctrl key enter
    vcctrl hold left 800             # press, dwell 800 ms, release
    vcctrl combo ctrl alt delete     # warm-boots the DOS box
    vcctrl keymap                    # key names, aliases, chord order
    vcctrl mouse move 40 -12
    vcctrl mouse click left
    vcctrl leds                      # PS/2 LED return channel
    vcctrl ledwait 5                 # block until the DOS host changes it

## Status -- proven end to end on real hardware 2026-08-19

| capability | state |
|---|---|
| Keyboard + mouse over PS/2 | **working** -- typed at the g2k, drove the game |
| On-screen keyboard in the KVM | **working** -- full QWERTY, per-board layout; *no key measured at the target* (WEBKVM 5.2) |
| PS/2 LED return channel | **working** -- non-video proof a keystroke landed |
| VGA capture | **working** -- locks on mode 12h *and* the game's 320x240 |
| Audio capture | **working** -- -30.8 dB vs -65.6 dB silence floor |
| CF card delivery from the Pi | **working** -- r15 populate, 15 PASS |
| Boot-profile selection | **working** -- blind digit+Enter, verified via `SET` |
| Reboot | prompt only; **Ctrl-Alt-Del is swallowed in-game** (findings 7) |
| Networking | NIC installed + `C:\NET` provisioned; not yet driven |

Known gaps: no hardware reset yet (GPIO to the motherboard reset header is the
plan), the Mach64 capture case is untested, and captured audio can be detected
but not yet judged. See PLAN.md.
