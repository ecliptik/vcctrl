# KVM interface — design direction

**Branch:** `webkvm` (merged). **Status:** shipped — the polish and
visual-identity pass described here landed, including the theme system
(`tools/themes.py`, `daemon/themes.css`). Written 2026-08-19.

Brief, from the operator: *I like the overall look but it could use some
polish.* Scope agreed: **polish plus a visual identity pass** — layout structure
stays, character changes. **Both phone and desktop matter equally.** **System
fonts only.** And the thing to answer at a glance is **what is on the screen
right now**.

---

## 1. What this page actually is

A bench instrument wrapped around the video output of a 1995 IBM-compatible PC
running MS-DOS 6.22. One operator, plus automated agents. Its job is to show
what is on that screen, truthfully, right now — and to let someone take over
when the automation is stuck.

### The constraint that decides the aesthetic

**The captured picture is evidence.** Nothing may tint it, smooth it, overlay
it, or stylise it. A missing right column on a Mach64 is a real defect someone
is trying to see; a scanline filter would manufacture one.

That rules out the entire retro-CRT vocabulary — phosphor glow, barrel
distortion, scanlines, amber wash — which is exactly where a "DOS-themed"
design goes by default. It is the obvious move and it would actively damage the
instrument.

So the thesis: **the screen is sacred; the design is everything around it.**
The chrome is a housing, and housings are quiet.

---

## 2. Tokens

### Colour — warm graphite chassis, VGA status

Current palette is cool blue-slate (`#14161a`, `#1c1f26`, `#2b303a`) with
modern muted status colours. That is the generic dark-dashboard palette; it
would suit a billing console equally well.

Two changes, both grounded in the subject:

**The chassis goes warm.** Bench equipment — the housings this thing is
conceptually related to — is warm graphite and putty, not blue. Warm neutrals
read as *equipment*; blue-grey reads as *dashboard*. It is a small shift and it
changes the whole register.

    --chassis   #17150F   page ground, warm near-black
    --housing   #201E19   panels and rails
    --rule      #322D25   hairlines and edges
    --ink       #E8E3D9   primary text, warm off-white
    --ink-dim   #9A9186   labels, units, secondary

**Status colours come from the VGA 16-colour palette** — the exact colours the
machine being watched can display:

    --vga-green #55FF55   locked, captured, on
    --vga-cyan  #55FFFF   informational, accents, links
    --vga-amber #FFFF55   frozen, degraded, held
    --vga-red   #FF5555   no signal, refused, fault

**These are used at small scale only** — indicator dots, hairlines, and mono
text at 10–11px. Never as filled bars or large areas. The operator's word for
the last attempt at a status colour was *garish*, and he was right: the fix is
not a duller colour, it is less area. A 6px lamp at full VGA green reads as an
instrument; a 40px bar of the same colour reads as a highlighter.

**Honest self-critique:** dark ground plus a bright accent is one of the
current generic looks, and this is adjacent to it. What keeps it from being the
default: the ground is warm rather than neutral-black, and there are four
semantic hues from a documented historical palette doing different jobs, rather
than one brand accent applied decoratively. If it starts to read as generic in
practice, the fix is to push the chassis further towards putty, not to add
another hue.

### Type — system faces, real hierarchy

Self-hosting was considered and declined: this is a tool you open *because
something is broken*, and it must not depend on the phone reaching anything but
the tailnet. So character comes from scale, weight and tracking rather than
from a typeface nobody else has.

**Monospace is promoted to the primary voice.** It is already carrying most of
the page, and it is right: this is an instrument, every value is data, and a
fixed grid echoes the 8×16 VGA text cell without any pastiche. `system-ui` is
demoted to buttons and prose.

The current scale is the real problem — **six sizes between 10px and 14px**, so
nothing has hierarchy and everything competes:

| role | now | proposed |
|---|---|---|
| micro label (units, LED names) | 10–11px | **10px mono, +0.10em, uppercase, `--ink-dim`** |
| log line, dense data | 11px | **11.5px mono / 1.5** |
| readout value | 12px | **13px mono 500** |
| state word (LOCKED / FROZEN) | 11px pill | **17px mono 600, +0.02em** |
| section heading | 11px caps | **10px, +0.12em, uppercase, hairline under** |
| button | 12px | **13px system-ui 500** |

