"""A record of every step an analysis took, in order, for the "How this recording was checked" part of the report."""
import time
from datetime import datetime


class StepLog:
    def __init__(self):
        self.items = []
        self._open = None
        self._m = 0.0

    def begin(self, title, checks, rec=""):
        self.done("")                      # close a step that wasn't closed
        it = {"t": datetime.now().isoformat(timespec="seconds"), "title": title, "checks": checks,
              "rec": rec, "result": "", "status": "ok", "secs": 0.0}
        self.items.append(it)
        self._open = it
        self._m = time.monotonic()
        return it

    def done(self, result, status="ok"):
        it = self._open
        if not it:
            return
        it["secs"] = round(time.monotonic() - self._m, 1)
        if result:
            it["result"] = result
        it["status"] = status
        self._open = None

    def skip(self, title, checks, why, rec=""):
        self.begin(title, checks, rec)
        self.done(why, "skipped")


def secs_text(s):
    s = float(s or 0)
    if s < 1:
        return "under 1 s"
    if s < 60:
        return f"{s:.0f} s"
    m, sec = divmod(int(round(s)), 60)
    return f"{m} min {sec:02d} s" if m < 60 else f"{m // 60} h {m % 60:02d} min"
