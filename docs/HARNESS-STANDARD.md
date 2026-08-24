# Agent-driven test harness standard

A contract for running measured tests on constrained or vintage hardware
when the thing reading the run sheet is a software agent rather than a
person.

**Status:** in use, and still short of a second independent adopter. It
lives in the vcctrl repository because vcctrl is its reference harness
implementation (sec. 15), not because it is about vcctrl -- nothing in
the normative text may assume a particular project or rig, and
project-specific requirements belong in a *profile* (sec. 12). It will
move to a repository of its own when a second project adopts it.

**Origin:** extracted from a Cave Story port to MS-DOS 6.22 (NXEngine-evo
/ SDL3 / DJGPP) driven by an agent over a VGA capture card, injected
keystrokes, and an FTP return channel. The failures cited throughout are
real and were expensive; a rule stated without its failure tends to get
optimised away by whoever next finds it inconvenient.

**On the numbers in this document.** Concrete figures appear throughout --
a band of 0.85, a gate going from 534 findings to 25, a round-trip check
at ~0.1 s. **Every one is a worked example from a named context, never a
default to adopt.** They are here because a rule that asserts a hazard
without ever showing it happening is a rule nobody acts on; the rule above
each number is always stated independently of it. Measure your own.

---

## 1. Terminology

**MUST** / **MUST NOT** -- required for conformance at the stated level.
**SHOULD** / **SHOULD NOT** -- strongly recommended; deviation is
permitted when recorded with a reason.
**MAY** -- optional.

A requirement carrying a level tag such as **[L2]** applies at that level
and above. Untagged requirements apply at all levels.

---

## 2. The problem this addresses

A conventional run sheet is written for an operator who can improvise:
notice an unexpected prompt, spot that the machine is in the wrong mode,
sense that a number looks wrong. An agent driving hardware cannot
improvise safely.

The sharper problem is not that an agent fails. It is that **an agent
fails in ways that resemble success.** Representative cases, all observed:

| Observed | Actually true |
|---|---|
| Result file arrived | The file was stale from an earlier run |
| Transfer confirmed | The sender had not returned to a prompt |
| Manifest states a video card | Whatever is physically fitted |
| Manifest states a config profile | Nothing; the field was never written |
| Script printed its banner | The shell consumed half the line |
| A convention used consistently | The mechanism it relies on does not exist |
| A measured improvement | Measured with a rendering defect present |

None of these announced themselves. Each was found by someone checking
something they had no specific reason to doubt.

**The standard's purpose: make every claim a run might make checkable
from an artifact that run produced.**

---

## 3. Invariants

These are cheap, apply at every level, and are the basis for deciding
cases the text does not cover.

### I1. Attest, do not recall

Every fact about a run MUST be recorded in an artifact that run produced.
Not in an operator's memory, an agent's transcript, or the run sheet's
prose.

*Test:* if the artifact bundle were the only survivor, could the run be
reconstructed -- hardware, configuration, build identity, order, outcome?
Whatever fails is a missing witness.

*Origin:* a manifest field added specifically to attest the boot profile
was written to a file the collector did not ship. The witness existed and
was never delivered; provenance rested on recollection for a whole sweep.

### I2. Witness the state, not the artifact

An artifact proves a side-effect occurred. It does not prove the system
reached the state that side-effect implies.

- A file arriving proves a transfer, **not** that the sender is idle.
- A result file existing proves a write, **not** that the run finished,
  and **not** that this run wrote it.
- A banner printing proves a code path was reached, **not** that the
  feature engaged.

Any state an agent acts upon MUST have a witness of *that state*. Where
none exists, the state MUST be established rather than inferred.

*Origin:* a collector confirmed each transfer by watching the file land,
then sent the next keystroke. Correct for the transfer; the machine was
still inside the FTP client, so the keystroke was swallowed.

### I3. Audit for the effect, not the syntax

When hunting a class of defect, enumerate what the system would actually
*do*, rather than searching for the syntax expected to cause it.

*Origin:* a search for unescaped arrows in shell scripts found four
instances. Parsing every line for redirect *targets* -- what files the
shell would create -- found the significant one, inside a comment, which
had been firing before every measurement ever taken. No arrow-shaped
search could have found it.

---

## 4. Conformance levels

Levels exist so a project can adopt the parts it needs. A bring-up
project should not pay for performance-grade rigour, and a project making
fine-grained numeric claims must.

**L1 -- Reproducible.** The run can be reconstructed from its artifacts.
Minimum for a result to be shared with anyone.

**L2 -- Attributable.** Differences between runs can be traced to a named
cause. Required before acting on a comparison.

**L3 -- Performance-grade.** Numeric claims about small effects survive
scrutiny. Required before shipping a change justified by a measurement.

A project MUST declare its level in its profile, and MAY declare
different levels for different result classes -- for example L1 for
correctness runs and L3 for benchmarks.

### 4.1 Result class **[L2]**

**Levels attach to result classes, not to files.** A single sweep
routinely mixes a paired performance comparison with diagnostic cells that
are not comparable to anything, and a file-level declaration cannot
express that.

Each cell MUST therefore declare its class, and the class determines which
requirements apply:

| Class | Meaning | Level |
|---|---|---|
| `perf` | contributes a number to a performance comparison | L3 |
| `probe` | answers a yes/no question about behaviour | L2 |
| `diag` | instrumented; timings are proportions only | L2 |
| `matrix` | one point in a configuration survey | L2 |
| `bringup` | establishes that something runs at all | L1 |

The class MUST be machine-readable, in the manifest or the cell record.
Stating it only in prose fails I1: tooling cannot read it, so the
distinction survives only as long as someone remembers it.

*Origin:* a conformance gate applied performance-grade pairing rules to a
thirteen-cell configuration survey and to instrumented cells whose own run
sheets said, in prose, that they were not comparison rows. The information
existed; nothing could act on it.

---

## 5. Platform profile

Each project MUST maintain a list of what its target platform does
silently, expressed as checkable rules rather than advice, and SHOULD
enforce it as a lint gate.

**The gate MUST run before packaging, not after deployment.** A check
that runs after an artifact reaches hardware finds defects one round too
late.

**A gate's signal-to-noise ratio is a functional requirement.** A gate
that reports mostly false positives will stop being run, and its
existence then creates confidence that the class is covered. That is
strictly worse than having no gate, because the belief persists after the
practice has lapsed.

Projects SHOULD therefore treat a gate's own false-positive rate as a
defect of the same severity as a missed detection, and MUST re-verify a
gate against known-bad input after changing it **[L2]**.

*Origin:* the originating project already had a gate that detected the
exact defect class that later shipped -- unescaped metacharacters in
shell scripts. It reported 534 failures, of which 22 were real: two
parser bugs generated the rest, one measuring an identifier's length
before variable expansion and one mistaking a conditional's test for a
command name. The gate was not run, the defects shipped, and both were
later found by hand. Repairing the two parser bugs took the same gate from
534 findings to 25, of which 22 were the real ones.

