# Hardware I/O runs on a background QThread (Qt widgets aren't thread-safe); results
# come back to the GUI thread via a Signal, which Qt marshals across threads.

from PySide6.QtCore import QThread, Signal


class Worker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)


def wait_for(worker, timeout_ms=10000):
    """Block until worker finishes, so it isn't destroyed mid-run during shutdown -
    Qt aborts the process if a running QThread is destroyed (reachable just by closing
    the window during the automatic connect). Bounded because a serial read can block
    far longer than anyone wants to wait on a window close."""
    if worker is not None and worker.isRunning():
        worker.wait(timeout_ms)
