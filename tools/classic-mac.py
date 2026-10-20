#!/usr/bin/env python3
"""Drive the repeated menu-and-dialog chores on a classic Mac through vcctrl.

    classic-mac.py shutdown [--power-off]
    classic-mac.py mount-share

Both are the by-hand recipes in the classic-Mac skill, with a look at the
screen before every step that cannot be undone. They only click what they have
read on the screen, and they stop with a plain message and a saved picture when
the screen is not what they expect.

Environment:
  VCCTRL        how to run the CLI (default "vcctrl"); for a remote host, for
                example "ssh HOST vcctrl". Split on spaces.
  VCCTRL_AS     the name to take the input lock under (default "vcctrl").
  MAC_SERVER    mount-share: the server name to pick in the Chooser's list.
  MAC_VOLUME    mount-share: the volume name to pick and to find on the desktop.
  MAC_PASSWORD_CMD  mount-share: a command that prints the password (for
                example a password manager's `show`). Its output goes straight
                to `type --secret` and is never kept or shown.
  MAC_SHOTS     a directory to keep the pictures of each step in (default: a
                new temporary directory, named when a step fails).

Needs python3 with PIL, and the tesseract binary. Written against the 512Ke
and Plus (System 6, 512x342); the coordinates are the Mac's own pixels, and
the picture is found on the screenshot rather than assumed, so a stretched or
offset capture is fine.
"""
import base64
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageOps

NATIVE_W, NATIVE_H = 512, 342
VCCTRL = shlex.split(os.environ.get("VCCTRL", "vcctrl"))
WHO = os.environ.get("VCCTRL_AS", "vcctrl")
SHOTS = os.environ.get("MAC_SHOTS") or tempfile.mkdtemp(prefix="classic-mac-")
_n = [0]


def say(msg):
    print(msg, flush=True)


def die(msg, code=1):
    say("STOPPED: " + msg)
    say("pictures: " + SHOTS)
    sys.exit(code)


def run(args, stdin=None, check=True):
    """Run one CLI verb. Returns (exit code, stdout)."""
    p = subprocess.run(VCCTRL + args, input=stdin, capture_output=True)
    out = p.stdout.decode(errors="replace")
    if check and p.returncode != 0:
        die("`%s` failed (%d): %s" % (" ".join(args), p.returncode,
                                       (out + p.stderr.decode(errors="replace")).strip()[:300]))
    return p.returncode, out


def drive(*args):
    return run(list(args) + ["--as", WHO])[1]


def note(text):
    run(["note-set", "--text", text, "--as", WHO], check=False)


# -- the screen ----------------------------------------------------------------

def shot(name=None):
    """One selected frame as a PIL image, kept under SHOTS."""
    for _ in range(3):
        rc, out = run(["shot"], check=False)
        try:
            d = json.loads(out)
        except ValueError:
            d = {}
        if d.get("picture") and d.get("jpeg"):
            im = Image.open(io.BytesIO(base64.b64decode(d["jpeg"]))).convert("L")
            _n[0] += 1
            im.save(os.path.join(SHOTS, "%02d-%s.png" % (_n[0], name or "shot")))
            return im
        time.sleep(2)
    die("no picture from the capture: %s" % (out.strip()[:200] or "no answer"))


def picture_box(im):
    """(x, y, w, h) of the Mac's screen in the capture: its bright bounding box."""
    px = im.point(lambda v: 255 if v > 24 else 0)
    box = px.getbbox()
    if not box or (box[2] - box[0]) < 200 or (box[3] - box[1]) < 150:
        die("no Mac picture on the screen (blank or too small)")
    return box[0], box[1], box[2] - box[0], box[3] - box[1]


def to_native(im):
    """The Mac screen cut out of a capture and scaled to its native 512x342."""
    x, y, w, h = picture_box(im)
    return im.crop((x, y, x + w, y + h)).resize((NATIVE_W, NATIVE_H), Image.LANCZOS)


