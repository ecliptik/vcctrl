# Contributing to vcctrl

## Where writing goes

**`docs/`** is the shipped record — architecture, the harness standard,
findings from real hardware, the open-faults register. The test for
whether something belongs there is *who it's for*, not how finished it is:
if a stranger cloning this repo needs it, it goes in `docs/`, indexed in
`docs/README.md` (a test enforces this — see below).

**`internal/`** is where planning work goes: drafts, scratch analysis,
working notes — anything written to think with rather than to ship. It is
gitignored **as a directory**, on purpose: an untracked single *file* is
one `git add -A` away from being committed by accident, and a gitignored
directory cannot be reached by a path-scoped `add` at all. The trade-off
cuts both ways — nothing in `internal/` is tracked, so a history rewrite
never has to touch it, but that also means **no `git bundle` contains it**.
Anything in there that actually matters wants a copy outside the tree.

**A measurement is never planning work, however planning-shaped the moment
it was taken in.** A number, a byte comparison, a screenshot that settles
a question, belongs in `docs/` **with the conditions it was taken
under** — the conditions are usually the part that rots first. A bare `9px`
in a stylesheet is a preference the next person adjusts; "13px put the
F-row on three lines at 639px of an 844px phone" is a decision they have
to argue with. Same value, different force.

## Two things that are not in the repo, and stay that way

`vcctrl.yaml` is a rig's real configuration: untracked, holds working
addresses and credentials, and **stays real** on a working rig. The
tracked template is `vcctrl.example.yaml`. Never "tidy" a live config into
placeholders — that's how a rig stops answering.

`~/.config/vcctrl/secrets.env` (and, on the daemon host,
`/etc/vcctrl/secrets.env`) hold the file-transfer credential named by
`control.fileserver.password_env`; they are outside the repo by design,
mode 600, and the daemon and the control-host FTP launcher both refuse to
start rather than fall back to a built-in default if either is missing.

## Before you commit

- **Run the tests**: `pytest tests/`. No hardware required — the suite
  runs against a hermetic `tests/test-config.yaml`, never a real
  `vcctrl.yaml`. It needs Python, PyYAML, node, a Chromium/Chrome binary,
  and ffmpeg.
- **The identifier guard**
  (`tests/test_core.py::test_no_rig_identifiers_in_the_code`) scans every
  tracked file for hostnames, addresses and credentials that belong in a
  gitignored config instead. If you're adding your own rig's literals
  (rather than a category the guard should catch generically for
  everyone), put them in an untracked `~/.config/vcctrl/identifiers.txt`
  (one regex fragment per line; override the path with
  `VCCTRL_IDENT_FILE`) rather than editing the test. `tools/scan-history.sh`
  runs the same category checks against every blob, commit message and
  tag in history, not just the current tree -- the guard above is
  HEAD-only by design and cannot see what the history scanner can.
- **Commit hashes changed on 2026-09-11.** This history was rewritten
  once, before its first genuinely public audience (the repo was
  tailnet-gated until that point, so no outside clone needed
  reconciling), to remove a small number of identifiers a first pass had
  missed. `docs/HISTORY-MAP.txt` maps every old hash to its current one,
  for anything citing a pre-2026-09-11 commit from outside this
  repository.
- **The docs-index guard**
  (`test_the_docs_index_cannot_rot_silently`) fails if you add a
  `docs/*.md` file without a matching backticked filename in
  `docs/README.md`. Add the entry in the same commit.
- **The docs-index guard's sibling**, `test_every_top_level_directory_is_deployed_or_deliberately_is_not`,
  fails if you add a top-level directory without saying in
  `tests/test_core.py` whether `pi/deploy.sh` should ship it and why. This
  has already gone wrong twice for real (a moved directory silently
  stopped being deployed) — write the answer down rather than relying on
  someone remembering.

## The working tree may be shared

If more than one person or agent works from the same checkout at once:
path-scope every `git add` (name the files; never `-A`, never `.`), and
before touching a shared file, check whether someone else has uncommitted
work in it. A `git status` that shows more than your own changes is a
signal to ask, not to sweep up.

## Commits

The message is the documentation. State what was wrong, what the fix is,
and what it does **not** fix. This repo has more than once been bitten by
a comment that stated a fact and kept stating it after it stopped being
true — a commit message that only says "what changed" without "why" or
"what's still broken" reproduces that same failure at a larger scale.
