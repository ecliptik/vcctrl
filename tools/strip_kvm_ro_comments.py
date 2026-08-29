#!/usr/bin/env python3
"""Tokenizer-based comment stripper for daemon/kvm-ro.html -- an AUTOMATED
DEPLOY STEP, not a one-off tool. Called from pi/install.sh's install_public()
and pi/deploy.sh's --page path so the file actually served to the public
mirror never carries this repo's own maintainer comments.

WHY THIS HAS TO BE AUTOMATED, NOT RUN BY HAND: it used to be exactly that --
run once, manually, during the 2026-08-28 security audit (finding F4), from a
copy of this same script kept in gitignored internal/ (per this repo's own
rule that planning/one-off work lives there, never in the tracked tree).
That strip covered every comment THEN in the file. It did not, and by
`internal/`'s own nature COULD not, cover a single comment written after --
and every edit to kvm-ro.html since (this file included) put its own,
often extensive, reasoning back in, all of it served in the clear, because
nothing re-ran the strip and nothing enforced that it should. Found the hard
way, same day this docstring was written: `curl .../` against the live
mirror turned up internal architecture detail, past-incident references and
even a commit hash, because comments accumulate with every edit and a manual
step doesn't. This being a deploy step instead of a habit is the actual fix;
see CLAUDE.md's own "a rule written where the failure was found lands one
layer from where it could prevent it" for why habit was never going to hold.

Carves the file into HTML / CSS (<style>) / JS (<script>) regions and finds
comment spans in each with a real tokenizer that understands JS strings,
template literals (with ${} nesting) and regex literals, so it never treats a
`//` inside a URL/string/regex as a comment. Emits:
  - the stripped file
  - every removed span, for review (each must open with //, /* or <!--)
It only DELETES comment spans; a line left all-whitespace *because* a comment
was removed from it is dropped entirely (its content was only that comment),
but a blank line inside a template literal is untouched (no dropped char).

Usage: strip_kvm_ro_comments.py SRC [OUT]
  SRC given, no OUT: prints every removed comment span to stdout, for review.
  SRC and OUT: writes the stripped file to OUT, count/line-delta to stderr.
Assumes exactly one <style>...</style> and one <script>...</script> block,
true of daemon/kvm-ro.html today; refuses (SystemExit) if either tag is
missing, and refuses separately if any span it's about to remove doesn't
actually start with a comment opener -- a tokenizer bug should stop a deploy,
never silently ship a mis-stripped page.
"""
import sys

SRC = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 else None
text = open(SRC, encoding="utf-8").read()
n = len(text)

def find(tag, frm=0):
    i = text.find(tag, frm)
    if i < 0:
        raise SystemExit("missing %r" % tag)
    return i

s_open = find("<style>")
s_close = find("</style>", s_open)
j_open = find("<script>", s_close)
j_close = find("</script>", j_open)

# regions as (start, end, lang); the literal tags stay in HTML/passthrough
regions = [
    (0, s_open, "html"),
    (s_open, s_open + len("<style>"), "pass"),
    (s_open + len("<style>"), s_close, "css"),
    (s_close, s_close + len("</style>"), "pass"),
    (s_close + len("</style>"), j_open, "html"),
    (j_open, j_open + len("<script>"), "pass"),
    (j_open + len("<script>"), j_close, "js"),
    (j_close, j_close + len("</script>"), "pass"),
    (j_close + len("</script>"), n, "html"),
]

comments = []  # (start, end) absolute offsets of comment tokens

def scan_html(a, b):
    i = a
    while i < b:
        if text.startswith("<!--", i):
            end = text.find("-->", i + 4)
            end = b if end < 0 else end + 3
            comments.append((i, end))
            i = end
        else:
            i += 1

def scan_css(a, b):
    i = a
    while i < b:
        c = text[i]
        if c in "\"'":
            q = c
            i += 1
            while i < b and text[i] != q:
                if text[i] == "\\":
                    i += 1
                i += 1
            i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = b if end < 0 else end + 2
            comments.append((i, end))
            i = end
        else:
            i += 1

ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")
# keywords/operators after which a `/` begins a regex, not division
RE_PREV_WORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "do", "else", "yield", "await", "case", "throw",
}

