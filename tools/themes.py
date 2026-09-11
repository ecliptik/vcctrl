#!/usr/bin/env python3
"""Theme table for the KVM page, and the generator that emits themes.css.

ONE source of truth. The CSS is generated from this table and the contrast test
reads the same table, so a scheme cannot pass review in one and fail in the
other.

Roles rather than raw base16 indices. Published base16 files assign accent
slots for SYNTAX HIGHLIGHTING -- tokyo-night, for instance, puts a pale blue in
base08, the slot a UI would use for "error". Mapping those positionally would
give a theme where faults are blue. So each scheme lists its own palette by
what the colour MEANS, taken from the scheme's documented names.

    bg      page ground            red      fault, refused
    panel   rails, panels          orange   degraded
    rule    hairlines, borders     yellow   frozen, held, warning
    dim     labels, units          green    locked, captured, on
    muted   secondary text         cyan     informational
    text    primary text           blue     accent, focus ring, links
    bright  emphasis               magenta  reserved

`dark` drives the light/dark toggle and the auto pairing: each scheme names its
opposite so the toggle has somewhere to go.
"""

# name: (group, label, dark?, pair, {roles})
#
# EVERY SCHEME IS HALF OF A PAIR. The picker shows one swatch per identity and
# the light/dark button moves between the halves, so a scheme with no opposite
# is a swatch that stops working the moment you press the button. That is why
# Nord, the phosphors and the house theme grew light counterparts here rather
# than borrowing somebody else's, which is what `pair="solarized-light"` on
# four unrelated schemes used to mean.
#
# Culled 2026-08-20: dracula, catppuccin macchiato/frappe, rose-pine (three),
# kanagawa (three) and apple-ii-green. Twenty-five schemes that mostly differed
# by a few degrees of hue read as one theme with a lot of settings; the ones
# left are the ones you can tell apart at swatch size.
THEMES = {
 "vga": ("House", "VGA", True, "vga-light", dict(
   bg="#17150F", panel="#201E19", rule="#322D25", dim="#9A9186",
   muted="#B5AC9E", text="#E8E3D9", bright="#FFFFFF",
   red="#FF5555", orange="#FFA855", yellow="#FFFF55", green="#55FF55",
   cyan="#55FFFF", blue="#5599FF", magenta="#FF55FF")),
 # The other half of the same 16 colours: VGA's low-intensity set on paper,
 # which is what the same palette looks like when the beam is off.
 "vga-light": ("House", "VGA Light", False, "vga", dict(
   bg="#EDE9DF", panel="#E3DDCF", rule="#CFC6B4", dim="#6E6558",
   muted="#544C41", text="#241F19", bright="#000000",
   red="#AA0000", orange="#A05000", yellow="#7A6000", green="#00701C",
   cyan="#006E75", blue="#0000AA", magenta="#AA00AA")),

# CREDIT. Each colour value below is drawn from a published, MIT-licensed
# base16/community colour scheme, re-keyed by MEANING rather than base16 slot
# (see the module docstring). No file is copied; the palettes themselves are
# not copyrightable, and this credit is a courtesy, not a licence obligation:
# Tokyo Night (enkia), Solarized (Ethan Schoonover), Gruvbox (morhetz), Nord
# (Arctic Ice Studio / Sven Greb), Catppuccin (the Catppuccin org), Dracula /
# Alucard (Zeno Rocha and contributors), Everforest (Sainnhe Park).
 "tokyo-night": ("Editor", "Tokyo Night", True, "tokyo-night-light", dict(
   bg="#1a1b26", panel="#16161e", rule="#292e42", dim="#565f89",
   muted="#a9b1d6", text="#c0caf5", bright="#ffffff",
   red="#f7768e", orange="#ff9e64", yellow="#e0af68", green="#9ece6a",
   cyan="#7dcfff", blue="#7aa2f7", magenta="#bb9af7")),
 "tokyo-night-light": ("Editor", "Tokyo Night Light", False, "tokyo-night", dict(
   bg="#e1e2e7", panel="#d5d6db", rule="#c4c8da", dim="#6a6f8e",
   muted="#4c5182", text="#343b58", bright="#1a1b26",
   red="#8c4351", orange="#965027", yellow="#8f5e15", green="#485e30",
   cyan="#0f4b6e", blue="#34548a", magenta="#5a3e8e")),

 "solarized-dark": ("Editor", "Solarized Dark", True, "solarized-light", dict(
   bg="#002b36", panel="#073642", rule="#0f4c5c", dim="#657b83",
   muted="#93a1a1", text="#eee8d5", bright="#fdf6e3",
   red="#dc322f", orange="#cb4b16", yellow="#b58900", green="#859900",
   cyan="#2aa198", blue="#268bd2", magenta="#d33682")),
 "solarized-light": ("Editor", "Solarized Light", False, "solarized-dark", dict(
   bg="#fdf6e3", panel="#eee8d5", rule="#d9d2c0", dim="#93a1a1",
   muted="#657b83", text="#073642", bright="#002b36",
   red="#dc322f", orange="#cb4b16", yellow="#b58900", green="#859900",
   cyan="#2aa198", blue="#268bd2", magenta="#d33682")),

 "gruvbox-dark": ("Editor", "Gruvbox Dark", True, "gruvbox-light", dict(
   bg="#282828", panel="#32302f", rule="#504945", dim="#928374",
   muted="#bdae93", text="#ebdbb2", bright="#fbf1c7",
   red="#fb4934", orange="#fe8019", yellow="#fabd2f", green="#b8bb26",
   cyan="#8ec07c", blue="#83a598", magenta="#d3869b")),
 "gruvbox-light": ("Editor", "Gruvbox Light", False, "gruvbox-dark", dict(
   bg="#fbf1c7", panel="#f2e5bc", rule="#d5c4a1", dim="#7c6f64",
   muted="#665c54", text="#3c3836", bright="#282828",
   red="#9d0006", orange="#af3a03", yellow="#b57614", green="#79740e",
   cyan="#427b58", blue="#076678", magenta="#8f3f71")),

 "nord": ("Editor", "Nord", True, "nord-light", dict(
   bg="#2e3440", panel="#3b4252", rule="#434c5e", dim="#7b88a1",
   muted="#d8dee9", text="#eceff4", bright="#ffffff",
   red="#bf616a", orange="#d08770", yellow="#ebcb8b", green="#a3be8c",
   cyan="#88c0d0", blue="#81a1c1", magenta="#b48ead")),
 # Nord ships no light theme, but it ships the palette for one: Snow Storm is
 # the ground, Polar Night is the ink, and Aurora goes down a few steps to
 # survive on white.
 "nord-light": ("Editor", "Nord Light", False, "nord", dict(
   bg="#eceff4", panel="#e5e9f0", rule="#d8dee9", dim="#5b6779",
   muted="#434c5e", text="#2e3440", bright="#242933",
   red="#9b2c36", orange="#a2542a", yellow="#7f6416", green="#4f6b3e",
   cyan="#2e6e80", blue="#3b5c8a", magenta="#7a5480")),

 "catppuccin-mocha": ("Editor", "Catppuccin Mocha", True, "catppuccin-latte", dict(
   bg="#1e1e2e", panel="#181825", rule="#313244", dim="#7f849c",
   muted="#bac2de", text="#cdd6f4", bright="#ffffff",
   red="#f38ba8", orange="#fab387", yellow="#f9e2af", green="#a6e3a1",
   cyan="#94e2d5", blue="#89b4fa", magenta="#cba6f7")),
 "catppuccin-latte": ("Editor", "Catppuccin Latte", False, "catppuccin-mocha", dict(
   bg="#eff1f5", panel="#e6e9ef", rule="#ccd0da", dim="#6c6f85",
   muted="#5c5f77", text="#4c4f69", bright="#1e1e2e",
   red="#d20f39", orange="#fe640b", yellow="#8c6a00", green="#40a02b",
   cyan="#179299", blue="#1e66f5", magenta="#8839ef")),

 "dracula": ("Editor", "Dracula", True, "alucard", dict(
   bg="#282a36", panel="#21222c", rule="#44475a", dim="#6272a4",
   muted="#bfbfd0", text="#f8f8f2", bright="#ffffff",
   red="#ff5555", orange="#ffb86c", yellow="#f1fa8c", green="#50fa7b",
   cyan="#8be9fd", blue="#bd93f9", magenta="#ff79c6")),
 # Alucard is Dracula's own light theme, published 2024 -- so the pair is the
 # project's, not one I invented to satisfy the rule.
 "alucard": ("Editor", "Alucard", False, "dracula", dict(
   bg="#fffbeb", panel="#f5f1dd", rule="#dcd8c3", dim="#6c664b",
   muted="#4a4636", text="#1f1f1f", bright="#000000",
   red="#cb3a2a", orange="#a34d14", yellow="#846e15", green="#14710a",
   cyan="#036a96", blue="#644ac9", magenta="#a3144d")),

 "everforest-dark": ("Editor", "Everforest Dark", True, "everforest-light", dict(
   bg="#2d353b", panel="#343f44", rule="#475258", dim="#859289",
   muted="#9da9a0", text="#d3c6aa", bright="#e8e0cc",
   red="#e67e80", orange="#e69875", yellow="#dbbc7f", green="#a7c080",
   cyan="#83c092", blue="#7fbbb3", magenta="#d699b6")),
 "everforest-light": ("Editor", "Everforest Light", False, "everforest-dark", dict(
   bg="#fdf6e3", panel="#f4f0d9", rule="#ddd8be", dim="#829181",
   muted="#5c6a72", text="#4f585e", bright="#2d353b",
   red="#f85552", orange="#f57d26", yellow="#8f6f00", green="#8da101",
   cyan="#35a77c", blue="#3a94c5", magenta="#df69ba")),

 # Phosphor: one hue against near-black, and the same hue as ink on paper.
 # Hand-authored -- these are not base16 schemes. They differ by HUE ONLY: a
 # phosphor is defined by persistence and bloom as much as colour, and this
 # design simulates neither, because the captured picture is evidence and must
 # not be filtered. The accent roles are all the one hue on purpose: in a
 # monochrome scheme the WORD carries the state, never the colour.
 "ibm-5151": ("Phosphor", "IBM 5151 Green", True, "ibm-5151-paper", dict(
   bg="#0b0f00", panel="#131a00", rule="#28380a", dim="#6f9124",
   muted="#93bf30", text="#b6ff3d", bright="#dcff9e",
   red="#b6ff3d", orange="#b6ff3d", yellow="#dcff9e", green="#b6ff3d",
   cyan="#dcff9e", blue="#93bf30", magenta="#b6ff3d")),
 "ibm-5151-paper": ("Phosphor", "Green Paper", False, "ibm-5151", dict(
   bg="#f2f4e8", panel="#e9edd9", rule="#d0d9b6", dim="#5a6b2c",
   muted="#44521c", text="#22300a", bright="#0b0f00",
   red="#22300a", orange="#22300a", yellow="#0b1400", green="#22300a",
   cyan="#0b1400", blue="#44521c", magenta="#22300a")),
 "dec-amber": ("Phosphor", "DEC Amber", True, "dec-amber-paper", dict(
   bg="#140c00", panel="#1e1200", rule="#3d2600", dim="#a2701a",
   muted="#d19426", text="#ffb000", bright="#ffd88a",
   red="#ffb000", orange="#ffb000", yellow="#ffd88a", green="#ffb000",
   cyan="#ffd88a", blue="#d19426", magenta="#ffb000")),
 "dec-amber-paper": ("Phosphor", "Amber Paper", False, "dec-amber", dict(
   bg="#f7f0e1", panel="#efe5cf", rule="#dccdaa", dim="#7a5814",
   muted="#5c420c", text="#3a2a00", bright="#1a1300",
   red="#3a2a00", orange="#3a2a00", yellow="#1a1300", green="#3a2a00",
   cyan="#1a1300", blue="#5c420c", magenta="#3a2a00")),
 "vt220-white": ("Phosphor", "VT220 White", True, "vt220-paper", dict(
   bg="#0d0d0d", panel="#151515", rule="#2b2b2b", dim="#8a8a85",
   muted="#b4b4ae", text="#e8e8e0", bright="#ffffff",
   red="#e8e8e0", orange="#e8e8e0", yellow="#ffffff", green="#e8e8e0",
   cyan="#ffffff", blue="#b4b4ae", magenta="#e8e8e0")),
 "vt220-paper": ("Phosphor", "Paper White", False, "vt220-white", dict(
   bg="#f4f4f0", panel="#eaeae4", rule="#d4d4cc", dim="#66665f",
   muted="#4a4a44", text="#1e1e1a", bright="#000000",
   red="#1e1e1a", orange="#1e1e1a", yellow="#000000", green="#1e1e1a",
   cyan="#000000", blue="#4a4a44", magenta="#1e1e1a")),
}

