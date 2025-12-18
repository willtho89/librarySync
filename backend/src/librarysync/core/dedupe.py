"""Progress dedupe/coalesce rules."""


def should_emit_progress(
    previous_progress: float | None,
    current_progress: float,
    seconds_since_last_emit: int | None,
    min_delta: float = 1.0,
    min_interval_seconds: int = 60,
) -> bool:
    raise NotImplementedError("Dedupe logic not implemented")
