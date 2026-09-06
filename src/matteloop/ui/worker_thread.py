"""The one QThread every background worker in the UI runs in.

Two rules live here, both answers to issue #39 -- the UI test job that
segfaulted or hung after a job was cancelled. Native stacks of both are in
the commit that added this module.

The worker is started by overriding this QThread's ``run`` method, not by
``thread.started.connect(worker.run)``. ``QThread.start()`` invokes the
override before the event loop begins, so the worker runs even if a caller
immediately requests that the thread quit. Once the worker returns, ``exec``
honours that request and returns promptly. A connection makes the worker a
*receiver* of this QThread, and when the worker is later deleted inside the
thread (``deleteLater`` from its own ``finished``), Qt calls ``disconnectNotify``
on the sender -- this QThread -- from that thread, holding the worker's signal
lock. PySide then looks up a Python override for it, which needs the GIL. If
the GUI thread holds the GIL and wants that same lock, the process deadlocks;
if this thread's Python wrapper is already being destroyed, the lookup reads
freed memory. Without the connection nothing in the worker's destruction
reaches this object.

The thread retires itself only after it has fully finished. ``finished`` is
emitted from inside ``QThreadPrivate::finish``, before the thread flushes
the deferred deletes still queued in it, so deleting the QThread from that
signal races the worker's destruction on the other thread. Waiting from the
first ``finished`` handler -- connected here, ahead of any handler a caller
adds -- means the thread has really ended before any later handler drops a
wrapper or the thread is deleted. The wait covers only that tail and
releases the GIL, which the tail needs.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Slot


class WorkerThread(QThread):
    """Run ``worker.run`` in a new thread and delete both when it is done."""

    def __init__(self, worker: QObject, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker = worker
        worker.moveToThread(self)
        self.finished.connect(self._retire)

    def run(self) -> None:
        self._worker.run()  # type: ignore[attr-defined]
        self.exec()

    @Slot()
    def _retire(self) -> None:
        self.wait()
        self.deleteLater()
