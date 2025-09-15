import asyncio
import base64
import json
import logging
import struct
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
import uuid
import os
from datetime import datetime
import requests

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from typing_extensions import assert_never

from agents.realtime import RealtimeRunner, RealtimeSession, RealtimeSessionEvent
from agents.realtime.config import RealtimeUserInputMessage
from agents.realtime.model_inputs import RealtimeModelSendRawMessage

# Import TwilioHandler class - handle both module and package use cases
if TYPE_CHECKING:
    # For type checking, use the relative import
    from .agent import get_starting_agent
else:
    # At runtime, try both import styles
    try:
        # Try relative import first (when used as a package)
        from .agent import get_starting_agent
    except ImportError:
        # Fall back to direct import (when run as a script)
        from agent import get_starting_agent


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RealtimeWebSocketManager:
    def __init__(self):
        self.active_sessions: dict[str, RealtimeSession] = {}
        self.session_contexts: dict[str, Any] = {}
        self.websockets: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        self.websockets[session_id] = websocket

        agent = get_starting_agent()
        runner = RealtimeRunner(agent)
        session_context = await runner.run()
        session = await session_context.__aenter__()
        self.active_sessions[session_id] = session
        self.session_contexts[session_id] = session_context

        # Start event processing task
        asyncio.create_task(self._process_events(session_id))

        # Load persisted config and forward to model for this session
        try:
            cfg = load_config()
            if cfg:
                await self.send_client_event(session_id, {"type": "client_config", **cfg})
        except Exception as e:
            logger.warning("Failed to send startup config: %s", e)

    async def disconnect(self, session_id: str):
        if session_id in self.session_contexts:
            await self.session_contexts[session_id].__aexit__(None, None, None)
            del self.session_contexts[session_id]
        if session_id in self.active_sessions:
            del self.active_sessions[session_id]
        if session_id in self.websockets:
            del self.websockets[session_id]

    async def send_audio(self, session_id: str, audio_bytes: bytes):
        if session_id in self.active_sessions:
            await self.active_sessions[session_id].send_audio(audio_bytes)

    async def send_client_event(self, session_id: str, event: dict[str, Any]):
        """Send a raw client event to the underlying realtime model."""
        session = self.active_sessions.get(session_id)
        if not session:
            return
        await session.model.send_event(
            RealtimeModelSendRawMessage(
                message={
                    "type": event["type"],
                    "other_data": {k: v for k, v in event.items() if k != "type"},
                }
            )
        )

    async def send_user_message(self, session_id: str, message: RealtimeUserInputMessage):
        """Send a structured user message via the higher-level API (supports input_image)."""
        session = self.active_sessions.get(session_id)
        if not session:
            return
        await session.send_message(message)  # delegates to RealtimeModelSendUserInput path

    async def interrupt(self, session_id: str) -> None:
        """Interrupt current model playback/response for a session."""
        session = self.active_sessions.get(session_id)
        if not session:
            return
        await session.interrupt()

    async def _process_events(self, session_id: str):
        try:
            session = self.active_sessions[session_id]
            websocket = self.websockets[session_id]

            async for event in session:
                event_data = await self._serialize_event(event)
                await websocket.send_text(json.dumps(event_data))
        except Exception as e:
            logger.error(f"Error processing events for session {session_id}: {e}")

    async def _serialize_event(self, event: RealtimeSessionEvent) -> dict[str, Any]:
        base_event: dict[str, Any] = {
            "type": event.type,
        }

        if event.type == "agent_start":
            base_event["agent"] = event.agent.name
        elif event.type == "agent_end":
            base_event["agent"] = event.agent.name
        elif event.type == "handoff":
            base_event["from"] = event.from_agent.name
            base_event["to"] = event.to_agent.name
        elif event.type == "tool_start":
            base_event["tool"] = event.tool.name
        elif event.type == "tool_end":
            base_event["tool"] = event.tool.name
            base_event["output"] = str(event.output)
        elif event.type == "audio":
            base_event["audio"] = base64.b64encode(event.audio.data).decode("utf-8")
        elif event.type == "audio_interrupted":
            pass
        elif event.type == "audio_end":
            pass
        elif event.type == "history_updated":
            base_event["history"] = [item.model_dump(mode="json") for item in event.history]
        elif event.type == "history_added":
            # Provide the added item so the UI can render incrementally.
            try:
                base_event["item"] = event.item.model_dump(mode="json")
            except Exception:
                base_event["item"] = None
        elif event.type == "guardrail_tripped":
            base_event["guardrail_results"] = [
                {"name": result.guardrail.name} for result in event.guardrail_results
            ]
        elif event.type == "raw_model_event":
            base_event["raw_model_event"] = {
                "type": event.data.type,
            }
        elif event.type == "error":
            base_event["error"] = str(event.error) if hasattr(event, "error") else "Unknown error"
        elif event.type == "input_audio_timeout_triggered":
            pass
        else:
            assert_never(event)

        return base_event


