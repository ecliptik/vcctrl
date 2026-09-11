"""Frequency-domain audio analysis, shared between the daemon and the
control host -- no numpy (this repo's dependency set stays PyYAML on the Pi;
`requirements.txt`), no hardware access here at all. Everything in this file
operates on plain sample lists or already-computed numbers.

Moved out of `daemon/vcctrld.py`'s `AudioCapability` so the same math has
exactly one implementation: a QA verdict computed in `agent/vcctrl_mcp.py`
must use the identical thresholds `bin/vcctrl-audio` prints, and a reference
file analyzed on the control host must use the identical band math the
daemon uses on the live ring -- two copies of either is how they quietly
drift apart. Imported the same way `common/vcconfig.py` already is from both
sides (see `daemon/vcctrld.py`'s own import block): a plain `import` first,
falling back to loading this file by path.
"""

import cmath
import math
import subprocess

# Octave-spaced: covers where PC game music/SFX actually sits. Above
# ~18 kHz on this capture path is mostly hiss, not content, and a QA check
# gets more robust from fewer, wider bands than from more of them. Each band
# covers [center/sqrt(2), center*sqrt(2)] -- contiguous, since centers
# double each step -- so all 8 tile 70.7 Hz-18.1 kHz with no gaps or overlap.
BAND_HZ = (100, 200, 400, 800, 1600, 3200, 6400, 12800)

# 4096 samples (~85 ms at 48 kHz): short enough that a handful of chunks fit
# in a 1-3 s request, long enough (11.7 Hz/bin) to resolve the lowest band's
# ~70 Hz width into several bins rather than one. This replaced a per-band
# Goertzel point-probe that read real Passage music as silent (measured
# 2026-08-27, docs/lab/FINDINGS.md sec 42): a multi-second Goertzel window has
# sub-Hz resolution, so it was asking "is there energy at EXACTLY 800.000
# Hz" rather than "how much energy is in 566-1131 Hz", and real music is
# essentially never sitting on that exact point.
SPECTRUM_FFT_N = 4096

# A band counts as "active" only if it clears BOTH of these -- within this
# many dB of the loudest band, AND above this absolute floor. The floor half
# matters because digital silence reads as flat across every band (nothing
# is louder than anything else), and without it that flatness would count
# as "every band active" instead of "none". Calibrated against the REAL rig
# (docs/lab/FINDINGS.md sec 42, 2026-08-27), not assumed: real Passage music
# read active_bands 7/8 (band_db -39.5 to -79.2, only the 12.8 kHz band
# excluded -- game audio genuinely has little content there) at the same
# moment `_levels` read mean -35.2 dB; Passage's own silent title screen
# read 0/8 with every band below -100 dB.
BAND_ACTIVE_MARGIN_DB = 30.0
BAND_FLOOR_DB = -65.0

# Below this, `verdict()` treats the reading as no real signal rather than a
# quiet one -- see FINDINGS sec 11 / WEBKVM-AUDIO.md sec 4: a physical
# volume knob sits in the capture path, so absolute level is not comparable
# across sessions, but a floor well under the measured working noise floor
# (-65.6 dB) still distinguishes "nothing on the wire" from "connected and
# quiet". Matches bin/vcctrl-audio's own FLOOR_DB.
LEVEL_FLOOR_DB = -80.0


def fft(x):
    """In-place iterative radix-2 FFT. len(x) MUST be a power of 2.

    Pure Python: this project has no numpy dependency and a QA check is not
    the reason to add one.
    """
    n = len(x)
    if n <= 1:
        return x
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            x[i], x[j] = x[j], x[i]
    length = 2
    while length <= n:
        wlen = cmath.exp(-2j * math.pi / length)
        half = length >> 1
        for i in range(0, n, length):
            w = 1 + 0j
            for k in range(i, i + half):
                u = x[k]
                v = x[k + half] * w
                x[k] = u + v
                x[k + half] = u - v
                w *= wlen
        length <<= 1
    return x


def hann(n):
    """Hann window, one per FFT size -- reduces spectral leakage across the
    band edges in `band_db_from_mono`. Without it, a loud tone near one
    band's boundary smears real energy into its neighbor, which would
    misreport a single tone as "two bands active" purely from window shape,
    not content.
    """
    if n <= 1:
        return [1.0] * n
    return [0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1)) for i in range(n)]


