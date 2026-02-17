"""Simple in-memory registry to map ACS call connection IDs to caller IDs.

When an incoming call arrives (AcsEventHandler), we register the mapping.
When the ACS WebSocket connects, the media handler looks up the caller ID.
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# callConnectionId -> {"callerId": str, "sessionId": str, "registeredAt": float}
_registry: dict[str, dict] = {}
_lock = asyncio.Lock()

# Auto-expire entries after 10 minutes
_TTL_SECONDS = 600


async def register(call_connection_id: str, caller_id: str, session_id: str = ""):
    async with _lock:
        _registry[call_connection_id] = {
            "callerId": caller_id,
            "sessionId": session_id,
            "registeredAt": time.time(),
        }
    logger.info("Registered caller %s for connection %s", caller_id, call_connection_id)


async def lookup(call_connection_id: str) -> Optional[dict]:
    async with _lock:
        entry = _registry.get(call_connection_id)
        if entry and (time.time() - entry["registeredAt"]) < _TTL_SECONDS:
            return entry
        # Expired or not found
        _registry.pop(call_connection_id, None)
        return None


async def unregister(call_connection_id: str):
    async with _lock:
        _registry.pop(call_connection_id, None)


async def cleanup():
    """Remove expired entries."""
    now = time.time()
    async with _lock:
        expired = [
            k for k, v in _registry.items() if now - v["registeredAt"] > _TTL_SECONDS
        ]
        for k in expired:
            del _registry[k]
