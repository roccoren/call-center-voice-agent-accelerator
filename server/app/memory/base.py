"""Abstract base class for conversation memory backends."""

import abc
from typing import Optional


class MemoryBackend(abc.ABC):
    """Interface that all memory backends must implement."""

    @abc.abstractmethod
    async def initialize(self) -> bool:
        """Connect to the backing store. Returns True if successful."""
        ...

    @property
    @abc.abstractmethod
    def is_ready(self) -> bool:
        ...

    @abc.abstractmethod
    async def save_turn(
        self,
        caller_id: str,
        role: str,
        text: str,
        *,
        session_id: str = "",
        metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """Persist a conversation turn. Returns doc id or None."""
        ...

    @abc.abstractmethod
    async def get_recent_turns(self, caller_id: str, limit: int = 20) -> list[dict]:
        """Return recent turns for a caller, oldest-first."""
        ...

    @abc.abstractmethod
    async def get_summary(self, caller_id: str) -> str:
        """Return the stored summary for a caller."""
        ...

    @abc.abstractmethod
    async def save_summary(self, caller_id: str, summary: str) -> bool:
        """Upsert the caller summary."""
        ...

    @abc.abstractmethod
    async def build_context_prompt(self, caller_id: str) -> str:
        """Build a memory-context string for session instructions."""
        ...

    @abc.abstractmethod
    async def delete_caller_history(self, caller_id: str) -> int:
        """Delete all turns for a caller. Returns count deleted."""
        ...

    @abc.abstractmethod
    async def close(self):
        """Gracefully close connections."""
        ...
