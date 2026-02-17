"""Azure AI Search conversation memory store.

Alternative to Cosmos DB.  Uses Azure AI Search as a vector/keyword store
for per-caller conversation history and summaries.  Documents are indexed
with caller ID as a filterable field, enabling efficient per-caller lookups
and optional semantic search over past conversations.
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from azure.identity.aio import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
)
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SimpleField,
    SearchableField,
    SearchFieldDataType,
    SearchField,
)

from .base import MemoryBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (env vars)
# ---------------------------------------------------------------------------
_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "")
_SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY", "")  # optional – prefer managed identity
_SEARCH_TURNS_INDEX = os.getenv("AZURE_SEARCH_TURNS_INDEX", "conversation-turns")
_SEARCH_SUMMARIES_INDEX = os.getenv("AZURE_SEARCH_SUMMARIES_INDEX", "conversation-summaries")
_MANAGED_IDENTITY_CLIENT_ID = os.getenv("AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID", "")

_MAX_CONTEXT_TURNS = int(os.getenv("MEMORY_MAX_CONTEXT_TURNS", "20"))


def _build_turns_index(name: str) -> SearchIndex:
    """Define the search index schema for conversation turns."""
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SimpleField(name="callerId", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="sessionId", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="role", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="text", type=SearchFieldDataType.String),
        SimpleField(name="timestamp", type=SearchFieldDataType.String, sortable=True),
        SimpleField(name="epochMs", type=SearchFieldDataType.Int64, sortable=True, filterable=True),
    ]
    return SearchIndex(name=name, fields=fields)


def _build_summaries_index(name: str) -> SearchIndex:
    """Define the search index schema for caller summaries."""
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SimpleField(name="callerId", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="summary", type=SearchFieldDataType.String),
        SimpleField(name="updatedAt", type=SearchFieldDataType.String, sortable=True),
    ]
    return SearchIndex(name=name, fields=fields)


class AISearchMemory(MemoryBackend):
    """Async Azure AI Search-backed conversation memory."""

    def __init__(self):
        self._turns_client: Optional[SearchClient] = None
        self._summaries_client: Optional[SearchClient] = None
        self._ready = False
        self._credential = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def initialize(self) -> bool:
        if not _SEARCH_ENDPOINT:
            logger.warning("AZURE_SEARCH_ENDPOINT not set – AI Search memory disabled")
            return False

        try:
            if _SEARCH_KEY:
                from azure.core.credentials import AzureKeyCredential
                self._credential = AzureKeyCredential(_SEARCH_KEY)
            elif _MANAGED_IDENTITY_CLIENT_ID:
                self._credential = ManagedIdentityCredential(
                    client_id=_MANAGED_IDENTITY_CLIENT_ID
                )
            else:
                self._credential = DefaultAzureCredential()

            # Ensure indexes exist
            index_client = SearchIndexClient(
                endpoint=_SEARCH_ENDPOINT, credential=self._credential
            )
            try:
                existing = []
                async for idx in index_client.list_index_names():
                    existing.append(idx)

                if _SEARCH_TURNS_INDEX not in existing:
                    logger.info("Creating turns index: %s", _SEARCH_TURNS_INDEX)
                    await index_client.create_index(_build_turns_index(_SEARCH_TURNS_INDEX))

                if _SEARCH_SUMMARIES_INDEX not in existing:
                    logger.info("Creating summaries index: %s", _SEARCH_SUMMARIES_INDEX)
                    await index_client.create_index(_build_summaries_index(_SEARCH_SUMMARIES_INDEX))
            finally:
                await index_client.close()

            self._turns_client = SearchClient(
                endpoint=_SEARCH_ENDPOINT,
                index_name=_SEARCH_TURNS_INDEX,
                credential=self._credential,
            )
            self._summaries_client = SearchClient(
                endpoint=_SEARCH_ENDPOINT,
                index_name=_SEARCH_SUMMARIES_INDEX,
                credential=self._credential,
            )
            self._ready = True
            logger.info("Conversation memory initialized (Azure AI Search)")
            return True
        except Exception:
            logger.exception("Failed to initialize AI Search memory")
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
        }

        try:
            await self._turns_client.upload_documents([doc])
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
        if not self._ready:
            return []

        try:
            results = self._turns_client.search(
                search_text="*",
                filter=f"callerId eq '{_escape(caller_id)}'",
                order_by=["epochMs desc"],
                top=limit,
                select=["role", "text", "timestamp", "sessionId"],
            )
            items = []
            async for r in results:
                items.append({
                    "role": r["role"],
                    "text": r["text"],
                    "timestamp": r["timestamp"],
                    "sessionId": r.get("sessionId", ""),
                })
            items.reverse()  # oldest first
            return items
        except Exception:
            logger.exception("Failed to query turns for %s", caller_id)
            return []

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    async def get_summary(self, caller_id: str) -> str:
        if not self._ready:
            return ""

        try:
            doc = await self._summaries_client.get_document(key=_summary_id(caller_id))
            return doc.get("summary", "")
        except Exception:
            return ""

    async def save_summary(self, caller_id: str, summary: str) -> bool:
        if not self._ready:
            return False

        doc = {
            "id": _summary_id(caller_id),
            "callerId": caller_id,
            "summary": summary,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await self._summaries_client.upload_documents([doc])
            logger.info("Updated summary for caller %s", caller_id)
            return True
        except Exception:
            logger.exception("Failed to save summary for %s", caller_id)
            return False

    # ------------------------------------------------------------------
    # Context builder
    # ------------------------------------------------------------------
    async def build_context_prompt(self, caller_id: str) -> str:
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
        if not self._ready:
            return 0

        deleted = 0
        try:
            # Find all turn docs for this caller
            results = self._turns_client.search(
                search_text="*",
                filter=f"callerId eq '{_escape(caller_id)}'",
                select=["id"],
                top=1000,
            )
            doc_ids = []
            async for r in results:
                doc_ids.append({"id": r["id"]})

            if doc_ids:
                await self._turns_client.delete_documents(doc_ids)
                deleted = len(doc_ids)

            # Also delete summary
            try:
                await self._summaries_client.delete_documents([{"id": _summary_id(caller_id)}])
            except Exception:
                pass

            logger.info("Deleted %d turns for caller %s", deleted, caller_id)
            return deleted
        except Exception:
            logger.exception("Failed to delete history for %s", caller_id)
            return 0

    async def close(self):
        if self._turns_client:
            await self._turns_client.close()
        if self._summaries_client:
            await self._summaries_client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _escape(value: str) -> str:
    """Escape single quotes for OData filter expressions."""
    return value.replace("'", "''")


def _summary_id(caller_id: str) -> str:
    """Deterministic document ID for a caller's summary."""
    # AI Search keys can't contain certain chars; use a safe hash
    import hashlib
    return hashlib.sha256(caller_id.encode()).hexdigest()[:32]