Example entries, established on real hardware for MS-DOS 6.22 /
COMMAND.COM / DJGPP:

| Rule | Silent failure |
|---|---|
| No escape character exists | An escaped redirect still redirects |
| Redirection is parsed inside comments | Zero-byte stray files; a comment emits nothing, so size zero is the signature |
| Command lines truncate past 127 chars | Manifest lines lose their tail |
| Filenames are 8.3 and case-insensitive | Two names collide into one file |
| Text files require CRLF | Scripts fail in ways resembling logic bugs |
| File I/O defaults to text mode | Newline translation corrupts binaries |
| Environment block overflows without warning | Variables silently absent |

A project on the same platform inherits the table. A project on a
different platform writes its own; the discipline transfers even where
the entries do not.

---

## 6. Run contract

### 6.1 The cell

A **cell** is one measurement: one build, one configuration, one
identifier, one outcome.

A cell MUST record: a unique identifier; the configuration artifact
applied; and the build identity it ran.

A cell SHOULD record: the environment-preparation step that ran before
it; what distinguishes it from the control; and whether it repeats
another cell. **[L2]** these become MUST.

### 6.2 The sweep

A **sweep** is an ordered set of cells sharing one session and one
hardware state.

A sweep MUST record its identifier, its cells, and its declared hardware
(sec. 6.4).

A sweep SHOULD declare whether its order is load-bearing. Where it is,
the harness MUST refuse a reordered run rather than proceed **[L2]**,
because interleaving a control destroys its meaning.

### 6.3 The result envelope

**A result file alone is not a result.**

An envelope MUST contain the result data and the build identity that
produced it. **[L2]** it MUST also contain the sweep manifest and a
completion witness that was captured rather than inferred.

An incomplete envelope makes a result **provisional**. This is a status,
not a verdict: provisional results remain useful, but SHOULD NOT enter a
comparison ledger.

**Envelope completeness does not imply cell success.** A sweep in which
every cell failed produces exactly the same envelope shape as one in
which every cell succeeded: the results arrive, the manifest is written,
the sweep-end banner fires. Structure is not outcome.

A harness MUST therefore determine each cell's outcome from that cell's
own content -- a fatal marker, an expected terminal record, a completion
field -- and MUST NOT treat arrival or envelope completeness as health
**[L2]**. Where a project emits a fatal marker, checking for it is the
cheapest sufficient test.

*Origin:* a sweep was run in an emulator with an incomplete data
directory. All cells died at video initialisation, every log was written
with a fatal line naming the cause, and the envelope was indistinguishable
at the structural level from a clean run. That a dying cell still writes a
log is useful in the other direction too: **no log at all means the cell
never reached the engine**, which is a different failure and separable on
that basis.

### 6.4 Declared versus detected

Two different kinds of fact. Conflating them is a recurring failure.

**Declared** is what a human asserts. It is unverifiable by the machine
and goes stale when hardware changes. Declared fields MUST be
parameterised, never hardcoded. An unset parameter yielding an empty
field reads as *nobody said*, which is honest; a hardcoded string keeps
asserting its original value forever.

**Detected** is what software observed, recorded verbatim. A detected
field MUST name what performed the detection **[L2]**.

*Detection can be masked, and masked detection is worse than none,
because it resembles evidence.* In the originating project a VESA
shim reported its own identity rather than the underlying chipset, so
capturing its output would have attested the shim. The usable
discriminators were fingerprints already present in logs: which display
modes existed, memory aperture addresses, chip-specific detection lines.

Projects SHOULD keep both kinds. They answer different questions and
neither substitutes for the other.

### 6.5 Result identifiers

Identifiers MUST be unique within the retention window of stored results.
**[L3]** they MUST be unique across all runs ever.

*Origin:* a reused identifier left an earlier run's file in place. Under
arrival-based collection a stale file is indistinguishable from a pass --
it arrives, it parses, its numbers are plausible.

Where identifiers are reused across runs, the harness MUST clear targets
before a run rather than rely on overwrite. Projects SHOULD do both:
allocate identifiers centrally *and* clear before running.

---

## 6.6 Hardware changes **[L2]**

Swapping a component is not merely a change of declared field. It is the
single most dangerous moment in a campaign, because it is when a stale
declaration is most likely and least visible.

**A hardware change ends the session.** Any comparison spanning a swap is
cross-session by construction -- different thermal state, a reseat, a
reboot -- so **[L3]** it MUST NOT be treated as same-session, regardless of
how little time passed.

**Re-anchor after every swap.** A control and its repeat MUST be run on
the new configuration before any variable **[L3]**. A baseline does not
survive a swap; assuming it does is how a hardware difference gets
attributed to a code change.

**Update declared fields as part of the swap procedure, not afterwards.**
The declaration and the physical change belong in one step. Where the
declaration is a parameter, changing it MUST be a precondition of the
first run on the new hardware.

**Re-read detection.** Fingerprint fields change at a swap and are the
only independent check on the declaration. A swap where declared and
detected disagree is a stopped campaign, not a footnote.

**Capability changes threaten validity, not just comparability.**
Different parts are not always the same part at a different speed. Where a
replacement lacks a mode, a resolution, or a feature the run depends on,
the run may not be the same run -- and the result is then invalid rather
than merely incomparable. A campaign MUST record, per configuration,
whether the workload executes identically; **[L3]** a configuration that
cannot run the reference workload identically MUST NOT contribute rows to
a shared comparison.

**Carry known defects forward explicitly.** Where a configuration has an
open defect, results from it MUST record that. Otherwise the defect
eventually gets rediscovered as a property of whatever was under test.

*Origin:* one video card in the originating project lacks the display mode
the others use, and carries an unresolved rendering defect. Its results
are therefore neither comparable to the other cards' nor safely readable
as evidence about anything else -- but nothing in the run artifacts said
so, and the declared-hardware field asserted a different card entirely.

---

## 7. Harness protocol

### 7.1 States and witnesses

**No state may be inferred from elapsed time.** Timeouts MAY bound
waiting; they MUST NOT establish state.

| State | Witness |
|---|---|
| `IDLE` | prompt observed, probed rather than assumed |
| `CONFIGURED` | readback echoing the requested value |
| `RUNNING` | start banner |
| `CELL_DONE` | end banner |
| `SWEEP_DONE` | sweep-end banner |
| `COLLECTED` | artifact received **and** sender returned to `IDLE` |
| `RECOVERED` | prompt re-established after a fault |

`COLLECTED` requiring both conditions is I2 expressed as a state.
Projects lacking a banner mechanism MUST substitute another positive
witness; elapsed time is not one.

### 7.2 Capture hazards

Where the harness reads a screen, the read is lossy and the loss is not
random.

- Capture devices drop frames. Any judgement that a region is *absent*
  MUST use multiple frames plus an in-frame positive control -- something
  that must appear if capture is working **[L2]**. Without it, a capture
  artifact and a real rendering defect are the same picture.