One prominent size, one dense size, one micro size. Everything else is a
variant. Because the picture is the point, **nothing in the chrome is allowed
to be visually louder than the picture** — the largest text on the page is
17px.

---

## 3. Layout — the picture wins

The operator's answer was *what is on the screen right now*, and the current
layout does not honour that: the stage is capped at `48vh` on a phone and boxed
to 4:3 regardless of orientation, so a landscape phone — the natural way to
hold a 4:3 screen — wastes most of the display.

### Phone, portrait

```
┌────────────────────────────────┐
│ ● LOCKED   ⇪ ⇩ ⇓   ⌨   ⚙      │  32px rail, one line, never wraps
├────────────────────────────────┤
│                                │
│                                │
│        [  4:3 SCREEN  ]        │  as large as fits, nothing over it
│                                │
│                                │
├────────────────────────────────┤
│ ctrl alt esc tab ↵ ⌫ ← → ...   │  key rail, scrolls sideways
├────────────────────────────────┤
│ ▸ Sound   ▸ Power   ▸ Activity │  collapsed; the picture keeps the space
└────────────────────────────────┘
```

### Phone, landscape

Screen fills the viewport. Rails collapse to a single translucent edge strip
that appears on tap and fades — the one place motion is used, because the
alternative is permanent chrome over a picture that is the whole point.

### Desktop

```
┌──────────────────────────────────────────┬───────────────┐
│ ● LOCKED  input free  power on  ⇪⇩⇓  ⌨ ⚙ │ ACTIVITY      │
├──────────────────────────────────────────┤ 19:42 type RB │
│                                          │ 19:42 lock ↑  │
│           [ SCREEN, centred ]            │ 19:41 power   │
│                                          │ …             │
├──────────────────────────────────────────┤───────────────┤
│ key rail                                 │ sound · power │
└──────────────────────────────────────────┴───────────────┘
```

The right column stops being a long scroll of unequal sections. Activity gets
the height, because it is the only genuinely unbounded content; controls sit
below it at a fixed size.

---

## 4. Signature: the PS/2 lamps

The one element this page should be remembered by is **three indicator lamps —
Caps, Num, Scroll — in the status rail**, rendered as small physical-looking
lamps with a lit core and a dark bezel.

They earn it three times over:

- **They are the real return channel.** The daemon reads them from
  `/sys/class/leds`; they are the non-video proof that a keystroke reached the
  machine. They are currently buried in a JSON field nobody opens.
- **They are unmistakably this system.** No other tool has them, because no other
  tool is bridged to a PS/2 keyboard on a 1995 PC.
- **They are honest about their own limits.** The LED level is a *retained*
  state — a powered-off host publishes nothing — so the lamps are drawn dim and
  hollow when the reading is stale, which is the visual form of the rule that
  cost a wrong readiness report this morning.

Everything else stays quiet. That is the one accessory kept.

---

## 5. Quality floor — currently missing

Measured against the existing page:

- **`:focus` styles: zero.** Nothing is keyboard-navigable with visible focus.
  A 2px `--vga-cyan` ring on every interactive element.
- **`prefers-reduced-motion`: not respected.** The landscape reveal and the
  lamp transitions must honour it.
- **Touch targets: 36px**, against the 44px both iOS and Android ask for. Every
  button and the volume slider thumb go to 44px on coarse pointers.
- **12 hard-coded hex values** outside the token block. All tokenised.
- **One `aria-` attribute in the whole page.** Labels on every icon-only
  control, `aria-live` on the state word so a change is announced.
- **`prefers-contrast: more`** — thicker rules, brighter dim text.

---

## 6. Build order