ROLES = ("bg", "panel", "rule", "edge", "dim", "muted", "text", "bright",
         "red", "orange", "yellow", "green", "cyan", "blue", "magenta")


# ------------------------------------------------------------------ contrast

def _lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hexcolor):
    h = hexcolor.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ------------------------------------------------------------------ fitting

# Text must clear 4.5:1 against any surface it sits on; indicator colours and
# large type must clear 3:1. WCAG's numbers, not invented ones.
#
# FLOOR BY USE, NOT BY ROLE NAME. `dim` and the accents were held to the 3:1
# indicator floor because their names suggest decoration. The page uses them
# as TEXT: `color:var(--dim)` appears seventeen times -- it is the most-used
# text colour in kvm.html, carrying the product name, the KBD/MOS/C/N/S lamp
# labels and every panel heading -- and the accents carry PWR/AUD, the Send
# button and DAEMON UNREACHABLE.
#
# Measured in a browser on solarized-light before changing anything, which is
# the only reason this was found: `test_theme_contrast` was GREEN throughout,
# because it asked whether each role cleared the floor for its NAME rather
# than the floor for its JOB.
#
#   3.28:1   dim     vcctrl-kvm, KBD, MOS, C N S, panel headings
#   3.15:1   green   PWR and AUD lit lamps
#   3.58:1   green   Send
#   3.77:1   red     DAEMON UNREACHABLE
#
# Modelled before committing, because raising a floor moves every value the
# generator emits and the failure mode is a palette that reads as one colour:
#
#   dim lands 4.94-5.39 across the light themes, muted sits 5.18-5.78, so the
#   text > muted > dim hierarchy survives -- narrowly, and that compression is
#   a real cost paid for legibility.
#
#   Accents stay TELLABLE APART. Measured with Lab dE, not with contrast
#   ratio: the first attempt compared accents by contrast and got ~1.0 for
#   every pair, which says only that they share a lightness. Two lamps of the
#   same lightness and different hue are not confusable, and a metric that
#   cannot see that is the wrong instrument. Worst pair moves 14.8 -> 13.8
#   (tokyo-night-light yellow/orange). Nothing collapses.
FLOOR = {"text": 4.5, "muted": 4.5, "dim": 4.5}
ACCENT_FLOOR = 4.5
# The boundary of an INTERACTIVE control, as opposed to a divider between two
# bits of text. WCAG 1.4.11 asks 3:1 of a UI component boundary and nothing of
# a decorative line, and `rule` was doing both jobs at 1.1-1.7:1.
#
# Reported as "buttons don't have borders like they do in Dark themes", and
# the interesting part is that the token was equally weak in BOTH -- median
# 1.25 light, 1.30 dark. What differs is the button's own ground, which is a
# BLACK wash on both polarities: on a dark theme it barely moves the fill, so
# a mid-tone border still shows against it; on a light theme it drags the fill
# down toward the border colour and cancels it. Measured on a header button:
#
#     border against its own fill    light 1.03-1.10    dark 1.31-1.46
#
# So the light themes were not missing a rule the dark ones had. Both were
# drawing a border nearly nobody could see, and only one of them had a ground
# that happened to hide it less.
EDGE_FLOOR = 3.0
ACCENTS = ("red", "orange", "yellow", "green", "cyan", "blue", "magenta")

