# DOS helper COMs for the vcctrl harness.
#
# These were originally hand-assembled in Python. That produced a working but
# subtly wrong RDYPULSE -- two loop displacements were off by 2, so the 8042
# wait re-tested a stale status byte instead of re-reading the port. Hardware
# testing did not catch it because the bounded-timeout path proceeds anyway and
# masks the fault. nasm caught it on the first byte-comparison.
#
# So: real source, real assembler, and the binary is a build artifact.

ASM      := nasm
ASMFLAGS := -f bin
OUTDIR   := ../../g2k/DRIVERS

all: $(OUTDIR)/RDYPULSE.COM

$(OUTDIR)/RDYPULSE.COM: rdypulse.asm
	$(ASM) $(ASMFLAGS) -o $@ $<
	@echo "built $@ ($$(stat -c%s $@) bytes)"

clean:
	rm -f $(OUTDIR)/RDYPULSE.COM

.PHONY: all clean
