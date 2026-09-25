# Third-party notices

`LICENSE` (MIT) covers vcctrl's own code and documentation. It does not cover
`vendor/`, which carries other people's code under their own licenses, kept
in separate files on purpose so this top-level license is never read as
extending to them. This file is the index; each `vendor/README.md` section
and `vendor/LICENSE.*` file has the full detail and provenance.

| Component | License | Notice file |
|---|---|---|
| `vendor/pyftpdlib/` 2.2.0 (Giampaolo Rodolà) | MIT | `vendor/LICENSE.pyftpdlib` |
| `vendor/asyncore.py`, `vendor/asynchat.py` (Sam Rushing / CPython stdlib) | Rushing notice + PSF License Agreement | `vendor/LICENSE.asyncore` |
| `vendor/ogg-opus-decoder-1.7.5.min.js` (Ethan Halsall) | MIT | `vendor/LICENSE.opus` |
| — its compiled-in libopus (Xiph.Org and contributors) | BSD-3-Clause | `vendor/LICENSE.opus` |
| `vendor/dinspect.exe` ([dinspect](https://github.com/ecliptik/dinspect)) | CC0 1.0 Universal | see `vendor/README.md` |
| — its statically linked Open Watcom C/C++ runtime | Open Watcom Public License 1.0 | see `vendor/README.md` |
| `vendor/usb4vc/PBFW_IBMPC_PBID1_V0_5_7.hex` ([USB4VC](https://github.com/dekuNukem/USB4VC) protocol-board firmware, dekuNukem) | MIT | `vendor/LICENSE.usb4vc` |
| `firmware/usb4vc-ibmpc/` (build files + local patches against USB4VC firmware; the source itself is fetched, not carried) | MIT (USB4VC); fetched ST startup file Apache-2.0 | `vendor/LICENSE.usb4vc`; see `firmware/usb4vc-ibmpc/README.md` |
| `tools/patch-usb4vc-*.py` (anchor strings only, from [USB4VC](https://github.com/dekuNukem/USB4VC) by dekuNukem) | MIT | credited in each file's header |
| `daemon/themes.css` colour values (from published base16/community schemes) | MIT (each scheme) | credited in `tools/themes.py` |

Nothing in this repository is GPL-licensed or otherwise copyleft. Where a
tool above (USB4VC) or a reference cited in a sibling repository's own
documentation (PicoGUS, consulted by `dinspect`, not by anything in this
repository) is GPL, this repository contains no copy of its code -- only
independently written interoperation code, credited above.