# HOW FAR APART TWO STATE COLOURS MUST BE, in CIE76 delta-E.
#
# Reported from the rig: on a light theme the lamps were "difficult to tell if
# they are green". Measuring said the contrast floor was not the problem --
# every theme clears 4.5:1 -- and that the real defect is SEPARATION. In
# solarized-light, gruvbox-light and everforest-light the authored green and
# yellow are both olive, landing 22-23 apart, and the lamp row distinguishes
# `on` from `warn` by colour alone. Two states rendered in nearly the same
# colour at 10px is one state.
#
# 25 is the usual "clearly different colours" threshold for CIE76. Nudging is
# by LIGHTNESS, using the same _mix toward the same extreme the contrast floors
# use, so the palette's hue is untouched and the correction can only increase
# contrast rather than trade it away. A reader who cannot separate the hues at
# all now has a lightness difference instead, which is the better answer for
# them anyway.
STATE_SEPARATION = 25.0

# The state roles carry more weight than an accent in a wall of code, so they
# get a higher contrast floor than the rest.
#
# 4.5:1 is the WCAG AA floor for NORMAL text, and it assumes something near
# 16px. The lamp labels are 10px bold mono. Fitted to 4.5 the light-theme
# greens landed at 4.50-4.96 -- clearing the standard and still reported from
# the rig as hard to read. Worse, the separation nudge above had pushed several
# yellows to 7.3-7.7 as a side effect, so `warn` read BETTER than `on` and made
# green look weaker by comparison.
#
# 7.0 is AA for small text and AAA for normal.
#
# LIGHT THEMES ONLY, and that is a measurement rather than a preference.
# Applying it to all 22 cost separation on the dark ones: nord lost green from
# both yellow and red, and everforest-dark lost yellow from red, going from
# comfortably separated to below the floor. On a light background the state
# colours are dark and have room to go darker; on a dark background they are
# light and converge on white quickly, so buying contrast there is paid for in
# the very distinguishability this file spent the previous change enforcing.
#
# The dark themes were not the complaint and are not near the edge -- their
# greens sit at 4.6-5.4 with separation intact -- so they keep the ordinary
# accent floor.
STATE_FLOOR = 7.0
STATE_ROLES = ("green", "yellow", "red")