def ocr(im, psm=6, scale=3, invert=False):
    """Text and word boxes (in the image's own pixels) read from an image."""
    big = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    if invert:
        big = ImageOps.invert(big)
    big = ImageOps.expand(big, 20, fill=255)
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        big.save(f.name)
        p = subprocess.run(["tesseract", f.name, "stdout", "--psm", str(psm), "tsv"],
                           capture_output=True, text=True)
    words = []
    for line in p.stdout.splitlines()[1:]:
        c = line.split("\t")
        if len(c) == 12 and c[11].strip() and float(c[10]) > 0:
            words.append({"t": c[11].strip(),
                          "x": (int(c[6]) - 20) / scale, "y": (int(c[7]) - 20) / scale,
                          "w": int(c[8]) / scale, "h": int(c[9]) / scale})
    return words


def text_of(words):
    return " ".join(w["t"] for w in words)


def find(words, pattern):
    """The first word whose text matches a regex, or None."""
    for w in words:
        if re.search(pattern, w["t"], re.I):
            return w
    return None


def goto(x, y):
    # `goto` reads the picture to find the pointer; on a still screen the
    # capture can be briefly stale, and it works a moment later.
    for attempt in range(3):
        rc, out = run(["mouse", "goto", str(int(x)), str(int(y)), "--as", WHO], check=False)
        if rc == 0:
            return
        time.sleep(3)
    die("`mouse goto %d %d` kept failing: %s" % (x, y, out.strip()[:200]))


def click():
    drive("mouse", "click")


# -- the lock ----------------------------------------------------------------

def take_lock():
    rc, out = run(["lock", "status"], check=False)
    try:
        owner = json.loads(out).get("owner")
    except ValueError:
        die("could not read the lock")
    if owner and owner != WHO:
        die("the input lock is held by %r; not driving" % owner, 2)
    run(["lock", "acquire", "--as", WHO])


def drop_lock():
    run(["lock", "release", "--as", WHO], check=False)


# -- menus ----------------------------------------------------------------

def highlighted_band(nat, x0, x1):
    """Rows (y0, y1) of the inverted menu row, or None.

    Read from the menu's left margin: a row is highlighted when its margin,
    left of where the text starts, goes black. (The row's average brightness
    cannot tell it from a row of black text on white.)
    """
    box = nat.crop((x0, 21, x1, 240)).point(lambda v: 255 if v > 200 else 0).getbbox()
    if not box:
        return None
    left = x0 + box[0]
    strip = nat.crop((left + 2, 21, left + 9, 240))
    runs, start, prev = [], None, None
    for y in range(strip.height):
        row = sum(strip.getpixel((x, y)) for x in range(strip.width)) / strip.width
        if row < 70:
            if start is None:
                start = y
            prev = y
        elif start is not None:
            runs.append((start + 21, prev + 21))
            start = None
    if start is not None:
        runs.append((start + 21, prev + 21))
    runs = [r for r in runs if r[1] - r[0] >= 9]
    return runs[0] if runs else None