- Modes outside a capture device's range yield nothing. Absence of signal
  is not absence of output.
- Text recognition of a success message is not a state witness. State
  SHOULD be read from the system rather than from text describing it.

### 7.2.1 A failed search is not evidence of absence

Where a system logs its own decisions, **that log is a witness and SHOULD
be read before a mechanism is theorised**. A search that returns nothing
constrains the search term, not the system.

Before concluding that a behaviour is unrecorded, an investigator SHOULD
read a representative log in full at least once. Vocabulary is the usual
failure: a log names things in the implementation's words, not the
investigator's.

*Origin:* an investigator seeking the cause of a cross-hardware
performance difference searched for `viewport`, `letterbox` and `logical`,
found nothing, and proposed two competing theories. The engine had logged
the answer in full on every run under the token `center-oversized`,
including the exact geometry and the reason the fast present path had
stood down. The correct conclusion was one grep away and was reached only
after someone read the file.

### 7.2.1a Prove the pipeline with a known presence before believing an absence

7.2.1 says a failed search is not evidence of absence. This is its
constructive form, and it is mechanical.

**Never prove an absence on a pipeline that has not just proved a
presence.** Run a probe you know is there through the *identical* path
first. Only after that returns a hit does a miss mean anything.

    -- witness --
      KNOWN_PRESENT_KEY     found        <- the pipeline is proven HERE
    -- and the one that must be ABSENT --
      FORBIDDEN_KEY         absent       <- only now believable

Why it works is worth stating, because it explains where to apply it. In
the originating incident the same broken pipeline was asked two questions.
One was about a value known to be set, where a false negative **refuses
loudly**. The other was about a value required to be absent, where a false
negative **passes silently**. The check that could only fail safely is what
exposed the bug in the check that could fail dangerously.

**So the rule generalises: pair every dangerous check with a safe one on
the same path.** A pipeline carrying only questions whose wrong answer is
silence has no way to tell you it is broken.

### 7.2.2 A check that cannot distinguish success from failure is not a check

Every verification MUST be able to return *both* answers. A check whose
negative case is indistinguishable from its positive case confirms only
that the check ran.

This is the general form of most failures this standard records, and they
recur because the proxy is always something genuinely observed:

| Proxy observed | Mistaken for |
|---|---|
| File arrived | Sender is ready |
| Result file exists | The cell that wrote it finished |
| Envelope complete | Cells succeeded |
| Prompt returned after a command | The command succeeded |
| Process still running | Progress is being made |
| A state indicator's last known value | Its current value |

Before relying on a check, state what its failure would look like. Where
that is the same observation as success, the check MUST be replaced by one
that reads the thing itself.

**A check that can fabricate an answer is worse than no check.** Where a
status source may be unset, stale, or inherited from an earlier operation,
reading it produces a confident result with no relationship to what
happened. Prefer a statement that **cannot be false** -- naming the attempt
and pointing at the witness -- over a status whose provenance is unverified.

*Origin:* a completion message was to be conditioned on an exit status, but
whether the program in question sets one was unverified. If it does not,
the test reads the *previous* command's value and reports a result about
the wrong operation. The line was rewritten to state the attempt and name
the witness instead. A sentence that cannot be false beats a status that
is not actually read.

*Origin:* three scripted cells set an environment variable and all three
reported the feature disengaged. That was read as a negative result for
the feature. The variable had never reached the program: the script
confirmed that the prompt returned after each assignment, which a failed
assignment also does. Setting it by hand and reading the shell's own
variable listing worked immediately. Ninety seconds of looking at state
beat three automated runs that could only ever have returned one answer.

### 7.2.2g An output gated separately from its collection can print a lie

The worst instrument failure in the originating project did not fail. It
emitted a complete, well-formed statistic -- correct field names, correct
block count, a number in every slot -- whose actual meaning was *nothing
was sampled*.

The cause: **the gate that permits the OUTPUT and the gate that permits the
COLLECTION were different flags.** Opening the first alone produced a row
of zeros. Every other failure in that codebase announced itself by absence
-- a missing line, a refused cell, a silent skip. An absence invites a
second look. **A zero closes the question.**

It was quoted as evidence that a subsystem was free. That subsystem later
measured as the largest unbucketed cost in the frame.

**Requirements.**

- An emitter MUST check the gate that controls its own data, not merely
  the gate that controls its printing.
- When collection is off, it MUST say so in the output, and MUST name the
  flag that would enable it.
- A zero MUST be distinguishable, in the log alone, from *not sampled*. The
  reader will not have the source in front of them.

**Where to look for this:** any instrument whose counters live in one
module and whose emit lives in another, and any marker whose name resembles
a flag it is not actually gated by. A shared prefix is not a shared gate.

### 7.2.2a Having just named a hazard is when it is most likely to recur

Naming a failure shape does not confer immunity to it. The shape describes
a *situation*, and situations recur whether or not anyone can name them.

The dangerous part is the feeling that follows a diagnosis. **Having just
identified a hazard is precisely when an investigator is most likely to
feel covered**, and therefore most likely to skip the check that would
catch it. Recent diagnosis SHOULD be treated as a reason to check more
carefully, not less.

**The defence is the procedure, not the understanding.** Where a mistake
was caught by re-reading rather than by reasoning, the re-read is the
control worth keeping -- understanding was present and did not prevent it.

*Origin:* an investigator diagnosed a divergence between two copies of a
file, correctly named the condition that produced it, and then within
twenty minutes edited a staging copy of a file tracked elsewhere. Separately,
the same person wrote a comment quoting a redirect defect to explain it,
while knowing that comments on that platform redirect. Both were caught by
checking, neither by knowing.

### 7.2.2h A check must assert its input set is non-empty before it may pass

An empty population and a passing population produce identical output
unless something explicitly distinguishes them.

Observed five times in one day across three subsystems, in checks written
by people who had spent that day cataloguing this exact hazard: a margin
check that examined **zero frames** and reported that the margins were
clean; a preflight that printed `ok` because every field of an empty
structure satisfied the test; a percentile computed over a population that
contained no samples.

**Requirement: a check MUST assert its input set is non-empty before
returning a pass, and MUST report the surviving count alongside the
verdict.** `refused: 0 of 46 judged` is a different object from `PASS`.

This is one line in most checks. It would have caught all five.

**Corollary for counted populations.** A count of N observations is only N
observations if they are independent. Where a capture path can repeat an
identical sample, deduplicate before counting -- but only for questions
about the SUBJECT. A repeat is still a member of a population that asks
about ARRIVAL (rate, bandwidth, timing), because it still consumed a slot.
**It depends on what the population is a sample OF**, and a rule applied
uniformly across a round will be wrong for half of it.

### 7.2.2b Verification must be routine, not reserved for doubtful claims

Cross-checking works only when it is **unconditional**. A practice of
verifying claims that look doubtful catches nothing, because a claim that
looked doubtful would have been questioned anyway.

