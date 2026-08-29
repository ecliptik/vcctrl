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

## `ogg-opus-decoder-1.7.5.min.js`

The browser-side Opus decoder for the public mirror's audio stream
(`daemon/vcweb_public.py` serves it; `daemon/kvm-ro.html` loads it). One
self-contained UMD file, WASM embedded -- exposes
`window["ogg-opus-decoder"].OggOpusDecoder`, which decodes an Ogg Opus byte
stream fed to it in arbitrary pieces. WASM rather than WebCodecs because the
operator's compatibility bar is Safari/Firefox/Chrome on desktop AND mobile,
and as of 2026-08 WebCodecs audio is absent from Firefox for Android
entirely and from Safari before 26.

MIT. Provenance, exactly:

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
permissive notice in its header — Sam Rushing, 1996 — which is the full licence
text for those files, so there is no separate file to keep beside them.

## The path

`daemon/vcctrld.py` puts this directory on `sys.path` itself rather than
relying on `PYTHONPATH`, so the daemon works the same when started by systemd,
by hand, or from a test. It is appended, not prepended: anything genuinely
installed on the host wins, and this is the fallback rather than an override.