def scan_js(a, b):
    """Full JS scan over [a,b). Uses an explicit stack to handle template
    literals whose ${...} sections are full expression contexts (which may
    themselves contain strings, regexes, or nested templates)."""
    i = a
    prev = None  # previous significant token: a punctuation char, an
                 # identifier/keyword string, or one of 'num','str','reg','tmpl'
    # stack entries mark that we are inside a ${ } of a template; when we see
    # the matching } at brace-depth 0 we resume template scanning.
    tmpl_stack = []  # list of brace-depth ints at the point ${ opened
    brace_depth = 0

    def read_string(i, q):
        i += 1
        while i < b:
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == q:
                return i + 1
            i += 1
        return i

    def read_regex(i):
        # text[i] == '/', known to start a regex
        i += 1
        in_class = False
        while i < b:
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                i += 1
                break
            elif ch == "\n":
                break  # unterminated; bail
            i += 1
        while i < b and text[i] in ID:  # flags
            i += 1
        return i

    def read_template(i):
        # text[i] == '`'; returns index after handling. May open ${...} which
        # switches back to expression mode via the outer loop + tmpl_stack.
        nonlocal prev
        i += 1
        while i < b:
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "`":
                return i + 1, False  # template closed
            if ch == "$" and i + 1 < b and text[i + 1] == "{":
                return i + 2, True   # entered ${ expression
            i += 1
        return i, False

    while i < b:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < b and text[i + 1] == "/":
            end = text.find("\n", i)
            end = b if end < 0 else end
            comments.append((i, end))
            i = end
            continue
        if c == "/" and i + 1 < b and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = b if end < 0 else end + 2
            comments.append((i, end))
            i = end
            continue
        if c in "\"'":
            i = read_string(i, c)
            prev = "str"
            continue
        if c == "`":
            j, entered = read_template(i)
            i = j
            if entered:
                tmpl_stack.append(brace_depth)
                brace_depth += 1  # the ${ opens a brace context
                prev = None
            else:
                prev = "tmpl"
            continue
        if c == "/":
            # regex vs division
            if prev is None or (isinstance(prev, str) and len(prev) == 1
                                and prev in "(,=:[!&|?{;}~+-*/%<>^")             \
                    or prev in RE_PREV_WORDS:
                i = read_regex(i)
                prev = "reg"
            else:
                prev = "/"
                i += 1
            continue
        if c in ID:
            if c.isdigit() or (c == "." and i + 1 < b and text[i+1].isdigit()):
                j = i
                while j < b and text[j] in ID or (j < b and text[j] == "."):
                    j += 1
                prev = "num"
                i = j
                continue
            j = i
            while j < b and text[j] in ID:
                j += 1
            prev = text[i:j]
            i = j
            continue
        if c == "{":
            brace_depth += 1
            prev = "{"
            i += 1
            continue
        if c == "}":
            brace_depth -= 1
            if tmpl_stack and brace_depth == tmpl_stack[-1]:
                # closing the ${...}; resume template scanning from i+1
                tmpl_stack.pop()
                k = i + 1
                # inline template resume
                while k < b:
                    ch = text[k]
                    if ch == "\\":
                        k += 2
                        continue
                    if ch == "`":
                        k += 1
                        prev = "tmpl"
                        break
                    if ch == "$" and k + 1 < b and text[k + 1] == "{":
                        k += 2
                        tmpl_stack.append(brace_depth)
                        brace_depth += 1
                        prev = None
                        break
                    k += 1
                i = k
                continue
            prev = "}"
            i += 1
            continue
        # any other punctuation
        prev = c
        i += 1

for a, b, lang in regions:
    if lang == "html":
        scan_html(a, b)
    elif lang == "css":
        scan_css(a, b)
    elif lang == "js":
        scan_js(a, b)
    # pass: nothing

# ---- validate every removed span opens as a comment --------------------
bad = []
for a, b in comments:
    frag = text[a:b].lstrip()
    if not (frag.startswith("//") or frag.startswith("/*")
            or frag.startswith("<!--")):
        bad.append((a, b, text[a:b][:60]))
if bad:
    sys.stderr.write("SUSPECT non-comment spans:\n")
    for a, b, f in bad:
        sys.stderr.write("  @%d: %r\n" % (a, f))
    raise SystemExit("refusing: some removed spans are not comments")

# ---- build a drop-mask, then reconstruct with line-collapse -------------
drop = bytearray(n)  # 1 = character is inside a comment
for a, b in comments:
    for k in range(a, b):
        drop[k] = 1

# line-aware reconstruction: for each source line, if every char is either
# dropped or whitespace AND the line has >=1 dropped char, remove the whole
# line (it was only a comment). Otherwise keep the line's non-dropped chars,
# trimming trailing whitespace only when we actually removed a trailing comment.
out = []
i = 0
while i < n:
    j = text.find("\n", i)
    line_end = n if j < 0 else j
    seg = text[i:line_end]
    seg_drop = drop[i:line_end]
    has_drop = any(seg_drop)
    kept = "".join(ch for ch, d in zip(seg, seg_drop) if not d)
    if has_drop and kept.strip() == "":
        # whole line was only comment (+ whitespace): drop line entirely
        i = line_end + 1
        continue
    if has_drop:
        kept = kept.rstrip()  # trailing comment removed -> trim trailing ws
    out.append(kept)
    if j < 0:
        break
    out.append("\n")
    i = line_end + 1

result = "".join(out)
sys.stderr.write("comments removed: %d\n" % len(comments))
sys.stderr.write("lines: %d -> %d\n"
                 % (text.count("\n") + 1, result.count("\n") + 1))
if OUT:
    open(OUT, "w", encoding="utf-8").write(result)
else:
    # print removed comment texts for review
    for a, b in comments:
        print("----", text[a:b][:200].replace("\n", "\\n"))
