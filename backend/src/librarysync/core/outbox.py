"""Outbox enqueue helpers."""


async def enqueue_progress(*_args, **_kwargs):
    raise NotImplementedError("Outbox enqueue not implemented")


async def enqueue_completed(*_args, **_kwargs):
    raise NotImplementedError("Outbox enqueue not implemented")
