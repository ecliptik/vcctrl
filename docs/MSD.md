# USB mass storage: mounting a disk image onto a target

Written 2026-09-02, alongside `MsdCapability` (`daemon/vcctrld.py`) and the
gadget script's `mass_storage.usb0` function
(`pi/files/vcctrl-hid-gadget-setup.sh`). WP4 of
`internal/KVM-MACHINES-PLAN.md`.

## 1. What this is, and what it is not

A `hdmi-usb`-kind machine (today: `modernpc`) is reached over the Pi's own
USB-C port acting as a HID keyboard/mouse gadget
(`pi/files/vcctrl-hid-gadget-setup.sh`). The same gadget can present a THIRD
function: `mass_storage.usb0`, one removable LUN, so the target sees a USB
drive the daemon controls.

**A copy, not a live share.** USB mass storage is block-level: whichever
side has the image open owns the filesystem. Mounting an image copies
nothing to the target — it presents the image's bytes as a block device —
but building an image (`msd_build`) DOES copy bytes, from a source
directory into the image, once, at build time. There is no live
sync between a directory on the daemon host and a mounted image.

**Never a re-enumeration.** The LUN exists on the gadget from boot, with no
file assigned (`lun.0/file` empty = no medium). Mounting or ejecting is a
configfs write to that one file — the same mechanism a card reader uses to
report "no medium" until you insert something — so the target's USB stack
sees the drive the whole time, medium present or not. The only thing that
re-enumerates the gadget at all is rebuilding it
(`pi/install.sh --hid-gadget-only`), which is why the mass-storage and
absolute-pointer functions were added in the SAME rebuild as the plan
required: one re-enumeration for both, not two.

## 2. The image library

Lives on the **daemon host's own disk**
(`capabilities.msd.settings.image_dir`, default
`/var/lib/vcctrl/msd/images`), not on the target and not in the repo. Two
kinds, by extension:

    .img, .vfat   presented as a drive (mode: drive)
    .iso          presented as a cdrom (mode: cdrom)

Getting an image into the library:

- **Upload through the browser** — the Disk popover's "Upload image", or
  `vcctrl msd stage <local-path> [name]` from the control host. Chunked
  (4 MiB, matching `FilesCapability.STAGE_CHUNK_MAX`) and sha256-verified
  end to end, same discipline as `stage-file`/`_file_stage` — the one real
  difference is no DOS 8.3 name conversion, because nothing here has to be
  opened by a DOS target.
- **`scp` a finished image directly** into `image_dir` on the Pi. Simplest
  path if the image already exists somewhere.
- **`vcctrl msd build <source_dir> <name> [fat|iso] [--label L]`** — builds
  a FAT image (`mkfs.vfat` + `mtools mcopy -s`, recursive) or an ISO9660
  image (`xorriso -as mkisofs -J -R`) from a directory ALREADY on the
  daemon host's disk. `source_dir` has to already be there (get it there
  with `scp` first) — a browser cannot hand the daemon a whole directory
  tree, only individual files, which is why upload and build are two
  separate commands rather than one "upload a folder" feature.

`msd list` reports free space on the image library's filesystem alongside
every image's name/size/kind, so a build or upload can be judged against
it before it starts — the same "the cost is part of the choice" rule the
capture-length menu already follows.

## 3. Commands

    msd list                  images, sizes, kind, free space
    msd status                what is mounted right now
    msd mount <image> [drive|cdrom] [--ro]
    msd eject
    msd stage <path> [name]   chunked upload into the library
    msd build <source_dir> <name> [fat|iso] [--label L]

`cdrom` defaults read-only; `drive` defaults writable — override either way
with `--ro`. Mounting always detaches first, then sets `cdrom`/`ro`, then
attaches the new file — the kernel's mass-storage function refuses a
`cdrom`/`ro` change while a file is already attached, so the write ORDER is
load-bearing, not a style choice (see `MsdCapability._msd_mount`'s own
comment).

## 4. `why: host_busy`, and what it actually means

The kernel refuses a `lun.0/file` write with `EBUSY` while the connected
host (the target machine, e.g. modernpc) has the drive open with removal
prevented (`SCSI PREVENT_ALLOW_MEDIUM_REMOVAL`, set by the target's OS when
it mounts the filesystem) — the same thing a real USB drive does if you
try to "eject" it in configfs while the OS still has it mounted. `msd_mount`
and `msd_eject` both report this rather than forcing anything: the daemon
does not un-mount the filesystem on the TARGET side, which it has no
channel to do for an `hdmi-usb`-kind machine (no keyboard/mouse-shaped way
to tell a Linux/Windows box "eject your drive first"). Eject it on the
target, then retry.

## 5. Absolute pointer, mentioned here because it shares the rebuild

`hid.mouse_abs` (buttons + 16-bit X/Y + a relative wheel, the same
descriptor shape QEMU's `usb-tablet` uses) was added to the gadget in the
SAME rebuild as mass storage, per the operator's own reasoning: one
re-enumeration for two features rather than two. **No daemon command uses
it yet** — `machine.mouse: absolute` names a mode `mouse_abs` would drive,
and nothing sends that command today (WP3/WP6 built the relative path
only; both machines this rig runs are `mouse: relative`). Treat
`/dev/hidg2` and the `hid.mouse_abs` function as present-but-unused until
that command exists.

## 6. What is measured, and what is not

**Measured 2026-09-02**, against a plain directory standing in for the
configfs LUN (not the real gadget) — `MsdCapability`'s own logic: list,
mount, eject, status, the path-traversal guard on both `image` and
`source_dir`/`name`, `msd_build`'s FAT and ISO output actually read back
correctly with `mdir`/`xorriso -find` (real `mkfs.vfat`/`mtools`/`xorriso`,
not mocked), and `msd_stage`'s chunking/resume/sha-mismatch/name-taken
paths. See `tests/test_core.py`'s `test_msd_capability_*` and
`test_msd_stage_*`.

**Not yet measured**: the real gadget's `mass_storage.usb0` function
against real hardware — whether modernpc's OS actually sees the drive
appear/disappear on mount/eject, whether `host_busy` is observed for real
rather than only reasoned about from kernel documentation, and whether a
built FAT/ISO image actually mounts cleanly on modernpc's own Linux desktop.
This section gets a "measured" entry the first time that happens, per this
repo's own rule that a measurement is filed in `docs/`, not left standing
as an assumption.
