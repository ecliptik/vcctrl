#!/usr/bin/env python3
"""Probe, back up, or flash the USB4VC IBM PC protocol board. Runs ON THE PI.

    sudo python3 pi-flash.py probe            # bootloader answers? chip id
    sudo python3 pi-flash.py backup OUT.bin   # read the whole flash back
    sudo python3 pi-flash.py write FILE.hex   # write + verify, then PB INFO
    sudo python3 pi-flash.py info             # PB INFO only (no bootloader)

The bootloader entry and the stm32flash invocation are upstream's own, copied
from rpi_app's flash_fw.py / usb4vc_ui.py (dekuNukem/USB4VC, MIT): BOOT0 on
BCM 12, RESET on BCM 25, the STM32 ROM bootloader on I2C address 0x3b,
/dev/i2c-1. What this adds:

  - REFUSES while usb4vc.service is running. rpi_app owns the same GPIO and
    SPI lines, and keep_alive.py restarts it on exit, so it cannot simply be
    killed. Stop the service first; this never stops it for you.
  - ALWAYS leaves the bootloader (`finally`), so an error mid-way cannot
    leave the board held in reset.
  - `backup`, so the image actually on the board can be kept before anything
    replaces it. Upstream's release hex is the fallback if the chip is
    read-protected and the read fails.
  - `write` verifies (-v) and then asks the board for PB INFO over SPI, the
    same check flash_fw.py makes, printing the firmware version it reports.

A failed write leaves the application erased, NOT the bootloader. The
bootloader is in ROM, so running `write` again with a good image recovers
the board. The one thing that cannot be fixed from here is BOOT0/RESET not
being driven at all. Then `probe` fails and nothing was changed.
"""

import os
import subprocess
import sys
import time

PBOARD_RESET_PIN = 25
PBOARD_BOOT0_PIN = 12
I2C_DEV = "/dev/i2c-1"
I2C_ADDR = "0x3b"


def _service_active():
    r = subprocess.run(["systemctl", "is-active", "--quiet", "usb4vc"])
    return r.returncode == 0


def _gpio():
    import RPi.GPIO as GPIO
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    return GPIO


def enter_bootloader(GPIO):
    # upstream flash_fw.py enter_dfu(), verbatim in effect
    GPIO.setup(PBOARD_RESET_PIN, GPIO.OUT)
    GPIO.output(PBOARD_RESET_PIN, GPIO.LOW)
    time.sleep(0.05)
    GPIO.setup(PBOARD_BOOT0_PIN, GPIO.OUT)
    GPIO.output(PBOARD_BOOT0_PIN, GPIO.HIGH)
    time.sleep(0.05)
    GPIO.setup(PBOARD_RESET_PIN, GPIO.IN)
    time.sleep(1)


def exit_bootloader(GPIO):
    # upstream flash_fw.py exit_dfu()
    GPIO.setup(PBOARD_BOOT0_PIN, GPIO.IN)
    GPIO.setup(PBOARD_RESET_PIN, GPIO.OUT)
    GPIO.output(PBOARD_RESET_PIN, GPIO.LOW)
    time.sleep(0.05)
    GPIO.setup(PBOARD_RESET_PIN, GPIO.IN)
    time.sleep(0.5)


def pb_info():
    """PB INFO over SPI, as flash_fw.py reads it: request, then one NOP."""
    import spidev
    spi = spidev.SpiDev(0, 0)
    spi.max_speed_hz = 2000000
    try:
        spi.xfer([0xde, 0, 1] + [0] * 29)
        time.sleep(0.1)
        r = spi.xfer([0xde] + [0] * 31)
    finally:
        spi.close()
    if r[0] != 0xcd or r[2] != 128:
        return None, r
    return {"board_id": r[3], "hw_rev": r[4],
            "fw": "%d.%d.%d" % (r[5], r[6], r[7])}, r


def stm32flash(*args):
    cmd = ["stm32flash", "-a", I2C_ADDR] + list(args) + [I2C_DEV]
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd).returncode


def main(argv):
    if not argv or argv[0] not in ("probe", "backup", "write", "info"):
        sys.stderr.write(__doc__)
        return 2
    op = argv[0]
    if os.geteuid() != 0:
        sys.stderr.write("run with sudo: GPIO, SPI and I2C need root\n")
        return 2
    if _service_active():
        sys.stderr.write("refusing: usb4vc.service is running and owns the same "
                         "GPIO/SPI lines.\n  sudo systemctl stop usb4vc  first "
                         "(and start it again afterwards).\n")
        return 2
    if op == "info":
        info, raw = pb_info()
        print(info if info else "no PB INFO reply: %r" % (raw,))
        return 0 if info else 1
    if op in ("backup", "write") and len(argv) < 2:
        sys.stderr.write("%s needs a file\n" % op)
        return 2
    if op == "write" and not os.path.isfile(argv[1]):
        sys.stderr.write("no such file: %s\n" % argv[1])
        return 2
    if op == "backup" and os.path.exists(argv[1]):
        sys.stderr.write("refusing to overwrite %s\n" % argv[1])
        return 2

    GPIO = _gpio()
    rc = 1
    try:
        enter_bootloader(GPIO)
        if op == "probe":
            rc = stm32flash()
        elif op == "backup":
            rc = stm32flash("-r", argv[1])
        elif op == "write":
            rc = stm32flash("-v", "-w", argv[1])
    finally:
        exit_bootloader(GPIO)
        GPIO.cleanup([PBOARD_RESET_PIN, PBOARD_BOOT0_PIN])
    print("stm32flash exit code %d" % rc)
    if op == "write":
        time.sleep(1.0)
        info, raw = pb_info()
        print("PB INFO after write: %s" % (info if info else "NO REPLY %r" % (raw,)))
        if rc == 0 and not info:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
