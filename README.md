# vcctrl

Claude-driven control of the g2k DOS test machine: keyboard and mouse over
USB4VC's PS/2 bridge, screen over a USB VGA capture stick, files over the CF
card and later over mTCP, power over a smart plug.

**[PLAN.md](./PLAN.md)** is the design. **[docs/FINDINGS.md](./docs/FINDINGS.md)**
is what measurement changed. **[docs/MCP-SERVER.md](./docs/MCP-SERVER.md)**
hooks an agent (Claude Code, Codex, ...) up to vcctrl directly over MCP.
**[docs/SKILLS.md](./docs/SKILLS.md)** carries rig/repo domain knowledge to
any of those same agents as portable `SKILL.md` files.

## Connect an agent: skills vs. MCP

Two different things, on purpose. **Skills** are portable knowledge --
copying them into another repo costs nothing and grants nothing. **MCP**
is real ability to drive physical hardware -- treat registering it as a
hardware-access decision, not a documentation one, and never bake a rig's
real hostname into a tracked/committed file (see `CLAUDE.md`).

**Skills** (Claude Code, Codex, Cursor, ...), no vcctrl checkout needed:

```sh
npx skills add https://forgejo.example.com/ecliptik/vcctrl.git \
  --full-depth --all -a claude-code -y   # --agent codex for Codex
```

Installs all 7 skills into `.agents/skills/` (symlinked into
`.claude/skills/` for Claude Code). `--full-depth` is required -- there's no
`SKILL.md` at the repo root. See [docs/SKILLS.md](./docs/SKILLS.md) sec. 8
for the same-machine symlink alternative and sec. 5 for verifying a skill
actually loaded (Claude Code needs a session restart for a brand-new
directory; Codex picks it up live).

**MCP** (drives the real rig):

```sh
# daemon mode -- once deployed (docs/MCP-SERVER.md sec. 4), no local checkout
claude mcp add --transport http vcctrl-mcp-daemon https://<rig>.ts.net/mcp
codex mcp add vcctrl-mcp-daemon --url https://<rig>.ts.net/mcp

# control mode -- needs a local clone + venv
cd vcctrl
python3 -m venv agent/.venv && agent/.venv/bin/pip install -r agent/requirements.txt
claude mcp add vcctrl-mcp -- "$(pwd)/agent/.venv/bin/python3" "$(pwd)/agent/vcctrl_mcp.py"
```

Restart the session afterward -- `/mcp` doesn't pick up a freshly
registered server live. Full reasoning, the safety model (shared input
lock, named `confirm=` arguments, board-scoped power), and Codex config-file
syntax: [docs/MCP-SERVER.md](./docs/MCP-SERVER.md).

## Layout

```
bin/vcctrl          VM-side wrapper -- ssh's to the Pi, holds no logic
bin/vcctrl-client   the real CLI; installed on the Pi as /usr/local/bin/vcctrl
daemon/vcctrld.py   input server; owns the uinput devices, listens on a socket
agent/vcctrl_mcp.py MCP server -- the CLI's tools, exposed to an MCP client
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
