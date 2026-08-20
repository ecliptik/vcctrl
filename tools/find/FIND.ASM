; FIND.COM -- MS-DOS 6.22-compatible text search, for the g2k.
;
; WHY THIS EXISTS. The g2k runs a stripped MS-DOS 6.22: 27 files in C:\DOS,
; with no FIND, no MORE, no SORT and no QBASIC. There is literally no way on
; that machine to read one line out of a file, and doskutsu writes 120 KB logs.
; Every time the harness needed one number out of one line it rebooted into the
; NET profile and FTP'd the whole log to Linux -- about 90 seconds to answer a
; question grep answers instantly.
;
; Written rather than downloaded on purpose. The machine's entire value is
; being a controlled measurement environment, so an unaudited binary is a worse
; trade than 300 lines of assembly we can read. It also lets us guarantee the
; part that matters to automation: the EXIT CODE, so a .BAT can branch on log
; content instead of a human reading a screen.
;
;   FIND [/V] [/C] [/N] [/I] "string" [file]
;
;   /V  lines NOT containing the string      /N  prefix each line with its number
;   /C  count only, do not print lines       /I  ignore case
;
;   No file argument reads standard input, so TYPE X.LOG | FIND "fps" works.
;
;   ERRORLEVEL 0  at least one line matched
;              1  no line matched
;              2  bad syntax, or the file could not be opened
;
; Deliberately matches MS-DOS behaviour rather than improving on it, including
; the "---------- FILENAME" banner. Someone who types FIND should get FIND.

        cpu 8086
        org 0x100

; ---------------------------------------------------------------- entry
start:
        cld
        ; DOS does not zero the memory past the loaded image, so anything in
        ; .bss starts as whatever the last program left there. `matches` is the
        ; only one not written before it is read, and leaving it uninitialised
        ; made /C report a garbage count AND made "no match" exit 0 -- the tool
        ; claimed to find things in files that did not contain them.
        mov     word [matches], 0
        mov     si, 0x81                ; PSP command tail
        mov     cl, [0x80]              ; its length
        xor     ch, ch
        mov     di, si
        add     di, cx
        mov     byte [di], 0            ; terminate it

.opts:
        call    skip_spaces
        cmp     al, 0
        je      .no_string
        cmp     al, '/'
        jne     .want_string
        inc     si                      ; past '/'
        lodsb
        call    upcase
        cmp     al, 'V'
        je      .set_v
        cmp     al, 'C'
        je      .set_c
        cmp     al, 'N'
        je      .set_n
        cmp     al, 'I'
        je      .set_i
        jmp     .to_usage
.set_v: mov     byte [opt_v], 1
        jmp     .opts
.set_c: mov     byte [opt_c], 1
        jmp     .opts
.set_n: mov     byte [opt_n], 1
        jmp     .opts
.set_i: mov     byte [opt_i], 1
        jmp     .opts

.no_string:
        jmp     near usage

; 8086 conditional jumps reach +/-128 bytes only, and `usage` sits past the
; scanner. Everything in the parser branches here instead.
.to_usage:
        jmp     near usage

; ------------------------------------------------- the quoted search string
.want_string:
        cmp     al, '"'
        jne     .to_usage
        inc     si
        mov     di, pattern
        xor     cx, cx
.copy:
        lodsb
        cmp     al, 0
        je      .to_usage                ; unterminated quote
        cmp     al, '"'
        je      .copied
        cmp     cx, PATMAX
        jae     .to_usage
        stosb
        inc     cx
        jmp     .copy
.copied:
        jcxz    .to_usage                ; empty pattern matches nothing useful
        mov     [patlen], cx

        ; Fold the pattern once up front when /I, rather than per comparison.
        cmp     byte [opt_i], 0
        je      .nofold
        mov     di, pattern
        mov     cx, [patlen]
.fold:  mov     al, [di]
        call    upcase
        mov     [di], al
        inc     di
        loop    .fold
.nofold:

; ------------------------------------------------------------- the filename
        call    skip_spaces
        cmp     al, 0
        je      .use_stdin
        mov     di, fname
.fcopy: lodsb
        cmp     al, 0
        je      .fdone
        cmp     al, ' '
        je      .fdone
        stosb
        jmp     .fcopy
.fdone: mov     byte [di], 0
        mov     byte [have_file], 1

        mov     dx, fname
        mov     ax, 0x3D00              ; open, read-only
        int     0x21
        jc      .openfail
        mov     [fhandle], ax

        ; MS-DOS prints a banner naming the file before its matches.
        mov     dx, banner
        mov     cx, 11
        call    write
        mov     dx, fname
        call    write_asciiz
        call    crlf
        jmp     scan

.openfail:
        mov     dx, msg_open
        call    write_asciiz
        mov     dx, fname
        call    write_asciiz
        call    crlf
        mov     al, 2
        jmp     exit

.use_stdin:
        mov     word [fhandle], 0       ; standard input

; ------------------------------------------------------------------ scanner
;
; Chunked, because the logs are far larger than the 64 KB this .COM owns. A
; line accumulates in linebuf and is judged when its terminator arrives; a line
; longer than LINEMAX is truncated rather than overflowing, which is the one
; place this differs from MS-DOS and the safe direction to differ in.
scan:
        mov     word [linelen], 0
        mov     word [lineno], 0

.block:
        mov     bx, [fhandle]
        mov     cx, BUFSIZE
        mov     dx, buffer
        mov     ah, 0x3F
        int     0x21
        jc      .eof
        or      ax, ax
        jz      .eof
        mov     cx, ax
        mov     si, buffer
