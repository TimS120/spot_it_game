import asyncio
import json
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
INDEX_PATH = BASE_DIR / "index.html"
STATIC_DIR = BASE_DIR / "static"
IMAGES_DIR = STATIC_DIR / "images"
RESULTS_PATH = BASE_DIR / "results.json"

MAX_ROOMS = 200
ROOM_ID_BYTES = 16
ROOM_TTL_SECONDS = 60 * 60
ROOM_IDLE_GRACE_SECONDS = 15 * 60
MAX_MESSAGE_BYTES = 8192
MAX_MESSAGES_PER_SECOND = 30

CLICK_RADIUS = 0.05
TEAM_RADIUS = 0.06

rooms_lock = asyncio.Lock()
rooms: Dict[str, Dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_cleanup_rooms())
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_PATH.read_text(encoding="utf-8"))


def _now() -> float:
    return time.time()


def _new_room_id() -> str:
    return secrets.token_urlsafe(ROOM_ID_BYTES)


def _new_player_id() -> str:
    return secrets.token_urlsafe(12)


def _is_room_expired(room: Dict[str, Any], now: float) -> bool:
    created_at = room.get("created_at", now)
    last_activity = room.get("last_activity", created_at)
    if now - created_at >= ROOM_TTL_SECONDS:
        return True
    if not room.get("players") and now - last_activity >= ROOM_IDLE_GRACE_SECONDS:
        return True
    return False


def _rate_ok(timestamps: Deque[float], limit_per_second: int, now: float) -> bool:
    cutoff = now - 1.0
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()
    if len(timestamps) >= limit_per_second:
        return False
    timestamps.append(now)
    return True


def _load_results() -> Dict[str, Any]:
    if not RESULTS_PATH.exists():
        return {}
    try:
        return json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _image_url(round_number: int, reveal: bool) -> Optional[str]:
    suffix = "_2" if reveal else ""
    base = f"{round_number}{suffix}"
    for ext in (".png", ".jpg", ".jpeg"):
        candidate = IMAGES_DIR / f"{base}{ext}"
        if candidate.exists():
            return f"/static/images/{candidate.name}"
    return None


def _room_snapshot(room: Dict[str, Any]) -> Dict[str, Any]:
    players = []
    for pid, pdata in room.get("players", {}).items():
        players.append(
            {
                "id": pid,
                "name": pdata.get("name") or "",
                "team": pdata.get("team"),
                "color": pdata.get("color") or "#999999",
            }
        )
    players.sort(key=lambda p: (p["team"] or "", p["name"]))
    round_state = room.get("round", {})
    return {
        "players": players,
        "scores": room.get("scores", {"A": 0, "B": 0}),
        "host_id": room.get("host_id"),
        "phase": round_state.get("phase", "lobby"),
        "round_number": round_state.get("number", 0),
        "team_turn": round_state.get("team_turn"),
        "mode": round_state.get("mode"),
    }


async def _safe_send(ws: WebSocket, message: Dict[str, Any]) -> None:
    try:
        await ws.send_json(message)
    except Exception:
        pass


async def _broadcast(room: Dict[str, Any], message: Dict[str, Any]) -> None:
    tasks = []
    for pdata in room.get("players", {}).values():
        ws = pdata.get("ws")
        if ws is not None:
            tasks.append(_safe_send(ws, message))
    if tasks:
        await asyncio.gather(*tasks)


async def _broadcast_room_update(room: Dict[str, Any]) -> None:
    await _broadcast(room, {"type": "room_update", **_room_snapshot(room)})


async def _cleanup_rooms() -> None:
    while True:
        await asyncio.sleep(30)
        now = _now()
        async with rooms_lock:
            expired = [room_id for room_id, room in rooms.items() if _is_room_expired(room, now)]
            for room_id in expired:
                rooms.pop(room_id, None)


def _centroid(points: List[Dict[str, float]]) -> Optional[Dict[str, float]]:
    if not points:
        return None
    x_sum = sum(p["x"] for p in points)
    y_sum = sum(p["y"] for p in points)
    count = len(points)
    return {"x": x_sum / count, "y": y_sum / count}


def _circles_overlap(a: Dict[str, float], b: Dict[str, float]) -> bool:
    dx = a["x"] - b["x"]
    dy = a["y"] - b["y"]
    dist_sq = dx * dx + dy * dy
    radius = a["r"] + b["r"]
    return dist_sq <= radius * radius


