; RDYPULSE.COM -- DOS-ready signal for the vcctrl harness.
;
; Sets the Scroll Lock LED via the 8042 keyboard controller. The harness clears
; Scroll Lock before rebooting, so a 0 -> 1 transition means: CONFIG.SYS done,
; every AUTOEXEC TSR loaded, prompt live. Measured on the g2k as ~11 s after the
; POST edge, which is exactly the window in which typing would corrupt things.
;
; Why Scroll Lock: NumLock is the BIOS's at POST and Caps Lock is the harness's
; reboot detector, so Scroll Lock is the only lock key left un-aliased.
;
; Two details that make this safe rather than merely working:
;
;   * The BDA keyboard-flags byte at 0040:0017 is updated as well as the LED.
;     The BIOS re-derives LED state from that byte on keyboard activity, so an
;     LED set only through the 8042 would be reverted by the next keypress --
;     and in this rig the next keypress is the harness's own.
;
;   * Both 8042 waits are bounded by a CX countdown. A COM that hangs here would
;     leave AUTOEXEC unable to finish, which is a far worse failure than a
;     missed pulse. On timeout it proceeds rather than spinning.
;
; The keyboard ACKs each byte with 0xFA; those are read and discarded. Left in
; the output buffer one could be taken for a scancode by the BIOS INT 09h
; handler -- which in this rig means a phantom keystroke landing in whatever the
; harness types next.
;
; Build:  nasm -f bin -o RDYPULSE.COM rdypulse.asm

                bits    16
                org     0x100

; ---- read BDA keyboard flags, set the Scroll Lock bit, write it back --------
                mov     ax, 0x0040
                mov     es, ax
                mov     al, [es:0x17]
                or      al, 0x10            ; bit 4 = Scroll Lock active
                mov     [es:0x17], al
                mov     bl, al

; ---- build the LED bitmask in DH: scroll=b0, num=b1, caps=b2 ---------------
; Derived from the BDA rather than assumed, so the other two LEDs are left
; consistent with what DOS actually thinks the lock state is. That also
; re-syncs any drift the harness introduced by writing sysfs directly.
                xor     dh, dh
                test    bl, 0x10
                jz      .no_scroll
                or      dh, 0x01
.no_scroll:
                test    bl, 0x20
                jz      .no_num
                or      dh, 0x02
.no_num:
                test    bl, 0x40
                jz      .no_caps
                or      dh, 0x04
.no_caps:

; ---- 0xED "set LEDs", then the mask ----------------------------------------
                mov     dl, 0xED
                call    kbd_send
                mov     dl, dh
                call    kbd_send

                mov     ax, 0x4C00
                int     0x21

; ---- kbd_send: send DL to port 0x60, then drain the ACK --------------------
kbd_send:
                push    cx
                xor     cx, cx              ; 65536 tries, then give up
.wait_ibf:
                in      al, 0x64
                test    al, 0x02            ; input buffer full?
                jz      .send
                loop    .wait_ibf
.send:
                mov     al, dl
                out     0x60, al
                xor     cx, cx
.wait_obf:
                in      al, 0x64
                test    al, 0x01            ; output buffer full?
                jnz     .read
                loop    .wait_obf
.read:
                in      al, 0x60            ; discard the 0xFA ACK
                pop     cx
                ret
