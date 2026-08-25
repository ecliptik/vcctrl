; KEYWIT.COM -- keyboard witness for the vcctrl keymap sweep.
;
; Records what the BIOS produced for DOS, key by key, into a file on the card.
; The harness fetches it over the --from leg and compares bytes. WEBKVM 5.2b.
;
; WHY THIS IS NOT AN INT 9 HOOK, WHICH IS WHAT THE PLAN ORIGINALLY SAID.
;
;   * An INT 9 hook is a SECOND READER OF PORT 0x60 on a path whose 8042 is an
;     STM32 emulation -- and that emulation is the thing under test. RDYPULSE's
;     own comments warn that a leftover 0xFA ACK left in the output buffer gets
;     taken for a scancode, "a phantom keystroke landing in whatever the
;     harness types next". That is a warning about this instrument.
;   * DOS IS NOT REENTRANT, so an ISR cannot write a file. A hook needs a RAM
;     buffer plus a foreground flush: more moving parts inside the single
;     component every row of the coverage table depends on.
;   * INT 16h AH=10h returns the BIOS scan code in AH and ASCII in AL, from
;     ordinary foreground code. No hook, no residency, no seam.
;
; THE TRADE, STATED RATHER THAN BURIED: this measures what the BIOS PRODUCED,
; not what the board put on the wire. A key the board sent and the BIOS
; discarded looks the same here as one the board never sent. That is the right
; layer for this table -- DOS reads through INT 16h, so a key the BIOS discards
; is a key that does not work, for every purpose the KVM has -- but it is a
; narrowing, and the wire-level question stays open for a later diagnostic run
; against whatever comes back silent.
;
; AH=11h FIRST, ALWAYS. The non-blocking check is what lets a run report that
; NOTHING arrived. AH=10h alone cannot: it waits, so a negative control built
; on it hangs instead of reporting zero. A control set made only of presences
; cannot tell a working witness from one that says yes to everything.
;
; CREATE/TRUNCATE, NEVER APPEND. A stale file from an earlier session is how
; the transfer path produced `no-net` about a healthy machine on 2026-08-24 --
; a leftover NETPROOF.TXT matched first and the fresh one was never examined.
; One run, one file, no landmines for the next run.
;
; THE TRAILER IS A TRUNCATION CHECK, not decoration. records = (size-20)/20
; must equal the count in END. A partial artifact must not read as a complete
; one, and a file that stopped early is exactly what a crash mid-sweep leaves.
;
; THE LOCK STATE IS PER RECORD, NOT IN THE HEADER, and that is a correctness
; choice rather than a thoroughness one. A header value ASSERTS the condition
; held for the whole run -- and this sweep falsifies that the moment it presses
; NumLock, which moves the AL of every keypad key (measured: kp5 reads 4C 00
; with NumLock off and 4C 35 with it on; the scan stays 4C, so identity
; survives and only AL moves). A header would be correct only under a
; scheduling discipline, and a discipline is a premise that stops holding the
; day somebody reorders the sweep, silently, with the header still claiming
; otherwise. Per record it is structural. Raised by the vckvm session.
;
; Kept as the RAW BDA byte rather than decoded flags: it is the reading rather
; than an interpretation of one, and the low nibble comes free -- a non-zero
; shift/ctrl/alt bit in record 0 means something was held down when the sweep
; began, which is a finding there would otherwise be no way to see. The header
; carries NO copy of it: two copies of one fact drift and then disagree.
;
;   KEYWIT [seconds]      default 60
;   writes C:\XFER\OUT\KEYWIT.LOG
;
;   KEYWIT04<CR><LF>                        10 bytes
;   SSSS AA CC LL MM NN TTTT<CR><LF>        26 bytes each
;   END NNNN<CR><LF>                        10 bytes
;     SSSS sequence, AA scan code from INT 16h AH, CC the AL byte (NOT ascii --
;     it carries E0 for extended keys), LL the BDA keyboard-flags byte at
;     0040:0017, TTTT low word of the tick count (a DECREASE is a wrap)
;
; Build:  nasm -f bin -o KEYWIT.COM keywit.asm

                bits    16
                org     0x100

TICKS_PER_SEC   equ     18                  ; plus a quarter, added below

; ---- parse an optional decimal seconds argument from the PSP command tail ---
start:
; ---- make Ctrl-C structurally unable to kill the witness -------------------
; DOS fires INT 23h on Ctrl-C/Ctrl-Break at its next INT 21h call, and this
; program is doing INT 21h writes. A bare IRET means the break is noticed and
; ignored rather than sequenced around. DOS restores the vector from the PSP
; on exit, so there is nothing to undo. Suggested by the vckvm session.
                mov     ax, 0x2523
                mov     dx, brk_stub
                int     0x21

                mov     si, 0x81
                xor     ax, ax
                mov     cx, ax              ; cx = accumulated value
.skip:
                lodsb
                cmp     al, ' '
                je      .skip
                cmp     al, 9
                je      .skip
.digit:
                cmp     al, '0'
                jb      .done_parse
                cmp     al, '9'
                ja      .done_parse
                sub     al, '0'
                mov     bx, cx
                shl     cx, 1
                shl     bx, 1
                shl     bx, 1
                add     cx, bx              ; cx = cx*2 + cx*8 = cx*10
                xor     ah, ah
                add     cx, ax
                lodsb
                jmp     .digit
