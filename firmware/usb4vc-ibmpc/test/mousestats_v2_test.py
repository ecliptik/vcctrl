"""Exercise tools/patch-usb4vc-mousestats.py's v2 block against a fake board.

The block is exec'd verbatim from the tool, with rpi_app's own globals
(xfer_when_not_busy, SPI_MOSI_MAGIC, nop_spi_msg_template) stubbed, so this
tests the text that gets inserted into rpi_app, not a copy of it.
"""
import importlib.util, json, os, sys, tempfile, time, io, contextlib

here = os.path.dirname(os.path.abspath(__file__))
tool = os.path.join(here, "..", "..", "..", "tools", "patch-usb4vc-mousestats.py")
spec = importlib.util.spec_from_file_location("ms", tool)
ms = importlib.util.module_from_spec(spec); spec.loader.exec_module(ms)

fails = 0
def check(name, ok, detail=""):
    global fails
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else "  %r" % (detail,)))
    fails += 0 if ok else 1

class Board:
    """Answers 0x40 always; 0x41 only when `resume` is set (0.5.108)."""
    def __init__(self, counters, resume=None):
        self.counters, self.resume, self.pending, self.asked = counters, resume, None, []
    def xfer(self, msg):
        if msg[0] == 0xde and msg[2] in (0x40, 0x41):
            self.asked.append(msg[2])
            vals = self.counters if msg[2] == 0x40 else self.resume
            if vals is None:
                self.pending = None          # unknown type: no reply armed
                return [0] * 32
            r = [0xcd, msg[1], 0xc0 if msg[2] == 0x40 else 0xc1, 1] + [0] * 28
            for i, v in enumerate(vals):
                r[4 + 2 * i], r[5 + 2 * i] = v & 0xff, v >> 8
            self.pending = r
            return [0] * 32
        r, self.pending = (self.pending or [0] * 32), None
        return r

def run(board):
    d = tempfile.mkdtemp()
    ns = {"os": os, "time": time, "SPI_MOSI_MAGIC": 0xde,
          "nop_spi_msg_template": [0xde, 0, 0] + [0] * 29,
          "xfer_when_not_busy": board.xfer}
    exec(ms.BLOCK_DEF, ns)
    ns["_VC_MS_DIR"] = d
    return ns, os.path.join(d, "mouse_stats.json")

print("mouse-stats v2 block")
c14 = list(range(1, 15))

b = Board(c14, resume=None)                       # firmware 0.5.107
ns, f = run(b)
for _ in range(7):
    ns["_vc_mouse_stats_poll"](0.0)
rec = json.load(open(f))
check("0.5.107: counters read as before", rec["supported"] and rec["counters"]["ev_in"] == 1
      and rec["counters"]["kb_inhibit_retry"] == 14, rec)
check("0.5.107: no 'resume' key (absent, never zeroed)", "resume" not in rec, rec)
check("0.5.107: stops asking for 0x41 after 5 misses", b.asked.count(0x41) == 5, b.asked)

b = Board(c14, resume=[3, 1, 9, 0, 0, 412])       # firmware 0.5.108
ns, f = run(b)
ns["_vc_mouse_stats_poll"](0.0)
rec = json.load(open(f))
check("0.5.108: resume page read with names",
      rec.get("resume") == {"pkt_resumed": 3, "inhibit_retry": 1, "partial_waits": 9,
                            "superseded": 0, "abandon_budget": 0, "max_wait_ms": 412}, rec)
b.resume = [4, 1, 12, 0, 0, 431]
out = io.StringIO()
with contextlib.redirect_stdout(out):
    ns["_vc_mouse_stats_poll"](0.0)
check("0.5.108: a resume prints one timestamped line",
      "VC MOUSE RESUME" in out.getvalue() and "'pkt_resumed': 1" in out.getvalue(), out.getvalue())

print("all passed" if not fails else "%d FAILED" % fails)
sys.exit(1 if fails else 0)