| step | change | risk |
|---|---|---|
| 1 | Tokens: warm chassis, VGA status, stray hex removed | low, visible immediately |
| 2 | Type scale and hierarchy | low |
| 3 | Quality floor: focus, motion, targets, aria, contrast | low, no visual change |
| 4 | Status rail: one line, never wraps, with the PS/2 lamps | medium |
| 5 | Stage: fill available space, landscape handling | medium — most user-visible |
| 6 | Desktop column: activity gets the height | medium |
| 7 | Collapse controls on phone | medium |
| 8 | Theme engine: base16 token shape, 27 schemes, grouped picker, contrast test | low — additive, and the token work in step 1 is most of it |

Steps 1–3 are safe and improve the page on their own. 4–7 change how it feels
and should land together so it does not look half-migrated.

---

## 7. Theme engine

Requested: Tokyo Night (dark and light), Dracula, Nord, Solarized (dark and
light), green and amber monochrome — then Catppuccin, Gruvbox, Rosé Pine,
Everforest, Kanagawa, and the terminal-authentic phosphors.

### What this does to section 2, said plainly

**If the palette is user-swappable, the palette cannot carry the identity.**
The warm-graphite-and-VGA scheme stops being *the* look and becomes the default
theme — one of nine. That is not a loss, but it does move the work: identity
now has to live in **structure, type and the lamps**, which is where it is
more durable anyway. A page recognisable only by its colours stops being
recognisable the moment someone picks Nord.

So section 2's palette ships as **`vga` — the house default**, and sections 3,
4 and 5 do the identifying.

### Structure: base16-shaped tokens

Do not hand-author nine palettes. Adopt the **base16** token shape — eight
greyscale steps plus eight hues — and map UI roles onto it once:

    base00 chassis        base08 red      fault, refused
    base01 housing        base09 orange   degraded
    base02 rule           base0A yellow   frozen, held, warning
    base03 ink-dim        base0B green    locked, captured, on
    base04 ink-muted      base0C cyan     informational
    base05 ink            base0D blue     accent, links
    base06 ink-bright     base0E magenta  (unused, reserved)
    base07 ink-inverse    base0F brown    (unused, reserved)

Every theme named above already exists as a published base16 scheme, so each is
**sixteen hex values in a table**, not a stylesheet. The engine is one
`data-theme` attribute on `<html>` and one CSS block per scheme. It also means
Gruvbox, Catppuccin, Rosé Pine, Everforest, One Dark, Monokai and several
hundred others become a five-minute addition rather than a design exercise.

The two monochromes are not base16 schemes and are authored by hand: a single
hue ramp against near-black, phosphor-style. They are the honest ones for this
system — a P1 green or P3 amber CRT is what a machine of this vintage was actually
watched on.

### Three rules the themes must not break

**1. The stage well stays near-black, not constant.** Changed 2026-08-30,
operator's explicit direction: the area immediately around the picture used to
be a literal `#000` in every scheme, including the light ones, so it never
varied at all. On a dark theme that made the well and a genuinely black patch
of real content (the hardware camera's own feed is often near-black in low
light) indistinguishable from each other — no contrast between "empty
letterbox" and "the picture is actually black here." It is now
`color-mix(in srgb, var(--bg) 35%, #2a2a2a 65%)` (`--well` in kvm.html) — a
guaranteed-dark floor from the fixed `#2a2a2a`, with the active theme's own
`--bg` mixed in on top so the well reads as "this theme's near-black," not a
universal one.

This was originally simultaneous contrast: a cream surround makes the captured
screen look darker and lower-contrast than it is, and judging whether a DOS
screen is too dark is a thing people do on this system. That is now an
approximate guarantee rather than an exact one — the well stays close to black
on every theme, but is no longer numerically identical across all of them.
Deliberate trade-off, not a regression if a future reader finds the well isn't
literal `#000` any more.

**2. State is never encoded in colour alone.** Green Monochrome has one hue, so
`locked` and `fault` cannot differ by colour there. Every state already has a
word — the lamps and the state readout keep it — and the lamps differ in fill
as well as colour: lit, hollow, or crossed. This is required by the monochrome
themes and is exactly what colour-blind users need anyway, so the constraint
pays for itself twice.