.byte:
        lodsb
        cmp     al, 0x0A                ; LF ends a line
        je      .endline
        cmp     al, 0x0D                ; CR is dropped
        je      .next
        cmp     al, 0x1A                ; ^Z ends the file
        je      .eof
        mov     di, [linelen]
        cmp     di, LINEMAX
        jae     .next                   ; truncate, do not overflow
        mov     bx, linebuf
        add     bx, di
        mov     [bx], al
        inc     word [linelen]
.next:
        loop    .byte
        jmp     .block

.endline:
        push    cx
        push    si
        call    judge
        pop     si
        pop     cx
        mov     word [linelen], 0
        loop    .byte
        jmp     .block

.eof:
        ; A final line with no terminator still counts.
        cmp     word [linelen], 0
        je      .closed
        call    judge
.closed:
        cmp     byte [have_file], 0
        je      .nofile
        mov     bx, [fhandle]
        mov     ah, 0x3E
        int     0x21
.nofile:
        cmp     byte [opt_c], 0
        je      .verdict
        mov     dx, count_pre
        call    write_asciiz
        mov     ax, [matches]
        call    print_dec
        call    crlf
.verdict:
        mov     ax, [matches]
        or      ax, ax
        jz      .none
        xor     al, al                  ; found
        jmp     exit
.none:  mov     al, 1                   ; not found
        jmp     exit

; ------------------------------------------------- judge the assembled line
judge:
        inc     word [lineno]
        call    contains                ; ZF set = contains
        jz      .hit
        cmp     byte [opt_v], 0         ; /V inverts
        je      .miss
        jmp     .report
.hit:
        cmp     byte [opt_v], 0
        jne     .miss
.report:
        inc     word [matches]
        cmp     byte [opt_c], 0
        jne     .miss                   ; counting only
        cmp     byte [opt_n], 0
        je      .body
        mov     al, '['
        call    putc
        mov     ax, [lineno]
        call    print_dec
        mov     al, ']'
        call    putc
.body:
        mov     dx, linebuf
        mov     cx, [linelen]
        call    write
        call    crlf
.miss:
        ret

; ---------------------------------------- substring search; ZF=1 if present
contains:
        mov     cx, [linelen]
        cmp     cx, [patlen]
        jb      .absent
        sub     cx, [patlen]
        inc     cx                      ; candidate start positions
        xor     bx, bx                  ; offset into linebuf
.at:
        push    cx
        mov     si, linebuf
        add     si, bx
        mov     di, pattern
        mov     cx, [patlen]
.cmp:
        lodsb
        cmp     byte [opt_i], 0
        je      .raw
        call    upcase
.raw:
        scasb
        jne     .nope
        loop    .cmp
        pop     cx
        xor     ax, ax                  ; ZF=1, found
        ret
.nope:
        pop     cx
        inc     bx
        loop    .at
.absent:
        or      al, 1                   ; ZF=0, not found
        ret

; ------------------------------------------------------------------ helpers
skip_spaces:
        mov     al, [si]
        cmp     al, ' '
        je      .adv
        cmp     al, 9
        je      .adv
        ret
.adv:   inc     si
        jmp     skip_spaces

upcase:
        cmp     al, 'a'
        jb      .done
        cmp     al, 'z'
        ja      .done
        sub     al, 32
.done:  ret

putc:
        mov     [onechar], al
        mov     dx, onechar
        mov     cx, 1
        ; fall through

; DX = buffer, CX = length
write:
        mov     bx, 1
        mov     ah, 0x40
        int     0x21
        ret

write_asciiz:
        push    dx
        mov     si, dx
        xor     cx, cx
.len:   lodsb
        or      al, al
        jz      .got
        inc     cx
        jmp     .len
.got:   pop     dx
        jcxz    .none
        call    write
.none:  ret

crlf:
        mov     dx, nl
        mov     cx, 2
        jmp     write

; AX = value
print_dec:
        mov     cx, 0
        mov     bx, 10
.div:   xor     dx, dx
        div     bx
        push    dx
        inc     cx
        or      ax, ax
        jnz     .div
.out:   pop     ax
        add     al, '0'
        push    cx                      ; putc sets CX=1 on its way into write,
        call    putc                    ; which silently truncated every number
        pop     cx                      ; past its first digit
        loop    .out
        ret

usage:
        mov     dx, msg_usage
        call    write_asciiz
        mov     al, 2
        ; fall through

exit:
        mov     ah, 0x4C
        int     0x21

; -------------------------------------------------------------------- data
PATMAX   equ 128
LINEMAX  equ 1024
BUFSIZE  equ 2048

opt_v     db 0
opt_c     db 0
opt_n     db 0
opt_i     db 0
have_file db 0
onechar   db 0
nl        db 13,10
banner    db '---------- '
count_pre db 'count: ',0
msg_open  db 'FIND: cannot open ',0
msg_usage db 'FIND [/V] [/C] [/N] [/I] "string" [file]',13,10
          db '  /V not containing  /C count only  /N number lines  /I ignore case',13,10
          db '  no file reads stdin.  errorlevel 0=found 1=not found 2=error',13,10,0

; Uninitialised, past the image. A .COM owns the whole segment, so this costs
; address space rather than file size.
section .bss
patlen    resw 1
linelen   resw 1
lineno    resw 1
matches   resw 1
fhandle   resw 1
pattern   resb PATMAX
fname     resb 128
linebuf   resb LINEMAX
buffer    resb BUFSIZE
