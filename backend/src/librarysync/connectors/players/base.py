from abc import ABC, abstractmethod

from librarysync.core.canonical import PlaybackSession


class PlayerConnector(ABC):
    @abstractmethod
    async def fetch_active_sessions(self, user_id: str) -> list[PlaybackSession]:
        raise NotImplementedError

    @abstractmethod
    async def validate_config(self, config: dict) -> bool:
        raise NotImplementedError