.done_parse:
                or      cx, cx
                jnz     .have_secs
                mov     cx, 60              ; default
.have_secs:
                mov     [secs], cx

; ---- deadline = now + secs*18.25 -------------------------------------------
                mov     ax, cx
                mov     bx, TICKS_PER_SEC
                mul     bx                  ; dx:ax = secs*18
                mov     bx, ax
                mov     ax, cx
                shr     ax, 1
                shr     ax, 1               ; secs/4
                add     bx, ax              ; bx = secs*18.25

                push    es
                mov     ax, 0x0040
                mov     es, ax
                mov     ax, [es:0x6C]       ; low word of the tick count
                pop     es
                add     ax, bx
                mov     [deadline], ax

; ---- create the output file (truncating any earlier run) --------------------
                mov     ah, 0x3C
                xor     cx, cx
                mov     dx, fname
                int     0x21
                jc      .no_file
                mov     [fh], ax

                mov     dx, header
                mov     cx, 10
                call    write

; ---- the loop: poll, consume, record ---------------------------------------
.loop:
                call    expired
                jc      .finish

                mov     ah, 0x11            ; NON-BLOCKING check. See the note.
                int     0x16
                jz      .loop               ; nothing waiting -- this is what
                                            ; lets a run report zero

                mov     ah, 0x10            ; consume it: AH=scan, AL=ascii
                int     0x16
                mov     [scan], ah
                mov     [asc], al

                mov     ax, [seq]
                mov     di, rec
                call    hex16
                inc     di                  ; space
                mov     al, [scan]
                call    hex8
                inc     di
                mov     al, [asc]
                call    hex8
                inc     di
                push    es                  ; the three flag bytes AS MEASURED
                mov     ax, 0x0040          ; for THIS record -- see the header
                mov     es, ax
                mov     al, [es:0x17]
                pop     es
                call    hex8
                inc     di
                push    es
                mov     ax, 0x0040
                mov     es, ax
                mov     al, [es:0x18]
                pop     es
                call    hex8
                inc     di
                push    es
                mov     ax, 0x0040
                mov     es, ax
                mov     al, [es:0x96]
                pop     es
                call    hex8
                inc     di
                push    es
                mov     ax, 0x0040
                mov     es, ax
                mov     ax, [es:0x6C]
                pop     es
                call    hex16

                mov     dx, rec
                mov     cx, 26
                call    write
                inc     word [seq]
                jmp     .loop

; ---- trailer, close, exit ---------------------------------------------------
.finish:
                mov     ax, [seq]
                mov     di, trail + 4
                call    hex16
                mov     dx, trail
                mov     cx, 10
                call    write
                mov     ah, 0x3E
                mov     bx, [fh]
                int     0x21
                mov     ax, 0x4C00
                int     0x21
.no_file:
                mov     ah, 0x09
                mov     dx, errmsg
                int     0x21
                mov     ax, 0x4C02          ; distinct code: could not create
                int     0x21

; ---- helpers ----------------------------------------------------------------
; CF set when the deadline has passed. Compares as a DIFFERENCE so that a
; midnight rollover of the tick counter cannot strand the loop forever.
expired:
                push    ax
                push    es
                mov     ax, 0x0040
                mov     es, ax
                mov     ax, [es:0x6C]
                pop     es
                sub     ax, [deadline]
                cmp     ax, 0x8000          ; < 32768 means we are past it
                jae     .not_yet
                pop     ax
                stc
                ret
.not_yet:
                pop     ax
                clc
                ret

write:                                      ; dx=buf cx=len
                push    ax
                push    bx
                mov     ah, 0x40
                mov     bx, [fh]
                int     0x21
                pop     bx
                pop     ax
                ret

hex8:                                       ; AL = byte, DI = dest; DI += 2
                push    ax
                push    bx
                push    cx
                mov     cl, al
                shr     al, 1
                shr     al, 1
                shr     al, 1
                shr     al, 1
                and     al, 0x0F
                xor     bh, bh
                mov     bl, al
                mov     al, [hexd + bx]
                mov     [di], al
                inc     di
                mov     al, cl
                and     al, 0x0F
                xor     bh, bh
                mov     bl, al
                mov     al, [hexd + bx]
                mov     [di], al
                inc     di
                pop     cx
                pop     bx
                pop     ax
                ret

hex16:                                      ; AX = word, DI = dest; DI += 4
                push    ax
                push    ax
                mov     al, ah
                call    hex8
                pop     ax
                call    hex8
                pop     ax
                ret

; ---- data -------------------------------------------------------------------
hexd            db      "0123456789ABCDEF"
fname           db      "C:\XFER\OUT\KEYWIT.LOG", 0
header          db      "KEYWIT04", 13, 10
; SSSS AA CC TTTT CRLF -- the spaces and the CRLF are written once, here, and
; only the hex fields are overwritten per record. Fixed width is what makes the
; trailer's count checkable against the file size.
rec             db      "0000 00 00 00 00 00 0000", 13, 10
brk_stub        iret
trail           db      "END 0000", 13, 10
errmsg          db      "KEYWIT: cannot create C:\XFER\OUT\KEYWIT.LOG", 13, 10, "$"
fh              dw      0
seq             dw      0
secs            dw      0
deadline        dw      0
scan            db      0
asc             db      0
