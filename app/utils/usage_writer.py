"""Ordered bounded ledger persistence for managed workers."""
import queue
import threading


class UsageWriter:
    def __init__(self, path, capacity=1024):
        self.path = path
        self.queue = queue.Queue(maxsize=capacity)
        self.failed = threading.Event()
        self.done = threading.Event()
        self.closed = False
        self.thread = threading.Thread(target=self._run, name="usage-writer", daemon=True)
        self.thread.start()

    def append(self, line):
        if self.closed or self.failed.is_set():
            raise RuntimeError("Usage persistence unavailable")
        try: self.queue.put_nowait(line)
        except queue.Full:
            self.failed.set()
            raise RuntimeError("Usage persistence overloaded") from None

    def _run(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as out:
                while True:
                    line = self.queue.get()
                    try:
                        if line is None: break
                        out.write(line)
                        out.flush()
                    finally: self.queue.task_done()
        except OSError:
            self.failed.set()
        finally: self.done.set()

    def close(self, timeout=5):
        if not self.closed:
            self.closed = True
            try: self.queue.put(None, timeout=timeout)
            except queue.Full: self.failed.set()
        if not self.done.wait(timeout): self.failed.set()
        return not self.failed.is_set()
