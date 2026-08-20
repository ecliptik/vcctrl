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

ROLES = ("bg", "panel", "rule", "dim", "muted", "text", "bright",
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
FLOOR = {"text": 4.5, "muted": 4.5, "dim": 3.0}
ACCENT_FLOOR = 3.0
ACCENTS = ("red", "orange", "yellow", "green", "cyan", "blue", "magenta")


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
    for role in ACCENTS:
        new, changed = fit(roles[role], surfaces, ACCENT_FLOOR, toward)
        if changed:
            notes.append("%s %s->%s" % (role, roles[role], new))
            roles[role] = new
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
