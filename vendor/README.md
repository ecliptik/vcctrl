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

MIT, `LICENSE.pyftpdlib`. **Verified 2026-09-11 against the real upstream
tag** (`release-2.2.0`, https://github.com/giampaolo/pyftpdlib): `LICENSE.pyftpdlib`
here is now the exact file from that tag, and a spot check of `__init__.py`,
`exceptions.py` and `authorizers.py` against the same tag came back
byte-for-byte identical. (An earlier version of this note said the license
file had been reconstructed from source-header text because the copy this
vendoring started from had none; that is no longer the case now that the
real file has been fetched.)

**This exact build is the one the rig's transfers were proven against** — it is
the copy that has been serving `~/doskutsu-netiter/` on the control host, so
`GET.BAT`, `PUT.BAT` and `CHK.BAT` have all worked against it. That is the
reason for copying it rather than fetching a newer release: a known-good pair
of ends is worth more here than a version number.

## `ogg-opus-decoder-1.7.5.min.js`

The browser-side Opus decoder for the public mirror's audio stream
(`daemon/vcweb_public.py` serves it; `daemon/kvm-ro.html` loads it). One
self-contained UMD file, WASM embedded -- exposes
`window["ogg-opus-decoder"].OggOpusDecoder`, which decodes an Ogg Opus byte
stream fed to it in arbitrary pieces. WASM rather than WebCodecs because the
operator's compatibility bar is Safari/Firefox/Chrome on desktop AND mobile,
and as of 2026-08 WebCodecs audio is absent from Firefox for Android
entirely and from Safari before 26.

MIT for the wrapper (`LICENSE.opus` carries the full text), **and it also
compiles in libopus as WebAssembly**, by upstream design -- libopus itself is
BSD-3-Clause (Xiph.Org and contributors). That notice was not carried
anywhere in this repo until 2026-09-11; `LICENSE.opus` now has both,
verbatim.

Provenance, exactly:

- upstream: https://github.com/eshaz/wasm-audio-decoders (author Ethan
  Halsall; MIT per the npm package metadata and the file's own header)
- taken from the npm registry: `ogg-opus-decoder` **1.7.5**, tarball
  shasum `aabc6f019da44acd9c127bf6f8ec298e50021602` (matched the registry's
  own dist.shasum at download, 2026-08-29)
- this file is `package/dist/ogg-opus-decoder.min.js` from that tarball,
  byte-identical, renamed to carry the version -- sha256
  `c5055d3410ca02728d10708e154b47219c38ceef52155f18702e34204150651f`

The version is in the filename because the public mirror serves it with an
immutable one-year cache: re-vendoring MUST change the name (and the script
tag in kvm-ro.html, and `OPUS_DECODER_JS` in vcweb_public.py), or cached
visitors keep the old build forever.

**One documented deviation from its own type declarations**: the package's
`types.d.ts` declares the main-thread `OggOpusDecoder.decode()` synchronous;
in this dist build it returns a Promise. kvm-ro.html awaits it. Found the
hard way -- an unawaited call "succeeds" with zero samples and no error.

## `asyncore.py`, `asynchat.py`

**Stdlib modules, removed in Python 3.12**, which `pyftpdlib` 2.2.0 still
imports. Without them the server does not start on any current Python.

They are NOT MIT and must not be described as such. Each carries its own
permissive notice in its header — Sam Rushing, 1996 — layered with the
Python Software Foundation License Agreement under which they were also
distributed as part of CPython. Full text of both: `LICENSE.asyncore`.

**These files were edited, found 2026-09-11.** Both had their real
deprecation-warning call quietly replaced with a bare `pass`, contradicting
this section's own "do not edit" rule and going undocumented until now. The
dead code (an unused message-string constant plus the no-op) has been
removed outright in both files -- no functional change, no deprecation
warning fires either way, and no other difference exists in `asyncore.py`
(verified against Debian's `python3-pyasyncore` package, byte-for-byte
identical elsewhere). See `LICENSE.asyncore` for why a clean "restore" was
not possible and what was done instead. **The "do not edit" rule above
applies going forward from here** -- if a future fix is needed, take it
upstream (there is no upstream anymore, so: to the Debian package, or the
`test.support` copy at least for the API shape) and re-vendor, and update
this note.

## `dinspect.exe`

The DOS-side hardware-inventory binary the KVM's sysinfo panel runs (see
`docs/DINSPECT-SYSINFO.md` and `.agents/skills/vcctrl-dinspect-sysinfo/SKILL.md`).
**Missing from this file until 2026-09-11**, which this section now fixes.

- Source: [dinspect](https://github.com/ecliptik/dinspect), a public sibling
  project, built at commit `d92f000` (2026-09-03).
- Licence: **CC0 1.0 Universal** (public-domain dedication) --
  `dinspect`'s own `LICENSE`. No conditions attach to using or redistributing
  it.
- Provenance verified 2026-09-11: this file's sha256 matches a build of that
  repository at `d92f000`.
- The binary links the **Open Watcom C/C++ runtime** (Open Watcom Public
  License 1.0, which permits distributing programs linked with its
  unmodified runtime). `dinspect`'s own `THIRD-PARTY.md` currently states
  that no Watcom code is linked into `dinspect.exe`; the binary's own string
  table contradicts that ("Open Watcom C/C++16 Run-Time system..."). That is
  a documentation issue in `dinspect`'s own repository, not this one, and is
  flagged there rather than fixed here.
- `dinspect` also credits, as protocol/design references only (not code
  copied into it): PicoGUS's presence-detection protocol (GPLv2, referenced
  as documented port/register facts, not copied), doskutsu's detection
  approach (MIT, reimplemented independently), and Leah Neukirchen's
  `dosfetch.pas` (CC0). None of that changes `dinspect.exe`'s own CC0 status;
  see `dinspect`'s own `THIRD-PARTY.md` for the complete analysis.

## `usb4vc/PBFW_IBMPC_PBID1_V0_5_7.hex` — USB4VC IBM PC board firmware 0.5.7

The stock protocol-board firmware, kept as the **rollback image** for
vcctrl's patched build (`firmware/usb4vc-ibmpc/`). It is here and not
fetched because the rollback is needed at the bench, and the daemon host has
no internet. It is not read by anything at runtime. The only way it reaches
the board is by hand, with `firmware/usb4vc-ibmpc/pi-flash.py write`.

MIT, `LICENSE.usb4vc` (dekuNukem). Exact file from
https://github.com/dekuNukem/USB4VC `firmware/releases/` at `2e21505`,
sha256 `e2f2dd4bdb799ff9ed11b3b534c0095333df12a7cb4c94c2f0488c7b90602ad7`.
That file is byte-identical to Keil's own build output checked in at
upstream `ae3813d` (2023-07-02), which is the source revision vcctrl's
patched build starts from.

## The path

`daemon/vcctrld.py` puts this directory on `sys.path` itself rather than
relying on `PYTHONPATH`, so the daemon works the same when started by systemd,
by hand, or from a test. It is appended, not prepended: anything genuinely
installed on the host wins, and this is the fallback rather than an override.