manager = RealtimeWebSocketManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(websocket, session_id)
    image_buffers: dict[str, dict[str, Any]] = {}
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            if message["type"] == "audio":
                # Convert int16 array to bytes
                int16_data = message["data"]
                audio_bytes = struct.pack(f"{len(int16_data)}h", *int16_data)
                await manager.send_audio(session_id, audio_bytes)
            elif message["type"] == "image":
                logger.info("Received image message from client (session %s).", session_id)
                # Build a conversation.item.create with input_image (and optional input_text)
                data_url = message.get("data_url")
                prompt_text = message.get("text") or "Please describe this image."
                if data_url:
                    logger.info(
                        "Forwarding image (structured message) to Realtime API (len=%d).",
                        len(data_url),
                    )
                    user_msg: RealtimeUserInputMessage = {
                        "type": "message",
                        "role": "user",
                        "content": (
                            [
                                {"type": "input_image", "image_url": data_url, "detail": "high"},
                                {"type": "input_text", "text": prompt_text},
                            ]
                            if prompt_text
                            else [{"type": "input_image", "image_url": data_url, "detail": "high"}]
                        ),
                    }
                    await manager.send_user_message(session_id, user_msg)
                    # Acknowledge to client UI
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "client_info",
                                "info": "image_enqueued",
                                "size": len(data_url),
                            }
                        )
                    )
                else:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "error": "No data_url for image message.",
                            }
                        )
                    )
            elif message["type"] == "commit_audio":
                # Force close the current input audio turn
                await manager.send_client_event(session_id, {"type": "input_audio_buffer.commit"})
            elif message["type"] == "image_start":
                img_id = str(message.get("id"))
                image_buffers[img_id] = {
                    "text": message.get("text") or "Please describe this image.",
                    "chunks": [],
                }
                await websocket.send_text(
                    json.dumps({"type": "client_info", "info": "image_start_ack", "id": img_id})
                )
            elif message["type"] == "image_chunk":
                img_id = str(message.get("id"))
                chunk = message.get("chunk", "")
                if img_id in image_buffers:
                    image_buffers[img_id]["chunks"].append(chunk)
                    if len(image_buffers[img_id]["chunks"]) % 10 == 0:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "client_info",
                                    "info": "image_chunk_ack",
                                    "id": img_id,
                                    "count": len(image_buffers[img_id]["chunks"]),
                                }
                            )
                        )
            elif message["type"] == "image_end":
                img_id = str(message.get("id"))
                buf = image_buffers.pop(img_id, None)
                if buf is None:
                    await websocket.send_text(
                        json.dumps({"type": "error", "error": "Unknown image id for image_end."})
                    )
                else:
                    data_url = "".join(buf["chunks"]) if buf["chunks"] else None
                    prompt_text = buf["text"]
                    if data_url:
                        logger.info(
                            "Forwarding chunked image (structured message) to Realtime API (len=%d).",
                            len(data_url),
                        )
                        user_msg2: RealtimeUserInputMessage = {
                            "type": "message",
                            "role": "user",
                            "content": (
                                [
                                    {
                                        "type": "input_image",
                                        "image_url": data_url,
                                        "detail": "high",
                                    },
                                    {"type": "input_text", "text": prompt_text},
                                ]
                                if prompt_text
                                else [
                                    {"type": "input_image", "image_url": data_url, "detail": "high"}
                                ]
                            ),
                        }
                        await manager.send_user_message(session_id, user_msg2)
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "client_info",
                                    "info": "image_enqueued",
                                    "id": img_id,
                                    "size": len(data_url),
                                }
                            )
                        )
                    else:
                        await websocket.send_text(
                            json.dumps({"type": "error", "error": "Empty image."})
                        )
            elif message["type"] == "interrupt":
                await manager.interrupt(session_id)
            elif message["type"] == "client_config":
                # Forward client config to underlying model as raw event
                await manager.send_client_event(session_id, message)

    except WebSocketDisconnect:
        await manager.disconnect(session_id)


# 静态文件服务
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def read_index():
    import os
    static_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    return FileResponse(static_path)


# ------------------- 配置读写接口 -------------------
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
MCP_REGISTRY_FILE = os.path.join(DATA_DIR, 'mcps.json')

DEFAULT_CONFIG = {
    "temperature": 0.8,
    "voice": "Alloy",
    "threshold": 0.5,
    "prefix_padding_ms": 300,
    "silence_duration_ms": 500,
    "instructions": ""
}


def load_config() -> dict:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**DEFAULT_CONFIG, **data}
    except Exception as e:
        logger.warning("Failed to read config.json: %s", e)
    return DEFAULT_CONFIG.copy()


