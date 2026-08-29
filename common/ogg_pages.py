"""Incremental Ogg page splitter -- the container awareness the Opus audio
stream needs, and deliberately nothing more.

The daemon's Opus side-stream (AudioCapability in daemon/vcctrld.py) frames
one whole Ogg page per WebSocket message, because two things only work at
page granularity:

  1. Skip-ahead. A listener that falls behind is skipped forward (same rule
     as the PCM path); cutting a stream anywhere but a page boundary is
     corruption, cutting at one is a resyncable discontinuity the browser
     decoder rides through (verified: docs/WEBKVM-AUDIO.md, Opus spike).
  2. Header replay. A listener joining a running stream must receive the
     OpusHead/OpusTags pages first; that requires knowing where those pages
     end (verified in the same spike).

This is PAGE-level parsing only: header length comes from the lacing table,
so an 'OggS' inside a page body is never mistaken for a boundary. It never
looks inside packets and never validates CRCs -- the bytes come from our own
ffmpeg over a pipe, and a decoder downstream is the actual integrity check.

Shared-module shape (and flat deploy beside vcctrld.py) for the same reason
as audio_bands.py: the tests exercise the same implementation the daemon
runs, not a copy.
"""

# 27-byte fixed header: capture(4) version(1) type(1) granule(8) serial(4)
# seq(4) crc(4) nsegs(1), then the lacing table, then the body.
_FIXED = 27


def page_granule(page):
    """The page's granule position, signed 64-bit little-endian."""
    return int.from_bytes(page[6:14], "little", signed=True)


class OggPageSplitter:
    """Feed bytes in any sized pieces; get back complete pages.

    Resyncs by scanning forward to the next 'OggS' if the buffer ever loses
    alignment (should not happen on a pipe from a healthy ffmpeg, but a
    truncated write during an encoder crash must not wedge the reader that
    outlives it).
    """

    def __init__(self):
        self.buf = b""

    def feed(self, data):
        self.buf += data
        pages = []
        while True:
            # Align on a capture pattern, discarding any garbage before it.
            i = self.buf.find(b"OggS")
            if i < 0:
                # Keep a tail shorter than the pattern: it may be a prefix
                # of a capture pattern split across two feeds.
                self.buf = self.buf[-3:] if len(self.buf) >= 4 else self.buf
                return pages
            if i:
                self.buf = self.buf[i:]
            if len(self.buf) < _FIXED:
                return pages
            nsegs = self.buf[26]
            if len(self.buf) < _FIXED + nsegs:
                return pages
            body = sum(self.buf[_FIXED:_FIXED + nsegs])
            total = _FIXED + nsegs + body
            if len(self.buf) < total:
                return pages
            pages.append(self.buf[:total])
            self.buf = self.buf[total:]
