#!/usr/bin/env bash
# Builds the USB HID keyboard+mouse+absolute-pointer gadget, and a mass
# storage function with one removable LUN, on the Pi's own USB-C port
# (dwc2 in peripheral mode) via configfs. Independent of vcctrld on purpose --
# same reasoning as USB4VC's own SPI/STM32 setup: vcctrld only ever talks to
# whatever kernel interface already exists (uinput there, /dev/hidg* here),
# it does not bring that interface into being itself.
#
# Idempotent: safe to re-run (systemd will, on every boot). Tears down any
# existing gadget of the same name first rather than erroring on a stale one.
#
# Requires dtoverlay=dwc2,dr_mode=peripheral under [pi5] in
# /boot/firmware/config.txt (this script does not add it -- that line needs
# a reboot to take effect, which is not something a boot-time unit should be
# the one deciding to do; pi/install.sh's install_hid_gadget() checks for it
# and tells the operator if a reboot is still owed).
set -euo pipefail

GADGET_NAME=vcctrl-hid-km
G="/sys/kernel/config/usb_gadget/$GADGET_NAME"

modprobe libcomposite

if [ -d "$G" ]; then
    echo "" > "$G/UDC" 2>/dev/null || true
    rm -f "$G"/configs/c.1/hid.keyboard "$G"/configs/c.1/hid.mouse \
        "$G"/configs/c.1/hid.mouse_abs "$G"/configs/c.1/mass_storage.usb0
    rmdir "$G"/configs/c.1/strings/0x409 "$G"/configs/c.1 \
        "$G"/functions/hid.keyboard "$G"/functions/hid.mouse \
        "$G"/functions/hid.mouse_abs "$G"/functions/mass_storage.usb0 \
        "$G"/strings/0x409 2>/dev/null || true
    rmdir "$G" 2>/dev/null || true
fi

# Only one gadget can be bound to a UDC at a time, and it does not have to
# be one this script created -- found the hard way redeploying over an
# ad hoc manual test gadget left bound from earlier bring-up: this script's
# own teardown above only knew how to unbind a PRIOR COPY OF ITSELF, so it
# built a fresh gadget tree correctly and then failed at the last step
# ("Device or resource busy") writing to a UDC something else still held.
# Unbind whatever currently claims the UDC this gadget is about to use,
# regardless of that gadget's name.
for udc_file in /sys/kernel/config/usb_gadget/*/UDC; do
    [ -e "$udc_file" ] || continue
    if [ -s "$udc_file" ]; then
        echo "" > "$udc_file" 2>/dev/null || true
    fi
done

mkdir -p "$G"
cd "$G"

# pid.codes (0x1209) test VID, same convention as capabilities.input.settings'
# 0x1209:0xDEA1/0xDEA2 for the existing USB4VC uinput identity -- one shared
# convention for every virtual input device this project presents, real or
# gadget.
echo 0x1209 > idVendor
echo 0xdea3 > idProduct
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

mkdir -p strings/0x409
echo "vcctrl-hid-km-0001"  > strings/0x409/serialnumber
echo "vcctrl"              > strings/0x409/manufacturer
echo "vcctrl HID KM gadget" > strings/0x409/product

mkdir -p configs/c.1/strings/0x409
echo "km" > configs/c.1/strings/0x409/configuration
echo 250  > configs/c.1/MaxPower

# --- keyboard: standard 8-byte boot-protocol report ---
mkdir -p functions/hid.keyboard
echo 1 > functions/hid.keyboard/protocol
echo 1 > functions/hid.keyboard/subclass
echo 8 > functions/hid.keyboard/report_length
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' > functions/hid.keyboard/report_desc

# --- mouse: standard 4-byte relative report (buttons, X, Y, wheel) ---
mkdir -p functions/hid.mouse
echo 2 > functions/hid.mouse/protocol
echo 1 > functions/hid.mouse/subclass
echo 4 > functions/hid.mouse/report_length
printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x03\x81\x06\xc0\xc0' > functions/hid.mouse/report_desc

# --- absolute pointer: buttons + X/Y as 0..32767 + a relative wheel, one
# 6-byte report (1 button byte, 2 X, 2 Y, 1 wheel) -- the same shape QEMU's
# usb-tablet uses, which is what makes an absolute HID mouse interoperable
# without a driver: every OS this rig targets (Windows, Linux, macOS) already
# has generic support for exactly this descriptor shape. NOT boot-protocol
# (protocol/subclass 0) -- absolute position has no boot-protocol
# equivalent, and nothing here needs the BIOS/bootloader mouse path the
# keyboard's boot protocol exists for.
mkdir -p functions/hid.mouse_abs
echo 0 > functions/hid.mouse_abs/protocol
echo 0 > functions/hid.mouse_abs/subclass
echo 6 > functions/hid.mouse_abs/report_length
printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x16\x00\x00\x26\xff\x7f\x75\x10\x95\x02\x81\x02\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x01\x81\x06\xc0\xc0' > functions/hid.mouse_abs/report_desc

# --- mass storage: one removable LUN, no backing file yet. "Mount" is a
# later configfs write to lun.0/file (see MsdCapability in vcctrld.py) --
# present from boot, exactly like the two HID functions above, so attaching
# or ejecting a disk image is never a USB re-enumeration once this script
# has run once. removable=1 so the host (modernpc) treats an empty LUN as
# "no medium" rather than an I/O error, the same as a real card reader with
# nothing inserted.
mkdir -p functions/mass_storage.usb0/lun.0
echo 1 > functions/mass_storage.usb0/lun.0/removable

ln -sf "$G/functions/hid.keyboard" configs/c.1/hid.keyboard
ln -sf "$G/functions/hid.mouse" configs/c.1/hid.mouse
ln -sf "$G/functions/hid.mouse_abs" configs/c.1/hid.mouse_abs
ln -sf "$G/functions/mass_storage.usb0" configs/c.1/mass_storage.usb0

# The UDC can take a moment to enumerate right after boot even though dwc2
# itself probed at kernel init -- sysfs population and this script's own
# start are two different clocks. Retry rather than fail on the first empty
# read (measured: bounded to a few seconds, not systemd's own service
# timeout, so a genuinely absent UDC still fails loudly).
UDC_NAME=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    UDC_NAME="$(ls /sys/class/udc 2>/dev/null | head -1)"
    [ -n "$UDC_NAME" ] && break
    sleep 1
done
if [ -z "$UDC_NAME" ]; then
    echo "vcctrl-hid-gadget-setup: no UDC found in /sys/class/udc -- is dtoverlay=dwc2,dr_mode=peripheral in /boot/firmware/config.txt, and has the Pi been rebooted since it was added?" >&2
    exit 1
fi
echo "$UDC_NAME" > UDC

for _ in 1 2 3 4 5; do
    [ -e /dev/hidg0 ] && [ -e /dev/hidg1 ] && [ -e /dev/hidg2 ] && exit 0
    sleep 0.5
done
echo "vcctrl-hid-gadget-setup: bound to UDC $UDC_NAME but /dev/hidg0 / /dev/hidg1 / /dev/hidg2 did not all appear" >&2
exit 1