def pick_menu(title_x, x0, x1, pattern):
    """Press the menu title at (title_x, 9), move to the row matching `pattern`,
    and release on it, checking what is highlighted first.

    The row is found by reading the open menu. If the row under the pointer
    does not read as the wanted one, the menu is cancelled (released over
    nothing) and this stops.
    """
    goto(title_x, 9)
    drive("mouse", "down", "left")
    done = False
    try:
        time.sleep(0.6)
        words = []
        for _ in range(5):
            nat = to_native(shot("menu"))
            # Read only the menu's own white box: the dithered desktop beside
            # it throws the reader off. A menu caught mid-draw is blank, so
            # look again.
            box = nat.crop((x0, 21, x1, 240)).point(lambda v: 255 if v > 200 else 0).getbbox()
            if box and box[2] - box[0] >= 40 and box[3] - box[1] >= 30:
                ix0, iy0 = x0 + box[0], 21 + box[1]
                words = ocr(nat.crop((ix0, iy0, x0 + box[2], 21 + box[3])), psm=6)
                for w in words:
                    w["y"] += iy0 - 14        # the frame the code below expects
                if len(words) >= 3:
                    break
            time.sleep(1.0)
        else:
            die("no readable menu opened under the %s title" % title_x)
        # a menu row is several words: join the words that share a line
        rows = {}
        for w in words:
            rows.setdefault(round(w["y"] / 8), []).append(w)
        target = None
        for k in sorted(rows):
            line = " ".join(w["t"] for w in sorted(rows[k], key=lambda w: w["x"]))
            if re.search(pattern, line, re.I):
                ys = [w["y"] + w["h"] / 2 for w in rows[k]]
                target = 14 + sum(ys) / len(ys)
        if target is None:
            die("the menu has no row matching %r: %s" % (pattern, text_of(words)))
        # Move to the row, then correct from what is highlighted. A move can
        # land a row short or a row long (seen on the real 512Ke), and greyed
        # rows and the space below the menu never highlight. So with nothing
        # highlighted, search around the spot: a little up, then well back
        # down.
        search = [-10, -10, -10, 36, 10, 10, 10]
        step = int(round(target - 9))
        for _ in range(16):
            drive("mouse", "px", "0", str(step))
            time.sleep(0.6)
            nat = to_native(shot("menu-row"))
            band = highlighted_band(nat, x0, x1)
            if band:
                centre = (band[0] + band[1]) / 2
                say("highlighted row %d-%d, wanted the row centred at %d"
                    % (band[0], band[1], target))
                # The wanted row was found by reading the open menu, so it is
                # the right one when the inverted row is centred on it. (Reading
                # the inverted text itself is too unreliable to trust.)
                if abs(centre - target) <= 5:
                    drive("mouse", "up", "left")
                    done = True
                    return
                step = int(round(target - centre))
            elif search:
                step = search.pop(0)
            else:
                break
        die("could not land on the %r row; the menu was cancelled" % pattern)
    finally:
        if not done:
            drive("mouse", "px", "400", "0")
            drive("mouse", "up", "left")


# -- shutdown ----------------------------------------------------------------

def shutdown(power_off):
    take_lock()
    try:
        note("shutting the Mac down from the Special menu")
        wait_finder(30)
        pick_menu(183, 150, 300, r"shut|hut\s*down|\bdown\b")
        say("Shut Down chosen; waiting for the Mac to finish")
        note("the Mac is shutting down; waiting for the switch-off message")
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(5)
            im = shot("wait")
            try:
                nat = to_native(im)
            except SystemExit:
                # the shutdown screen is a small dialog on black: read the whole frame
                nat = im
            text = text_of(ocr(nat, psm=6))
            if re.search(r"switch\s*off", text, re.I) and re.search(r"safely|macintosh", text, re.I):
                say("the Mac says it can be switched off")
                if power_off:
                    drive("power", "off")
                    time.sleep(3)
                    rc, out = run(["power", "state"], check=False)
                    pw = json.loads(out).get("power", {})
                    say("power on=%s, %s mW" % (pw.get("on"), pw.get("power_mw")))
                    if pw.get("on"):
                        die("the plug still reports on", 1)
                    note("the Mac was shut down and powered off at the plug")
                else:
                    note("the Mac was shut down and is ready to be switched off")
                return 0
        die("the 'You may now switch off' message did not appear within 3 minutes; "
            "not cutting power", 1)
    finally:
        drop_lock()


# -- mount-share ----------------------------------------------------------------