# ...and the TEXT roles the status row is actually made of.
#
# Raising only the state colours fixed the handful of lamps that happen to be
# `on` and left the rest of the row exactly as it was -- which is what the
# operator saw when they said it looked the same as before any of this started.
# `.lamp span` is --dim for every lamp NOT on, `#state` (ACTIVE) is --dim, and
# every .chip is --dim. That is most of the row, and it sat at 4.57-5.19:1
# throughout.
#
# Same floor, same reason, same scope: 4.5 is AA for 16px text and this row is
# 10px. Light themes only, for the same measured reason as STATE_FLOOR.
#
# ONLY `muted`, and that is the second thing measurement changed. Applying the
# floor to dim, muted AND text collapsed the three emphasis levels into one:
# everforest-light came out 7.06 / 7.22 / 7.27, and eight other light themes
# were within 0.4 of flat. text > muted > dim is a contract this file already
# enforces by name, and buying contrast by destroying it is the same trade the
# state floor refused to make on the dark themes.
#
# So `muted` rises to 7.0 and the status row moves onto it in kvm.html. `dim`
# stays where it is and keeps being the genuinely faint level -- there is
# still something for it to mean.
ROW_ROLES = ("muted",)

# A ceiling on how far the text/muted gap enforcement will push. Without it a
# palette with very soft text chases its own tail toward black and stops being
# the palette anyone chose.
T_CONTRAST_CEIL = 12.0

# The pairs the status lamps actually rely on. Not every accent pair: `blue`
# and `cyan` sitting close costs nothing, because nothing reads a machine's
# state from them.
STATE_PAIRS = (("green", "yellow"), ("green", "red"), ("yellow", "red"))


def _lab(hexcolor):
    r, g, b = (_lin(int(hexcolor.lstrip("#")[i:i + 2], 16))
               for i in (0, 2, 4))
    X = r * 0.4124 + g * 0.3576 + b * 0.1805
    Y = r * 0.2126 + g * 0.7152 + b * 0.0722
    Z = r * 0.0193 + g * 0.1192 + b * 0.9505
    Xn, Yn, Zn = 0.95047, 1.0, 1.08883

    def f(t):
        return t ** (1.0 / 3) if t > 0.008856 else 7.787 * t + 16.0 / 116
    fx, fy, fz = f(X / Xn), f(Y / Yn), f(Z / Zn)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _lab_to_hex(L, a, b):
    """Lab -> sRGB hex, clamped into gamut."""
    def finv(t):
        return t ** 3 if t ** 3 > 0.008856 else (t - 16.0 / 116) / 7.787
    fy = (L + 16) / 116.0
    X = 0.95047 * finv(fy + a / 500.0)
    Y = 1.00000 * finv(fy)
    Z = 1.08883 * finv(fy - b / 200.0)
    r = X * 3.2406 + Y * -1.5372 + Z * -0.4986
    g = X * -0.9689 + Y * 1.8758 + Z * 0.0415
    bl = X * 0.0557 + Y * -0.2040 + Z * 1.0570

    def enc(c):
        c = max(0.0, min(1.0, c))
        c = 1.055 * (c ** (1 / 2.4)) - 0.055 if c > 0.0031308 else 12.92 * c
        return max(0, min(255, int(round(c * 255))))
    return "#%02x%02x%02x" % (enc(r), enc(g), enc(bl))


