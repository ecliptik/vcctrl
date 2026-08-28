# Accessibility (WCAG 2.1 AA) review of the web KVM

**Companion to [WEBKVM.md](./WEBKVM.md).** Covers both `daemon/kvm.html`
(full, read-write) and `daemon/kvm-ro.html` (read-only mirror, see
[WEBKVM-SCRUB.md](./WEBKVM-SCRUB.md) for why it is a separate hand-maintained
file rather than a build artifact).

## The review **[measured 2026-08-28, peer session `audit`]**

Run against the live pages -- the rig's tailnet host on `:443` (full) and its
`:8091` read-only mirror -- with chromium (puppeteer-core) + axe-core 4.x
(tags `wcag2a`/`wcag2aa`/`21a`/`21aa`/`best-practice`), plus manual
geometry/contrast probes and eyeballed screenshots, at two viewports:
desktop 1280x800 and a **genuine 390x844** taken via puppeteer's
device-metrics override. That distinction matters on this page specifically:
[headless chromium clamps a top-level viewport to 500px regardless of
`--window-size`](./WEBKVM.md); a device-metrics override (or an iframe) is
what gets a true 390.

**Limits, stated up front so the negative results aren't over-read:** both
pages were audited in the "connecting.../No Signal" veil state -- no live
stream was up during the scan. That means three things were **not** checked
and still want a manual pass with a stream running: the video-present layout,
focus-trapping inside the open Settings/other `role="dialog"` panels, and a
real keyboard tab-order walk. **All three closed out in the full re-run,
below.** Contrast (see Open, below) came back
`INCOMPLETE` from axe on both pages (4 nodes RO, 6 full -- the veil,
`#dimbtn`, `#savebtn` over the black video area) and needs a manual per-theme
check across the ~10 themes in `themes.css`; treat it as unverified, not
passed.

## Fixed, in this diff (both `kvm.html` and `kvm-ro.html`)