# Fixed spots, in the Mac's own pixels, for the things the screen reader cannot
# find because they are pictures or bordered buttons (measured on the 512Ke
# and the Plus, System 6, AppleShare 2.0.2). Each step reads the screen first
# and stops if the dialog it expects is not there.
CHOOSER_ICON = (50, 76)        # the first icon in the Chooser: AppleShare
CHOOSER_OK = (278, 164)        # OK under the server list
LOGIN_PASSWORD = (245, 204)    # the Password field
LOGIN_OK = (388, 256)
VOLUMES_OK = (335, 256)        # OK in the volumes list
CHOOSER_CLOSE = (19, 37)       # the Chooser's close box
ALERT_CONTINUE = (176, 195)    # "Please make sure that you are connected..."


def screen_text(name, psm=11):
    return text_of(ocr(to_native(shot(name)), psm=psm))


def wait_for(pattern, what, name, seconds, psm=11):
    """Poll the screen until its text matches; stop if it never does."""
    deadline = time.time() + seconds
    text = ""
    while time.time() < deadline:
        text = screen_text(name, psm)
        if re.search(pattern, text, re.I):
            return text
        time.sleep(2)
    die("waited %d s for %s and did not see it; the screen read: %s"
        % (seconds, what, text[:200]))


DESKTOP_STRIP = (350, 60, 512, 330)     # where volume icons sit, right edge