Where two parties check each other's work, the corrections run in both
directions at roughly equal rate. That symmetry is the mechanism and not a
courtesy: neither party needs to be reliable, provided each checks the
other against **artifacts rather than against plausibility**, and says so
when a claim does not survive.

**The rule must cover reports that absolve.** A claim that explains away
one's own error arrives with a reason to accept it, and is therefore the
claim least likely to be checked. Unconditional means unconditional in
this direction too: **the claims that pass inspection include the ones the
inspector wants to pass.**

*Origin:* across one evening's collaboration, every defect caught in
either direction was in something that looked entirely fine at the time --
a plausible mechanism, a consistent convention, a number in the expected
range, a field with a sensible value. None would have been selected for
scrutiny by a policy of checking suspicious things.

### 7.2.2i A pre-registered outcome table must enumerate a third state

Pre-registration protects against choosing the analysis once the data is
visible. It introduces its own hazard, and it is worse than the one it
replaces.

A check was registered in advance with two outcomes: marker present meant
the instrument ran; marker absent meant the code path was never entered.
Reality was a third state neither branch covered -- **the marker was absent
because the OPPOSITE marker was present**, and the run had silently used
the wrong configuration. Followed literally, the table would have returned
its "vacuous" verdict: a wrong conclusion, authorised in writing, with no
reason to interrogate it.

**A pre-registered wrong answer is more dangerous than an unplanned one,
because the plan supplies the permission not to look again.**

**Requirements when writing an outcome table.**

- Ask what a THIRD state would look like, and whether it is distinguishable
  from the two written down. **"Absent" is the usual place two meanings
  hide** -- *not there*, and *could not look*.
- Verify a search pattern against something known present before trusting
  its null (7.2.1a).
- **A threshold must name the quantity it measures AND the decision it
  gates**, side by side. A threshold in milliseconds gating a question
  about whether a cost is bandwidth-bound or overhead-bound is well-formed
  and cannot answer the question it was written for. Naming both makes the
  mismatch visible before the data rather than after.

### 7.2.2c The watch is a check, and can be blind to its own event

A monitor, filter or alarm is subject to every rule in 7.2.2, and is
unusually good at escaping them: it runs unattended, and the state it
reports most of the time is "nothing to report", which is also what it
reports when broken. **Before arming a watch, state what it would emit if
the failure it exists for happened right now.** If the answer is nothing,
it is not a watch.

Three variants seen in a single afternoon, all on the same run:

- **The exclusion swallowed the event.** A filter dropped routine lines by
  matching a substring those lines carried -- and the failure line carried
  it too, because failure and routine differ in a *different* field. The
  alarm was deaf to precisely the event it was built for, and had been
  since the moment it was armed.
- **Case.** The filter looked for a lowercase word; the runner emitted it
  uppercase. Completion never surfaced.
- **The absent reading compared unequal.** A stall detector compared the
  current frame signature against the previous one. A capture failure
  yielded a null signature, null never equals the prior value, so **no
  picture scored as movement** and a wedge read as activity indefinitely.

The third is the general form and deserves its own statement: **a
null, absent or error reading MUST NOT flow into a comparison that treats
difference as health.** Absence is a third state and MUST be handled as
one -- separately from "same" and "different".

**The two failure directions are not symmetric.** A cheap check that can
produce a false ABSENCE is safe as a green light and dangerous as an
abort: acting on its positive costs nothing and is sound, while acting on
its negative stops healthy work and names the wrong cause while doing it.
So such a check SHOULD be wired as a fast path with a fall-through, never
as a gate. Zero-cost and cannot-fabricate are different properties, and
where they conflict the cheap check earns the green light while the
expensive one keeps the veto.

A watch SHOULD be tested by inducing its event, or by replaying a recorded
failure through it, before it is relied upon. A watch that has never
emitted has not been shown to work.

### 7.2.2d Silence may be the failure signal

Where a component reports success by emitting a banner and failure by
emitting nothing, a normal-looking run is indistinguishable from a broken
one, and the absence is easily read as "no errors". A harness MUST NOT
infer that a step succeeded from the absence of complaint. Assert the
positive consequence -- the module resident, the file present, the
capability offered -- rather than the absence of a message.

The originating case: a video driver loaded from a startup script printed
a chip-identifying banner when it installed and nothing at all when it
declined, having been configured for hardware no longer fitted. Every boot
afterwards looked clean and ran on a fallback provider with materially
different capabilities.

### 7.2.2e An unstated scope is read as covering the reader's worry

A check, contract or verdict is sound only within some boundary. Where the
boundary is not stated **in the output itself**, readers supply their own,
and they supply the one their current question needs. The guarantee is not
wrong; the reader's extension of it is -- and nothing in the artifact
contradicts them.

So any check emitting a verdict MUST name its **subject**, and SHOULD name
what it does **not** cover wherever a reader could plausibly over-read it.
A bare `ok` is an invitation.

Two instances from the reference implementation, the same shape rotated:

- A file-output contract was strictly two-valued -- the file exists and the
  status is zero, or it does not exist and the status is one -- and held
  exactly as written **on the machine it ran on**. Invoked across a
  transport boundary it wrote on the far host, so the caller saw a zero
  status and no local file: the forbidden third state, produced by a
  contract that never said which filesystem it was about.
- A readiness verdict covering the harness was, on inspection, capable of
  reading as "fit to run". It answered whether the apparatus could drive
  the target, not whether the target was fit to be measured. A fully green
  harness had already driven a misconfigured target for an entire round.

Both were correct. Both were read wider than they were written, by people
who had written them.

The remedy is cheap and belongs in the artifact rather than the
documentation: a `scope` field, and a `does_not_cover` note naming the
adjacent question most likely to be confused with this one. Documentation
is read before the tool is trusted; output is read at the moment of
trusting it.

### 7.2.2f A reading must be shown to belong to the CURRENT epoch

Status surfaces retain the last value published to them. A target that is
off, resetting or re-enumerating publishes nothing, so the surface keeps
reporting what was true before -- a real measurement, correctly recorded,
and no longer about the present. It does not read as stale, because a
stale value and a current one are the same value.

Therefore, across any discontinuity -- power cycle, reboot, device
re-enumeration, reconnection -- **a level check on retained state is
invalid**. Readiness MUST be established from a transition observed after
the discontinuity, or from a value carrying an epoch marker that
distinguishes this boot from the last.

*Origin:* the reference harness reads target readiness from keyboard lock
LEDs. Cutting power leaves the last published state in place, and the
readiness indicator is the last thing a healthy boot sets -- so a level
check for "ready" returned TRUE 2.5 seconds after power-on, on a machine
that had not begun to POST. The fix is an edge pair: wait for the
indicator to go false, which proves the reading now belongs to this boot,
and only then wait for it to go true.

The general form is worth stating because the specific one looks like a
quirk: **the most misleading moment for a retained reading is exactly the
moment it is most likely to be consulted**, since that is when something
has just changed and the reader wants to know whether it has finished
changing. Any check running across a power event inherits this and MUST
handle it explicitly.

