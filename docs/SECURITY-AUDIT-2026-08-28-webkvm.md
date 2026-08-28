# Web KVM security audit — 2026-08-28 (pre-release)

> Rig identifiers are redacted in this file on purpose. The real hostname,
> LAN addresses and plug alias live in the untracked `vcctrl.yaml`; this is a
> tracked docs record and the identifier guard
> (`tests/test_core.py::test_no_rig_identifiers_in_the_code`) scans every
> tracked file, so the findings below name the *class* of each value ("the
> plug's LAN address"), never the literal. That is also better practice for a
> security document — it should not be the place the secrets are written down.

Scope: the browser-facing KVM stack, in preparation for release of the public
read-only mirror. Reviewed the code (`daemon/kvm.html`, `daemon/kvm-ro.html`,
`daemon/vcweb.py`, `daemon/vcweb_public.py`, `pi/files/vcctrl-web-public.service`,
`vcctrl.example.yaml`) and both live endpoints over the tailnet: the public
read-only mirror on port 8091 (`vcweb_public.py`) and the private control KVM on
443 (`vcweb.py`).

Conditions: probed 2026-08-28 from a tailnet-connected host. No SSH to the Pi
was available from the audit host, so `tailscale serve/funnel` state was
inferred from public DNS (no public A record for the rig hostname) rather than
read from the box directly — worth a direct `tailscale funnel status` confirm
before anything is made public.

## What is sound (verified, not assumed)

- **The isolation boundary of the public mirror holds under live probing.**
  `POST /cmd` returns **501** (there is no `do_POST` on the public handler at
  all — the stock `BaseHTTPRequestHandler` answers). `GET` of `/ws`, `/config`,
  `/public.json`, `/buffer.avi`, `/pulled`, `/keymap.json`, `/timeline.json`
  all return **404**. The input-carrying `/ws` channel is genuinely absent; the
  only websocket the public process serves is `/wsaudio`, which is output-only.
- **Public state is whitelisted, not passed through.** `_filter_public_state`
  drops the plug host/model, the FTP host:port and DOS paths, and the Pi host
  facts. Confirmed against the live `/state.json`: none of those appear.
- **Typed text is redacted.** `_redact_public_events` replaces the `detail` of
  `type` events with `[redacted]`; the live `/events` feed shows only
  `shot`/`lastgood`/`led_changes` with empty detail.
- **Not funneled.** No public-DNS A record for the hostname; reachable over the
  tailnet only, matching the systemd unit's stated posture.

## Findings (all six implemented 2026-08-28, uncommitted for review)

### F1 — Private `/state.json` was `Access-Control-Allow-Origin: *` and carries LAN topology (medium)

`daemon/vcweb.py` `do_GET` served `/state.json` with `ACAO: *`. The live body
includes the smart plug's LAN address and hardware model (the exact device that
mains-cycles the target), the file-transfer server's LAN address:port with its
DOS `dest`/`out_dir` paths, and the full Pi host block (model, kernel, mem/disk,
load). The inline comment claimed "Read-only, tailnet-only, no secrets," but
`ACAO: *` widened the reader from "anyone who can reach the tailnet host" to
"**any origin's JavaScript running in a browser that can reach the tailnet
host**." A random site opened in a tailnet-connected browser could `fetch()`
this cross-origin and exfiltrate the rig's LAN topology.

The `*` existed only for the page's cross-port reachability probe (`:443` page
checking `:8443`). The page's normal same-origin polling needs no CORS, so
narrowing does not break rendering.

**Fix implemented:** `_state_cors()` reflects the request `Origin` + adds
`Vary: Origin` only when the Origin host equals the request's own Host-header
host (same host, any port); otherwise no ACAO header is sent. Unit-checked:
the `:443 → :8443` probe is allowed, a third-party origin is denied, a
no-Origin poll gets no header.

### F2 — Public mirror leaked an internal git commit SHA (low)

Live public `/state.json` carried a `build` field holding the repo's current
git commit SHA — internal state with no public purpose.

**Fix implemented:** `build` removed from the passthrough set in
`_filter_public_state` (kept: `viewers`, `listeners`, `inflight`, `note`,
`led_log`).

### F3 — Public mirror sent no hardening headers (low)

`Handler._send` in `vcweb_public.py` set only `Content-Type`, `Content-Length`,
`Cache-Control`.

**Fix implemented:** added `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, and `Content-Security-Policy: frame-ancestors
'none'` to `_send` (covers the HTML page, state.json, images), plus nosniff +
frame-ancestors on the `_mjpeg` path. `frame-ancestors` blocks the page being
embedded to impersonate the feed and is unaffected by the page's inline
`<script>`/`<style>`. A full `default-src`/`script-src` CSP is deferred: the
inline-heavy page (not a build artifact) would need serve-time nonces.

### F4 — kvm-ro.html shipped ~2,124 comment lines, ~80 leaking internal detail (medium — primary release item)

The public page is served verbatim. ~45% of its 4,869 lines were comments, and
~80 leaked internal specifics: server module/function names, the private
control KVM and its removed `/cmd`/`/ws`/scrub surface, the sweep/cell/collect
harness, internal doc paths and finding numbers, host/hardware detail (Pi
model, tailnet daemon CPU figures, capture-stick fps, LAN latency, target board
names), operator decisions, and a running bug/dev-history narrative.

**Decision (operator, 2026-08-28):** strip **all** comments from the served
`kvm-ro.html`, after harvesting any genuine measurements (with conditions) not
already recorded in `docs/` or `kvm.html`. `kvm.html` (private, tailnet-only)
keeps its comments as the maintained record.

**Fix implemented:** a tokenizer-based stripper (kept in gitignored `internal/`)
that understands JS strings/template-literals/regexes removed all 1,365 comment
tokens (4,869 → 2,745 lines), verified byte-exact against
original-minus-comment-spans and with `node --check` on the stripped script.
**Harvest:** of the 92 kvm-ro-only comments, the 5 containing digits are
fork-divergence rationale (breakpoints, gap px), not measurements; every
genuine measurement is confirmed still present in `kvm.html`, so nothing needed
harvesting to `docs/`.

### F5 — Two visible (rendered) leaks in kvm-ro.html (low)

Not caught by a comment strip, because they are rendered text: a settings hint
using the internal word "harness", and the Status panel heading naming the
internal inventory tool ("DOS System (dinspect)"). The DOS hardware inventory
itself is intentionally public (it is in the public `sysinfo.fields`); only the
internal tool name was the problem.

**Fix implemented:** hint reworded to drop "harness" (now describes automated
checks that periodically test whether the machine responds); heading relabeled
to "DOS System". Field values untouched.

### F6 — `Server:` header revealed the Python version (informational)

**Fix implemented:** `Handler.sys_version = ""` on the public process drops the
version sub-string from the `Server` header.

## Not findings (design decisions confirmed sound)

- The public plug alias and target board label are retained deliberately —
  human labels for a "watch it work" feed, not network identifiers.
- Tailnet-with-no-auth on the private daemon is the intended trust model
  (`web.bind: 127.0.0.1` + proxy as the auth boundary). F1 was about CORS
  widening that boundary, not the model itself.
- The public DOS `sysinfo.fields` (CPU/memory/video/sound) are intentionally
  public.
