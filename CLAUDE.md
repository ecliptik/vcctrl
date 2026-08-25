# Working in this repository

## `internal/` is where planning work goes, and it is gitignored

Plans, drafts, scratch analysis, working notes — anything written to think
with rather than to ship — goes in `internal/`. It is ignored as a
**directory**, and that matters more than it looks.

**Why a directory rather than a filename.** An untracked single file is one
`git add -A docs` away from being committed. That is not hypothetical: during
the phase-7 history rewrite, `docs/CONFIG-PLAN.md` — the one file the operator
had explicitly said not to commit — was swept into a commit exactly that way.
It was caught only because the post-rewrite validation ran from a fresh clone,
where the identifier guard failed on it and the docs-index guard failed because
it was tracked and unindexed. Two guards caught what care did not. A gitignored
directory cannot be reached by a path-scoped `add` at all.

**What follows from it, and it cuts both ways.** Nothing in `internal/` is
tracked, so nothing is in any blob, so a history scrub never has to exempt it.
The same fact means **no bundle contains it** — a `git bundle` carries refs and
objects and nothing else. Anything in here that matters wants a copy outside
the tree; do not let "the repo is backed up" stand in for it.

**What does NOT go here:** anything a stranger cloning this repo needs.
`docs/` is the shipped record — findings, open faults, the harness standard.
The test is who the writing is for, not how finished it is.

**And a MEASUREMENT is never planning work, however planning-shaped the moment
it was taken in.** The rule above is correct and it does not fire, because a
number measured while drafting feels like part of the draft — the failure is
in the moment of filing, not in the rule. On 2026-08-25 the KVM keyboard's
geometry was measured in a real browser and the figures were written into
`internal/`: one disk, no history, no bundle, gone on a fresh clone. The
document that records that consequence was the document they went into, which
makes measuring and filing it there **worse than not measuring at all** — you
spend the observation and lose it.

So: a number, a byte comparison, a screenshot that settles a question, goes to
`docs/` **with the conditions it was taken under**, or it is spent. The
conditions are not optional and are usually the part that rots first — a
"390 px phone" figure is a 500 px figure wearing a phone's name unless the
note says it was taken inside an iframe, because headless chromium clamps a
top-level viewport whatever `--window-size` says.

The reason this is worth the words: written as a bare `9px` in a stylesheet, a
number is a preference the next person adjusts. Written with what it was
measured against — 13 px put the F-row on three lines and the panel at 639 px
of an 844 px phone — it is a decision they have to argue with. Same value,
different force.

## Two other things that are not in the repo

`vcctrl.yaml` is the operator's real configuration: untracked, holds working
addresses, and **stays real**. The tracked template is `vcctrl.example.yaml`.
A scrub's job is history and the tracked tree — do not "tidy" the live config
into placeholders and then wonder why the rig stops answering.

`server.py`, `serve.sh` and `~/.config/vcctrl/secrets.env` are outside the repo
by design and belong to the FTP path.

## Commits

The message is the documentation. State what was wrong, what the fix is, and
what it does **not** fix — this repo has been bitten more than once by a
comment that stated a fact and kept stating it after it stopped being true.