A related distinction, which is 7.2.3b's fault-versus-unknown applied to
the same subsystem: **"the target is off" is a FAULT when the power
controller answered and said so; it is UNKNOWN when the controller cannot
be reached.** The severity is not what separates them -- what separates
them is whether the harness knows. One says go and switch it on, the other
says go and find out why you cannot see it.

### 7.2.3 The physical layer is upstream of every instrument

Logs, capture, and status channels all report **software state**. A fault
in the physical layer -- a marginal edge connector, a partially seated
card, a failing cable -- sits upstream of all of them and is reachable
only by hands.

A harness MUST therefore be able to report **"I cannot determine this"**
and escalate, rather than continuing to generate hypotheses. Where the
observable evidence has been exhausted without a cause, the correct output
is an admission and a request for physical inspection.

*Origin:* a network adapter stopped being detected between two runs. Three
mechanisms were proposed in sequence -- reseat, resource over-commitment,
configuration loss -- and all three were wrong. The cause was intermittent
contact, found by reseating every card, and confirmed by an unexplained
POST tone disappearing at the same time. **No log, capture frame or status
channel on that machine could have detected it**, because every one of
them reports on software that was running correctly on a card that was
electrically half-present.

A corollary worth stating plainly: **a confident sequence of plausible
causes is a symptom of not yet having evidence.** Plausibility is cheap
and is not progress.

### 7.2.3a Prove the control path at the FAR end **[L2]**

Every status a harness reports about its own input path describes the
harness's end of the wire. A driver can be loaded, a device node open, a
write succeed, and every diagnostic read green while **nothing arrives at
the target**. A run in that state types into the void and its cells fail
in whatever way an unattended machine fails.

So before a run, the harness MUST obtain a response **from the target**
that proves the path end to end -- a return channel, an echoed state, an
observable side effect on the target's own screen. Near-end status is not
a substitute and MUST NOT be treated as one.

The result has four states, not two, for the reason in 7.2.2c:

    answered          the path is proven
    did not answer    a real fault
    could not look    no return channel on this configuration
    tool failed       the check itself did not run

**"Could not look" MUST NOT collapse into "did not answer."** Some target
configurations have no return channel at all, and a harness that aborts on
could-not-look refuses to run on working hardware.

*Origin:* on the reference harness a structure-size mismatch in the input
read path silently discarded every keystroke while all local diagnostics
stayed green. The near-end was healthy in every observable respect and the
far end received nothing.

Cost is not an argument here: the reference implementation measures this
at ~0.1 s, so it belongs before every run, between cells, and after any
power event.

### 7.2.3b Preflight is ONE command with ONE exit code **[L2]**

Where a harness has several readiness checks, it MUST expose them as a
single invocation returning a single verdict. A set of individually
correct checks presented as a list is not a gate: lists are read in order
and abandoned at the moment a run is finally ready to start, which is
exactly when they matter. The cost of the checks is not the obstacle --
the reference implementation runs seven in well under a second. The
obstacle is that running them is a decision rather than a default.

**Combining several three-state checks into one verdict.** The states do
not merge symmetrically, and the ordering is forced:

    any FAULT      -> FAULT     a definite fault is the actionable finding
    else any UNKNOWN -> UNKNOWN an unknown MUST NOT read as a pass
    else            -> PASS

A fault outranks an unknown because it is the more actionable of the two,
but the composite verdict MUST still report the unknown separately -- an
earlier could-not-look must not be masked by a later fault, nor the
reverse.

**The verdict MUST name which check decided it.** A bare failing exit code
sends the reader to inspect every subsystem to discover which one spoke.

**Non-invasive checks MUST run before invasive ones.** Anything that
writes to the target -- a keystroke, a mode change, a power action -- runs
last, so a target in no state to be written to is discovered without
having written to it.

**A check that cannot run is UNKNOWN, never FAULT.** Where another agent
holds an arbitration lock, the invasive check is *skipped* rather than
attempted, and reports could-not-look with the lock named.

Contrast this with the dependent case in 10.0a, where one quantity
estimates the premise of another's threshold: there the combination is
conditional, not a precedence order. **Independent checks take a
precedence; dependent ones take a condition.** Determining which applies
is part of specifying the verdict.

### 7.2.3c The harness may mutate the channel it measures through **[L2]**

An input harness typed uppercase by holding SHIFT. Caps Lock inverts SHIFT.
And the liveness probe -- the thing that decided whether the target was at
a prompt -- worked by toggling Caps Lock, several times a second, for the
life of every cell.

**So the case of every character the harness typed was decided by a bit its
own prompt detector was flipping.** Measured, same command, one second
apart:

    capslock=1  ->  set | find "name" | find /c "="   ->  count: 0
    capslock=0  ->  SET | FIND "NAME" | FIND /C "="   ->  count: 1

It hid because the platform ignored the case of command *names*, so
everything ran. Only the search string was case-sensitive, and a
case-mismatched search returns *no match* -- which is indistinguishable
from a true absence.

**It therefore failed toward false confidence**, in exactly the direction
7.2.1a exists to catch: a gate built to prove a variable was absent
reported absence for a variable that was set.

**Requirements.**

- A liveness or readiness probe MUST NOT mutate shared state that other
  operations depend on. Where it must, that coupling MUST be documented at
  both ends.
- Any case-sensitive matcher driven through a synthetic input path MUST be
  made case-insensitive, or the case MUST be asserted before use.

**The general form: a probe that writes is part of the system under test.**
Look for this wherever readiness is established by doing something rather
than by reading something.

### 7.3 Measurement preconditions **[L2]**

Conditions that must hold for a measurement to be admissible MUST be
recorded as fields rather than assumed.

- **No resident interrupt source may be armed during measurement.** State
  the precondition this way rather than in terms of a specific mitigation.
  In the originating project a network adapter stayed resident during
  gameplay, so unrelated traffic raised interrupts mid-measurement, and
  the local remedy was disconnecting the cable. That remedy is not the
  requirement: **a disconnected adapter with a resident driver is quiet,
  not quiescent** -- it still services its own timers and still costs
  cycles. A harness satisfying the literal mitigation while the driver
  remains loaded has not satisfied the precondition.
- **Instrumentation perturbs what it measures.** Heavy diagnostic modes
  can halve throughput, after which absolute timings are meaningless and
  only proportions survive. A cell's own rate MUST be read before quoting
  its timings **[L3]**.
### 7.3a A precondition verified after the run is a receipt **[L2]**

Preconditions MUST be checked **before** the work they gate, and the check
MUST be executable rather than prose. This is an ordering requirement, not
a diligence one: the same assertion, the same threshold, the same operator
produce a saved run on one side of the work and a post-mortem on the other.

