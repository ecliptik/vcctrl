# Skills: portable domain knowledge for any coding agent

Written 2026-08-25. `SKILL.md` files under `.agents/skills/` carry knowledge
about this rig and this repo that would otherwise have to be rediscovered by
measurement (again) or read out of `docs/`/`CLAUDE.md` by hand -- a
`description` field that an agent matches against its own task, and a body
it only loads once that match fires.

**Modeled on [Axiom](https://github.com/CharlesWiltgen/Axiom)'s
multi-harness layout** -- canonical skill content in one place, thin
per-harness discovery paths pointing at it, rather than one copy per harness
that can drift.

## 1. Catalog

| skill | triggers on | carries |
|---|---|---|
| `vcctrl-rig-hazards` | interpreting a capture/status read, writing or editing DOS batch files this rig runs, trusting a single frame/keystroke/config file as ground truth | black-frame-vs-black-screen, fps surviving a dead source, config-file-vs-running-state, DOS 6.22's non-escaping caret, `REM` lines still redirecting, Caps Lock/OCR input hazards, cell-inherits-boot-profile, board-vs-machine identity |
| `vcctrl-mcp-workflows` | calling any `vcctrl_*` MCP tool that sends input, changes power state, reboots the target, or runs a harness workflow | the shared hardware lock and its refuse-don't-force behavior, named `confirm=` arguments, verify-after-input discipline, control-vs-daemon-mode tool availability, everything being audit-visible |
| `vcctrl-repo-conventions` | creating a file in this repo, running a broad `git add`, writing a commit message | `internal/` vs `docs/`, a measurement is never planning work, `vcctrl.yaml` stays real and untracked, commit messages must state what a fix does *not* fix |

Each file is short enough to read directly -- open
`.agents/skills/<name>/SKILL.md` for the full text; the table above is a
finding aid, not a substitute.

## 2. Why `.agents/skills/` is the canonical location

Claude Code and Codex CLI converged, independently, on the same file shape:
a `SKILL.md` with `name`/`description` YAML frontmatter inside its own
directory. This is the [Agent Skills](https://agentskills.io) open standard.
Where they differ is only the directory each harness scans:

| harness | scans |
|---|---|
| Codex CLI | `.agents/skills/` (walked from cwd up to the repo root), then `~/.agents/skills/`, then admin/system locations |
| Claude Code | `.claude/skills/` (project), `~/.claude/skills/` (personal), enterprise/plugin locations |

`.agents/skills/` was picked as the one tracked copy because Codex's own
convention already generalizes past one vendor (`.agents`, not `.codex`),
and because that's also the direction the open standard itself points.
`.claude/skills` is a symlink to `../.agents/skills` -- Claude Code reads the
same files, nothing is duplicated, and nothing can drift between the two
copies because there is only one copy.

**Only the portable frontmatter fields are used** (`name`, `description`) --
no `disable-model-invocation`, `context: fork`, or other Claude Code
extension, so the same files mean the same thing in either harness rather
than silently degrading in one of them.

## 3. Adding a skill

1. `mkdir .agents/skills/<name>` and write `SKILL.md`:

   ```markdown
   ---
   name: <name>
   description: State plainly when this applies, and when it doesn't --
     this is the only text an agent sees before deciding to load the rest.
   ---

   Body: the actual guidance, written for an agent mid-task, not a human
   reading top to bottom.
   ```

2. Add a row to the catalog in sec. 1 of this file.
3. Restart before relying on it in Claude Code -- see sec. 5.

## 4. Updating a skill

Edit `.agents/skills/<name>/SKILL.md` in place (it's the only copy; the
`.claude/skills` symlink means there is nothing else to update). Keep the
catalog row in sec. 1 in sync if the trigger or contents changed enough that
the one-line summary would mislead. No restart needed for an edit to a
skill a session has already loaded once -- only a brand-new directory needs
one (sec. 5).

## 5. Verifying a skill loaded

```
Skill({skill: "<name>"})
```

`"Unknown skill: <name>"` means this session hasn't discovered it yet.
**Codex** picks up a new skill directory without a restart -- it scans
`.agents/skills/` fresh each time. **Claude Code does not, at least for a
brand-new directory** -- measured 2026-08-25: a session already running
before `.claude/skills/vcctrl-rig-hazards/` existed still returned "Unknown
skill" after the files were written and the symlink was in place. Restart
the session (or start a fresh one in this project); the same gotcha as MCP
server registration (`docs/MCP-SERVER.md` sec. 5). *Editing* the text of a
skill Claude Code has already discovered is reported to reload live --
adding a new directory is the case that needed a restart here.

**The restart fix confirmed working the same day**: a fresh Claude Code
session opened after the commit landed ran `Skill({skill:
"vcctrl-rig-hazards"})` and got "Successfully loaded skill" with the full
hazard list back, not "Unknown skill." Two data points now: stale session
before the files exist -> unknown; new session after -> loads. Not yet
tested: whether a session that was *already open* when the files landed
picks them up on its own without a restart, or needs one regardless of
whether it ever tried and failed to load the skill first.

**Explicit invocation, if you don't want to wait for the description to
match:** `/vcctrl-rig-hazards` (Claude Code, bare project-skill slash
command, shows up in `/` autocomplete) or `$vcctrl-rig-hazards` (Codex).
Same name either way -- only the sigil changes.

## 6. Removing a skill

`rm -rf .agents/skills/<name>` and delete its catalog row in sec. 1. Nothing
else references it -- there's no separate registration step to undo the way
there is for an MCP server (`claude mcp remove`).

## 7. Any other coding harness

Point it at `.agents/skills/` if it implements the Agent Skills standard;
otherwise the files are still plain Markdown any agent can be told to read
directly.

## 8. Adopting a skill into another repo

Two ways for a *different* repo -- a port repo, or any other project -- to
pull one of these skills in, without copying files that can drift out from
under it unnoticed.

1. **The same symlink convention this hub uses for `.claude/skills` ->
   `.agents/skills` (sec. 2).** From the target repo:

   ```
   ln -s /path/to/vcctrl/.agents/skills/<name> .claude/skills/<name>
   ```

   No new tooling, and the skill stays in sync with this repo's copy the
   same way `.claude/skills` here stays in sync with `.agents/skills` --
   there is still only one copy, just referenced from a second place.
   Requires a local checkout of this repo reachable from the target repo's
   filesystem; a port repo on a different machine needs its own clone to
   point at.

2. **`npx skills add <git-url> --skill <name> --agent claude`**
   ([vercel-labs/skills](https://github.com/vercel-labs/skills), the
   `skills` npm package). Confirmed it accepts arbitrary git URLs --
   including this repo's own self-hosted remote over HTTPS or SSH, not
   just github.com -- and installs per-agent (Claude Code, Codex, Cursor,
   etc., picked by `--agent`) into that agent's own skills directory.
   Copies the skill in rather than symlinking, so the copy can drift from
   this repo's until re-run; touches only the target agent's skills
   directory, not MCP config.

Either way, verify the load in the target repo's own session the same way
sec. 5 describes here -- `Skill({skill: "<name>"})`, restarting first if
it's a brand-new directory there too.
