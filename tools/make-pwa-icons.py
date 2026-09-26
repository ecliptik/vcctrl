#!/usr/bin/env python3
"""Render daemon/pwa/*.png from daemon/favicon.svg. Run after editing the svg.

ONE SOURCE, AS WITH THE INLINED ICON. kvm.html inlines favicon.svg as a
data: URI, and test_favicon_single_source keeps the two identical. The home-
screen icons cannot be inlined -- Safari ignores an SVG apple-touch-icon and
wants a PNG it can fetch -- so they are rendered from the same svg, and the
svg's sha256 is written beside them. test_pwa_icons_match_the_favicon fails
when the svg changes and this script has not been re-run.

Three shapes, because the platforms mask differently:

    apple-touch-icon.png  180  full-bleed square. iOS rounds the corners
                               itself; a pre-rounded icon gets dark corners
                               inside its mask.
    icon-192/512.png           the favicon as drawn, rounded corners and all,
                               for `purpose: any`.
    icon-maskable-512.png      full bleed with the glyph shrunk into the
                               centre 80%, for `purpose: maskable` -- a mask
                               may cut anything outside that circle.

Needs inkscape (the control host has it; the Pi does not need it -- the PNGs
are committed).
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
SVG = os.path.join(ROOT, "daemon", "favicon.svg")
OUT = os.path.join(ROOT, "daemon", "pwa")


def full_bleed(svg, scale):
    """Square background, glyph scaled about the centre of the 32x32 box."""
    m = re.match(r'(<svg[^>]*>)(<rect [^>]*/>)(.*)(</svg>)\s*$', svg, re.S)
    if not m:
        sys.exit("favicon.svg is not <svg><rect background/>...</svg> -- "
                 "update make-pwa-icons.py for its new shape")
    head, bg, glyph, tail = m.groups()
    bg = re.sub(r'\s*rx="[^"]*"', "", bg)
    t = 16 * (1 - scale)
    return '%s%s<g transform="translate(%g %g) scale(%g)">%s</g>%s' % (
        head, bg, t, t, scale, glyph, tail)


def render(svg, size, name):
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as f:
        f.write(svg)
    try:
        subprocess.run(["inkscape", f.name, "--export-type=png",
                        "--export-filename=" + os.path.join(OUT, name),
                        "-w", str(size), "-h", str(size)],
                       check=True, capture_output=True)
    finally:
        os.unlink(f.name)
    print("wrote daemon/pwa/%s (%dpx)" % (name, size))


def main():
    raw = open(SVG, "rb").read()
    svg = raw.decode("utf-8")
    os.makedirs(OUT, exist_ok=True)
    render(full_bleed(svg, 0.85), 180, "apple-touch-icon.png")
    render(svg, 192, "icon-192.png")
    render(svg, 512, "icon-512.png")
    render(full_bleed(svg, 0.75), 512, "icon-maskable-512.png")
    with open(os.path.join(OUT, "SOURCE.sha256"), "w") as f:
        f.write(hashlib.sha256(raw).hexdigest() + "  daemon/favicon.svg\n")
    print("wrote daemon/pwa/SOURCE.sha256")


if __name__ == "__main__":
    main()
