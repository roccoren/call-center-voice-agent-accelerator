import asyncio
import logging
import os

from app.handler.acs_event_handler import AcsEventHandler
from app.handler.acs_media_handler import ACSMediaHandler
from app.memory.cosmos_memory import memory as conversation_memory
from app.memory import call_registry
from dotenv import load_dotenv
from quart import Quart, request, websocket, jsonify

load_dotenv()

app = Quart(__name__)
app.config["AZURE_VOICE_LIVE_API_KEY"] = os.getenv("AZURE_VOICE_LIVE_API_KEY", "")
app.config["AZURE_VOICE_LIVE_ENDPOINT"] = os.getenv("AZURE_VOICE_LIVE_ENDPOINT")
app.config["VOICE_LIVE_MODEL"] = os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini")
app.config["ACS_CONNECTION_STRING"] = os.getenv("ACS_CONNECTION_STRING")
app.config["ACS_DEV_TUNNEL"] = os.getenv("ACS_DEV_TUNNEL", "")
app.config["AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID"] = os.getenv(
    "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID", ""
)

# Ambient Scenes Configuration
# Options: none, office, call_center (or custom presets)
app.config["AMBIENT_PRESET"] = os.getenv("AMBIENT_PRESET", "none")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Log ambient configuration on startup
ambient_preset = app.config["AMBIENT_PRESET"]
if ambient_preset and ambient_preset != "none":
    logger.info(f"Ambient scenes ENABLED: preset='{ambient_preset}'")
else:
    logger.info("Ambient scenes DISABLED (preset=none)")

acs_handler = AcsEventHandler(app.config)


@app.before_serving
async def startup():
    """Initialize conversation memory on app startup."""
    ok = await conversation_memory.initialize()
    if ok:
        logger.info("Conversation memory (Cosmos DB) is READY")
    else:
        logger.warning("Conversation memory is DISABLED — set AZURE_COSMOS_ENDPOINT to enable")


@app.route("/acs/incomingcall", methods=["POST"])
async def incoming_call_handler():
    """Handles initial incoming call event from EventGrid."""
    events = await request.get_json()
    host_url = request.host_url.replace("http://", "https://", 1).rstrip("/")
    return await acs_handler.process_incoming_call(events, host_url, app.config)


@app.route("/acs/callbacks/<context_id>", methods=["POST"])
async def acs_event_callbacks(context_id):
    """Handles ACS event callbacks for call connection and streaming events."""
    raw_events = await request.get_json()
    return await acs_handler.process_callback_events(context_id, raw_events, app.config)


@app.websocket("/acs/ws")
async def acs_ws():
    """WebSocket endpoint for ACS to send audio to Voice Live."""
    logger = logging.getLogger("acs_ws")
    logger.info("Incoming ACS WebSocket connection")

    # ACS media streaming sends caller info in the first message metadata,
    # but we also look up from the call registry populated by event handler.
    caller_id = "phone-anonymous"
    session_id = ""

    handler = ACSMediaHandler(app.config, caller_id=caller_id, session_id=session_id)
    await handler.init_incoming_websocket(websocket, is_raw_audio=False)
    asyncio.create_task(handler.connect())
    try:
        while True:
            msg = await websocket.receive()
            await handler.acs_to_voicelive(msg)
    except asyncio.CancelledError:
        logger.info("ACS WebSocket cancelled")
    except Exception:
        logger.exception("ACS WebSocket connection closed")
    finally:
        await handler.stop_audio()


@app.websocket("/web/ws")
async def web_ws():
    """WebSocket endpoint for web clients to send audio to Voice Live."""
    logger = logging.getLogger("web_ws")
    logger.info("Incoming Web WebSocket connection")
    # Web clients can pass caller_id as query param for memory
    caller_id = websocket.args.get("caller_id", "web-anonymous")
    handler = ACSMediaHandler(app.config, caller_id=caller_id)
    await handler.init_incoming_websocket(websocket, is_raw_audio=True)
    asyncio.create_task(handler.connect())
    try:
        while True:
            msg = await websocket.receive()
            await handler.web_to_voicelive(msg)
    except asyncio.CancelledError:
        logger.info("Web WebSocket cancelled")
    except Exception:
        logger.exception("Web WebSocket connection closed")
    finally:
        await handler.stop_audio()


@app.route("/")
async def index():
    """Serves the static index page."""
    return await app.send_static_file("index.html")


# ---------------------------------------------------------------------------
# Memory REST API
# ---------------------------------------------------------------------------

@app.route("/memory/<caller_id>/history", methods=["GET"])
async def memory_get_history(caller_id: str):
    """Get recent conversation history for a caller."""
    if not conversation_memory.is_ready:
        return jsonify({"error": "Memory not configured"}), 503
    limit = request.args.get("limit", 20, type=int)
    turns = await conversation_memory.get_recent_turns(caller_id, limit=limit)
    return jsonify({"callerId": caller_id, "turns": turns})


@app.route("/memory/<caller_id>/summary", methods=["GET"])
async def memory_get_summary(caller_id: str):
    """Get the stored summary for a caller."""
    if not conversation_memory.is_ready:
        return jsonify({"error": "Memory not configured"}), 503
    summary = await conversation_memory.get_summary(caller_id)
    return jsonify({"callerId": caller_id, "summary": summary})


@app.route("/memory/<caller_id>/summary", methods=["PUT"])
async def memory_put_summary(caller_id: str):
    """Update the summary for a caller."""
    if not conversation_memory.is_ready:
        return jsonify({"error": "Memory not configured"}), 503
    body = await request.get_json()
    summary = body.get("summary", "")
    ok = await conversation_memory.save_summary(caller_id, summary)
    if ok:
        return jsonify({"status": "ok"})
    return jsonify({"error": "Failed to save summary"}), 500


@app.route("/memory/<caller_id>", methods=["DELETE"])
async def memory_delete(caller_id: str):
    """Delete all conversation history for a caller."""
    if not conversation_memory.is_ready:
        return jsonify({"error": "Memory not configured"}), 503
    deleted = await conversation_memory.delete_caller_history(caller_id)
    return jsonify({"callerId": caller_id, "deletedTurns": deleted})


@app.route("/health", methods=["GET"])
async def health():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "memory": "enabled" if conversation_memory.is_ready else "disabled",
    })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8000)
