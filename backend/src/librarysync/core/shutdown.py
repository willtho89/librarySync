"""Process-wide graceful shutdown flag for worker loops.

The worker sets it on SIGTERM/SIGINT; long-running batches check it between
units of work so claimed jobs can be released instead of stranded.
"""

_shutdown_requested = False


def request_shutdown() -> None:
    global _shutdown_requested
    _shutdown_requested = True


def shutdown_requested() -> bool:
    return _shutdown_requested


def reset_shutdown() -> None:
    global _shutdown_requested
    _shutdown_requested = False
