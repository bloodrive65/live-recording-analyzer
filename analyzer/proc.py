"""Run an analysis in its own process, so reading a big recording never slows down the window.

The child process runs at below-normal priority (and so do ffprobe/ffmpeg, which inherit it on Windows),
and sends progress back through a queue."""
import multiprocessing as mp
import queue
import sys
import time
import traceback


def _child(args, q, cancel_evt, reports_dir):
    try:
        if sys.platform == "win32":
            import psutil
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass
    try:
        from analyzer import analyze, report
        log_path, videos, monitor_path, deep, rec_filter = args
        last = [0.0]

        def prog(m, p):
            now = time.monotonic()
            if now - last[0] >= 0.15 or p >= 1:      # don't flood the window with updates
                last[0] = now
                q.put(("progress", str(m), float(p)))

        res = analyze.run(log_path, video_paths=videos, monitor_path=monitor_path, deep=deep,
                          progress=prog, cancel=cancel_evt.is_set, recordings_filter=rec_filter)
        if cancel_evt.is_set():
            q.put(("failed", "Cancelled."))
            return
        path = report.save(res, reports_dir)
        q.put(("done", res, path))
    except Exception:
        q.put(("failed", traceback.format_exc()))


class Job:
    """Start with start(); read messages with get(timeout). Messages: ("progress", msg, p), ("done", res, path), ("failed", text)."""

    def __init__(self, args, reports_dir):
        ctx = mp.get_context("spawn")
        self.q = ctx.Queue()
        self.cancel_evt = ctx.Event()
        self.p = ctx.Process(target=_child, args=(args, self.q, self.cancel_evt, reports_dir), daemon=True)
        self._cancel_t = None

    def start(self):
        self.p.start()

    def cancel(self):
        self.cancel_evt.set()
        self._cancel_t = time.monotonic()

    def get(self, timeout=0.5):
        """Next message, or None. Returns ("failed", ...) if the process ended without a result."""
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            pass
        if self._cancel_t and time.monotonic() - self._cancel_t > 8 and self.p.is_alive():
            self.p.terminate()                  # it didn't stop by itself in time
            return ("failed", "Cancelled.")
        if not self.p.is_alive():
            try:
                return self.q.get(timeout=1.0)
            except queue.Empty:
                return ("failed", "Cancelled." if self._cancel_t else
                        f"The analysis stopped unexpectedly (exit code {self.p.exitcode}).")
        return None
