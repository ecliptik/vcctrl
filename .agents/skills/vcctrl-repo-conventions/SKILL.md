---
name: vcctrl-repo-conventions
description: This repo's own working rules for anyone -- human or agent -- writing files into vcctrl or preparing a commit. Covers where planning work goes, what stays untracked and real, and what a commit message must say. Use before creating a new file in this repo, before running a broad `git add`, and before writing a commit message.
---

# Working in the vcctrl repo

Full source of these rules: `CONTRIBUTING.md` at the repo root (pointed at
by both `CLAUDE.md` and `AGENTS.md`). This skill exists so the rules apply
even in a harness that doesn't load a project-instructions file at all.

**`internal/` is where planning work goes, and it is gitignored as a
directory, not a filename pattern.** Plans, drafts, scratch analysis, working
notes -- anything written to think with rather than to ship -- goes there. An
untracked *file* is one `git add -A` away from being swept into a commit; a
gitignored *directory* cannot be reached by a path-scoped add at all. This
already happened once (`docs/CONFIG-PLAN.md` during a history rewrite) and is
why the rule is a directory rule.

**A measurement is never planning work, no matter how planning-shaped the
moment it was taken in.** A number, a byte comparison, a screenshot that
settles a question, belongs in `docs/` **with the conditions it was measured
under** -- or it is lost the moment nobody reopens that draft. "390 px" means
nothing without noting it was read inside an iframe, because headless
Chromium clamps a top-level viewport regardless of `--window-size`.

**`docs/` is the shipped record; `internal/` is not.** The test for where
something goes is who the writing is for, not how finished it is -- if a
stranger cloning this repo would need it, it belongs in `docs/`, even in
draft form.

**`vcctrl.yaml` is the operator's real configuration and stays real.** It is
untracked and holds working addresses; the tracked template is
`vcctrl.example.yaml`. Never rewrite the live config into placeholders as
part of a "cleanup" -- a scrub's job is history and the tracked tree, not the
live config, and doing so breaks the rig, not just the repo.

**`~/.config/vcctrl/secrets.env` (and the daemon host's own
`/etc/vcctrl/secrets.env`) are outside the repo by design** -- they hold the
file-transfer credential `control.fileserver.password_env` names, and both
the daemon and the control-host FTP launcher refuse to start rather than
fall back to a built-in default if either is missing. Don't propose
bringing them in without being asked.

**Before committing, run `pytest tests/`.** Two guards specifically gate
what you're about to do: `test_no_rig_identifiers_in_the_code` (a rig's own
literals go in an untracked `~/.config/vcctrl/identifiers.txt`, never in
the test itself) and `test_the_docs_index_cannot_rot_silently` (a new
`docs/*.md` file needs a matching entry in `docs/README.md` in the same
commit).

**A commit message is documentation, not a label.** State what was wrong,
what the fix is, and what it does **not** fix. This repo has specifically
been bitten by a commit message that stated a fact and kept stating it after
the fact stopped being true -- write commit messages that will still be
correct after the next change, or that clearly describe a moment in time
rather than an ongoing property.
