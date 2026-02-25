"""mem0-backed semantic conversation memory.

Uses mem0 (with Qdrant vector store + Azure OpenAI) to extract and store
semantic memories from conversations.  Unlike raw transcript storage,
mem0 distills conversations into meaningful facts and preferences that
can be retrieved via semantic search.

This backend can run standalone OR alongside Cosmos DB / AI Search:
- Cosmos/Search stores raw transcripts (audit trail)
- mem0 stores distilled semantic memories (context injection)

Requires the mem0 CLI to be available at /usr/local/bin/mem0, backed by
the mem0-integration setup on the gateway.
"""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Optional

from azure.identity import DefaultAzureCredential

from .base import MemoryBackend

logger = logging.getLogger(__name__)

_MEM0_CLI = "/usr/local/bin/mem0"
_MAX_CONTEXT_TURNS = 20


class Mem0Memory(MemoryBackend):
    """Async mem0-backed conversation memory using the CLI."""

    def __init__(self):
        self._ready = False
        self._identity_credential: Optional[DefaultAzureCredential] = None
        self._azure_openai_api_key: str = ""
        self._search_api_key: str = ""
        self._azure_kwargs: dict = {}
        self._vector_store_config: dict = {}
        # In-memory buffer for current session turns (flushed to mem0 on close)
        self._session_turns: dict[str, list[dict]] = {}  # caller_id -> turns

    async def initialize(self) -> bool:
        """Check identity auth and mem0 CLI availability."""
        try:
            managed_identity_client_id = (
                os.getenv("AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID") or None
            )
            self._identity_credential = DefaultAzureCredential(
                managed_identity_client_id=managed_identity_client_id
            )
            credential = self._identity_credential

            self._azure_openai_api_key = await asyncio.to_thread(
                lambda: credential.get_token(
                    "https://cognitiveservices.azure.com/.default"
                ).token
            )
            self._search_api_key = await asyncio.to_thread(
                lambda: credential.get_token(
                    "https://search.azure.com/.default"
                ).token
            )

            # Keep config shape aligned with mem0 Azure auth expectations.
            self._azure_kwargs = {"api_key": self._azure_openai_api_key}
            self._vector_store_config = {"api_key": self._search_api_key}

            result = await asyncio.to_thread(
                subprocess.run,
                [_MEM0_CLI, "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                self._ready = True
                logger.info("Conversation memory initialized (mem0)")
                return True
            else:
                logger.warning("mem0 CLI not functional: %s", result.stderr)
                return False
        except FileNotFoundError:
            logger.warning("mem0 CLI not found at %s — memory disabled", _MEM0_CLI)
            return False
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
        """Buffer a turn and also add it to mem0 for extraction."""
        if not self._ready or not text.strip():
            return None

        # Buffer for session context
        if caller_id not in self._session_turns:
            self._session_turns[caller_id] = []
        turn = {
            "role": role,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sessionId": session_id,
        }
        self._session_turns[caller_id].append(turn)

        # Add to mem0 as a message (mem0 extracts facts automatically)
        user_id = _caller_to_user_id(caller_id)
        try:
            mem0_role = "user" if role == "user" else "assistant"
            message = f"[{mem0_role}] {text}"
            result = await asyncio.to_thread(
                subprocess.run,
                [_MEM0_CLI, "add", user_id, message],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout)
                    doc_id = data.get("results", [{}])[0].get("id", "")
                    logger.debug("mem0 add for %s: %s", caller_id, doc_id)
                    return doc_id
                except (json.JSONDecodeError, IndexError):
                    return "ok"
            else:
                logger.warning("mem0 add failed: %s", result.stderr)
                return None
        except Exception:
            logger.exception("Failed to save turn to mem0 for %s", caller_id)
            return None

    async def get_recent_turns(
        self, caller_id: str, limit: int = _MAX_CONTEXT_TURNS
    ) -> list[dict]:
        """Return buffered in-memory turns for current session."""
        turns = self._session_turns.get(caller_id, [])
        return turns[-limit:]

    async def get_summary(self, caller_id: str) -> str:
        """Not used for mem0 — see build_context_prompt instead."""
        return ""

    async def save_summary(self, caller_id: str, summary: str) -> bool:
        """Add summary as a mem0 memory."""
        if not self._ready:
            return False
        user_id = _caller_to_user_id(caller_id)
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [_MEM0_CLI, "add", user_id, f"[summary] {summary}"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode == 0
        except Exception:
            logger.exception("Failed to save summary to mem0")
            return False

    async def build_context_prompt(self, caller_id: str) -> str:
        """Search mem0 for relevant memories about this caller.

        This is the key advantage of mem0: instead of replaying raw
        transcripts, we retrieve distilled semantic memories.
        """
        if not self._ready:
            return ""

        user_id = _caller_to_user_id(caller_id)
        try:
            # Get all memories for this caller
            result = await asyncio.to_thread(
                subprocess.run,
                [_MEM0_CLI, "list", user_id],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return ""

            data = json.loads(result.stdout)
            # mem0 list returns {"results": [{"id": ..., "memory": ..., ...}]}
            memories = data.get("results", [])
            if not memories:
                return ""

            parts: list[str] = []
            parts.append("\n--- Caller Memory (semantic) ---")
            parts.append(f"Caller ID: {caller_id}")
            parts.append(f"Known facts about this caller ({len(memories)} memories):")
            for m in memories[:20]:  # cap at 20 memories
                memory_text = m.get("memory", "")
                if memory_text:
                    parts.append(f"  • {memory_text}")
            parts.append("--- End Memory ---\n")
            return "\n".join(parts)

        except Exception:
            logger.exception("Failed to build mem0 context for %s", caller_id)
            return ""

    async def delete_caller_history(self, caller_id: str) -> int:
        """Delete all mem0 memories for a caller."""
        if not self._ready:
            return 0

        user_id = _caller_to_user_id(caller_id)
        try:
            # List all memories first
            result = await asyncio.to_thread(
                subprocess.run,
                [_MEM0_CLI, "list", user_id],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return 0

            data = json.loads(result.stdout)
            memories = data.get("results", [])
            deleted = 0
            for m in memories:
                mid = m.get("id", "")
                if mid:
                    del_result = await asyncio.to_thread(
                        subprocess.run,
                        [_MEM0_CLI, "delete", mid],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if del_result.returncode == 0:
                        deleted += 1

            # Clear session buffer
            self._session_turns.pop(caller_id, None)

            logger.info("Deleted %d mem0 memories for caller %s", deleted, caller_id)
            return deleted
        except Exception:
            logger.exception("Failed to delete mem0 history for %s", caller_id)
            return 0

    async def close(self):
        """Flush any remaining session turns to mem0 as conversations."""
        for caller_id, turns in self._session_turns.items():
            if len(turns) >= 2:  # Only flush if there was actual conversation
                user_id = _caller_to_user_id(caller_id)
                messages = []
                for t in turns:
                    role = "user" if t["role"] == "user" else "assistant"
                    messages.append({"role": role, "content": t["text"]})
                try:
                    proc = await asyncio.to_thread(
                        subprocess.run,
                        [_MEM0_CLI, "chat", user_id],
                        input=json.dumps(messages),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if proc.returncode == 0:
                        logger.info(
                            "Flushed %d turns to mem0 for %s", len(turns), caller_id
                        )
                except Exception:
                    logger.exception("Failed to flush session to mem0 for %s", caller_id)
        self._session_turns.clear()
        if self._identity_credential:
            self._identity_credential.close()


def _caller_to_user_id(caller_id: str) -> str:
    """Convert a phone number / caller ID to a mem0 user_id.

    Uses a short hash to keep it clean while remaining unique.
    """
    # Strip +, spaces, etc for consistency
    clean = caller_id.strip().replace(" ", "").replace("-", "")
    if clean.startswith("+"):
        return f"caller-{clean[1:]}"
    return f"caller-{clean}"
