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
real keyboard tab-order walk. Contrast (see Open, below) came back
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

- Every clickable control is a real `<button>` (59 in the full page, 29 in
  RO; zero `div`/`span role="button"`), so keyboard operability comes free.
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