def fit_keep_chroma(colour, surfaces, floor, dark):
    """Reach `floor` by moving LIGHTNESS, keeping the hue and its intensity.

    WHY NOT _mix. Mixing toward black or white walks a straight line through
    sRGB toward a grey point, so it drains chroma along with lightness: fitting
    tokyo-night-light's green to 7:1 that way produced #364624 -- 7.03:1 and
    chroma 22.8, against 29.3 for the colour it started from. More readable and
    less green, which is the opposite of what was asked for. The operator's
    words were "difficult to tell if they are green", and that is a question
    about HUE that a contrast number cannot answer.

    Holding a and b while L falls raises chroma relative to lightness, so the
    colour gets darker and MORE saturated. Out-of-gamut results are clamped by
    the conversion, and the loop re-checks contrast afterwards, so a clamp
    cannot silently return something that misses the floor.
    """
    L, a, b = _lab(colour)
    best = colour
    for _ in range(100):
        if all(contrast(best, s) >= floor for s in surfaces):
            return best, best != colour
        L = L + 1.5 if dark else L - 1.5
        if L <= 0 or L >= 100:
            break
        cand = _lab_to_hex(L, a, b)
        if cand == best:
            break
        best = cand
    return best, best != colour


def fit_max_chroma(colour, surfaces, floor, dark, hue=None):
    """The MOST saturated colour of this hue that still clears `floor`.

    fit_keep_chroma preserved the chroma the palette authored, which was the
    wrong target: tokyo-night-light authors its green as an olive at chroma
    29.3, so faithfully preserving that produced a faithful olive. The report
    was that the lamps still did not read as green. At the SAME 7:1 floor
    against the same row, chroma 49.9 is available -- the constraint was never
    the contrast, it was that fitting only ever moved lightness.

    `hue` overrides the palette's own hue angle. Used for the `green` state
    role, and deliberately: several of these palettes author green at 84-111
    degrees, which is a yellow-green, and a state lamp that means "this is
    working" has to READ as green before it has to match a scheme. Yellow and
    red keep their authored hues -- they are already unmistakable and moving
    them would cost theme character for nothing.
    """
    import math
    L0, a0, b0 = _lab(colour)
    h = math.radians(hue) if hue is not None else math.atan2(b0, a0)
    best = colour
    best_c = math.hypot(a0, b0) if all(
        contrast(colour, s) >= floor for s in surfaces) else -1.0
    lo, hi = (5, 95)
    for Li in range(lo, hi):
        L = float(Li)
        for Ci in range(6, 100, 2):
            C = float(Ci)
            cand = _lab_to_hex(L, C * math.cos(h), C * math.sin(h))
            # The conversion clamps out-of-gamut requests, so verify the
            # colour we got is the colour we asked for before trusting it.
            _L, ca, cb = _lab(cand)
            if abs(math.hypot(ca, cb) - C) > 3.0:
                continue
            if all(contrast(cand, s) >= floor for s in surfaces) and C > best_c:
                best, best_c = cand, C
    return best, best != colour


def delta_e(a, b):
    """CIE76. Crude next to CIEDE2000 and sufficient here: the question is
    "are these two obviously different colours", not "how different"."""
    return sum((x - y) ** 2 for x, y in zip(_lab(a), _lab(b))) ** 0.5


def _mix(hexcolor, target, t):
    a = hexcolor.lstrip("#")
    b = target.lstrip("#")
    out = []
    for i in (0, 2, 4):
        ca, cb = int(a[i:i + 2], 16), int(b[i:i + 2], 16)
        out.append(int(round(ca + (cb - ca) * t)))
    return "#%02x%02x%02x" % tuple(out)


def fit(colour, surfaces, floor, toward):
    """Nudge `colour` toward `toward` until it clears `floor` on every surface.

    The published schemes are kept verbatim in THEMES -- that table is a record
    of what Dracula and Everforest actually are, and editing it would make the
    names lies. But several of them place accents at 2.1:1 against their own
    background, which is fine for a syntax token you glance at inside a wall of
    code and not fine for the only thing telling you a machine is on fire.

    So the table records the scheme and the generator guarantees the floor.
    Corrections are reported, never silent.
    """
    best = colour
    for step in range(0, 21):
        cand = _mix(colour, toward, step / 20.0)
        if all(contrast(cand, s) >= floor for s in surfaces):
            return cand, step > 0
        best = cand
    return best, True