The originating incident is worth stating because nothing about it looks
like negligence. A provider assertion existed in the profile. It was
copied into that round's run sheet the same morning, correctly worded,
with the exact strings to match. It was placed under a heading meaning
"conditions for the returned artifacts to be scoreable", and it was
applied faithfully -- to the logs, after a fourteen-minute run, on a
machine whose graphics provider had silently failed to load. The check
worked. It reported, accurately, that the data was worthless.

Three properties make this failure mode persistent:

- **A post-hoc precondition still passes review.** It is present,
  correctly specified, and it does fire. Its defect is invisible in the
  document and visible only in the calendar.
- **It is cheapest to write on the wrong side.** Returned artifacts are
  greppable; the live machine may need a different instrument entirely. In
  the incident the pre-flight was one command against a resident-module
  table, but the post-hoc form was a grep over a log that did not yet
  exist, and the grep is the one that gets written first.
- **The run sheet is read in order, and preparation ends where the round
  begins.** A list of conditions positioned after the run is read after
  the run.

Therefore: a precondition SHOULD be expressed against state observable
before any work starts, even where that means a different instrument than
the one used to score results afterwards. Where both forms exist they MUST
NOT be treated as interchangeable. A harness SHOULD provide the pre-flight
as a command with an exit code, because a list of boxes in a document is
exactly what gets skipped when a round is finally ready to run after a
morning of preparation.

The post-hoc form is still worth keeping. It catches drift **during** a
run, which the pre-flight cannot see. It is a second gate, not the gate.

- **The software stack under the measurement MUST be attested per cell,
  not only the hardware** **[L2]**. Drivers, firmware, resident providers
  and shims are part of the configuration being measured, and they change
  without announcing it. Assert the provider's own identity and the
  capability the measurement depends on -- both, since a fallback provider
  may still work while offering less.

  *Origin:* a display-driver reconfiguration replaced a working driver
  with one that silently declined to install -- no banner, no error. The
  next cell fell back to the card's own ROM: fewer modes, no linear
  framebuffer, a different write path. The cell ran, the game played,
  capture locked, and the figure was plausible. It was measuring a
  different machine. Two findings were drawn from it, and both dissolved
  when the driver was found missing: an apparently independent "second
  confound" was forced by the fallback, and an apparent "safe
  configuration" was simply what the absent provider looks like. The
  provider's identity string and one capability flag were in every log and
  would have caught it at the first cell rather than the fourth.

- **Where the measured configuration differs from the shipped
  configuration, the difference MUST be stated.** Hermetic cells that
  clear the environment may run on defaults rather than the settings
  normal use applies. A defensible choice; an indefensible accident.

### 7.4 Fault handling

A harness MAY retry a failed cell. It MUST record that a retry occurred
and which attempt produced the reported result. It MUST NOT present a
retry as a first attempt.

**[L3]** a hung cell MUST be reported as a hole rather than retried. A
round with a known gap is worth more than one with an undisclosed
substitution.

**Classify from a WINDOW, not a frame.** A timeout has several causes --
a genuine wedge, a legitimately slow stage, a stage that renders nothing
-- and they are indistinguishable in a single still. Over tens of seconds
they are obvious. Where the harness can retain a rolling window of the
target's screen, a fault report SHOULD carry that window rather than one
frame.

**A recording is not a frame count.** Where a writer preserves timing by
repeating frames across gaps in the source, the file holds more frames
than were ever captured, and reporting the written count as "frames
captured" overstates the evidence. Report both. The difference between
them IS the stall structure, which is the quantity a wedge investigation
wants -- so it is read, not discarded.

---

## 8. What a project must emit to be drivable

The reusable payoff. A project satisfying these is drivable by a
conforming harness with no new mechanism.

**P1. A fixed-length run with an observable end.** **MUST for L2.**

State the requirement this way rather than as "determinism", because
determinism is what a project usually supplies and a bounded, observable
run is what a harness actually needs. A run that plays until something
stops it is drivable but not measurable: completion becomes a wall-clock
judgement, which sec. 7.1 forbids.

Full input determinism -- a recorded sequence, fixed random seed, fixed
termination point -- is the strongest form and the one to prefer, since it
also makes two runs differ only by the variable under test. Where a
project has no replay facility, the cheaper substitute is usually a
scripted demonstration plus a hard termination cap, which buys the bound
and the observable end without buying determinism.

Expect this to require engine changes. It gates everything else.

**P2. Machine-readable run manifest.** One schema-versioned record at
exit carrying build identity and headline metrics. **MUST for L1.**

**P3. Start and end banners.** Often a harness's only reliable progress
signal. **MUST for L2.**

**P4. Decomposed metrics.** A single throughput number conflates distinct
costs. Emitting at minimum an in-loop rate and an out-of-loop overhead
lets a regression be attributed. **MUST for L3**, SHOULD otherwise. In
the originating project, cells have differed by seconds of overhead at
identical loop rates -- indistinguishable in a single figure.

**P5. Configuration from an artifact.** Cells configured by a file that
can be shipped and diffed, with a documented precedence against
environment variables. **SHOULD for L1, MUST for L2.**

**Two-witness rule.** A string present in a binary proves compilation;
only a runtime banner proves execution. Dead code retains its string
literals. Both witnesses SHOULD be required before believing a feature is
active **[L2: MUST]**.

---

## 9. Generation

Projects SHOULD generate run scripts from cell specifications rather than
authoring them by hand.

Nearly every platform-level defect in the originating project was a
hand-authoring defect that generation makes structurally impossible:

| Hand-authoring defect | Generator behaviour |
|---|---|
| Escape sequence that does nothing | Never emits bare metacharacters in message text |
| Over-length line silently truncated | Wraps or refuses |
| Wrong line endings | Emits the platform's |
| Metacharacter inside a comment | Same rule applies to comments |
| Reused identifier | Allocates centrally; collision is an error |
| Preparation step omitted from one cell | Emitted unconditionally |
| Manifest field missing | Emitted unconditionally |
| Many scripts drifting apart | One template |

**The generator SHOULD emit the manifest as well as the script.** Every
provenance defect in the originating project -- a hardcoded hardware
field, a missing configuration witness, a truncated line -- occurred in a
manifest hand-written alongside the script that produced the data it
describes. Generated from one specification, a script and its manifest
cannot disagree; maintained separately, they eventually will.

Adoption does not require regenerating an existing suite. It requires
generating the *next* addition, with existing scripts converted when
touched.

*Note on consistency as evidence:* eight scripts in the originating
project used the same escape idiom. It never worked. Consistency across
files shows that someone believed something, and nothing more.

---

## 10. Statistical requirements

Stated by level, so admissibility is decided by construction rather than
argued after the numbers arrive.

### 10.0 Two bands: within-run and across-run

A harness has **two** repeatability figures and they are not
interchangeable. Measure both, publish both, and state which one any
comparison is being read against.

- **Within-session band** -- the spread of a control and its repeat inside
  one sitting. This is what a profile's noise band usually quotes.
- **Across-session band** -- the spread of the *same configuration* run in
  different sittings, matched on every recorded field.

