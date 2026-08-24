# vendor/ — third-party code carried in the tree

Nothing here is ours. It is checked in so that a clone is a complete rig: the
file-transfer capability needs an FTP server on the daemon host, and the
alternative was an `apt` line in a deploy script — which is a step that
silently does not happen, and which makes a Pi built without a network get a
working KVM and a broken feature.

**Do not edit these files.** If a fix is needed, take it upstream and re-vendor
the result, so the next person who compares against upstream finds a match.

## `pyftpdlib/` — 2.2.0

The FTP server the DOS target pulls from. Pure Python, no build step.

MIT, `LICENSE.pyftpdlib`. **That file was reconstructed**, and this note exists
because the reconstruction is the sort of thing that should not be silent: the
copy this came from arrived with no licence file at all, while every source
header says the licence "can be found in the LICENSE file". The text is the
standard MIT licence and the copyright line is taken verbatim from those
headers. The canonical copy is in the upstream project; if this is ever
re-vendored, take the real file with it.

**This exact build is the one the rig's transfers were proven against** — it is
the copy that has been serving `~/doskutsu-netiter/` on the control host, so
`GET.BAT`, `PUT.BAT` and `CHK.BAT` have all worked against it. That is the
reason for copying it rather than fetching a newer release: a known-good pair
of ends is worth more here than a version number.

## `asyncore.py`, `asynchat.py`

**Stdlib modules, removed in Python 3.12**, which `pyftpdlib` 2.2.0 still
imports. Without them the server does not start on any current Python.

They are NOT MIT and must not be described as such. Each carries its own
permissive notice in its header — Sam Rushing, 1996 — which is the full licence
text for those files, so there is no separate file to keep beside them.

## The path

`daemon/vcctrld.py` puts this directory on `sys.path` itself rather than
relying on `PYTHONPATH`, so the daemon works the same when started by systemd,
by hand, or from a test. It is appended, not prepended: anything genuinely
installed on the host wins, and this is the fallback rather than an override.
