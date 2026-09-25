# USB4VC IBM PC protocol-board firmware: vcctrl's local build

A GCC build of [USB4VC](https://github.com/dekuNukem/USB4VC)'s IBM PC
protocol-board firmware (STM32F072C8), plus local patches. Not offered
upstream; that is the operator's decision. The problem it exists for is
`docs/MOUSE.md` sec. 10. Stock firmware abandons a PS/2 mouse packet when the
host inhibits the clock mid-byte and counts nothing, so a lost click has no
witness anywhere.

## What is here

| File | What |
|---|---|
| `fetch.sh` | Fetches the two inputs, **pinned**: upstream `firmware/ibmpc` at `ae3813d`, and ST's GCC startup file (sha256-checked). |
| `Makefile` | `make VARIANT=stock` builds upstream unmodified. `make VARIANT=counters` applies `patches/*.patch` first. Output: `build/<variant>/ibmpc.hex`. |
| `STM32F072C8_FLASH.ld` | GCC linker script. ST ships none for this part; upstream builds with Keil. |
| `gcc_compat.c` | `_write()` → upstream's `fputc()`, so the SPI-error dump still reaches USART1 under newlib. Built into every variant. |
| `patches/0001-mouse-delivery-counters.patch` | Counters only. Changes nothing that is sent. Reports as **0.5.107**. The header documents the SPI layout. |
| `pi-flash.py` | Runs on the daemon host: `probe` / `backup` / `write` / `info`. Upstream's own bootloader entry and `stm32flash` call, plus guards. |
| `../../vendor/usb4vc/PBFW_IBMPC_PBID1_V0_5_7.hex` | The stock release: the **rollback image**. |

The rpi_app half is `tools/patch-usb4vc-mousestats.py`. The daemon half is
`vcctrl mouse-stats`, plus `mouse.dropped` rows in `vcctrl input-log`.

## Why `ae3813d` and not master

The stock release hex is byte-identical to Keil's own output checked in at
`ae3813d` (2023-07-02). Master adds `aa5f90b` (2023-08-30, extended codes in
keyboard scan-code set 1), which no release ever shipped. Building from
master would change the keyboard as a side effect of a mouse change, on the
board the harness types through.

## Build

Needs `gcc-arm-none-eabi` and `libnewlib-arm-none-eabi` (Debian). Built on
the control host; the daemon host has no internet.

    ./fetch.sh
    make VARIANT=stock
    make VARIANT=counters

Verified 2026-09-25, arm-none-eabi-gcc 14.2.1:
- both variants build with **no compiler warnings**;
- `make clean` + rebuild reproduces both hashes;
- stock-GCC: 28,232 B flash of 64 KB; counters: 28,636 B;
- the linker's "_close/_read... not implemented" notes are newlib's nosys
  stubs and are expected.

A GCC image is **not** byte-comparable to the Keil release (different
compiler, and microlib versus newlib-nano). Only running it on the board
can show equivalence, which is why `stock` exists: flash it first, and any
misbehaviour is the compiler rather than the patch.

## On the board: 2026-09-25, operator at the bench

Flashed 15:04-15:08Z, following the runbook below, with the Gateway off:

- **`probe` works from the Pi 5.** BOOT0/RESET are driven through the same
  RPi.GPIO shim rpi_app uses, and the ROM bootloader answered on I2C 0x3b:
  device ID 0x0448 (STM32F07x), bootloader 0x10. `stm32flash`'s
  "serial_posix ... Not a tty" lines are it trying serial first, and are
  harmless.
- **`backup` read all 128 KB, with no read protection.** The first 18,236
  bytes were **byte-identical to upstream's stock 0.5.7 release**, and the
  rest was erased (all 0xFF). So the board ran exactly the vendored rollback
  image, and that rollback is exact. The backup is on the Pi at
  `~claude/usb4vc-fw/board-backup-20260925.bin`, sha256 `475df17a…`.
- **stock-GCC** wrote and verified, PB INFO 0.5.7. rpi_app came up on it,
  and `vcctrl board` showed the IBM PC board from the live status file.
- **counters** wrote and verified, PB INFO **0.5.107**, and board.json
  `fw_ver` [0, 5, 107].
- With the rpi_app patch applied, `vcctrl mouse-stats` returned
  `supported: true` with all counters 0. Two 1-count `mouse move`s then gave
  `ev_in` 2 and `pkt_built` 0. The board's PS/2 port reads as absent while
  the Gateway is off, so the events wait in the board's 16-deep queue, to be
  sent or discarded at the next power-on.

**Not yet checked on the board:** keyboard, LEDs and mouse with the Gateway
powered. That needs the operator's approval to power on.

## Flashing: the bench runbook

Only with the operator at the bench and nothing else using the Gateway. The
Pi's `/home/pi/usb4vc/firmware/` is USB4VC's own OLED-menu update folder;
**never** put an image there. The OLED menu flashes anything in it whose
version is newer than the board's. Stage images elsewhere (below).

The IBM PC board's bootloader is the STM32 ROM bootloader, reached over I2C
(address 0x3b on `/dev/i2c-1`) by driving BOOT0 (BCM 12) and RESET (BCM 25).
No USB cable. `stm32flash` is installed on the Pi from Debian's arm64
package.

1. **Preconditions.** The Gateway is off, the input lock is free,
   `vcctrl activity` shows nothing in flight, and every peer session has
   been told. Deploy vcctrl first, so `vcctrl mouse-stats` exists.
2. `sudo systemctl stop usb4vc`. rpi_app owns the same GPIO/SPI lines, and
   `pi-flash.py` refuses while it runs. Nothing reaches the target while
   the service is stopped.
3. `sudo python3 pi-flash.py probe`: the chip should answer. If it does not,
   stop here. Nothing was changed; start usb4vc again.
4. `sudo python3 pi-flash.py backup ~/usb4vc-fw/board-backup-YYYYMMDD.bin`
   keeps what is actually on the board. It may fail if the chip is
   read-protected. Then the vendored stock hex is the rollback.
5. `sudo python3 pi-flash.py write ~/usb4vc-fw/stock-gcc.hex`. PB INFO
   should read 0.5.7. `sudo systemctl start usb4vc`, then check:
   `vcctrl board`, `vcctrl verify-input`, LED changes, and a mouse click
   at a DOS program.
6. `sudo systemctl stop usb4vc`, then
   `sudo python3 pi-flash.py write ~/usb4vc-fw/counters.hex`. PB INFO
   should read **0.5.107**.
7. `sudo python3 ~/vcctrl-src/tools/patch-usb4vc-mousestats.py`, then
   `sudo systemctl start usb4vc`. `vcctrl mouse-stats` should say
   `supported: true`, and `ev_in` / `pkt_ok` should rise with each click.
   Check the keyboard and LEDs again.

**Rollback, at any step:** stop usb4vc, then
`sudo python3 pi-flash.py write ~/usb4vc-fw/PBFW_IBMPC_PBID1_V0_5_7.hex`,
then start usb4vc. A failed write erases the application, not the ROM
bootloader, so writing again with a good image always recovers the board,
provided `probe` worked.