1. **No `lang` attribute.** Neither file had an explicit `<html>` element at
   all (bare `<!doctype html>` straight into `<meta>` tags, relying on the
   browser's implicit element). Added `<html lang="en">` after the doctype.
2. **Viewport blocked pinch-zoom.** `maximum-scale=1` on the viewport meta
   (WCAG 1.4.4/1.4.10) prevented low-vision users from zooming. Removed;
   `width=device-width,initial-scale=1,viewport-fit=cover` is unchanged.
3. **No landmark regions** (axe: `landmark-one-main` + `region` x4-7 across
   the two pages). `#stage` (the picture) is now a `<main>` rather than a
   `<div>` -- safe because every CSS rule and JS lookup addresses it by
   `#stage`, none by tag, and it is a **direct grid child of `#app`** at
   desktop width (`#app { grid-template-columns: ...; }` places `#stage` by
   ID, not by combinator), so retagging it doesn't touch layout. `#tabs`
   (the mobile view-switcher / bottom nav) got `role="navigation"
   aria-label="Panel view"`; `#body` (the Activity/Status rail) got
   `role="complementary" aria-label="Status and activity"`; `#cmdbar` (the
   type/file/send row, RO's read-only variant of the same bar) got
   `role="region" aria-label="Command bar"`. `<header>` was already a real
   `<header>` element and needed nothing.
4. **Command input had no accessible name.** `#line` (full page only --
   confirmed RO has no such input) relied on `placeholder="type a command"`,
   which is not a label and disappears once the user types. Added
   `aria-label="Command to type"`.
5. **Mobile tab bar carried no active-tab state to assistive tech**
   (`#tabs button.on` was CSS-only). The tab-switch handler now toggles
   `aria-current="true"` alongside the `.on` class; the initial SCREEN
   button carries it in the markup too, for first paint before the handler
   has run once.
6. **Status lamps were opaque to assistive tech.** `PWR`/`LNK`/`AUD`/`KBD`/
   `MOS` now carry `aria-label`s ("Power"/"Link"/"Audio"/"Keyboard"/"Mouse")
   on the outer `.lamp` span, which `setLamp()` never touches (it only sets
   `.title` and toggles classes), so the label survives every state change.
   **Not fully closed:** only the *name* is fixed. State changes on the
   individual lamps still aren't announced -- only the master `#state` span
   is `aria-live`. Explicitly not a use-of-color failure either way --
   `.lamp.warn`/`.lamp.bad`/`.lamp.stale` already carry non-color cues
   (`::after` glyph, dashed border).
7. **`#lamps` (the PS/2-LED button) tap target -- turned out not to be a
   defect.** Originally measured 43x16 by the audit, under the WCAG 2.5.8
   24x24 minimum. The page already had
   `@media (pointer:coarse) { #lamps { min-height:var(--tap) } }`
   (`--tap: 44px`) -- gated on a coarse (touch) pointer, which a real phone
   reports but which puppeteer's device-metrics override does not set
   unless `hasTouch`/`isMobile` are also passed. **[confirmed 2026-08-28,
   peer session `audit`, real touch emulation]:** re-run against the live
   deployed pages with `isMobile`/`hasTouch` set and verified
   (`matchMedia('(pointer:coarse)')` true, `innerWidth` a true 390, no
   500px clamp), measured with `getBoundingClientRect` --
   **`#lamps` is 31.2x44 under real touch.** Height is a proper 44 (the
   gated rule does fire); width 31.2px passes the WCAG AA 24x24 floor and
   only misses AAA's 44x44. The original 43x16 reading really was the
   [headless viewport-clamp class of artifact](./WEBKVM.md), confirmed, not
   a real gap. The unconditional `min-height:var(--tap)` fix landed anyway
   (operator reviewed it live, no visual regression) -- it's a harmless
   belt-and-braces change, not evidence the original reading was real.
8. **The ~23px log control -- real, found, and fixed.** **[confirmed
   2026-08-28, peer session `audit`, same real-touch sweep]:** it's the
   mobile tab bar's LOG button (`#tabs button[data-tab="log"]`,
   `kvm.html`/`kvm-ro.html`), measured **22.8x46 on both pages** -- correction
   to an earlier guess of the Activity toggle, which is `display:none` at
   390px and was never the culprit. 22.8px fails even the AA 24x24 floor.
   Root cause, found by reading the CSS rather than patched around: `#tabs
   button` declared `padding:0 6px` (added in the commit that switched the
   tab row from equal-width cells to natural-width-with-`space-between`, to
   fix visibly uneven gaps between short and long labels) immediately
   followed, a few lines later in the *same rule*, by a pre-existing
   `padding:0` that silently overrode it back to zero -- so that commit's
   own fix never actually took effect, on either page, since it was
   written. LOG (three letters, the shortest label in the row) was the one
   button small enough for the missing padding to cross the 24px line;
   longer labels' own glyph width covered for the bug. Fix: deleted the
   dead trailing `padding:0;`, restoring the `0 6px` the row was already
   supposed to have. Other sub-44px controls the sweep turned up
   (`#dbtn` 32x44, `#zoombtn`/`#gbtn` 38x44, Sound/Power 38x46, the
   scrub/frame row's `#dimbtn`/`#savebtn`/`#reviewbtn` at 32px tall) are
   all AA-pass/AAA-fail by the same margin as `#lamps` above and were left
   alone.

   **Verification status:** `audit` static-confirmed the fix (read the CSS
   in both files post-edit, single `padding:0 6px` with no override left)
   and the arithmetic (22.8px content + 6px each side ~= 34.8px, clears the
   24px AA floor) -- but could not re-measure it live, because `#tabs` is
   JS-populated and the sweep needs the deployed pages. **[deployed
   2026-08-28]:** `pi/deploy.sh --page` shipped `kvm.html`/`themes.css` (its
   normal safe path, no daemon restart); `kvm-ro.html` isn't wired into
   deploy tooling yet (see its service file's own note), so it was pushed
   by hand with the same atomic copy-then-`mv` pattern deploy.sh uses.
   Both live copies were read back over ssh and confirmed to carry the
   single `padding:0 6px` rule. Peer session `audit`, who ran the original
   touch-emulation sweep, went unreachable before it could re-measure the
   deployed pages, so no CDP/puppeteer remeasurement happened.

   **Closed instead by the operator, on a real phone, same day:** tapped
   the LOG tab on the deployed page and confirmed it takes the touch
   cleanly. No emulator, no `getBoundingClientRect` -- the actual test this
   whole finding was a proxy for. Device/browser weren't recorded beyond
   "mobile"; if this control ever regresses, a fuller repro (device model,
   OS, browser) would strengthen this note, but a real tap on a real phone
   already outranks another headless sweep for this specific question.

## Full re-run with real video present -- [measured 2026-08-28, this session]

The three gaps the original review named above -- video-present layout,
focus-trapping in open dialogs, a real keyboard tab-order walk -- closed out
together: axe-core against the live deployed pages with a real capture lock
(state `ACTIVE`, not the veil), plus a genuine keyboard-only Tab walk, since
axe does not test focus order or trap behavior at all.

**Method:** chromium (puppeteer-core) + axe-core 4.x, same tags as the
original review (`wcag2a`/`wcag2aa`/`wcag21a`/`wcag21aa`/`best-practice`),
against both live pages at both viewports (desktop 1280x800, genuine mobile
390x844 via device-metrics override, same distinction as the original
review), scanned in four states each -- base, Settings open, Sound popover
open, Zoom popover open -- 16 scan states total.
`page.setBypassCSP(true)` was needed for axe's own injected script to run at
all under the nonce'd CSP this session added; the pages' own CSP is not
itself part of what axe evaluates here.

**Found and fixed, four real issues, each confirmed by a before/after axe
diff:**

1. `aria-allowed-role` on `#settings`: `<aside role="dialog">` -- axe says
   the dialog role "must be removed... not allowed for the element."
   `<aside>`'s implicit role (complementary) doesn't permit an explicit
   override per ARIA-in-HTML. Retagged to `<div>` in both files; nothing
   selects on the tag itself, only `#settings` by ID, so no CSS or JS
   needed to change.
2. `aria-prohibited-attr` on four of the five header lamps (PWR/AUD/KBD/
   MOS): "aria-label attribute is not well supported on a span with no
   valid role attribute." A bare `<span>`'s implicit role is `generic`,
   which WAI-ARIA excludes from author-supplied naming outright. Gave
   each `role="img"` -- matches what they are (single labelled status
   glyphs), doesn't imply interactivity they don't have.
3. **The fifth lamp, `#lamp-link`, was a REAL WCAG 2.1.1 (Keyboard)
   failure, not an ARIA technicality: it has a working `onclick` (retries
   the WebSocket) with no keyboard path to trigger it at all.** Fixed with
   `role="button" tabindex="0"` plus an `onkeydown` handler (Enter/Space)
   beside the existing `onclick`, in both files. This is the one exception
   to "every clickable control is a real `<button>`" below -- converting
   the element itself would have meant unpicking the `.lamp` row's compact
   inline styling from the base `button, select` reset; the ARIA-widget
   pattern was the lower-risk fix for a control that already worked for a
   mouse.
4. `label-title-only` on `#btnstylesel` (both files) and `#bufsel`
   (`kvm.html` only -- no equivalent control on the read-only page): two
   `<select>`s relied solely on `title` for their accessible name, which
   axe correctly does not count as one. Both already sit beside a visible
   label span ("Show" / "Capture length") -- gave each span an `id` and
   pointed the select at it with `aria-labelledby`, rather than a second
   `aria-label` string that could drift from the visible text over time.

**Investigated and left alone, confirmed as false leads, not skipped:**

- `aria-valid-attr-value` [critical], `INCOMPLETE`, on `#filebtn`/
  `#keysbtn`/`#bufbtn` (`kvm.html` only): "Unable to determine if
  aria-controls referenced ID exists on the page." Checked by hand --
  `#pop-file`, `#pop-keys` and `#scrub` all genuinely exist in the DOM.
  Axe's own stated uncertainty, not a confirmed failure; most likely the
  `hidden` attribute on the referenced popovers confusing the reference
  check at scan time rather than anything wrong with the markup.
- `color-contrast`: unchanged from the original review, still
  `INCOMPLETE`, still the operator's own prior decision not to pursue
  (below) -- not re-litigated by this re-run.

**Result: zero confirmed violations across all 16 scan states** (2 pages x
2 viewports x 4 states) after the four fixes above, verified by re-running
axe against the live deployed pages a second time.

**Keyboard walk (axe cannot test this -- done separately, live, video
present):** opening Settings, Sound, Zoom, Keys or File left focus sitting
on `<body>` -- a keyboard user had to tab from the top of the page to reach
the panel they had just opened. None of this page's dialogs are modal
(`aria-modal` false or absent throughout, deliberately, so the picture
stays reachable while one is open), but a non-modal dialog still owes
whoever opened it a landing place, and closing one must not leave focus
stranded on a now-hidden element either. Fixed in both files: every
`[role="dialog"]` gets `tabindex="-1"` (focusable without joining the
normal Tab order); opening a dialog moves focus onto it; closing one
returns focus to the button that opened it, but ONLY if focus was still
actually inside it -- a panel closed programmatically while focus was
elsewhere (e.g. a resize) must not steal focus from wherever it already
was. Verified live, on the deployed pages: a 60-tab walk through the base
`kvm.html` page (real video, not the veil) found 18 unique targets cycling
cleanly with no trap and no hidden element ever focused; every dialog
tested individually (Settings, Sound, Zoom, Keys, File) landed focus on
open and returned it to the trigger on both Escape and backdrop-click.

**Not done here, and still open if full re-certification matters before
going public:** this re-run answers the three specific gaps the original
review named, not a second full manual sweep of every control -- see Open,
below, for what is still genuinely outstanding.

## Open

- **Per-lamp state announcements** (the second half of #6 above): folding
  individual lamp changes into an `aria-live` summary, or deciding that's
  overkill next to the existing master `#state` announcement. Needs a
  wording/frequency call, not just a markup change -- see
  `vcctrl-webkvm-copy` conventions before touching this.
- **Contrast** (axe returned `INCOMPLETE`, not pass/fail, on both pages) --
  operator has said not to pursue this; left unverified by design, not
  oversight.

## `kvm-ro.html` is a shared, untracked file -- a note on how this went

Mid-review, peer session `audit` ran an unrelated security-audit task that
rewrote `kvm-ro.html` wholesale (stripped all comments for the public-served
file, ~260KB to 108KB, plus two reworded visible strings) while this
review's edits were also live in the same untracked file. `audit` verified
before reporting back that every a11y edit above survived their rewrite
(`lang`, the `<main>`/landmark roles, the zoomable viewport, all 20
aria-label/aria-current attributes) and flagged the collision so it could be
independently re-confirmed rather than assumed -- it was, by re-reading the
file after their rewrite (see grep evidence: all landmarks, `aria-label`s,
and the unconditional `#lamps` rule present at their expected lines). No
edits were lost. Recorded here because it is exactly the shared-working-tree
hazard this repo has been bitten by before, and it went cleanly only because
the other session said so before either side found out the hard way.

## What's already right, worth not regressing

- Nearly every clickable control is a real `<button>` (59 in the full page,
  29 in RO), so keyboard operability comes free. One exception as of the
  full re-run above: `#lamp-link`, a `span role="button" tabindex="0"` with
  its own `onkeydown` -- converting it to a real `<button>` would have meant
  unpicking the `.lamp` row's compact inline styling from the base
  `button, select` CSS reset; ARIA-widget was the lower-risk fix for one
  already-working control, not a pattern to reach for by default.
- Global `:focus-visible { outline:2px solid var(--blue); outline-offset:2px }`.
  Only `#line` overrides its own outline, and `#linewrap:focus-within`
  changing the wrapper border compensates.
- `prefers-reduced-motion: reduce` is honored
  (`* { animation:none!important; transition:none!important }`), and
  `prefers-contrast: more` is handled.
- Menus carry `aria-haspopup`/`aria-expanded` (26 full / 16 RO); popovers are
  `role="dialog"` with `aria-label`, one `aria-modal`.
- Every `<img>` has `alt`; exactly one `<h1>`; the master status lamp is
  `aria-live="polite"`.
- Responsive reflow passes WCAG 1.4.10: `overflowX` measured 0 at 390px on
  both pages, no horizontal scroll; the desktop side panel collapses cleanly
  to the mobile tab bar.