def fuzzy_has(text, name):
    """Does OCR text contain most of a name? Inverted desktop labels read badly."""
    flat = re.sub(r"[^a-z0-9]", "", text.lower())
    want = re.sub(r"[^a-z0-9]", "", name.lower())
    grams = {want[i:i + 3] for i in range(len(want) - 2)}
    return bool(grams) and sum(1 for g in grams if g in flat) >= max(2, len(grams) // 3)


def wait_finder(seconds=150):
    """Wait until the Finder's menu bar is up (a cold boot takes about 80 s)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            nat = to_native(shot("finder"))
        except SystemExit:
            time.sleep(3)
            continue
        bar = text_of(ocr(nat.crop((0, 0, 260, 20)), psm=7))
        if re.search(r"file", bar, re.I) and re.search(r"edit|view|special", bar, re.I):
            return
        time.sleep(4)
    die("the Finder's menu bar did not come up within %d s" % seconds)


def click_at(xy, wait=1.0):
    goto(*xy)
    click()
    time.sleep(wait)


def mount_share():
    server = os.environ.get("MAC_SERVER")
    volume = os.environ.get("MAC_VOLUME")
    pw_cmd = os.environ.get("MAC_PASSWORD_CMD")
    if not (server and volume and pw_cmd):
        die("mount-share needs MAC_SERVER, MAC_VOLUME and MAC_PASSWORD_CMD", 3)
    take_lock()
    try:
        note("mounting the file share by hand in the Chooser")
        wait_finder()
        before = to_native(shot("before"))
        if fuzzy_has(text_of(ocr(before.crop(DESKTOP_STRIP), psm=11)), volume):
            say("%r is already on the desktop" % volume)
            return 0
        pick_menu(23, 10, 180, r"chooser")
        text = wait_for(r"user\s*name|appletalk", "the Chooser", "chooser", 20)
        # AppleTalk: the Chooser shows Active / Inactive radio buttons.
        words = ocr(to_native(shot("chooser")), psm=11)
        active = [w for w in words if re.fullmatch(r"active", w["t"], re.I)]
        if active:
            click_at((active[0]["x"] + active[0]["w"] / 2, active[0]["y"] + active[0]["h"] / 2), 2)
        # Pick AppleShare; with AppleTalk just switched on, Chooser first asks
        # "Please make sure that you are connected to an AppleTalk network".
        # Look after each click and click again if nothing came of it.
        for _ in range(5):
            click_at(CHOOSER_ICON, 4)
            text = screen_text("after-icon")
            if re.search(r"select a file\s*server", text, re.I):
                break
            if re.search(r"connected to an appletalk|make sure", text, re.I):
                click_at(ALERT_CONTINUE, 3)
        else:
            die("AppleShare would not open in the Chooser: %s" % text[:200])
        wait_for(r"select a file\s*server", "the server list", "servers", 40)
        # The list fills in over several seconds as the servers answer.
        row = None
        deadline = time.time() + 60
        while time.time() < deadline and not row:
            words = ocr(to_native(shot("servers")), psm=11)
            row = find(words, re.escape(server.split()[0]))
            if not row:
                time.sleep(3)
        if not row:
            die("%r is not in the Chooser's server list: %s" % (server, text_of(words)[:200]))
        click_at((row["x"] + row["w"] / 2, row["y"] + row["h"] / 2), 1.5)
        click_at(CHOOSER_OK, 2)
        wait_for(r"connect to the file", "the login dialog", "login", 40)
        click_at(LOGIN_PASSWORD, 1)
        # The password is streamed from its source into `type --secret` and
        # never stored, printed or logged here.
        src = subprocess.Popen(pw_cmd, shell=True, stdout=subprocess.PIPE)
        first = src.stdout.read(1)
        if not first:
            die("the password command printed nothing; not logging in")
        pw = subprocess.Popen(VCCTRL + ["type", "--secret", "--as", WHO],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        pw.stdin.write(first)
        for chunk in iter(lambda: src.stdout.read(64), b""):
            pw.stdin.write(chunk.replace(b"\n", b""))
        pw.stdin.close()
        pw.wait()
        src.wait()
        time.sleep(4)
        click_at(LOGIN_OK, 2)
        # On the real 512Ke the first OK click after typing is often ignored
        # (5 of 5 on 2026-10-19). Logging in can also just be slow, so look
        # for a change for a while, then press OK once more.
        for _ in range(6):
            time.sleep(2.5)
            if not re.search(r"connect to the file", screen_text("login-wait"), re.I):
                break
        else:
            say("the login dialog is still up; pressing OK once more")
            click_at(LOGIN_OK, 2)
        # Logging in over a slow link can take 30 s or more.
        text = wait_for(r"select the server volumes|volumes you want|incorrect|try again",
                        "the volume list", "volumes", 90)
        if re.search(r"incorrect|try again", text, re.I):
            die("the server refused the login (password incorrect)")
        words = ocr(to_native(shot("volumes")), psm=11)
        # the volume's name is several words; click the first one's left part,
        # away from the check box at the row's right edge
        first_word = find(words, re.escape(volume.split()[0]))
        if not first_word:
            die("volume %r is not in the list: %s" % (volume, text_of(words)[:200]))
        click_at((first_word["x"] + first_word["w"] / 2, first_word["y"] + first_word["h"] / 2), 1)
        click_at(VOLUMES_OK, 2)
        # Open-at-startup is never ticked and the password never saved: this
        # only ever presses OK.
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(3)
            nat = to_native(shot("mounted"))
            if not re.search(r"select the server volumes|file\s*server", text_of(ocr(nat, psm=11)), re.I):
                break
        click_at(CHOOSER_CLOSE, 3)
        after = to_native(shot("done"))
        desk = text_of(ocr(after.crop(DESKTOP_STRIP), psm=11))
        # A new volume draws an icon and an inverted label at the right edge,
        # and the label reads badly, so a fuzzy text match or a clear change
        # in that strip both count.
        changed = sum(1 for a, b in zip(before.crop(DESKTOP_STRIP).getdata(),
                                        after.crop(DESKTOP_STRIP).getdata())
                      if abs(a - b) > 80)
        if not (fuzzy_has(desk, volume) or changed > 400):
            die("%r did not appear on the desktop (strip read %r, %d pixels changed)"
                % (volume, desk[:80], changed))
        say("mounted %r" % volume)
        note("the file share is mounted")
        return 0
    finally:
        drop_lock()


def main(argv):
    if len(argv) < 2 or argv[1] not in ("shutdown", "mount-share"):
        print(__doc__)
        return 3
    if argv[1] == "shutdown":
        return shutdown("--power-off" in argv)
    return mount_share()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
