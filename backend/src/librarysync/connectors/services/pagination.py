from typing import Any


class PagedEntries(list[dict[str, Any]]):
    """Entries collected from a paginated endpoint.

    ``truncated`` is True when collection stopped at the page cap before the
    provider signalled the end of the list, so callers must not treat the
    result as the complete list (e.g. when reconciling removals).
    """

    truncated: bool = False
