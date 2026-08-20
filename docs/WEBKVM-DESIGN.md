# KVM interface — design direction

**Branch:** `webkvm`. **Status:** plan. Written 2026-08-19.

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
- **They are unmistakably this rig.** No other tool has them, because no other
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

Steps 1–3 are safe and improve the page on their own. 4–7 change how it feels
and should land together so it does not look half-migrated.

---

## 7. What this deliberately does not do

- **No CRT effects of any kind.** Section 1.
- **No webfont.** Section 2.
- **No animation beyond the landscape reveal and lamp state changes.** Motion
  on an instrument implies something is happening; using it decoratively makes
  the page lie in a small way.
- **No layout rethink beyond what the picture-first answer forces.** The
  operator likes the current structure; the brief is polish, not replacement.
