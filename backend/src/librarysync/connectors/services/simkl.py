from librarysync.connectors.services.base import ServiceConnector
from librarysync.core.canonical import ProgressEvent


class SimklConnector(ServiceConnector):
    async def oauth_start(self, user_id: str) -> str:
        raise NotImplementedError("SIMKL OAuth start not implemented")

    async def oauth_callback(self, user_id: str, code: str, state: str) -> None:
        raise NotImplementedError("SIMKL OAuth callback not implemented")

    async def refresh_token_if_needed(self, user_id: str) -> None:
        raise NotImplementedError("SIMKL token refresh not implemented")

    async def push_progress(self, user_id: str, event: ProgressEvent) -> None:
        raise NotImplementedError("SIMKL progress push not implemented")

    async def push_completed(self, user_id: str, event: ProgressEvent) -> None:
        raise NotImplementedError("SIMKL completion push not implemented")
