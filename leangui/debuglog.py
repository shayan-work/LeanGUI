"""Per-run debug log: every line the Debug panel shows is also written to
debug_logs/[TIMESTAMP]_[CENTERFREQ]_[SYMBOLRATE].txt (Hz, integers; NA when
the value is unknown - e.g. a recorded file has no RF center frequency)."""
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "debug_logs"


def _hz(value):
    return "NA" if value is None else str(int(round(value)))


class DebugLog:
    def __init__(self, center_hz, symbol_rate_hz, header_lines=()):
        LOG_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.path = LOG_DIR / f"{stamp}_{_hz(center_hz)}_{_hz(symbol_rate_hz)}.txt"
        self._f = open(self.path, "w", buffering=1)  # line-buffered: survives a crash
        self._f.write(f"# LeanGUI debug log, started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        for line in header_lines:
            self._f.write(f"# {line}\n")

    def write(self, line):
        if self._f is None:
            return
        self._f.write(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {line}\n")

    def close(self):
        if self._f is not None:
            self._f.write(f"# closed {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            self._f.close()
            self._f = None
