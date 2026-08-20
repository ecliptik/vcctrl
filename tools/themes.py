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

# name: (group, dark?, pair, {roles})
THEMES = {
 "vga": ("House", True, "solarized-light", dict(
   bg="#17150F", panel="#201E19", rule="#322D25", dim="#9A9186",
   muted="#B5AC9E", text="#E8E3D9", bright="#FFFFFF",
   red="#FF5555", orange="#FFA855", yellow="#FFFF55", green="#55FF55",
   cyan="#55FFFF", blue="#5599FF", magenta="#FF55FF")),

 "tokyo-night": ("Editor", True, "tokyo-night-light", dict(
   bg="#1a1b26", panel="#16161e", rule="#292e42", dim="#565f89",
   muted="#a9b1d6", text="#c0caf5", bright="#ffffff",
   red="#f7768e", orange="#ff9e64", yellow="#e0af68", green="#9ece6a",
   cyan="#7dcfff", blue="#7aa2f7", magenta="#bb9af7")),
 "tokyo-night-light": ("Editor", False, "tokyo-night", dict(
   bg="#e1e2e7", panel="#d5d6db", rule="#c4c8da", dim="#6a6f8e",
   muted="#4c5182", text="#343b58", bright="#1a1b26",
   red="#8c4351", orange="#965027", yellow="#8f5e15", green="#485e30",
   cyan="#0f4b6e", blue="#34548a", magenta="#5a3e8e")),

 "dracula": ("Editor", True, "catppuccin-latte", dict(
   bg="#282a36", panel="#21222c", rule="#44475a", dim="#6272a4",
   muted="#bfbfd0", text="#f8f8f2", bright="#ffffff",
   red="#ff5555", orange="#ffb86c", yellow="#f1fa8c", green="#50fa7b",
   cyan="#8be9fd", blue="#8be9fd", magenta="#ff79c6")),

 "nord": ("Editor", True, "catppuccin-latte", dict(
   bg="#2e3440", panel="#3b4252", rule="#434c5e", dim="#7b88a1",
   muted="#d8dee9", text="#eceff4", bright="#ffffff",
   red="#bf616a", orange="#d08770", yellow="#ebcb8b", green="#a3be8c",
   cyan="#88c0d0", blue="#81a1c1", magenta="#b48ead")),

 "solarized-dark": ("Editor", True, "solarized-light", dict(
   bg="#002b36", panel="#073642", rule="#0f4c5c", dim="#657b83",
   muted="#93a1a1", text="#eee8d5", bright="#fdf6e3",
   red="#dc322f", orange="#cb4b16", yellow="#b58900", green="#859900",
   cyan="#2aa198", blue="#268bd2", magenta="#d33682")),
 "solarized-light": ("Editor", False, "solarized-dark", dict(
   bg="#fdf6e3", panel="#eee8d5", rule="#ded8c3", dim="#657b83",
   muted="#586e75", text="#073642", bright="#002b36",
   red="#dc322f", orange="#cb4b16", yellow="#a07800", green="#6f7d00",
   cyan="#2aa198", blue="#268bd2", magenta="#d33682")),

 "catppuccin-mocha": ("Editor", True, "catppuccin-latte", dict(
   bg="#1e1e2e", panel="#181825", rule="#313244", dim="#7f849c",
   muted="#a6adc8", text="#cdd6f4", bright="#ffffff",
   red="#f38ba8", orange="#fab387", yellow="#f9e2af", green="#a6e3a1",
   cyan="#94e2d5", blue="#89b4fa", magenta="#cba6f7")),
 "catppuccin-macchiato": ("Editor", True, "catppuccin-latte", dict(
   bg="#24273a", panel="#1e2030", rule="#363a4f", dim="#8087a2",
   muted="#a5adcb", text="#cad3f5", bright="#ffffff",
   red="#ed8796", orange="#f5a97f", yellow="#eed49f", green="#a6da95",
   cyan="#8bd5ca", blue="#8aadf4", magenta="#c6a0f6")),
 "catppuccin-frappe": ("Editor", True, "catppuccin-latte", dict(
   bg="#303446", panel="#292c3c", rule="#414559", dim="#838ba7",
   muted="#a5adce", text="#c6d0f5", bright="#ffffff",
   red="#e78284", orange="#ef9f76", yellow="#e5c890", green="#a6d189",
   cyan="#81c8be", blue="#8caaee", magenta="#ca9ee6")),
 "catppuccin-latte": ("Editor", False, "catppuccin-mocha", dict(
   bg="#eff1f5", panel="#e6e9ef", rule="#ccd0da", dim="#6c6f85",
   muted="#5c5f77", text="#4c4f69", bright="#1e1e2e",
   red="#d20f39", orange="#fe640b", yellow="#a07a00", green="#40a02b",
   cyan="#179299", blue="#1e66f5", magenta="#8839ef")),

 "gruvbox-dark": ("Editor", True, "gruvbox-light", dict(
   bg="#282828", panel="#1d2021", rule="#3c3836", dim="#928374",
   muted="#bdae93", text="#ebdbb2", bright="#fbf1c7",
   red="#fb4934", orange="#fe8019", yellow="#fabd2f", green="#b8bb26",
   cyan="#8ec07c", blue="#83a598", magenta="#d3869b")),
 "gruvbox-light": ("Editor", False, "gruvbox-dark", dict(
   bg="#fbf1c7", panel="#f2e5bc", rule="#d5c4a1", dim="#7c6f64",
   muted="#665c54", text="#3c3836", bright="#282828",
   red="#9d0006", orange="#af3a03", yellow="#b57614", green="#79740e",
   cyan="#427b58", blue="#076678", magenta="#8f3f71")),

 "rose-pine": ("Editor", True, "rose-pine-dawn", dict(
   bg="#191724", panel="#1f1d2e", rule="#26233a", dim="#908caa",
   muted="#c4c1d6", text="#e0def4", bright="#ffffff",
   red="#eb6f92", orange="#f6c177", yellow="#f6c177", green="#9ccfd8",
   cyan="#9ccfd8", blue="#31748f", magenta="#c4a7e7")),
 "rose-pine-moon": ("Editor", True, "rose-pine-dawn", dict(
   bg="#232136", panel="#2a273f", rule="#393552", dim="#908caa",
   muted="#c4c1d6", text="#e0def4", bright="#ffffff",
   red="#eb6f92", orange="#f6c177", yellow="#f6c177", green="#a3be8c",
   cyan="#9ccfd8", blue="#3e8fb0", magenta="#c4a7e7")),
 "rose-pine-dawn": ("Editor", False, "rose-pine", dict(
   bg="#faf4ed", panel="#fffaf3", rule="#e6dfd8", dim="#797593",
   muted="#6a6480", text="#575279", bright="#2a273f",
   red="#b4637a", orange="#ea9d34", yellow="#9a7000", green="#3f7f70",
   cyan="#56949f", blue="#286983", magenta="#907aa9")),

 "everforest-dark": ("Editor", True, "everforest-light", dict(
   bg="#2d353b", panel="#272e33", rule="#3d484d", dim="#859289",
   muted="#b9c0ab", text="#d3c6aa", bright="#f2efdf",
   red="#e67e80", orange="#e69875", yellow="#dbbc7f", green="#a7c080",
   cyan="#83c092", blue="#7fbbb3", magenta="#d699b6")),
 "everforest-light": ("Editor", False, "everforest-dark", dict(
   bg="#fdf6e3", panel="#f4f0d9", rule="#e0dcc7", dim="#829181",
   muted="#708089", text="#5c6a72", bright="#3a454a",
   red="#f85552", orange="#f57d26", yellow="#a68100", green="#8da101",
   cyan="#35a77c", blue="#3a94c5", magenta="#df69ba")),

 "kanagawa-wave": ("Editor", True, "kanagawa-lotus", dict(
   bg="#1f1f28", panel="#16161d", rule="#2a2a37", dim="#727169",
   muted="#9e9b93", text="#dcd7ba", bright="#ffffff",
   red="#e46876", orange="#ffa066", yellow="#e6c384", green="#98bb6c",
   cyan="#7aa89f", blue="#7e9cd8", magenta="#957fb8")),
 "kanagawa-dragon": ("Editor", True, "kanagawa-lotus", dict(
   bg="#181616", panel="#282727", rule="#393836", dim="#a6a69c",
   muted="#b6b6ae", text="#c5c9c5", bright="#e8e6e3",
   red="#c4746e", orange="#b6927b", yellow="#c4b28a", green="#8a9a7b",
   cyan="#8ea4a2", blue="#8ba4b0", magenta="#a292a3")),
 "kanagawa-lotus": ("Editor", False, "kanagawa-wave", dict(
   bg="#f2ecbc", panel="#e7dba0", rule="#d5cea3", dim="#716e61",
   muted="#63615a", text="#545464", bright="#1f1f28",
   red="#c84053", orange="#cc6d00", yellow="#77713f", green="#6f894e",
   cyan="#597b75", blue="#4d699b", magenta="#624c83")),

 # Phosphor: one hue against near-black. Hand-authored -- these are not base16
 # schemes. They differ by HUE ONLY: a phosphor is defined by persistence and
 # bloom as much as colour, and this design simulates neither, because the
 # captured picture is evidence and must not be filtered.
 "apple-ii-green": ("Phosphor", True, "solarized-light", dict(
   bg="#001200", panel="#001a00", rule="#0a3d0a", dim="#2f8f3f",
   muted="#57c46a", text="#7dfb8f", bright="#c8ffd0",
   red="#7dfb8f", orange="#7dfb8f", yellow="#a9ffb5", green="#7dfb8f",
   cyan="#a9ffb5", blue="#57c46a", magenta="#7dfb8f")),
 "ibm-5151": ("Phosphor", True, "solarized-light", dict(
   bg="#0b0f00", panel="#131a00", rule="#28380a", dim="#6f9124",
   muted="#93bf30", text="#b6ff3d", bright="#dcff9e",
   red="#b6ff3d", orange="#b6ff3d", yellow="#dcff9e", green="#b6ff3d",
   cyan="#dcff9e", blue="#93bf30", magenta="#b6ff3d")),
 "dec-amber": ("Phosphor", True, "solarized-light", dict(
   bg="#140c00", panel="#1e1200", rule="#3d2600", dim="#a2701a",
   muted="#d19426", text="#ffb000", bright="#ffd88a",
   red="#ffb000", orange="#ffb000", yellow="#ffd88a", green="#ffb000",
   cyan="#ffd88a", blue="#d19426", magenta="#ffb000")),
 "vt220-white": ("Phosphor", True, "solarized-light", dict(
   bg="#0d0d0d", panel="#151515", rule="#2b2b2b", dim="#8a8a85",
   muted="#b4b4ae", text="#e8e8e0", bright="#ffffff",
   red="#e8e8e0", orange="#e8e8e0", yellow="#ffffff", green="#e8e8e0",
   cyan="#ffffff", blue="#b4b4ae", magenta="#e8e8e0")),
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
    _g, dark, _p, roles = THEMES[name]
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
    for name, (_group, _dark, pair, _raw) in THEMES.items():
        roles, notes = fitted(name)
        sel = ":root" if name == "vga" else '[data-theme="%s"]' % name
        out.append("%s {" % sel)
        for r in ROLES:
            out.append("  --%s: %s;" % (r, roles[r]))
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
