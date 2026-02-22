import asyncio
import json
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_cleanup_rooms())
    yield


app = FastAPI(lifespan=lifespan)

BASE_DIR = Path(__file__).resolve().parent
INDEX_PATH = BASE_DIR / "index.html"

rooms_lock = asyncio.Lock()
rooms: Dict[str, Dict[str, Any]] = {}

MAX_ROOMS = 1000
ROOM_ID_BYTES = 16
ROOM_TTL_SECONDS = 60 * 60
ROOM_IDLE_GRACE_SECONDS = 15 * 60
MAX_MESSAGE_BYTES = 4096
MAX_MESSAGES_PER_SECOND = 25
ALLOWED_ORIGINS = {origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "").split(",") if origin.strip()}


def _new_room_id() -> str:
    return secrets.token_urlsafe(ROOM_ID_BYTES)


async def _safe_send(ws: WebSocket, message: Dict[str, Any]) -> None:
    try:
        await ws.send_json(message)
    except Exception:
        pass


def _now() -> float:
    return time.time()


def _is_room_expired(room: Dict[str, Any], now: float) -> bool:
    created_at = room.get("created_at", now)
    last_activity = room.get("last_activity", created_at)
    if now - created_at >= ROOM_TTL_SECONDS:
        return True
    if room.get("host") is None and not room.get("players") and now - last_activity >= ROOM_IDLE_GRACE_SECONDS:
        return True
    return False


def _allow_origin(ws: WebSocket) -> bool:
    if not ALLOWED_ORIGINS:
        return True
    origin = ws.headers.get("origin")
    return origin in ALLOWED_ORIGINS


def _rate_ok(timestamps: Deque[float], limit_per_second: int, now: float) -> bool:
    cutoff = now - 1.0
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()
    if len(timestamps) >= limit_per_second:
        return False
    timestamps.append(now)
    return True


def _find_player(room: Dict[str, Any], ws: WebSocket) -> Optional[Dict[str, Any]]:
    for player in room["players"]:
        if player["ws"] is ws:
            return player
    return None


def _room_snapshot(room: Dict[str, Any]) -> Dict[str, Any]:
    teams = {"1": [], "2": []}
    for player in room["players"]:
        name = player.get("name")
        team = player.get("team")
        if name and team in (1, 2):
            teams[str(team)].append(name)
    return {
        "teams": teams,
        "member_counts": {key: len(names) for key, names in teams.items()},
        "join_allowed": room["join_allowed"],
        "game_started": room["game_started"],
    }


async def _broadcast(room: Dict[str, Any], message: Dict[str, Any]) -> None:
    sockets: List[WebSocket] = []
    if room.get("host") is not None:
        sockets.append(room["host"])
    sockets.extend(player["ws"] for player in room["players"])
    for ws in sockets:
        await _safe_send(ws, message)


async def _cleanup_rooms() -> None:
    while True:
        await asyncio.sleep(30)
        now = _now()
        async with rooms_lock:
            expired = [room_id for room_id, room in rooms.items() if _is_room_expired(room, now)]
            for room_id in expired:
                rooms.pop(room_id, None)


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_PATH.read_text(encoding="utf-8"))