**3. Contrast floor is checked, not assumed.** Some published schemes have
comment colours around 3:1 against their own background. Any token used for
body text must clear **4.5:1** and any used for large text or an indicator
**3:1**, in every shipped theme. That is a script over the token table, run in
the test suite — not a judgement made by eye in one theme and hoped for in the
rest.

### Behaviour

- Picker lives in the settings panel (§ the `⚙` panel that already exists), as
  a plain list — nine names, current one marked. No swatch grid; the themes are
  named things people already recognise.
- Persisted in `localStorage` alongside the existing options.
- Default is **`auto`**: `vga` when the system reports dark, `solarized-light`
  when it reports light, following `prefers-color-scheme`. An explicit choice
  wins and sticks.
- `<meta name="theme-color">` updated on change, so the iOS status bar matches
  rather than clashing.
- Theme switching must not disturb the stream. It is a CSS variable swap —
  nothing re-renders, no socket reconnects.

### Shipping list

Twenty-seven schemes, in four groups. All but the phosphor set are published
base16 schemes, so each is sixteen hex values — the count is a data table, not
twenty-seven design exercises.

**House**

| theme | ground |
|---|---|
| `vga` **(default)** | warm graphite — §2 |

**Editor schemes**

| theme | ground | | theme | ground |
|---|---|---|---|---|
| `tokyo-night` | dark | | `catppuccin-mocha` | dark |
| `tokyo-night-light` | light | | `catppuccin-macchiato` | dark |
| `dracula` | dark | | `catppuccin-frappe` | dark |
| `nord` | dark | | `catppuccin-latte` | light |
| `solarized-dark` | dark | | `gruvbox-dark` | dark |
| `solarized-light` | light | | `gruvbox-light` | light |
| `rose-pine` | dark | | `everforest-dark` | dark |
| `rose-pine-moon` | dark | | `everforest-light` | light |
| `rose-pine-dawn` | light | | `kanagawa-wave` | dark |
| `kanagawa-dragon` | dark | | `kanagawa-lotus` | light |

**Phosphor** — hand-authored, one hue against near-black

| theme | phosphor | where you would have seen it |
|---|---|---|
| `apple-ii-green` | P1 | Apple II, early terminals — the pure green |
| `ibm-5151` | P39 | IBM MDA monitor — yellower, longer persistence |
| `dec-amber` | P3 | VT220 and the amber-terminal era |
| `vt220-white` | P4 | DEC white/grey phosphor — legible, least period-kitsch |

This replaces the generic `green-mono` and `amber-mono` from the request with
named historical equivalents: `apple-ii-green` **is** the green monochrome and
`dec-amber` **is** the amber one, they just say which green and which amber.
Two more sit alongside them because they are genuinely different — P39 is
visibly yellower than P1, and a white-phosphor scheme is the one monochrome
that stays comfortable to read for an hour.

**Honest limit on the phosphor set:** a phosphor is defined by persistence and
bloom as much as by hue, and this design does not simulate either — §1 rules
out screen effects because the picture is evidence. So these differ by **hue
only**. They are a palette named after a monitor, not an emulation of one, and
the naming should not overpromise.

### The picker at this size

Nine names could be a flat list. Twenty-seven cannot: the settings panel gets
four labelled groups in the order above, house first, with the current theme
marked. Still names rather than swatches — these are things people already
recognise by name, and a grid of twenty-seven colour chips is a worse way to
find "Gruvbox Dark" than the words are.

**The contrast test earns its place here.** Checking four schemes by eye is
plausible; checking twenty-seven is not, and several of these are known to be
low-contrast in their published form — Nord's comment greys and Everforest's
soft variants especially. The script over the token table is what makes adding
the twenty-eighth safe.

---

## 8. What this deliberately does not do

- **No CRT effects of any kind.** Section 1.
- **No webfont.** Section 2.
- **No animation beyond the landscape reveal and lamp state changes.** Motion
  on an instrument implies something is happening; using it decoratively makes
  the page lie in a small way.
- **No layout rethink beyond what the picture-first answer forces.** The
  operator likes the current structure; the brief is polish, not replacement.