def fitted(name):
    """A theme's roles with the contrast floor enforced. Returns (roles, notes)."""
    _g, _label, dark, _p, roles = THEMES[name]
    roles = dict(roles)
    surfaces = [roles["bg"], roles["panel"]]
    # Push text darker on light themes, lighter on dark ones.
    toward = "#000000" if not dark else "#ffffff"
    notes = []
    for role, floor in FLOOR.items():
        new, changed = fit(roles[role], surfaces, floor, toward)
        if changed:
            notes.append("%s %s->%s" % (role, roles[role], new))
            roles[role] = new
    # Derived, not authored: every theme ships a `rule` and none ships an
    # `edge`, and a hand-picked edge per theme is 22 more values to keep in
    # step with a palette that already defines the relationship.
    roles["edge"], _e = fit(roles["rule"], surfaces, EDGE_FLOOR, toward)

    for role in ACCENTS:
        new, changed = fit(roles[role], surfaces, ACCENT_FLOOR, toward)
        if changed:
            notes.append("%s %s->%s" % (role, roles[role], new))
            roles[role] = new

    # The state roles are re-fitted to their own, higher floor before the
    # separation pass, so separation is enforced on the final colours rather
    # than on ones a later step would move.
    authored = THEMES[name][4]
    if not dark:
        for role in ROW_ROLES:
            new_c, changed = fit(roles[role], surfaces, STATE_FLOOR, toward)
            if changed and new_c != roles[role]:
                notes.append("%s %s->%s (light floor)"
                             % (role, roles[role], new_c))
                roles[role] = new_c
        # Raising `muted` closes the gap ABOVE it as well as the one below.
        # catppuccin-latte and everforest-light author unusually soft text, and
        # after the floor their muted and text landed 0.09 and 0.18 apart --
        # so the fix for one level quietly flattened the next one up. Keep a
        # proportional gap rather than the bare `>=` the ordering pass
        # enforces: `>=` is satisfied by three hundredths, which is an
        # inversion nobody can see and a hierarchy nobody can read.
        want = min(T_ for T_ in (
            T_CONTRAST_CEIL,
            max(contrast(roles["muted"], s) for s in surfaces) * 1.15))
        for _ in range(40):
            if min(contrast(roles["text"], s) for s in surfaces) >= want:
                break
            before = roles["text"]
            roles["text"] = _mix(roles["text"], toward, 0.05)
            if roles["text"] == before:
                break
        # A single-phosphor theme has no hue to spare. ibm-5151 is green,
        # dec-amber is amber and the vt220 pair are white, and in all three
        # the authored green and red are the SAME COLOUR on purpose. Pinning
        # green to a green hue there paints an amber terminal green and stops
        # it being the thing it is. Detected from the authored values, the
        # same way the separation pass detects it, so a new monochrome theme
        # needs no metadata.
        monochrome = delta_e(authored["green"], authored["red"]) < 1.0
        # A single-phosphor theme is not fitted AT ALL here. Not merely
        # un-pinned: maximising chroma at their own hue still moved green away
        # from red, and green == red is the property that makes them what they
        # are. It would have made a white VT220 draw one lamp in olive.
        #
        # They need nothing anyway -- their state colours already sit at
        # 11-14:1, far above the floor this loop exists to reach. Skipping is
        # not a concession, it is the observation that there is nothing to fix.
        for role in ([] if monochrome else STATE_ROLES):
            # 136 degrees in Lab is an unambiguous green. Only `green` is
            # pinned; see fit_max_chroma.
            pin = 136 if role == "green" else None
            new_c, changed = fit_max_chroma(
                roles[role], surfaces, STATE_FLOOR, dark, hue=pin)
            if changed and new_c != roles[role]:
                notes.append("%s %s->%s (state floor)"
                             % (role, roles[role], new_c))
                roles[role] = new_c

    # STATE COLOURS MUST BE TELLABLE APART, not merely legible.
    #
    # Enforced after the accent floors so a nudge here cannot undo one there:
    # every correction mixes toward the same extreme the floors use, which
    # only ever increases contrast against both surfaces.
    #
    # SKIPPED WHERE THE PALETTE IS MONOCHROME BY DESIGN. ibm-5151, dec-amber
    # and the vt220 themes are single-phosphor terminals: green and red are the
    # SAME COLOUR in the authored table, deliberately, and separating them
    # would destroy the thing the theme is for. Detected by measuring the
    # authored values rather than by a flag, so a new monochrome theme needs no
    # metadata to be handled correctly -- and so the exemption cannot be
    # claimed by a colour theme that merely drifted.
    #
    # The page carries a non-colour state marker for exactly those themes:
    # solid underline for on, dashed for not-proven, double for held
    # elsewhere. That is what makes the exemption safe rather than a hole.
    for a, b in STATE_PAIRS:
        if delta_e(authored[a], authored[b]) < 1.0:
            continue                     # monochrome by design; leave it
        start = roles[b]
        for _ in range(40):
            if delta_e(roles[a], roles[b]) >= STATE_SEPARATION:
                break
            # Move the SECOND of the pair. green is the state a reader sees
            # most and the one they calibrate on, so it stays put and the
            # exceptional states move.
            before = roles[b]
            roles[b] = _mix(roles[b], toward, 0.05)
            if roles[b] == before:
                break                    # already at the extreme
        if delta_e(roles[a], roles[b]) < STATE_SEPARATION:
            # UNREACHABLE: revert rather than keep a nudge that bought
            # nothing. A palette this tight -- vt220's yellow and red differ
            # by a hair on a white phosphor -- cannot be separated by
            # lightness, and leaving it half-moved changes the theme while
            # still failing the thing the change was for. The note says so, so
            # the reliance on the non-colour marker is recorded rather than
            # implied by a number that looks like a result.
            roles[b] = start
            note = "%s/%s UNSEPARABLE dE%.0f -- relies on the state marker" % (
                a, b, delta_e(roles[a], roles[b]))
            if note not in notes:
                notes.append(note)
            continue
        if roles[b] != authored[b]:
            note = "%s/%s dE%.0f" % (a, b, delta_e(roles[a], roles[b]))
            if note not in notes:
                notes.append(note)

    # THE ORDERING IS PART OF THE CONTRACT, not a by-product of the floors.
    #
    # text > muted > dim is what the page means by those names: three levels
    # of emphasis. Fitting moves each role independently toward the same
    # extreme, so a role that started far from its floor barely moves while
    # one that started below it jumps -- and raising dim's floor to 4.5
    # inverted everforest-dark, where dim landed at 5.41 against muted at
    # 5.38. Three hundredths is invisible and that is exactly the problem: it
    # is an inversion nobody would see, in a rule the names promise.
    #
    # So enforce it. muted is pushed until it is at least as strong as dim,
    # and text until it is at least as strong as muted -- toward the same
    # extreme the floors use, so the nudge never reduces contrast.
    for weaker, stronger in (("dim", "muted"), ("muted", "text")):
        for _ in range(40):
            wc = min(contrast(roles[weaker], s) for s in surfaces)
            sc = min(contrast(roles[stronger], s) for s in surfaces)
            if sc >= wc:
                break
            before = roles[stronger]
            roles[stronger] = _mix(roles[stronger], toward, 0.06)
            if roles[stronger] == before:
                break                      # already at the extreme
        else:
            continue
        if roles[stronger] != THEMES[name][4][stronger]:
            note = "%s>=%s" % (stronger, weaker)
            if note not in notes:
                notes.append(note)
    return roles, notes


