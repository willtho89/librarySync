from abc import ABC, abstractmethod

from librarysync.core.canonical import ProgressEvent


class ServiceConnector(ABC):
    @abstractmethod
    async def oauth_start(self, user_id: str) -> str:
        raise NotImplementedError

    @abstractmethod
    async def oauth_callback(self, user_id: str, code: str, state: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def refresh_token_if_needed(self, user_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def push_progress(self, user_id: str, event: ProgressEvent) -> None:
        raise NotImplementedError

    @abstractmethod
    async def push_completed(self, user_id: str, event: ProgressEvent) -> None:
        raise NotImplementedError
