"""Memory backend factory.

Set ``MEMORY_BACKEND`` env var to choose the backing store:

- ``cosmosdb``  (default) — Azure Cosmos DB
- ``aisearch``  — Azure AI Search

The factory returns a singleton ``MemoryBackend`` instance.
"""

import logging
import os

from .base import MemoryBackend

logger = logging.getLogger(__name__)

_BACKEND = os.getenv("MEMORY_BACKEND", "cosmosdb").lower().strip()


def get_memory() -> MemoryBackend:
    """Return the configured memory backend singleton."""
    if _BACKEND in ("aisearch", "ai_search", "search"):
        from .ai_search_memory import AISearchMemory
        return AISearchMemory()
    else:
        from .cosmos_memory import ConversationMemory
        return ConversationMemory()


# Singleton — importable as ``from app.memory.factory import memory``
memory = get_memory()