@app.post("/create-room")
async def create_room(request: Request) -> Dict[str, str]:
    async with rooms_lock:
        if len(rooms) >= MAX_ROOMS:
            raise HTTPException(status_code=429, detail="Room limit reached.")
        room_id = _new_room_id()
        rooms[room_id] = {
            "host": None,
            "players": [],
            "join_allowed": True,
            "game_started": False,
            "created_at": _now(),
            "last_activity": _now(),
        }
    origin = str(request.base_url).rstrip("/")
    print(f"Invite link: {origin}/?room={room_id}")
    return {"room_id": room_id}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    if not _allow_origin(ws):
        await ws.accept()
        await ws.close(code=1008)
        return
    await ws.accept()
    role = None
    room_id = None
    message_timestamps: Deque[float] = deque()

    try:
        while True:
            raw = await ws.receive_text()
            if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
                await _safe_send(ws, {"type": "error", "message": "Message too large."})
                continue
            now = _now()
            if not _rate_ok(message_timestamps, MAX_MESSAGES_PER_SECOND, now):
                await _safe_send(ws, {"type": "error", "message": "Too many messages."})
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _safe_send(ws, {"type": "error", "message": "Invalid JSON."})
                continue

            if not isinstance(msg, dict) or "type" not in msg:
                await _safe_send(ws, {"type": "error", "message": "Missing message type."})
                continue

            msg_type = msg.get("type")

            if role is None:
                if msg_type == "create_room":
                    async with rooms_lock:
                        if len(rooms) >= MAX_ROOMS:
                            await _safe_send(ws, {"type": "error", "message": "Room limit reached."})
                            continue
                        room_id = _new_room_id()
                        rooms[room_id] = {
                            "host": ws,
                            "players": [],
                            "join_allowed": True,
                            "game_started": False,
                            "created_at": _now(),
                            "last_activity": _now(),
                        }
                    role = "host"
                    await _safe_send(ws, {"type": "room_created", "room_id": room_id, "role": "host"})
                    origin = ws.headers.get("origin")
                    if origin:
                        print(f"Invite link: {origin}/?room={room_id}")
                    else:
                        print(f"Room created: {room_id}")
                    continue

                if msg_type == "host_join":
                    room_id = msg.get("room_id")
                    if not isinstance(room_id, str) or not room_id:
                        await _safe_send(ws, {"type": "error", "message": "Invalid room ID."})
                        continue
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room not found."})
                            continue
                        if room.get("host") is not None:
                            await _safe_send(ws, {"type": "error", "message": "Room already has a host."})
                            continue
                        room["host"] = ws
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                    role = "host"
                    await _safe_send(ws, {"type": "room_created", "room_id": room_id, "role": "host"})
                    await _safe_send(ws, {"type": "lobby_state", **snapshot})
                    continue

                if msg_type == "join_room":
                    room_id = msg.get("room_id")
                    if not isinstance(room_id, str) or not room_id:
                        await _safe_send(ws, {"type": "error", "message": "Invalid room ID."})
                        continue
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room not found."})
                            continue
                        if not room["join_allowed"]:
                            await _safe_send(ws, {"type": "error", "message": "Joining is closed."})
                            continue
                        room["players"].append({"ws": ws, "name": None, "team": None})
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                    role = "player"
                    await _safe_send(ws, {"type": "room_joined", "room_id": room_id, "role": "player"})
                    await _safe_send(ws, {"type": "lobby_state", **snapshot})
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    continue

                await _safe_send(
                    ws,
                    {"type": "error", "message": "First message must be create_room, host_join, or join_room."},
                )
                continue

            if role == "host":
                if msg_type == "start_game":
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        room["join_allowed"] = False
                        room["game_started"] = True
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                    await _broadcast(room, {"type": "join_closed"})
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    await _broadcast(room, {"type": "game_started", "message": "Game loop placeholder started."})
                    continue

                await _safe_send(ws, {"type": "error", "message": "Host can only start_game."})
                continue

            if role == "player":
                if msg_type == "set_name":
                    name = msg.get("name")
                    if not isinstance(name, str) or not name.strip():
                        await _safe_send(ws, {"type": "error", "message": "Name is required."})
                        continue
                    name = name.strip()[:32]
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        if not room["join_allowed"]:
                            await _safe_send(ws, {"type": "error", "message": "Joining is closed."})
                            continue
                        player = _find_player(room, ws)
                        if not player:
                            await _safe_send(ws, {"type": "error", "message": "Player not registered."})
                            continue
                        player["name"] = name
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    continue

                if msg_type == "set_team":
                    team = msg.get("team")
                    if team not in (1, 2):
                        await _safe_send(ws, {"type": "error", "message": "Team must be 1 or 2."})
                        continue
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        if not room["join_allowed"]:
                            await _safe_send(ws, {"type": "error", "message": "Joining is closed."})
                            continue
                        player = _find_player(room, ws)
                        if not player:
                            await _safe_send(ws, {"type": "error", "message": "Player not registered."})
                            continue
                        if not player.get("name"):
                            await _safe_send(ws, {"type": "error", "message": "Set name first."})
                            continue
                        player["team"] = team
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    continue

                await _safe_send(ws, {"type": "error", "message": "Player can only set_name or set_team."})
                continue

    except WebSocketDisconnect:
        pass
    finally:
        if role and room_id:
            async with rooms_lock:
                room = rooms.get(room_id)
                if room:
                    if role == "host":
                        room["host"] = None
                        room["join_allowed"] = False
                        await _broadcast(room, {"type": "status", "message": "Host disconnected. Room closed."})
                        rooms.pop(room_id, None)
                    else:
                        room["players"] = [player for player in room["players"] if player["ws"] is not ws]
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                        await _broadcast(room, {"type": "lobby_state", **snapshot})
                        if room.get("host") is None and not room.get("players"):
                            rooms.pop(room_id, None)
