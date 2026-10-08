import asyncio

from librarysync import worker
from librarysync.core import shutdown


def _mode(handler) -> worker.ModeConfig:
    return worker.ModeConfig("test", handler, idle_delay=1.0, busy_delay=0.1)


def test_failures_back_off_exponentially_with_cap() -> None:
    mode = _mode(None)
    assert worker._next_delay(mode, processed=0, consecutive_failures=0) == 1.0
    assert worker._next_delay(mode, processed=3, consecutive_failures=0) == 0.1
    assert worker._next_delay(mode, processed=0, consecutive_failures=3) == 8.0
    assert worker._next_delay(mode, processed=0, consecutive_failures=20) == worker.MAX_FAILURE_BACKOFF_SECONDS


def test_stop_request_ends_loop_after_current_iteration() -> None:
    calls = 0

    async def _scenario() -> None:
        stop = asyncio.Event()

        async def _handler() -> int:
            nonlocal calls
            calls += 1
            worker._request_stop(stop)
            return 1

        await asyncio.wait_for(worker._run_mode_loop(_mode(_handler), 0, stop), timeout=2)

    try:
        asyncio.run(_scenario())
        assert calls == 1
        assert shutdown.shutdown_requested()
    finally:
        shutdown.reset_shutdown()