In the originating project the within-session band was 0.0-0.1 on two
separate occasions, while the same configuration measured a day apart
differed by 0.85 -- over four times the profile's stated band. That figure
is one rig's example, not a threshold to copy; what transfers is that the
two bands differed by a factor nobody predicted. The cause was not
established, and the operational rule does not need it: **an
effect smaller than the across-session band cannot be attributed to
anything changed between sessions**, however tight the within-session
figures look.

A profile MUST NOT quote a single "noise floor" without saying which of
the two it is **[L3]**. Quoting the within-session figure and comparing
across sessions against it is the most common way a harness manufactures a
result.

### 10.0a Validating a change to the apparatus **[L2]**

When the *measuring apparatus* changes -- harness host, interconnect,
driver, capture path -- validation MUST be based on within-run
**differences**, not on absolute levels compared against banked figures.

The reason is the one a control exists for: a difference of two cells from
the same sitting cancels whatever session-level confound is present, and
by 10.0 there demonstrably is one even when nobody can name it. An
absolute comparison is read against the wide band, so it cannot resolve
what an apparatus change would plausibly do -- and its dangerous outcome
is the quiet one, a new figure landing near the banked figure and being
reported as a clean pass when the comparison had no power to fail.

Validate on, in order of sensitivity:

1. **Control-pair spread** on the new apparatus against the old. Widening
   means the apparatus has added variance, which is a regression whatever
   the absolute does.
2. **A known arm delta** re-measured. A previously characterised
   difference that reproduces within the within-session band is strong
   evidence the apparatus is not perturbing the measurement.

Pick a workload that is already gated end to end, so apparatus effects are
not confounded with envelope or precondition failures.

### 10.0b A repeatability figure can be an artifact of its own binning

10.0 requires two bands. This is a named way one of them can be neither a
band nor a property of the subject.

A stationarity statistic was emitted as a within-block ratio. Both its
counters incremented per logic tick, so the *quantity* was
platform-independent. **But the block boundary was per rendered frame**,
and the frame-to-tick ratio is a property of the machine. The same reel,
same build, same counters:

    machine A    ~18 blocks over ~5140 ticks   ~285 ticks/block   mean 0.11
    machine B   ~505 blocks over ~5140 ticks    ~10 ticks/block   mean 0.004

Neither is wrong. **They are different statistics wearing one name.** The
distribution was bimodal, so a single extreme block contributes 1/18 to one
mean and 1/505 to the other.

**Requirements.**

- **A mean of per-block ratios MUST NOT be compared across configurations
  whose block sizes differ.** Emit the raw numerator and denominator so a
  pooled figure can be computed, and pool before comparing.
- Where a distribution is bimodal, report the median and the extremes. A
  mean summarises a shape it does not have.

This does not explain every across-session gap, and it should not be
offered as one. It is one mechanism, now named, by which a figure that
looks like repeatability is a fact about the apparatus.

**Corollary -- threshold constants do not transfer between content
classes.** A luminance threshold swept for stability sat on a plateau of
50-80 for one content class and 4-24 for another, on the same capture path.
Neither plateau contained the other. **A threshold must be swept on the
content class actually being measured, and its content class must be
recorded beside the value.** A bare constant is a measurement of whatever
its author happened to be looking at.

### 10.0c The control arm needs the same rigour as the treatment arm

A round compared a lever against stock across eight counterbalanced cells.
Every cell silently inherited the lever from an earlier cell, so both arms
ran the same configuration. **It looked exemplary**: deltas agreed to two
decimal places across independent blocks, and the control-to-control spread
was well inside its pre-registered limit. A condition compared against
itself is extremely consistent.

A guard existed and had been tested in both directions. **It was pointed at
the variable that would spoil the CLASS of measurement, while the variable
that DEFINED the comparison went unchecked.**

**Requirement: where an arm is defined by a setting's ABSENCE, that absence
MUST be asserted per cell, positively, through a pipeline proven per
7.2.1a.** Asserting the treatment and assuming the control is half a test.

**And verify the whole environment, not the fields you expect.** A gate
that confirms the wanted settings are present will never notice an extra
one. Comparing the total count against an expected value refuses any stray
inheritance, whatever it is called -- which matters because the leaked
setting is rarely the one anybody thought to name.

### 10.0d A contrast within one sitting is not a finding until the archive says so

Three claims were built in one afternoon on the same four cells, each
refuted by a single pass over runs already on disk.

A control arm read unusually tight -- four cells agreeing where the
treatment arms scattered. From that came a mechanism (the lever makes the
work data-dependent), then a cost derived from the mechanism (it widens
every future measurement band), then a third claim about *how* the control
was tight (its variation cancelled rather than being absent).

**Every one was false.** Pooling every same-configuration pair in the
archive:

    control arms   7 pairs, spread 0 1 2 5 12 15 16
    treated arms   6 pairs, spread 4 8 9 11 12 13

The treated arms sit **inside** the control distribution. **The four cells
that started it were the two tightest control pairs of seven**, and were
read as typical.

**Requirement: before proposing a mechanism for a contrast, establish the
base rate for both sides from banked runs.** Not a repeat -- the archive.
A repeat of an unusual sitting can be unusual in the same way.

**The asymmetry is what makes this worth a rule.** Querying the archive
costs one pass over data already collected. A mechanism costs a design, a
round, and everything downstream that cites it -- and it is
self-reinforcing, because a mechanism that explains the contrast makes the
contrast feel established.

**The tell is specific and checkable:** the cells that produced the
contrast are the same cells you would naturally reach for to test it. When
the evidence for an effect and the evidence for its explanation are the
same measurements, no amount of further reasoning over them adds
information.

**This applies with full force to a passing gate.** A stability gate
answers *is this sitting usable*, not *is this sitting typical*. Passing it
comfortably is not evidence that the configuration is unusually stable --
it is evidence that it is within tolerance, which most sittings are.

### L1 -- Reproducible

- A result MUST carry the build identity and configuration that produced
  it.
- Reported precision MUST NOT exceed measured precision.

### L2 -- Attributable

- A run introducing more than one change SHOULD isolate them; where it
  does not, the result MUST NOT be attributed to any single one.
- Comparisons SHOULD use a control from the same session. Where a control
  from another session is used, that MUST be stated.
- A result from a setup that could not exhibit the expected effect by
  construction MUST NOT be reported as evidence about the idea. Checking
  that the setup admits the outcome is cheaper than the run.

### L3 -- Performance-grade

- Every arm MUST be paired with a repeat, controls included.
- Controls MUST be same-session.
- The noise band MUST be measured per session, never borrowed. A repeated
  control pair *is* that session's band.
- A claimed effect MUST exceed the session's own band. Below it the
  result is *unresolved* -- not negative.
- Reported precision bounds the claim: figures printed to one decimal
  cannot demonstrate a band tighter than that. Two cells equal at a given
  precision are equal *to that precision*.
