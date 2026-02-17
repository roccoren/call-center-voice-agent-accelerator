"""Azure Cosmos DB conversation memory store.

Persists per-caller conversation history so the voice agent can reference
prior interactions.  Each document represents a single conversation turn
(user utterance or AI response) keyed by caller ID.

A lightweight summary is maintained per caller so it can be injected into
the Voice Live session instructions without replaying the full transcript.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
)

from .base import MemoryBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (env vars)
# ---------------------------------------------------------------------------
_COSMOS_ENDPOINT = os.getenv("AZURE_COSMOS_ENDPOINT", "")
_COSMOS_DATABASE = os.getenv("AZURE_COSMOS_DATABASE", "voiceagent")
_COSMOS_CONTAINER = os.getenv("AZURE_COSMOS_CONTAINER", "conversations")
_COSMOS_SUMMARY_CONTAINER = os.getenv("AZURE_COSMOS_SUMMARY_CONTAINER", "summaries")
_COSMOS_KEY = os.getenv("AZURE_COSMOS_KEY", "")  # optional – prefer managed identity
_MANAGED_IDENTITY_CLIENT_ID = os.getenv("AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID", "")

# How many recent turns to retrieve when building context
_MAX_CONTEXT_TURNS = int(os.getenv("MEMORY_MAX_CONTEXT_TURNS", "20"))
# How many recent turns to include in the summary prompt
_MAX_SUMMARY_TURNS = int(os.getenv("MEMORY_MAX_SUMMARY_TURNS", "50"))


class ConversationMemory(MemoryBackend):
    """Async Cosmos DB-backed conversation memory."""

    def __init__(self):
        self._client: Optional[CosmosClient] = None
        self._container = None
        self._summary_container = None
        self._ready = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def initialize(self) -> bool:
        """Connect to Cosmos DB.  Returns True if successful."""
        if not _COSMOS_ENDPOINT:
            logger.warning("AZURE_COSMOS_ENDPOINT not set – memory disabled")
            return False

        try:
            if _COSMOS_KEY:
                self._client = CosmosClient(_COSMOS_ENDPOINT, credential=_COSMOS_KEY)
            elif _MANAGED_IDENTITY_CLIENT_ID:
                credential = ManagedIdentityCredential(
                    client_id=_MANAGED_IDENTITY_CLIENT_ID
                )
                self._client = CosmosClient(_COSMOS_ENDPOINT, credential=credential)
            else:
                credential = DefaultAzureCredential()
                self._client = CosmosClient(_COSMOS_ENDPOINT, credential=credential)

            db = self._client.get_database_client(_COSMOS_DATABASE)
            self._container = db.get_container_client(_COSMOS_CONTAINER)
            self._summary_container = db.get_container_client(
                _COSMOS_SUMMARY_CONTAINER
            )
            self._ready = True
            logger.info("Conversation memory initialized (Cosmos DB)")
            return True
        except Exception:
            logger.exception("Failed to initialize Cosmos DB memory")
            return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    async def save_turn(
        self,
        caller_id: str,
        role: str,
        text: str,
        *,
        session_id: str = "",
        metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """Persist a conversation turn.

        Args:
            caller_id: Phone number or raw ID of the caller (partition key).
            role: ``"user"`` or ``"assistant"``.
            text: The transcribed utterance.
            session_id: Call/session identifier for grouping turns.
            metadata: Arbitrary extra data to store.

        Returns:
            The document ``id`` on success, or ``None``.
        """
        if not self._ready or not text.strip():
            return None

        doc_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        doc = {
            "id": doc_id,
            "callerId": caller_id,
            "sessionId": session_id,
            "role": role,
            "text": text,
            "timestamp": now.isoformat(),
            "epochMs": int(now.timestamp() * 1000),
            "metadata": metadata or {},
        }

        try:
            await self._container.upsert_item(doc)
            logger.debug("Saved turn %s for caller %s", doc_id, caller_id)
            return doc_id
        except Exception:
            logger.exception("Failed to save turn for caller %s", caller_id)
            return None

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    async def get_recent_turns(
        self, caller_id: str, limit: int = _MAX_CONTEXT_TURNS
    ) -> list[dict]:
        """Return the most recent turns for a caller, ordered oldest-first."""
        if not self._ready:
            return []

        query = (
            "SELECT c.role, c.text, c.timestamp, c.sessionId "
            "FROM c WHERE c.callerId = @callerId "
            "ORDER BY c.epochMs DESC OFFSET 0 LIMIT @limit"
        )
        params = [
            {"name": "@callerId", "value": caller_id},
            {"name": "@limit", "value": limit},
        ]

        try:
            items = []
            async for item in self._container.query_items(
                query=query,
                parameters=params,
                partition_key=caller_id,
            ):
                items.append(item)
            items.reverse()  # oldest first
            return items
        except Exception:
            logger.exception("Failed to query turns for %s", caller_id)
            return []

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    async def get_summary(self, caller_id: str) -> str:
        """Return the stored summary for a caller, or empty string."""
        if not self._ready:
            return ""

        try:
            doc = await self._summary_container.read_item(
                item=caller_id, partition_key=caller_id
            )
            return doc.get("summary", "")
        except Exception:
            # Not found or error – fine
            return ""

    async def save_summary(self, caller_id: str, summary: str) -> bool:
        """Upsert the caller summary document."""
        if not self._ready:
            return False

        doc = {
            "id": caller_id,
            "callerId": caller_id,
            "summary": summary,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await self._summary_container.upsert_item(doc)
            logger.info("Updated summary for caller %s", caller_id)
            return True
        except Exception:
            logger.exception("Failed to save summary for %s", caller_id)
            return False

    # ------------------------------------------------------------------
    # Context builder
    # ------------------------------------------------------------------
    async def build_context_prompt(self, caller_id: str) -> str:
        """Build a memory-context string to inject into session instructions.

        Returns an empty string if there is no prior history.
        """
        summary = await self.get_summary(caller_id)
        turns = await self.get_recent_turns(caller_id, limit=10)

        if not summary and not turns:
            return ""

        parts: list[str] = []
        parts.append("\n--- Caller Memory ---")
        parts.append(f"Caller ID: {caller_id}")

        if summary:
            parts.append(f"Summary of previous interactions:\n{summary}")

        if turns:
            parts.append("Recent conversation history:")
            for t in turns:
                role_label = "Caller" if t["role"] == "user" else "Agent"
                parts.append(f"  [{role_label}] {t['text']}")

        parts.append("--- End Memory ---\n")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    async def delete_caller_history(self, caller_id: str) -> int:
        """Delete all turns for a caller.  Returns count deleted."""
        if not self._ready:
            return 0

        query = "SELECT c.id FROM c WHERE c.callerId = @callerId"
        params = [{"name": "@callerId", "value": caller_id}]
        deleted = 0

        try:
            async for item in self._container.query_items(
                query=query, parameters=params, partition_key=caller_id
            ):
                await self._container.delete_item(
                    item=item["id"], partition_key=caller_id
                )
                deleted += 1

            # Also remove summary
            try:
                await self._summary_container.delete_item(
                    item=caller_id, partition_key=caller_id
                )
            except Exception:
                pass

            logger.info("Deleted %d turns for caller %s", deleted, caller_id)
            return deleted
        except Exception:
            logger.exception("Failed to delete history for %s", caller_id)
            return 0

    async def close(self):
        """Close the Cosmos client."""
        if self._client:
            await self._client.close()


# Singleton instance
memory = ConversationMemory()
