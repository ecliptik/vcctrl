# DOS helper COMs for the vcctrl harness.
#
# These were originally hand-assembled in Python. That produced a working but
# subtly wrong RDYPULSE -- two loop displacements were off by 2, so the 8042
# wait re-tested a stale status byte instead of re-reading the port. Hardware
# testing did not catch it because the bounded-timeout path proceeds anyway and
# masks the fault. nasm caught it on the first byte-comparison.
#
# So: real source, real assembler, and the binary is a build artifact.

# BUILD INSIDE THE REPO; INSTALL IS A SEPARATE, EXPLICIT TARGET.
#
# `OUTDIR` used to be `../../g2k/DRIVERS` -- a SIBLING REPOSITORY. Two things
# followed, both found by the vckvm session after `keycoverage.json` began
# citing keywit.asm as the instrument that produced its rows:
#
#   * `make` FAILED IN A BARE CLONE of this repo, at the first target, with
#     "unable to open output file" naming RDYPULSE. The reproducibility claim
#     attached to that citation was therefore false as stated -- the binary
#     does rebuild byte-identically, but not by the documented method, and a
#     stated method that errors out is worse than no claim because the person
#     who tries it concludes the REPO is broken rather than the instruction.
#
#   * WORSE: from any checkout under the same parent -- a throwaway
#     `git worktree`, for instance -- that relative path resolves to the REAL
#     sibling, so `make` in a scratch tree OVERWRITES the card payload in
#     another repository. Whether it did depended on where you happened to
#     have checked out, which is luck rather than care.
#
# So the default target writes only inside this repo, and the copy that leaves
# it is `make install` -- you have to ask for the thing that writes elsewhere.
ASM      := nasm
ASMFLAGS := -f bin
OUTDIR   := build
PAYLOAD  := ../../g2k/DRIVERS

all: $(OUTDIR)/RDYPULSE.COM $(OUTDIR)/KEYWIT.COM

$(OUTDIR):
	mkdir -p $(OUTDIR)

$(OUTDIR)/RDYPULSE.COM: rdypulse.asm | $(OUTDIR)
	$(ASM) $(ASMFLAGS) -o $@ $<
	@echo "built $@ ($$(stat -c%s $@) bytes)"

$(OUTDIR)/KEYWIT.COM: keywit.asm | $(OUTDIR)
	$(ASM) $(ASMFLAGS) -o $@ $<
	@echo "built $@ ($$(stat -c%s $@) bytes)"

# The only thing that writes outside this repository. Refuses rather than
# creating the directory: if PAYLOAD is not there, this is not the machine
# that holds the card image, and silently making one would put the COMs
# somewhere nobody looks.
install: all
	@test -d $(PAYLOAD) || { echo "no $(PAYLOAD) -- not the card-image host"; exit 1; }
	cp $(OUTDIR)/RDYPULSE.COM $(OUTDIR)/KEYWIT.COM $(PAYLOAD)/
	@echo "installed into $(PAYLOAD)"

clean:
	rm -rf $(OUTDIR)

.PHONY: all clean install