- Repeated samples from one deterministic sequence are one trial, not
  several.
- **A result matching the control exactly is not automatically
  confirmation.** Where an arm reproduces its control to the reported
  precision, consider that both may share a cause that makes them the same
  measurement. Check that the arm actually changed the quantity under
  test -- workload, drawn area, work count -- before reading agreement as
  a null result.

  *Origin:* a display-mode arm returned a figure equal to its control pair
  to the decimal. The agreement was the signature of a defect: the engine
  had not adopted the new geometry, so the arm was running the control's
  workload in a different presentation. The most convincing-looking number
  in the set was the one measuring nothing.
 Per-sample consistency MUST NOT be presented as replication.
- One new mechanism per build.

The L3 rules exist because an improvement was banked, shipped enabled by
default, and did not reproduce.

---

## 11. Failure taxonomy

Regression tests for the standard. A revision that stops catching one of
these has lost ground.

| # | Failure | Caught by |
|---|---|---|
| F1 | Stale result read as a pass | 6.5 |
| F2 | Transfer confirmed, machine not ready | I2 / 7.1 |
| F3 | Manifest asserts stale hardware | 6.4 |
| F4 | Witness written but never delivered | 6.3 |
| F5 | Shell consumed part of a message | 5 + 9 |
| F6 | Convention believed but never verified | I3 + two-witness |
| F7 | Gain measured with a defect present | 10 (L2) |
| F8 | Instrumented timings quoted as absolute | 7.3 |
| F9 | Emulator result assumed to hold on hardware | 13 |
| F10 | Capture artifact read as a real defect | 7.2 |

---

## 12. Profiles

A **profile** records one project's adoption: its conformance level, its
platform profile, its concrete parameters, and any tightening beyond this
document.

A profile MUST NOT loosen a requirement of its declared level. It MAY
declare a higher level, add requirements, or fix parameters this document
leaves open.

Profiles keep project specifics out of the standard, so the standard can
move between repositories without dragging them along.

---

### 12.1 A derived value must not be presented as configuration

A profile field invites editing. Some values are measurements of the rig
and belong there. Some are *derived* from two or more of them and do not.

A keypress budget was published as an editable field. It was really the
smaller of what a target's menu required and what the platform's input
buffer could hold without overflowing into the next command. **Placed in
either file, one of its two constraints becomes invisible to whoever tunes
it** -- and it was tuned, past the buffer depth, flushing stray input into
a later command line.

**A derived value presented as configuration is a value with its reasoning
deleted.**

**Requirements.**

- Publish the measured inputs. **Compute the derived value**, and refuse to
  start if a supplied one violates the constraint it was derived from.
- A cost that varies by installation -- a round-trip time, a transfer rate
  -- SHOULD be measured at startup and logged rather than configured. A
  stale constant mis-sizes every budget derived from it with no symptom,
  because the budgets still look like budgets.

**The test for whether a thing belongs in a profile at all: if disabling or
mis-setting it produces plausible output rather than an error, it is not
configuration.** Checks that establish trust in a result are not settings,
however inconvenient they are to whoever hits them first.

## 13. Adopting the standard on a new project

**Phase 0 -- inherit.** Take the platform profile unchanged if the
platform matches; take the transport layer unchanged if the machine
matches. Neither is project-specific.

**Phase 1 -- satisfy P1-P5** for the target level (sec. 8). This is the
real work and belongs before any measurement.

**Phase 2 -- one sweep, by hand, once.** Author a single sweep manually to
learn what the project actually needs. Do not generalise from zero cases.

**Phase 3 -- generate.** Turn that sweep into a specification, generate
it, and confirm the generated form reproduces the hand-written one.

**Phase 4 -- anchor.** Run a control and its repeat before any variable.
That establishes the session band and the baseline together. A campaign
without an anchor has nothing to attribute against.

**Phase 5 -- write the profile.** Record the level, parameters, and
tightenings.

*On emulators:* where an emulator and the real target disagree, the
target wins. Emulators are correctness instruments, not performance
proxies; measured divergence in the originating project spanned three
orders of magnitude depending on the code path.

---

## 14. Non-goals

- **A general test framework.** The value is the contract, not code.
- **Detection replacing declaration.** Detection can be masked; the pair
  is the point.
- **A results database.** A ledger file is sufficient and diffable.
- **Emulator parity gates.** See sec. 13.
- **Prescribing a transport.** Serial, network, or physical media are all
  compatible; only the witnesses matter.

---

## 15. Reference implementation

One implementation exists: **vcctrl**, the harness this document ships
with. It is listed here so the requirements above can be read against
something real, and because an unimplemented standard tends to contain
requirements that cannot be met.

vcctrl drives a vintage target over an agent-controlled KVM -- injected
keyboard and mouse across a PS/2 bridge, screen over a capture stick,
mains power over a smart plug. Where the standard names a requirement, it
names the mechanism that satisfies it:

| Requirement | Mechanism |
|---|---|
| 7.1 states have witnesses | a keyboard-lock return channel the target itself drives; read it, or block until it changes |
| 7.2.3a prove the path at the FAR end | one command whose four exit codes are exactly the four states that section demands: answered, did not answer, could not look, tool failed |
| 7.2.3b preflight is ONE command | one gate over every readiness check, one exit code, `FAULT` outranking `UNKNOWN` outranking `PASS` |
| 7.2.3b name which check decided | the verdict carries `decided_by`, and lists `unknowns` separately so an earlier could-not-look is not masked by a later fault |
| 7.2.2e state your scope in the output | that verdict carries `scope` and a `does_not_cover` note naming the adjacent question -- fitness of the *target* -- that it does not answer |
| 7.2.2f readings belong to an epoch | the LED reading reports `unproven` when power changed and the target has not published since, rather than returning the retained level |
| 7.2.2c absence is a third state | "no picture" is an explicit answer distinct from a frame, and the last positively-picture frame is retrievable with its age |
| 7.2 capture is lossy | duplicate-hash statistics over a frame window, raw frames by sequence number, and a ring that can be pinned while it is examined |
| 6.4 declared versus detected | the installed protocol board is detected and reported, with `unknown` as a real answer that never defaults |
| 7.4 classify from a WINDOW | a rolling scrub ring, recordable to a file, rather than a single still |
| 6.3 completion witness | an append-only record of every mains action taken, and an activity log |

**What it does not provide, stated because 7.2.2e applies to this section
too.** vcctrl is the transport and witness layer. **P1-P5 in section 8 are
the project's obligations and no harness can supply them** -- a bounded run
with an observable end, a machine-readable manifest, start and end banners,
decomposed metrics, and configuration from a shippable artifact all live in
the software under test. A conforming harness driving a project that has
not done P1-P5 will run it, and will not be able to measure it.

The same boundary applies to the preflight gate: it answers whether the
apparatus can drive the machine, never whether the machine is fit to be
measured. A fully green harness has driven a misconfigured target for an
entire round, which is the incident 7.2.2e records.
