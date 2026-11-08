"""Ctrl-C while a lake statement runs: noticed at once during a load in the main thread, and
DuckDB's own answer to it given back as `KeyboardInterrupt`.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import socket
import threading
from collections.abc import Callable
from types import FrameType


def _on_ctrl_c(on_interrupt: Callable[[], None] | None, pressed: list[bool]) -> Callable[[], None]:
    """While a load runs in the main thread, Ctrl-C calls `on_interrupt` at once and is noted in
    `pressed`; the load then raises `KeyboardInterrupt` itself. Returns the restoring function.

    Python runs signal handlers only in the main thread, and only when it gets control: DuckDB,
    waiting for a stream blocked on a quiet network read, may not give it back until the next
    rows arrive (7.9 s measured on CI). So the signal is also watched from another thread through
    Python's wakeup file descriptor, which the C-level handler writes to as the signal arrives.
    """
    if on_interrupt is None or threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = signal.getsignal(signal.SIGINT)
    if previous is signal.SIG_IGN or previous is None:  # Ctrl-C is ignored, or not Python's
        return lambda: None

    once = threading.Lock()

    def interrupt() -> None:
        # Both the handler and the watcher see the signal; the source is closed once.
        with once:
            first = not pressed
            pressed.append(True)
        if first:
            with contextlib.suppress(Exception):
                on_interrupt()

    def handler(number: int, frame: FrameType | None) -> None:
        interrupt()

    receiver, sender = socket.socketpair()
    sender.setblocking(False)
    stop = threading.Event()
    try:
        signal.signal(signal.SIGINT, handler)
        earlier = signal.set_wakeup_fd(sender.fileno(), warn_on_full_buffer=False)
    except ValueError:  # not the main interpreter
        signal.signal(signal.SIGINT, previous)
        receiver.close()
        sender.close()
        return lambda: None

    def watch() -> None:
        while not stop.is_set():
            ready, _, _ = select.select([receiver], [], [], 0.2)
            if not ready:
                continue
            with contextlib.suppress(OSError):
                received = receiver.recv(256)
                if earlier != -1:
                    os.write(earlier, received)  # whoever watched signals before still sees them
                if signal.SIGINT in received:
                    interrupt()

    watcher = threading.Thread(target=watch, name="load-ctrl-c", daemon=True)
    watcher.start()

    def restore() -> None:
        stop.set()
        signal.set_wakeup_fd(earlier)
        signal.signal(signal.SIGINT, previous)
        watcher.join()
        receiver.close()
        sender.close()

    return restore


def _user_interrupt(error: RuntimeError) -> bool:
    """Whether a `RuntimeError` from a statement is Ctrl-C.

    DuckDB's Python client (measured on 2.0) answers Ctrl-C during a statement by interrupting
    it and raising `RuntimeError("Query interrupted")`, consuming the signal. It is given back to
    the caller as `KeyboardInterrupt`, so an interrupted program stops as Python programs do
    (the transaction still rolls back on the way out). `test_ctrl_c_is_a_keyboard_interrupt`
    pins the measured behavior.
    """
    return type(error) is RuntimeError and str(error) == "Query interrupted"
