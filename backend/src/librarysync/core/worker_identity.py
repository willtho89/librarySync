from __future__ import annotations

import os
import socket
import uuid

_WORKER_ID = os.environ.get("LIBRARYSYNC_WORKER_ID")
if not _WORKER_ID:
    _WORKER_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def worker_instance_id() -> str:
    return _WORKER_ID
