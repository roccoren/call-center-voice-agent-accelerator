"""mem0-backed semantic conversation memory using Azure AI Search.

Uses the mem0 Python library with:
- Azure AI Search as vector store (API key auth)
- Azure OpenAI via managed identity (token-based auth, no API key needed)

This is self-contained for the call center app — does NOT share state
with the OpenClaw mem0 instance.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from .base import MemoryBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (env vars)
# ---------------------------------------------------------------------------
_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "")
_SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY", "")
_OPENAI_ENDPOINT = os.getenv("MEM0_OPENAI_ENDPOINT", "")  # Azure OpenAI endpoint
_OPENAI_API_KEY = os.getenv("MEM0_OPENAI_API_KEY", "")  # optional — falls back to managed identity
_MANAGED_IDENTITY_CLIENT_ID = os.getenv("AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID", "")
_LLM_DEPLOYMENT = os.getenv("MEM0_LLM_DEPLOYMENT", "gpt-4o-mini")
_LLM_API_VERSION = os.getenv("MEM0_LLM_API_VERSION", "2025-01-01-preview")
_EMBEDDING_DEPLOYMENT = os.getenv("MEM0_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
_EMBEDDING_API_VERSION = os.getenv("MEM0_EMBEDDING_API_VERSION", "2023-05-15")
_COLLECTION_NAME = os.getenv("MEM0_COLLECTION_NAME", "voice_agent_memories")


def _get_openai_api_key() -> str:
    """Get Azure OpenAI API key — use env var or acquire a token via managed identity."""
    if _OPENAI_API_KEY:
        return _OPENAI_API_KEY

    # Use managed identity to get an AAD token for Azure OpenAI
    try:
        from azure.identity import ManagedIdentityCredential, DefaultAzureCredential

        if _MANAGED_IDENTITY_CLIENT_ID:
            credential = ManagedIdentityCredential(client_id=_MANAGED_IDENTITY_CLIENT_ID)
        else:
            credential = DefaultAzureCredential()

        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        return token.token
    except Exception:
        logger.exception("Failed to acquire Azure OpenAI token via managed identity")
        return ""


def _search_service_name() -> str:
    """Extract service name from endpoint URL."""
    return _SEARCH_ENDPOINT.replace("https://", "").split(".")[0]


def _build_mem0_config(api_key: str) -> dict:
    """Build mem0 config dict."""
    return {
        "llm": {
            "provider": "azure_openai",
            "config": {
                "model": _LLM_DEPLOYMENT,
                "temperature": 0.1,
                "max_tokens": 2000,
                "azure_kwargs": {
                    "azure_deployment": _LLM_DEPLOYMENT,
                    "api_version": _LLM_API_VERSION,
                    "azure_endpoint": _OPENAI_ENDPOINT,
                    "api_key": api_key,
                },
            },
        },
        "embedder": {
            "provider": "azure_openai",
            "config": {
                "model": _EMBEDDING_DEPLOYMENT,
                "azure_kwargs": {
                    "azure_deployment": _EMBEDDING_DEPLOYMENT,
                    "api_version": _EMBEDDING_API_VERSION,
                    "azure_endpoint": _OPENAI_ENDPOINT,
                    "api_key": api_key,
                },
            },
        },
        "vector_store": {
            "provider": "azure_ai_search",
            "config": {
                "service_name": _search_service_name(),
                "api_key": _SEARCH_KEY,
                "collection_name": _COLLECTION_NAME,
                "embedding_model_dims": 1536,
            },
        },
    }


class Mem0Memory(MemoryBackend):
    """mem0 semantic memory backed by Azure AI Search."""

    def __init__(self):
        self._mem0 = None
        self._ready = False
        self._session_turns: dict[str, list[dict]] = {}

    async def initialize(self) -> bool:
        if not _SEARCH_ENDPOINT or not _SEARCH_KEY:
            logger.warning("AZURE_SEARCH_ENDPOINT/KEY not set — mem0 memory disabled")
            return False
        if not _OPENAI_ENDPOINT:
            logger.warning("MEM0_OPENAI_ENDPOINT not set — mem0 memory disabled")
            return False

        try:
            api_key = _get_openai_api_key()
            if not api_key:
                logger.warning("Could not obtain Azure OpenAI API key — mem0 disabled")
                return False

            config = _build_mem0_config(api_key)

            def _init():
                from mem0 import Memory
                return Memory.from_config(config)

            self._mem0 = await asyncio.to_thread(_init)
            self._ready = True
            logger.info("Conversation memory initialized (mem0 + Azure AI Search)")
            return True
        except Exception:
            logger.exception("Failed to initialize mem0 memory")
            return False

    @property
    def is_ready(self) -> bool:
        return self._ready

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

        # Buffer for session
        if caller_id not in self._session_turns:
            self._session_turns[caller_id] = []
        self._session_turns[caller_id].append({
            "role": role,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sessionId": session_id,
        })

        # Add to mem0 (extracts facts via LLM)
        user_id = _caller_to_user_id(caller_id)
        mem0_role = "user" if role == "user" else "assistant"
        messages = [{"role": mem0_role, "content": text}]

        try:
            result = await asyncio.to_thread(
                self._mem0.add, messages, user_id=user_id
            )
            results = result.get("results", [])
            doc_id = results[0].get("id", "") if results else ""
            logger.debug("mem0 add for %s: %s", caller_id, doc_id)
            return doc_id or "ok"
        except Exception:
            logger.exception("Failed to save turn to mem0 for %s", caller_id)
            return None

    async def get_recent_turns(
        self, caller_id: str, limit: int = 20
    ) -> list[dict]:
        turns = self._session_turns.get(caller_id, [])
        return turns[-limit:]

    async def get_summary(self, caller_id: str) -> str:
        return ""

    async def save_summary(self, caller_id: str, summary: str) -> bool:
        if not self._ready:
            return False
        user_id = _caller_to_user_id(caller_id)
        try:
            await asyncio.to_thread(
                self._mem0.add,
                [{"role": "user", "content": f"[summary] {summary}"}],
                user_id=user_id,
            )
            return True
        except Exception:
            logger.exception("Failed to save summary to mem0")
            return False

    async def build_context_prompt(self, caller_id: str) -> str:
        """Retrieve distilled semantic memories for this caller."""
        if not self._ready:
            return ""

        user_id = _caller_to_user_id(caller_id)
        try:
            result = await asyncio.to_thread(
                self._mem0.get_all, user_id=user_id
            )
            memories = result.get("results", [])
            if not memories:
                return ""

            parts: list[str] = [
                "\n--- Caller Memory (semantic) ---",
                f"Caller ID: {caller_id}",
                f"Known facts about this caller ({len(memories)} memories):",
            ]
            for m in memories[:20]:
                memory_text = m.get("memory", "")
                if memory_text:
                    parts.append(f"  • {memory_text}")
            parts.append("--- End Memory ---\n")
            return "\n".join(parts)
        except Exception:
            logger.exception("Failed to build mem0 context for %s", caller_id)
            return ""

    async def delete_caller_history(self, caller_id: str) -> int:
        if not self._ready:
            return 0

        user_id = _caller_to_user_id(caller_id)
        try:
            result = await asyncio.to_thread(
                self._mem0.get_all, user_id=user_id
            )
            memories = result.get("results", [])
            deleted = 0
            for m in memories:
                mid = m.get("id", "")
                if mid:
                    try:
                        await asyncio.to_thread(self._mem0.delete, mid)
                        deleted += 1
                    except Exception:
                        pass

            self._session_turns.pop(caller_id, None)
            logger.info("Deleted %d mem0 memories for caller %s", deleted, caller_id)
            return deleted
        except Exception:
            logger.exception("Failed to delete mem0 history for %s", caller_id)
            return 0

    async def close(self):
        for caller_id, turns in self._session_turns.items():
            if len(turns) >= 2:
                user_id = _caller_to_user_id(caller_id)
                messages = []
                for t in turns:
                    role = "user" if t["role"] == "user" else "assistant"
                    messages.append({"role": role, "content": t["text"]})
                try:
                    await asyncio.to_thread(
                        self._mem0.add, messages, user_id=user_id
                    )
                    logger.info("Flushed %d turns to mem0 for %s", len(turns), caller_id)
                except Exception:
                    logger.exception("Failed to flush session to mem0 for %s", caller_id)
        self._session_turns.clear()


def _caller_to_user_id(caller_id: str) -> str:
    clean = caller_id.strip().replace(" ", "").replace("-", "")
    if clean.startswith("+"):
        return f"caller-{clean[1:]}"
    return f"caller-{clean}"