async def _start_round(room_id: str, round_number: int, duration: int) -> None:
    async with rooms_lock:
        room = rooms.get(room_id)
        if not room:
            return
        room["round"] = {
            "number": round_number,
            "phase": "active",
            "team_turn": "A" if round_number % 2 == 1 else "B",
            "mode": "individual",
            "ends_at": _now() + duration,
            "clicks": {},
        }
        room["last_activity"] = _now()
        image_url = _image_url(round_number, reveal=False)
        ends_at = room["round"]["ends_at"]
    await _broadcast(
        room,
        {
            "type": "round_started",
            "round_number": round_number,
            "team_turn": "A" if round_number % 2 == 1 else "B",
            "mode": "individual",
            "duration": duration,
            "ends_at": ends_at,
            "image_url": image_url,
            "click_radius": CLICK_RADIUS,
        },
    )
    await _broadcast_room_update(room)
    await asyncio.sleep(duration)
    await _finish_round(room_id)


async def _finish_round(room_id: str) -> None:
    async with rooms_lock:
        room = rooms.get(room_id)
        if not room:
            return
        round_state = room.get("round", {})
        if round_state.get("phase") != "active":
            return
        round_number = round_state.get("number", 0)
        team_turn = round_state.get("team_turn")
        clicks = list(round_state.get("clicks", {}).values())
        centroid = _centroid(clicks)
        results = _load_results()
        result_entry = results.get(str(round_number), {})
        solution = result_entry.get("solution")
        if solution:
            solution_circle = {
                "x": float(solution.get("x", 0.5)),
                "y": float(solution.get("y", 0.5)),
                "r": float(solution.get("r", 0.06)),
            }
        else:
            solution_circle = {"x": 0.5, "y": 0.5, "r": 0.06}
        team_circle = None
        won = False
        if centroid:
            team_circle = {"x": centroid["x"], "y": centroid["y"], "r": TEAM_RADIUS}
            won = _circles_overlap(team_circle, solution_circle)
        if won and team_turn in ("A", "B"):
            room.setdefault("scores", {"A": 0, "B": 0})
            room["scores"][team_turn] += 1
        room["round"]["phase"] = "reveal"
        room["round"]["team_circle"] = team_circle
        room["round"]["solution_circle"] = solution_circle
        room["last_activity"] = _now()
        reveal_url = _image_url(round_number, reveal=True)
        snapshot = _room_snapshot(room)
    await _broadcast(
        room,
        {
            "type": "round_reveal",
            "round_number": round_number,
            "team_turn": team_turn,
            "clicks": clicks,
            "team_circle": team_circle,
            "solution_circle": solution_circle,
            "won": won,
            "scores": snapshot.get("scores"),
            "reveal_url": reveal_url,
        },
    )
    await _broadcast_room_update(room)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    room_id = None
    player_id = None
    is_host = False
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

            if room_id is None:
                if msg_type == "create_room":
                    async with rooms_lock:
                        if len(rooms) >= MAX_ROOMS:
                            await _safe_send(ws, {"type": "error", "message": "Room limit reached."})
                            continue
                        room_id = _new_room_id()
                        player_id = _new_player_id()
                        rooms[room_id] = {
                            "host_id": player_id,
                            "players": {
                                player_id: {
                                    "ws": ws,
                                    "name": "",
                                    "team": None,
                                    "color": "#3f8cff",
                                    "last_click": None,
                                }
                            },
                            "scores": {"A": 0, "B": 0},
                            "round": {"number": 0, "phase": "lobby", "team_turn": "A", "mode": "individual"},
                            "created_at": _now(),
                            "last_activity": _now(),
                        }
                    is_host = True
                    invite_path = f"/?room={room_id}"
                    print(f"Invite link: http://localhost:8000{invite_path}", flush=True)
                    await _safe_send(
                        ws,
                        {
                            "type": "room_created",
                            "room_id": room_id,
                            "player_id": player_id,
                            "invite_path": invite_path,
                        },
                    )
                    async with rooms_lock:
                        room = rooms.get(room_id)
                    if room:
                        await _broadcast_room_update(room)
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
                            room_id = None
                            continue
                        player_id = _new_player_id()
                        room["players"][player_id] = {
                            "ws": ws,
                            "name": "",
                            "team": None,
                            "color": "#ff8c3f",
                            "last_click": None,
                        }
                        room["last_activity"] = _now()
                    await _safe_send(
                        ws,
                        {"type": "room_joined", "room_id": room_id, "player_id": player_id},
                    )
                    await _broadcast_room_update(room)
                    continue

                await _safe_send(ws, {"type": "error", "message": "First message must be create_room or join_room."})
                continue

            async with rooms_lock:
                room = rooms.get(room_id)
            if not room:
                await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                continue

            if msg_type == "set_profile":
                if room.get("host_id") == player_id:
                    await _safe_send(ws, {"type": "error", "message": "Host cannot set a player profile."})
                    continue
                name = msg.get("name")
                color = msg.get("color")
                if not isinstance(name, str) or not name.strip():
                    await _safe_send(ws, {"type": "error", "message": "Name required."})
                    continue
                if not isinstance(color, str) or not color.startswith("#"):
                    await _safe_send(ws, {"type": "error", "message": "Color required."})
                    continue
                async with rooms_lock:
                    pdata = room.get("players", {}).get(player_id)
                    if pdata:
                        pdata["name"] = name.strip()[:32]
                        pdata["color"] = color[:9]
                        room["last_activity"] = _now()
                await _broadcast_room_update(room)
                continue

            if msg_type == "join_team":
                if room.get("host_id") == player_id:
                    await _safe_send(ws, {"type": "error", "message": "Host cannot join a team."})
                    continue
                team = msg.get("team")
                if team not in ("A", "B"):
                    await _safe_send(ws, {"type": "error", "message": "Invalid team."})
                    continue
                async with rooms_lock:
                    pdata = room.get("players", {}).get(player_id)
                    if pdata:
                        pdata["team"] = team
                        room["last_activity"] = _now()
                await _broadcast_room_update(room)
                continue

            if msg_type == "start_round":
                if room.get("host_id") != player_id:
                    await _safe_send(ws, {"type": "error", "message": "Only host can start rounds."})
                    continue
                duration = msg.get("duration")
                if not isinstance(duration, int):
                    duration = 30
                duration = max(5, min(duration, 600))
                round_number = msg.get("round_number")
                if not isinstance(round_number, int) or round_number <= 0:
                    round_number = room.get("round", {}).get("number", 0) + 1
                async with rooms_lock:
                    players = list(room.get("players", {}).values())
                    if not players or any(not p.get("team") or not p.get("name") for p in players):
                        await _safe_send(ws, {"type": "error", "message": "All players must set name and team."})
                        continue
                    existing_task = room.get("round_task")
                    if existing_task and not existing_task.done():
                        existing_task.cancel()
                task = asyncio.create_task(_start_round(room_id, round_number, duration))
                async with rooms_lock:
                    room["round_task"] = task
                continue

            if msg_type == "click":
                x = msg.get("x")
                y = msg.get("y")
                if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                    continue
                if x < 0 or x > 1 or y < 0 or y > 1:
                    continue
                async with rooms_lock:
                    round_state = room.get("round", {})
                    if round_state.get("phase") != "active":
                        continue
                    pdata = room.get("players", {}).get(player_id)
                    if not pdata:
                        continue
                    if pdata.get("team") != round_state.get("team_turn"):
                        continue
                    click = {"x": float(x), "y": float(y), "color": pdata.get("color", "#ffffff")}
                    round_state.setdefault("clicks", {})[player_id] = click
                    room["last_activity"] = _now()
                    team_turn = round_state.get("team_turn")
                    other_team_players = [
                        p for p in room.get("players", {}).values() if p.get("team") and p.get("team") != team_turn
                    ]
                    all_clicks = list(round_state.get("clicks", {}).values())
                await _safe_send(ws, {"type": "your_click", "click": click})
                for p in other_team_players:
                    await _safe_send(p.get("ws"), {"type": "click_update", "clicks": all_clicks})
                continue

            await _safe_send(ws, {"type": "error", "message": "Unknown message type."})

    except WebSocketDisconnect:
        pass
    finally:
        if room_id and player_id:
            async with rooms_lock:
                room = rooms.get(room_id)
                if room:
                    room.get("players", {}).pop(player_id, None)
                    if room.get("host_id") == player_id:
                        remaining = list(room.get("players", {}).keys())
                        room["host_id"] = remaining[0] if remaining else None
                    room["last_activity"] = _now()
                    if not room.get("players"):
                        rooms.pop(room_id, None)
                    else:
                        await _broadcast_room_update(room)