# ------------------------------------------------------------------ emit

def css():
    out = ["/* GENERATED by tools/themes.py -- do not edit by hand. */",
           "/* Roles, not base16 indices: see the module docstring. */",
           "/* Values marked (fitted) were nudged to clear the contrast floor. */",
           ""]
    # The page needs the LIST of themes, not just their values, and reading it
    # out of document.styleSheets means touching cssRules -- which throws on an
    # opaque origin and would empty the picker with no error anyone sees. A
    # custom property is just a computed style: it survives file://, it
    # survives a cross-origin stylesheet, and it cannot half-work.
    out.append(":root { --themes: \"%s\"; }" % " ".join(THEMES))
    out.append("")
    for name, (group, label, dark, pair, _raw) in THEMES.items():
        roles, notes = fitted(name)
        # The house theme is the bare :root default AND an addressable name,
        # so a swatch can wear it like any other. Without the second selector
        # the one theme with no [data-theme] block is the one whose swatch
        # comes out blank.
        sel = (':root, [data-theme="vga"]' if name == "vga"
               else '[data-theme="%s"]' % name)
        out.append("%s {" % sel)
        for r in ROLES:
            out.append("  --%s: %s;" % (r, roles[r]))
        # The page builds its picker from these: name, group, which half of
        # the pair this is, and where the other half lives. Kept here rather
        # than in a second list inside the HTML, because two lists of themes
        # drift and the failure is a swatch with no palette behind it.
        # THE STATUS ROW'S OWN SURFACE.
        #
        # The row is dark-text-on-light on a light theme, and that caps how
        # green a green can be: at 7:1 against a light panel the most
        # saturated green available is around chroma 50, which reads as dark
        # olive. Against a DARK surface the same 7:1 allows chroma 94. The
        # operator asked for a green that looks green, and the background is
        # the constraint, not the colour.
        #
        # So a light theme borrows its row from ITS OWN DARK HALF rather than
        # from a generic grey: a light Tokyo Night page gets a Tokyo Night
        # bar. The pairing already exists -- it is what the light/dark button
        # toggles -- so this introduces no new table to drift.
        #
        # A dark theme's row is simply its own panel, so nothing changes there
        # and there is no second code path to keep in step.
        src = name if dark else pair
        try:
            rrow, _n = fitted(src)
        except KeyError:
            rrow = roles
        # The row's GREEN is re-fitted against the row's own surface, for the
        # reason the row exists: a dark half authors green as a pastel
        # (tokyo-night is #9ece6a, chroma 55), and the whole point of moving
        # the bar to a dark surface was the chroma that becomes reachable
        # there. Maximising it at 7:1 against the bar gives roughly double.
        #
        # Only green. Yellow and red are already unmistakable in every dark
        # palette and re-fitting them would flatten theme character for
        # nothing -- the same rule as on the light themes.
        # THE BAR IS THE DARK HALF'S `rule`, NOT ITS `panel`.
        #
        # panel is near-black on most dark themes -- tokyo-night is #16161e at
        # Lab lightness 7.6 -- and a black strip across a light page was
        # reported as looking "really bad". `rule` is the palette's own
        # elevated surface, L19-32 depending on the theme, which is a slate or
        # charcoal rather than a hole in the page. It is a token the theme
        # already ships, so this stays inside the palette instead of inventing
        # a grey.
        #
        # It costs nothing measurable. Lifting tokyo-night's bar from L7.6 to
        # L19.3 leaves the best green at 7:1 unchanged at chroma 98 and text at
        # 8.3:1. Past `rule` it does start to cost: at L27 text falls to 6.35
        # and by L35 no green clears 7:1 at all, so this is the top of the
        # usable range rather than a midpoint.
        bar = rrow["rule"]
        # CAP THE BAR'S LIGHTNESS. `rule` is L19 on tokyo-night but L34 on
        # everforest-dark, and a bar that light cannot carry a 7:1 green at
        # all -- everforest came out at 4.01:1 with the fit silently returning
        # the colour it started from, because there was nothing better to
        # find. Darkening back toward panel until the bar can hold its own
        # contrast is the difference between a palette-shaped choice and a
        # palette-shaped failure.
        for _ in range(40):
            if _lab(bar)[0] <= 26.0:
                break
            bar = _mix(bar, rrow["panel"], 0.12)
        row_surfaces = [bar]
        # A single-phosphor theme keeps its own green, which IS its red. The
        # main token pass already exempts these; the row is a second place the
        # same rule has to hold, and it did not -- ibm-5151, dec-amber and the
        # vt220 pair all came out with a bright green bar lamp, which is the
        # one thing those themes must never have.
        # MUTED, NOT MAXIMISED. fit_max_chroma proved the bar COULD carry a
        # vivid green -- chroma 98 at 7:1, where a light panel caps out near
        # 50 -- and that was the point worth proving. Shipping it made every
        # theme's lamp the same neon, which reads as an alert rather than as
        # "this is fine", and threw away the palette's own character on the
        # way.
        #
        # The bar is the theme's dark half, so the dark half's own green is
        # the colour that belongs on it. Lifted only as far as the floor
        # requires and no further, keeping its chroma: tokyo-night keeps
        # #9ece6a exactly, and the four whose authored green misses 7:1 on the
        # lifted bar rise just enough to clear it. Chroma now spans 28-99
        # across the themes, which is the palettes talking rather than the
        # fitter.
        # EVERY ROLE ON THE BAR IS FITTED AGAINST THE BAR.
        #
        # This is the cost of lifting the surface from `panel` to `rule`, and
        # it was nearly missed. The palettes are fitted against bg and panel;
        # `rule` is lighter than both, so a colour that cleared its floor on
        # panel does not necessarily clear it here. Dracula's red came out at
        # 3.48:1 on its own bar -- a fault lamp harder to read than the state
        # it reports. Fitting only `green` fixed the colour that was asked
        # about and left the other five sitting on a surface nobody had
        # checked them against.
        mono = delta_e(rrow["green"], rrow["red"]) < 1.0
        if mono:
            row_green = rrow["green"]
        else:
            row_green, _c = fit_keep_chroma(rrow["green"], row_surfaces,
                                            STATE_FLOOR, True)
        row_red = rrow["red"] if mono else fit_keep_chroma(
            rrow["red"], row_surfaces, STATE_FLOOR, True)[0]
        # Text and its two quieter levels, against the same surface.
        row_text = fit(rrow["text"], row_surfaces, STATE_FLOOR, "#ffffff")[0]
        row_muted = fit(rrow["muted"], row_surfaces, STATE_FLOOR, "#ffffff")[0]
        row_dim = fit(rrow["dim"], row_surfaces, ACCENT_FLOOR, "#ffffff")[0]
        # AND THE ROW NEEDS THE SEPARATION FLOOR TOO.
        #
        # Third time a rule written for the main tokens had to be written
        # again for the row: the monochrome exemption, the lightness cap, and
        # now this. everforest came out with the bar's green and yellow dE22.5
        # apart -- two soft pastels a reader cannot tell apart at 10px, which
        # is the exact defect the separation floor exists to prevent, arriving
        # through the one code path that did not enforce it.
        #
        # Nudged the same way and in the same direction as the main pass:
        # green is what a reader calibrates on, so yellow moves. Toward white,
        # because this surface is dark.
        row_yellow = rrow["yellow"] if mono else fit_keep_chroma(
            rrow["yellow"], row_surfaces, STATE_FLOOR, True)[0]
        if not mono:
            for _ in range(40):
                if delta_e(row_green, row_yellow) >= STATE_SEPARATION:
                    break
                nxt = _mix(row_yellow, "#ffffff", 0.05)
                if nxt == row_yellow:
                    break
                row_yellow = nxt

        # The border has to separate the bar from the page, so it cannot be
        # the bar's own colour -- which `rule` now is.
        row_edge = _mix(bar, rrow["dim"], 0.45)
        vals = dict(rrow)
        vals["panel"] = bar
        vals["bg"] = bar
        vals["green"] = row_green
        vals["yellow"] = row_yellow
        vals["red"] = row_red
        vals["text"] = row_text
        vals["muted"] = row_muted
        vals["dim"] = row_dim
        vals["edge"] = row_edge
        vals["rule"] = row_edge
        for r in ("bg", "panel", "text", "muted", "dim", "edge", "rule",
                  "green", "yellow", "red"):
            out.append("  --row-%s: %s;" % (r, vals[r]))
        out.append("  --label: \"%s\";" % label)
        out.append("  --group: \"%s\";" % group)
        out.append("  --dark: %d;" % (1 if dark else 0))
        out.append("  --pair: \"%s\";" % pair)
        out.append("}")
        if notes:
            out.append("/* %s fitted: %s */" % (name, "; ".join(notes)))
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    dest = os.path.join(here, os.pardir, "daemon", "themes.css")
    with open(dest, "w") as f:
        f.write(css())
    print("wrote %s (%d themes)" % (os.path.normpath(dest), len(THEMES)))
