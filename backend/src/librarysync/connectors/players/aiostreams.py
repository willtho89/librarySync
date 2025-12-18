from librarysync.connectors.players.base import PlayerConnector
from librarysync.core.canonical import PlaybackSession


class AIOStreamsConnector(PlayerConnector):
    def __init__(self, base_url: str, api_key: str | None = None):
        self.base_url = base_url
        self.api_key = api_key

    async def fetch_active_sessions(self, user_id: str) -> list[PlaybackSession]:
        raise NotImplementedError("AIOStreams polling not implemented")

    async def validate_config(self, config: dict) -> bool:
        raise NotImplementedError("AIOStreams validation not implemented")
