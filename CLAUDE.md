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