def save_config(data: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Failed to write config.json: %s", e)


@app.get("/config")
async def get_config():
    return JSONResponse(load_config())


@app.post("/config")
async def post_config(payload: dict):
    cfg = load_config()
    cfg.update({
        "temperature": float(payload.get("temperature", cfg["temperature"])),
        "voice": str(payload.get("voice", cfg["voice"])),
        "threshold": float(payload.get("threshold", cfg["threshold"])),
        "prefix_padding_ms": int(payload.get("prefix_padding_ms", cfg["prefix_padding_ms"])),
        "silence_duration_ms": int(payload.get("silence_duration_ms", cfg["silence_duration_ms"])),
        "instructions": str(payload.get("instructions", cfg.get("instructions", ""))),
    })
    save_config(cfg)
    return JSONResponse({"ok": True})


# ------------------- MCP 管理接口（复用 test 项目风格） -------------------
mcp_registry: dict[str, dict[str, Any]] = {}


def _load_mcp_registry() -> None:
    global mcp_registry
    try:
        if os.path.exists(MCP_REGISTRY_FILE):
            with open(MCP_REGISTRY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    mcp_registry = data
    except Exception:
        mcp_registry = {}


def _save_mcp_registry() -> None:
    try:
        with open(MCP_REGISTRY_FILE, 'w', encoding='utf-8') as f:
            json.dump(mcp_registry, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("保存MCP注册表失败: %s", e)


def _validate_mcp_connectivity(config: dict[str, Any]) -> tuple[bool, str]:
    try:
        if not isinstance(config, dict):
            return False, "配置需为JSON对象"
        url = config.get('url')
        if not url:
            return False, "缺少 url 字段"
        headers = config.get('headers') or {}
        timeout = float(config.get('timeout', 10))
        resp = requests.get(url, headers=headers, timeout=timeout)
        if 200 <= resp.status_code < 300:
            return True, ""
        body = resp.text[:200] if isinstance(resp.text, str) else ''
        return False, f"HTTP {resp.status_code}: {body}"
    except requests.exceptions.RequestException as e:
        return False, f"网络异常: {e}"
    except Exception as e:
        return False, f"未知错误: {e}"


@app.get('/api/mcps')
async def list_mcps():
    _load_mcp_registry()
    return JSONResponse({"mcps": list(mcp_registry.values())})


@app.post('/api/mcps')
async def create_mcp(payload: dict):
    _load_mcp_registry()
    name = (payload.get('name') or '').strip()
    description = (payload.get('description') or '').strip()
    enabled = bool(payload.get('enabled', True))
    config = payload.get('config') or {}
    if not name:
        return JSONResponse({"error": "名称不能为空"}, status_code=400)
    ok, err = _validate_mcp_connectivity(config)
    if not ok:
        return JSONResponse({"error": f"校验失败：{err}"}, status_code=400)
    mcp_id = str(uuid.uuid4())
    mcp_info = {
        'id': mcp_id,
        'name': name,
        'description': description,
        'enabled': enabled,
        'config': config,
        'created_at': datetime.now().isoformat()
    }
    mcp_registry[mcp_id] = mcp_info
    _save_mcp_registry()
    return JSONResponse({"message": "创建成功", "mcp": mcp_info})


@app.put('/api/mcps/{mcp_id}')
async def update_mcp(mcp_id: str, payload: dict):
    _load_mcp_registry()
    if mcp_id not in mcp_registry:
        return JSONResponse({"error": "MCP 不存在"}, status_code=404)
    name = (payload.get('name') or '').strip()
    description = (payload.get('description') or '').strip()
    enabled = bool(payload.get('enabled', True))
    config = payload.get('config') or {}
    if not name:
        return JSONResponse({"error": "名称不能为空"}, status_code=400)
    ok, err = _validate_mcp_connectivity(config)
    if not ok:
        return JSONResponse({"error": f"校验失败：{err}"}, status_code=400)
    m = mcp_registry[mcp_id]
    m.update({
        'name': name,
        'description': description,
        'enabled': enabled,
        'config': config,
        'updated_at': datetime.now().isoformat()
    })
    _save_mcp_registry()
    return JSONResponse({"message": "更新成功", "mcp": m})


@app.patch('/api/mcps/{mcp_id}/enable')
async def enable_mcp(mcp_id: str, payload: dict):
    _load_mcp_registry()
    if mcp_id not in mcp_registry:
        return JSONResponse({"error": "MCP 不存在"}, status_code=404)
    enabled = bool(payload.get('enabled', True))
    mcp_registry[mcp_id]['enabled'] = enabled
    mcp_registry[mcp_id]['updated_at'] = datetime.now().isoformat()
    _save_mcp_registry()
    return JSONResponse({"message": "状态已更新"})


@app.delete('/api/mcps/{mcp_id}')
async def delete_mcp(mcp_id: str):
    _load_mcp_registry()
    if mcp_id not in mcp_registry:
        return JSONResponse({"error": "MCP 不存在"}, status_code=404)
    del mcp_registry[mcp_id]
    _save_mcp_registry()
    return JSONResponse({"message": "删除成功"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="localhost",
        port=8000,
        # Increased WebSocket frame size to comfortably handle image data URLs.
        ws_max_size=16 * 1024 * 1024,
    )