def band_bins(band_hz, n_fft, rate):
    """FFT bin range [lo, hi) for each center frequency in `band_hz`."""
    sqrt2 = 2.0 ** 0.5
    bins = []
    for center in band_hz:
        lo_hz, hi_hz = center / sqrt2, center * sqrt2
        lo = max(1, int(round(lo_hz * n_fft / rate)))
        hi = max(lo + 1, int(round(hi_hz * n_fft / rate)))
        bins.append((lo, hi))
    return bins


def band_db_from_mono(mono, rate=48000, n_fft=SPECTRUM_FFT_N, band_hz=BAND_HZ,
                       window=None, full_scale=32768.0):
    """Per-band energy in dB, from a mono sample sequence.

    Splits `mono` into non-overlapping `n_fft`-sample chunks, applies a Hann
    window, FFTs each, sums |X(k)|**2 over each band's bin range, and
    averages that SUM (never the dB) across chunks -- real music is
    nonstationary, so one frame can land between notes, and averaging
    several ~85ms frames is what makes a brief gap read as "mostly present"
    rather than flipping the verdict.

    Returns None if there are fewer than one full chunk's worth of samples.
    """
    n = len(mono)
    n_chunks = n // n_fft
    if n_chunks < 1:
        return None
    win = window if window is not None else hann(n_fft)
    bins = band_bins(band_hz, n_fft, rate)
    totals = [0.0] * len(band_hz)
    for c in range(n_chunks):
        chunk = mono[c * n_fft:(c + 1) * n_fft]
        windowed = [chunk[i] * win[i] for i in range(n_fft)]
        spec = fft([complex(v) for v in windowed])
        power = [abs(v) * abs(v) for v in spec[:n_fft // 2 + 1]]
        for bi, (lo, hi) in enumerate(bins):
            totals[bi] += sum(power[lo:hi])
    avg = [t / n_chunks for t in totals]

    # Normalize by the window's own energy loss (coherent power gain of a
    # Hann window is 0.375) and by n_fft, so a bin-aligned full-scale tone
    # reads close to 0 dB rather than at some arbitrary offset that would
    # only mean something once BAND_FLOOR_DB was tuned around it.
    norm = (n_fft ** 2) * 0.375

    def db(power):
        return 10.0 * math.log10(power / norm / (full_scale * full_scale)) \
            if power > 0 else -140.0

    return [round(db(p), 2) for p in avg]


def active_bands(band_db, margin_db=BAND_ACTIVE_MARGIN_DB, floor_db=BAND_FLOOR_DB):
    """How many bands are both within `margin_db` of the loudest band and
    above `floor_db`. See BAND_ACTIVE_MARGIN_DB/BAND_FLOOR_DB above for why
    both halves are needed.
    """
    if not band_db:
        return 0
    loudest = max(band_db)
    return sum(1 for d in band_db if d >= loudest - margin_db and d >= floor_db)


def verdict(mean_db, peak_db, hist, active=None, n_bands=None,
            floor_db=LEVEL_FLOOR_DB):
    """The same judgement `bin/vcctrl-audio` prints, as data instead of text.

    `active`/`n_bands` are optional -- a caller with only `_levels` (no
    `_spectrum` reading, e.g. the daemon is old or the call failed) still
    gets the amplitude-only verdict, same as `vcctrl-audio` falls back to
    when its own spectrum call fails.

    Returns {"verdict": "NO_SIGNAL"|"SILENT"|"AUDIO_PRESENT",
             "tone_like": bool or None, "notes": [str, ...]}.
    `tone_like` is None when there isn't enough information to judge it
    (spread not wide enough to rule out amplitude-domain, AND no spectrum
    reading to check directly) -- distinct from False, which is a positive
    "no, this looks like real content" reading.
    """
    notes = []
    # A constant signal has mean == max. Real audio, even near-silence, has
    # a spread -- there is always dither and analog noise. This
    # distinguishes "nothing on the wire" from "connected but quiet", which
    # a threshold on the mean alone cannot do.
    flat = abs(mean_db - peak_db) < 0.05
    if flat and mean_db <= floor_db:
        return {"verdict": "NO_SIGNAL", "tone_like": None, "notes": [
            "mean equals peak at the floor -- that is a constant, not a "
            "quiet passage. The source is disconnected, powered off, or "
            "muted at the hardware. A low volume KNOB does not look like "
            "this; it still carries noise."]}
    if mean_db <= floor_db:
        return {"verdict": "SILENT", "tone_like": None, "notes": [
            "there is a noise floor, so the path is intact and nothing is "
            "playing. Expected at a DOS prompt; a fault during a cell."]}

    spread = peak_db - mean_db
    tone_like = None
    if spread < 3.0:
        tone_like = True
        notes.append(
            "peak sits close to mean, which is more like a steady tone or "
            "hum than music. Music has a wide spread. Worth a listen "
            "before calling this a working sound path.")
    elif active is not None and n_bands is not None and active <= 1:
        tone_like = True
        notes.append(
            "amplitude spread looks wide, but only %d/%d frequency band(s) "
            "are active -- more like an amplitude-modulated tone than "
            "music, which a crest-factor check alone cannot catch. Worth "
            "a listen before calling this a working sound path." % (active, n_bands))
    else:
        tone_like = False
        notes.append(
            "spread is wide, which is what music looks like rather than a "
            "steady tone. This does NOT verify the right notes -- only "
            "that a varying signal is present.")
    return {"verdict": "AUDIO_PRESENT", "tone_like": tone_like, "notes": notes}


def similarity(band_db_a, band_db_b, margin_db=BAND_ACTIVE_MARGIN_DB,
               floor_db=BAND_FLOOR_DB):
    """Jaccard overlap between which bands are "active" in each signal --
    the SAME active-band test `active_bands()`/`_spectrum`'s own verdict
    already uses, applied to both vectors and compared as sets.

    The first version of this function compared raw per-band dB directly
    (cosine similarity on each vector shifted by its own max). Measured
    against synthetic tones it read a pure 800 Hz tone as 78-86% similar to
    a pure 6400 Hz tone -- two signals sharing NOTHING except being quiet at
    the same six bands. Cosine similarity on a mostly-floor vector is
    dominated by the floor agreeing with itself, which is common to nearly
    any narrow-band or quiet source and is not evidence two SPECIFIC signals
    match. Comparing which bands clear the SAME active/floor test this
    module's own verdict logic uses fixes it: two unrelated single tones
    correctly score 0.0, a signal against itself scores 1.0, and a partial
    overlap (e.g. one track's three notes against a mix containing it) reads
    in between rather than being swamped by shared silence.

    Shape-only, deliberately: comparing raw levels at all would compare
    LOUDNESS, which a physical volume knob makes meaningless across
    sessions/recordings (FINDINGS sec 11). Coarse by construction -- 8 bands
    cannot distinguish two tracks with similar broad frequency balance, and
    this says nothing about tempo, melody, or timing. It answers "does the
    live signal's active-band pattern resemble the reference's", not "is
    this exact track playing right now".

    Both-silent is defined as similarity 1.0 ("trivially the same shape:
    nothing") -- callers comparing a live reading against a reference should
    treat a reference that decoded to no active bands at all (too short, or
    genuinely silent) as a reason to distrust the score, not confirm a
    match; `decode_file_to_band_db` returning `None` is the signal for that,
    checked before this function is ever called.
    """
    def active_set(v):
        if not v:
            return set()
        loudest = max(v)
        return {i for i, d in enumerate(v)
                if d >= loudest - margin_db and d >= floor_db}

    a, b = active_set(band_db_a), active_set(band_db_b)
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def decode_file_to_band_db(path, rate=48000, n_fft=SPECTRUM_FFT_N,
                            band_hz=BAND_HZ):
    """Decode a local audio file to mono PCM via ffmpeg and return its
    band_db signature -- the reference side of a `similarity()` comparison.

    Control-host use only in practice: it shells out to a LOCAL ffmpeg
    against a LOCAL file path. Nothing here touches the rig; the live side
    of a comparison comes from a separate `spectrum` call against the
    daemon. ffmpeg is already a project dependency (the same tool
    `AudioCapability` uses for the live ALSA capture), so this adds no new
    one -- just a different `-i`.
    """
    import array as _array
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-i", path, "-ac", "1", "-ar", str(rate),
         "-acodec", "pcm_s16le", "-f", "s16le", "-"],
        capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg could not decode %r: %s"
                           % (path, proc.stderr.decode(errors="replace").strip()))
    a = _array.array("h")
    data = proc.stdout
    a.frombytes(data[:len(data) // 2 * 2])
    mono = [float(v) for v in a]
    return band_db_from_mono(mono, rate=rate, n_fft=n_fft, band_hz=band_hz)
