# vcsysinfo.py -- parses a dinspect report into a structured dict.
#
# vendor/dinspect.exe writes its `-o FILE` report as plain
# "LABEL: value\n" lines, one per detected field. This module owns
# turning that text into the dict SysinfoCapability serves, and nothing
# else: no daemon-internal dependencies, so it can be unit-tested against
# a sample report without a rig.
#
# The field list and format below are copied from vendor/dinspect.exe's
# own behaviour as of the build currently vendored -- not read from it at
# runtime, and not read from the dosfetch repo that built it, which this
# host has no checkout of. Updating the vendored binary (see the
# vcctrl-dinspect-sysinfo skill) is the moment to re-check this list
# against a fresh report and update KNOWN_FIELDS by hand if it changed.

# The exact labels the currently-vendored dinspect emits, in its own
# order. Kept here as the KNOWN set so a line the parser recognizes and a
# line it does not are visibly different outcomes -- the latter goes to
# "other" rather than being silently dropped, which is how a field added
# by a future vendored build would otherwise vanish without a trace.
KNOWN_FIELDS = (
    "OS", "Shell", "CPU", "CPU Speed", "CPU Features",
    "Floating Point Unit", "L1 Cache", "L2 Cache",
    "Base Memory", "Ext. Memory",
    "Video", "Video Memory", "Video Chipset",
    "Sound BLASTER", "Sound OPL", "Sound SB DSP", "Sound MPU-401",
    "PicoGUS",
    "Network Packet Driver", "Network IP Config",
    "Floppy drives",
)


def parse_dinspect_report(text):
    """A dinspect `-o` report -> {"fields": {...}, "other": {...}}.

    EVERY KNOWN LABEL IS ALWAYS A KEY IN `fields`, `None` if its line was
    absent -- the same "every key always present" contract
    BoardCapability.snapshot() uses, so a caller never has to branch on
    whether a key exists at all, only on whether its value is None.

    Disk fields (`Disk C`, `Disk D`, ... -- appended dynamically by
    dinspect, one per hard-disk-class drive letter, not in KNOWN_FIELDS,
    and named this way because the line itself is "Disk C: 1263040/...",
    so partitioning on the first ": " leaves "Disk C" as the label rather
    than "C") and any label
    this parser does not recognize both land in `other`, so a change in
    a future vendored build is visible here as new keys rather than
    silently dropped lines.

    Ordering, not just content, matters for one thing: fields written
    with `--show-undetected` still emit their label with a placeholder
    value (dinspect prints "not detected" / "UNKNOWN" rather than
    omitting the line) -- that placeholder is returned as the string
    dinspect wrote, not translated to None here. None means "the label
    never appeared in this report at all" (an older vendored build, or a
    report run without --show-undetected where the field truly was not
    found); it is a different fact from dinspect having looked and found
    nothing, and collapsing the two would lose that distinction.
    """
    fields = dict.fromkeys(KNOWN_FIELDS, None)
    other = {}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        label, sep, value = line.partition(": ")
        if not sep:
            continue
        value = value.rstrip("\r")
        if label in fields:
            fields[label] = value
        else:
            other[label] = value
    return {"fields": fields, "other": other}
