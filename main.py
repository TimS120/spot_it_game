import asyncio
import base64
import copy
import json
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
import hashlib
import os
from pathlib import Path
import re
import shutil
from typing import Any, Deque, Dict, List, Optional, Tuple

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError



@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_rooms())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await _stop_cloudflared()


app = FastAPI(lifespan=lifespan)

BASE_DIR = Path(__file__).resolve().parent
INDEX_PATH = BASE_DIR / "index.html"
EDITOR_PATH = BASE_DIR / "editor.html"
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
GAMESETS_DIR = DATA_DIR / "game_sets"
IMAGE_TARGET_SIZE = int(os.getenv("IMAGE_TARGET_SIZE", "1080"))
IMAGE_JPEG_QUALITY = int(os.getenv("IMAGE_JPEG_QUALITY", "82"))
PIL_AVAILABLE = Image is not None
RESAMPLE_LANCZOS = Image.Resampling.LANCZOS if PIL_AVAILABLE and hasattr(Image, "Resampling") else getattr(Image, "LANCZOS", None)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
DATA_DIR.mkdir(parents=True, exist_ok=True)
GAMESETS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")

rooms_lock = asyncio.Lock()
rooms: Dict[str, Dict[str, Any]] = {}

MAX_ROOMS = 1000
ROOM_ID_BYTES = 16
ROOM_TTL_SECONDS = 60 * 60
ROOM_IDLE_GRACE_SECONDS = 15 * 60
MAX_MESSAGE_BYTES = 4096
MAX_MESSAGES_PER_SECOND = 25
ALLOWED_ORIGINS = {origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "").split(",") if origin.strip()}
DISALLOWED_COLOR = "#ff0000"
DEFAULT_COLORS = [
    "#1f6feb",
    "#2da44e",
    "#e85d04",
    "#9d4edd",
    "#0081a7",
    "#f9844a",
    "#3a86ff",
    "#ffb703",
]
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
_cloudflared_autostart_env = os.getenv("CLOUDFLARED_AUTOSTART", "").strip().lower()
CLOUDFLARED_AUTOSTART = _cloudflared_autostart_env not in {"0", "false", "no", "off"}
CLOUDFLARED_BIN = os.getenv("CLOUDFLARED_BIN", "cloudflared").strip() or "cloudflared"
CLOUDFLARED_LOCAL_URL = os.getenv("CLOUDFLARED_LOCAL_URL", "http://127.0.0.1:8000").strip()
CLOUDFLARED_TIMEOUT_SECONDS = float(os.getenv("CLOUDFLARED_TIMEOUT_SECONDS", "20"))
TRYCLOUDFLARE_URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

tunnel_lock = asyncio.Lock()
cloudflared_process: Optional[asyncio.subprocess.Process] = None
cloudflared_public_url: Optional[str] = None


ROUND_DURATION_SECONDS = 60.0




def _new_room() -> Dict[str, Any]:
    return {
        "host": None,
        "players": [],
        "join_allowed": True,
        "game_started": False,
        "created_at": _now(),
        "last_activity": _now(),
        "round_index": 0,
        "round_type": None,
        "phase": "idle",
        "active_team": 1,
        "question": "",
        "question_duration_seconds": ROUND_DURATION_SECONDS,
        "timer_end": None,
        "clicks": {},
        "scores": {1: 0, 2: 0},
        "centroid": None,
        "solution": None,
        "team_radius": 0.06,
        "round_data": None,
        "game_set_id": None,
        "game_set_name": None,
        "rounds": [],
        "timer_task": None,
        "leader_id": None,
    }


def _new_player(ws: WebSocket, default_color: Optional[str] = None) -> Dict[str, Any]:
    return {"id": secrets.token_urlsafe(8), "ws": ws, "name": None, "team": None, "color": default_color}


def _new_room_id() -> str:
    return secrets.token_urlsafe(ROOM_ID_BYTES)


async def _safe_send(ws: WebSocket, message: Dict[str, Any]) -> None:
    try:
        await ws.send_json(message)
    except Exception:
        pass


def _now() -> float:
    return time.time()


def _normalize_base_url(url: str) -> str:
    return url.strip().rstrip("/")


def _invite_url(base_url: str, room_id: str) -> str:
    return f"{_normalize_base_url(base_url)}/?room={room_id}"


async def _read_cloudflared_url(process: asyncio.subprocess.Process, timeout_seconds: float) -> Optional[str]:
    deadline = _now() + timeout_seconds
    streams = [process.stdout, process.stderr]
    while _now() < deadline:
        if process.returncode is not None:
            break
        found_line = False
        for stream in streams:
            if stream is None:
                continue
            try:
                raw_line = await asyncio.wait_for(stream.readline(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            if not raw_line:
                continue
            found_line = True
            line = raw_line.decode("utf-8", errors="ignore")
            match = TRYCLOUDFLARE_URL_PATTERN.search(line)
            if match:
                return match.group(0)
        if not found_line:
            await asyncio.sleep(0.05)
    return None


async def _stop_cloudflared() -> None:
    global cloudflared_process
    process = cloudflared_process
    if not process:
        return
    cloudflared_process = None
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def _start_cloudflared_tunnel() -> Optional[str]:
    global cloudflared_process
    global cloudflared_public_url
    if shutil.which(CLOUDFLARED_BIN) is None:
        print("cloudflared not found. Install it or set PUBLIC_BASE_URL.")
        return None
    process = await asyncio.create_subprocess_exec(
        CLOUDFLARED_BIN,
        "tunnel",
        "--url",
        CLOUDFLARED_LOCAL_URL,
        "--no-autoupdate",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    public_url = await _read_cloudflared_url(process, CLOUDFLARED_TIMEOUT_SECONDS)
    if not public_url:
        if process.returncode is None:
            process.terminate()
            with suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=5)
        print("cloudflared started but no public URL was detected. Falling back to local invite URL.")
        return None
    cloudflared_process = process
    cloudflared_public_url = _normalize_base_url(public_url)
    print(f"Cloudflared URL: {cloudflared_public_url}")
    return cloudflared_public_url


async def _share_base_url(fallback_base_url: str) -> str:
    fallback = _normalize_base_url(fallback_base_url)
    if PUBLIC_BASE_URL:
        return _normalize_base_url(PUBLIC_BASE_URL)
    if cloudflared_public_url:
        return cloudflared_public_url
    if not CLOUDFLARED_AUTOSTART:
        return fallback
    async with tunnel_lock:
        if cloudflared_public_url:
            return cloudflared_public_url
        url = await _start_cloudflared_tunnel()
        return url or fallback


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


def _find_player_by_id(room: Dict[str, Any], player_id: str) -> Optional[Dict[str, Any]]:
    for player in room["players"]:
        if player["id"] == player_id:
            return player
    return None


def _round_data(room: Dict[str, Any], round_index: int) -> Optional[Dict[str, Any]]:
    rounds = room.get("rounds", [])
    if round_index <= 0 or round_index > len(rounds):
        return None
    return rounds[round_index - 1]


def _team_radius_for_solution(solution: Optional[Dict[str, Any]]) -> float:
    if isinstance(solution, dict):
        raw = solution.get("r")
        if isinstance(raw, (int, float)):
            return float(raw)
    return 0.06


def _normalize_round_type(raw_value: Any) -> Optional[str]:
    if not isinstance(raw_value, str):
        return None
    value = raw_value.strip().lower()
    if value == "individual":
        return "individual_seeing"
    if value in ("individual_blind", "blind", "individual-blind"):
        return "individual_blind"
    if value in ("individual_seeing", "seeing", "individual-seeing"):
        return "individual_seeing"
    if value == "leader":
        return "leader"
    return None


def _game_set_path(game_set_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", game_set_id):
        raise HTTPException(status_code=404, detail="Game-set not found.")
    return GAMESETS_DIR / game_set_id


def _read_game_set(game_set_id: str) -> Dict[str, Any]:
    path = _game_set_path(game_set_id) / "game.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Game-set not found.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="Game-set data is invalid.")
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Game-set data is invalid.")
    return raw


def _public_game_set(game_set_id: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    questions = []
    for item in raw.get("questions", []):
        if not isinstance(item, dict):
            continue
        question = copy.deepcopy(item)
        for key in ("image", "reveal_image"):
            filename = question.get(key)
            if isinstance(filename, str):
                question[key] = f"/data/game_sets/{game_set_id}/images/{filename}"
        questions.append(question)
    return {"id": game_set_id, "name": raw.get("name", ""), "questions": questions}


def _list_game_sets() -> List[Dict[str, Any]]:
    result = []
    for folder in GAMESETS_DIR.iterdir():
        if not folder.is_dir():
            continue
        try:
            raw = _read_game_set(folder.name)
            questions = raw.get("questions", [])
            result.append({"id": folder.name, "name": raw.get("name", "Untitled"), "question_count": len(questions) if isinstance(questions, list) else 0})
        except HTTPException:
            continue
    return sorted(result, key=lambda item: item["name"].lower())


def _game_set_is_active(game_set_id: str) -> bool:
    return any(room.get("game_started") and room.get("game_set_id") == game_set_id for room in rooms.values())


def _decode_image(data_url: Any) -> Tuple[bytes, str]:
    if not isinstance(data_url, str):
        raise HTTPException(status_code=422, detail="Both images are required.")
    match = re.fullmatch(r"data:image/(png|jpeg|jpg|webp|gif|bmp);base64,([A-Za-z0-9+/=]+)", data_url)
    if not match:
        raise HTTPException(status_code=422, detail="Images must be valid PNG files after conversion.")
    try:
        content = base64.b64decode(match.group(2), validate=True)
    except ValueError:
        raise HTTPException(status_code=422, detail="Image data is invalid.")
    if not content or len(content) > 15 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="Each image must be smaller than 15 MB.")
    if PIL_AVAILABLE:
        try:
            with Image.open(__import__("io").BytesIO(content)) as image:
                image.verify()
        except (OSError, SyntaxError, UnidentifiedImageError):
            raise HTTPException(status_code=422, detail="An uploaded image could not be read.")
    return content, {"png": "png", "jpeg": "jpg", "jpg": "jpg", "webp": "webp", "gif": "gif", "bmp": "bmp"}[match.group(1)]


def _validate_and_store_game_set(game_set_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    name = payload.get("name")
    questions = payload.get("questions")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise HTTPException(status_code=422, detail="A game-set name of up to 100 characters is required.")
    if not isinstance(questions, list) or not questions:
        raise HTTPException(status_code=422, detail="A game-set needs at least one complete question.")
    if len(questions) > 100:
        raise HTTPException(status_code=422, detail="A game-set can contain at most 100 questions.")
    folder = _game_set_path(game_set_id)
    images_dir = folder / "images"
    folder.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(exist_ok=True)
    stored_questions = []
    for index, item in enumerate(questions, start=1):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f"Question {index} is invalid.")
        text = item.get("question")
        solution = item.get("solution")
        kind = _normalize_round_type(item.get("default_type"))
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > 200:
            raise HTTPException(status_code=422, detail=f"Question {index} needs question text.")
        if kind not in ("individual_blind", "individual_seeing", "leader"):
            raise HTTPException(status_code=422, detail=f"Question {index} needs a valid default type.")
        if not isinstance(solution, dict):
            raise HTTPException(status_code=422, detail=f"Question {index} needs a solution circle.")
        try:
            x, y, r = float(solution["x"]), float(solution["y"]), float(solution["r"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"Question {index} has an invalid solution circle.")
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0.01 <= r <= 0.5):
            raise HTTPException(status_code=422, detail=f"Question {index} has an invalid solution circle.")
        duration = item.get("duration_seconds", ROUND_DURATION_SECONDS)
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"Question {index} has an invalid time limit.")
        if not 5 <= duration <= 600:
            raise HTTPException(status_code=422, detail=f"Question {index} time limit must be between 5 and 600 seconds.")
        stored = {
            "question": text.strip(),
            "default_type": kind,
            "duration_seconds": duration,
            "solution": {"x": x, "y": y, "r": r},
            "show_crosshair": bool(item.get("show_crosshair")),
        }
        for key in ("image", "reveal_image"):
            value = item.get(key)
            if isinstance(value, str) and value.startswith("/data/game_sets/"):
                filename = Path(value.split("?")[0]).name
                if not re.fullmatch(r"[a-zA-Z0-9_-]+\.(png|jpg|webp)", filename) or not (images_dir / filename).exists():
                    raise HTTPException(status_code=422, detail=f"Question {index} has a missing {key.replace('_', ' ')}.")
            else:
                content, extension = _decode_image(value)
                filename = f"q{index}_{key}_{secrets.token_hex(6)}.{extension}"
                (images_dir / filename).write_bytes(content)
            stored[key] = filename
        stored_questions.append(stored)
    raw = {"name": name.strip(), "questions": stored_questions}
    (folder / "game.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return _public_game_set(game_set_id, raw)


def _solution_overlap(team_center: Tuple[float, float], team_radius: float, solution: Dict[str, Any]) -> bool:
    dx = team_center[0] - solution["x"]
    dy = team_center[1] - solution["y"]
    distance_sq = dx * dx + dy * dy
    radius = team_radius + solution["r"]
    return distance_sq <= radius * radius


def _room_snapshot(room: Dict[str, Any]) -> Dict[str, Any]:
    teams = {"1": [], "2": []}
    for player in room["players"]:
        name = player.get("name")
        team = player.get("team")
        if name and team in (1, 2):
            teams[str(team)].append({"id": player["id"], "name": name, "color": player.get("color")})
    return {
        "teams": teams,
        "member_counts": {key: len(names) for key, names in teams.items()},
        "join_allowed": room["join_allowed"],
        "game_started": room["game_started"],
    }


def _game_snapshot(room: Dict[str, Any]) -> Dict[str, Any]:
    clicks = []
    for player_id, click in room["clicks"].items():
        player = _find_player_by_id(room, player_id)
        if not player:
            continue
        clicks.append(
            {
                "player_id": player_id,
                "x": click["x"],
                "y": click["y"],
                "team": player.get("team"),
                "color": player.get("color"),
            }
        )
    round_data = room.get("round_data") or {}
    leader_id = room.get("leader_id")
    leader = _find_player_by_id(room, leader_id) if isinstance(leader_id, str) else None
    return {
        "round_index": room["round_index"],
        "round_type": room["round_type"],
        "phase": room["phase"],
        "active_team": room["active_team"],
        "question": room["question"],
        "timer_end": room["timer_end"],
        "image": round_data.get("image"),
        "reveal_image": round_data.get("reveal_image"),
        "scores": room["scores"],
        "clicks": clicks,
        "centroid": room["centroid"],
        "solution": room["solution"],
        "team_radius": room["team_radius"],
        "leader_id": leader_id,
        "leader_name": leader.get("name") if leader else None,
        "default_round_type": round_data.get("default_type"),
        "default_duration_seconds": round_data.get("duration_seconds") if round_data else None,
        "next_default_duration_seconds": (room.get("rounds") or [{}])[room["round_index"]].get("duration_seconds") if room["round_index"] < len(room.get("rounds", [])) else None,
        "game_set_id": room.get("game_set_id"),
        "game_set_name": room.get("game_set_name"),
        "next_default_type": (room.get("rounds") or [{}])[room["round_index"]].get("default_type") if room["round_index"] < len(room.get("rounds", [])) else None,
    }


async def _broadcast(room: Dict[str, Any], message: Dict[str, Any]) -> None:
    sockets: List[WebSocket] = []
    if room.get("host") is not None:
        sockets.append(room["host"])
    sockets.extend(player["ws"] for player in room["players"])
    for ws in sockets:
        await _safe_send(ws, message)


def _cancel_timer(room: Dict[str, Any]) -> None:
    task = room.get("timer_task")
    if task and not task.done():
        task.cancel()
    room["timer_task"] = None


async def _finish_round(room_id: str) -> None:
    delay = 0.0
    async with rooms_lock:
        room = rooms.get(room_id)
        if not room or room["phase"] != "question":
            return
        if room["timer_end"]:
            delay = max(0.0, room["timer_end"] - _now())
    if delay:
        await asyncio.sleep(delay)
    async with rooms_lock:
        room = rooms.get(room_id)
        if not room or room["phase"] != "question":
            return
        room["phase"] = "reveal"
        room["timer_end"] = None
        room["timer_task"] = None
        clicks = []
        for player_id, click in room["clicks"].items():
            player = _find_player_by_id(room, player_id)
            if player and player.get("team") == room["active_team"]:
                clicks.append((click["x"], click["y"]))
        if clicks:
            avg_x = sum(point[0] for point in clicks) / len(clicks)
            avg_y = sum(point[1] for point in clicks) / len(clicks)
            room["centroid"] = {"x": avg_x, "y": avg_y}
            solution = room.get("solution")
            if solution and _solution_overlap((avg_x, avg_y), room["team_radius"], solution):
                room["scores"][room["active_team"]] += 1
        else:
            room["centroid"] = None
        snapshot = _game_snapshot(room)
    await _broadcast(room, {"type": "game_state", **snapshot})


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


@app.get("/editor")
async def editor() -> HTMLResponse:
    return HTMLResponse(EDITOR_PATH.read_text(encoding="utf-8"))


@app.get("/api/game-sets")
async def list_game_sets() -> List[Dict[str, Any]]:
    return _list_game_sets()


@app.get("/api/game-sets/{game_set_id}")
async def get_game_set(game_set_id: str) -> Dict[str, Any]:
    return _public_game_set(game_set_id, _read_game_set(game_set_id))


@app.post("/api/game-sets")
async def create_game_set(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Game-set data is invalid.")
    return _validate_and_store_game_set(secrets.token_urlsafe(9).replace("-", "_").replace("~", "_"), payload)


@app.put("/api/game-sets/{game_set_id}")
async def save_game_set(game_set_id: str, request: Request) -> Dict[str, Any]:
    _read_game_set(game_set_id)
    if _game_set_is_active(game_set_id):
        raise HTTPException(status_code=409, detail="This game-set is read-only while a game is active.")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Game-set data is invalid.")
    return _validate_and_store_game_set(game_set_id, payload)


@app.post("/api/game-sets/{game_set_id}/duplicate")
async def duplicate_game_set(game_set_id: str) -> Dict[str, Any]:
    raw = _read_game_set(game_set_id)
    target_id = secrets.token_urlsafe(9).replace("-", "_").replace("~", "_")
    shutil.copytree(_game_set_path(game_set_id), _game_set_path(target_id))
    raw["name"] = f"{str(raw.get('name', 'Game set'))} copy"
    (_game_set_path(target_id) / "game.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return _public_game_set(target_id, raw)


@app.delete("/api/game-sets/{game_set_id}")
async def delete_game_set(game_set_id: str) -> Dict[str, bool]:
    folder = _game_set_path(game_set_id)
    if not folder.exists():
        raise HTTPException(status_code=404, detail="Game-set not found.")
    if _game_set_is_active(game_set_id):
        raise HTTPException(status_code=409, detail="This game-set is read-only while a game is active.")
    shutil.rmtree(folder)
    return {"ok": True}


@app.post("/create-room")
async def create_room(request: Request) -> Dict[str, str]:
    async with rooms_lock:
        if len(rooms) >= MAX_ROOMS:
            raise HTTPException(status_code=429, detail="Room limit reached.")
        room_id = _new_room_id()
        rooms[room_id] = _new_room()
    fallback_base = _normalize_base_url(str(request.base_url))
    share_base = await _share_base_url(fallback_base)
    invite_url = _invite_url(share_base, room_id)
    print(f"Invite link: {invite_url}")
    return {"room_id": room_id, "invite_url": invite_url}


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
                        rooms[room_id] = _new_room()
                        rooms[room_id]["host"] = ws
                    origin = ws.headers.get("origin")
                    fallback_base = _normalize_base_url(origin) if origin else _normalize_base_url(CLOUDFLARED_LOCAL_URL)
                    share_base = await _share_base_url(fallback_base)
                    invite_url = _invite_url(share_base, room_id)
                    role = "host"
                    await _safe_send(
                        ws,
                        {"type": "room_created", "room_id": room_id, "role": "host", "invite_url": invite_url},
                    )
                    print(f"Invite link: {invite_url}")
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
                        game_snapshot = _game_snapshot(room)
                    origin = ws.headers.get("origin")
                    fallback_base = _normalize_base_url(origin) if origin else _normalize_base_url(CLOUDFLARED_LOCAL_URL)
                    share_base = await _share_base_url(fallback_base)
                    invite_url = _invite_url(share_base, room_id)
                    role = "host"
                    await _safe_send(
                        ws,
                        {"type": "room_created", "room_id": room_id, "role": "host", "invite_url": invite_url},
                    )
                    await _safe_send(ws, {"type": "lobby_state", **snapshot})
                    await _safe_send(ws, {"type": "game_state", **game_snapshot})
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
                        default_color = DEFAULT_COLORS[len(room["players"]) % len(DEFAULT_COLORS)]
                        player = _new_player(ws, default_color=default_color)
                        room["players"].append(player)
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                        game_snapshot = _game_snapshot(room)
                    role = "player"
                    await _safe_send(
                        ws,
                        {
                            "type": "room_joined",
                            "room_id": room_id,
                            "role": "player",
                            "player_id": player["id"],
                            "colors": DEFAULT_COLORS,
                            "default_color": player.get("color"),
                        },
                    )
                    await _safe_send(ws, {"type": "lobby_state", **snapshot})
                    await _safe_send(ws, {"type": "game_state", **game_snapshot})
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    continue

                await _safe_send(
                    ws,
                    {"type": "error", "message": "First message must be create_room, host_join, or join_room."},
                )
                continue

            if role == "host":
                if msg_type == "select_game_set":
                    selected_id = msg.get("game_set_id")
                    if not isinstance(selected_id, str):
                        await _safe_send(ws, {"type": "error", "message": "Choose a game-set."})
                        continue
                    try:
                        raw_set = _read_game_set(selected_id)
                    except HTTPException as exc:
                        await _safe_send(ws, {"type": "error", "message": str(exc.detail)})
                        continue
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        if room["game_started"]:
                            await _safe_send(ws, {"type": "error", "message": "The game-set is read-only while a game is active."})
                            continue
                        room["game_set_id"] = selected_id
                        room["game_set_name"] = str(raw_set.get("name", ""))
                        room["rounds"] = _public_game_set(selected_id, raw_set)["questions"]
                        room["last_activity"] = _now()
                        game_snapshot = _game_snapshot(room)
                    await _safe_send(ws, {"type": "game_state", **game_snapshot})
                    continue

                if msg_type == "start_game":
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        if not room.get("rounds"):
                            await _safe_send(ws, {"type": "error", "message": "Select a game-set before starting."})
                            continue
                        room["join_allowed"] = True
                        room["game_started"] = True
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    await _broadcast(room, {"type": "status", "message": "Game started. Players can still join."})
                    continue

                if msg_type == "start_round":
                    round_type = _normalize_round_type(msg.get("round_type"))
                    if round_type not in ("individual_blind", "individual_seeing", "leader"):
                        await _safe_send(
                            ws,
                            {
                                "type": "error",
                                "message": "Round type must be individual_blind, individual_seeing, or leader.",
                            },
                        )
                        continue
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        if not room["game_started"]:
                            await _safe_send(ws, {"type": "error", "message": "Start the game after selecting a game-set first."})
                            continue
                        _cancel_timer(room)
                        room["round_index"] += 1
                        data = _round_data(room, room["round_index"])
                        if not data:
                            room["round_index"] -= 1
                            await _safe_send(ws, {"type": "error", "message": "No more rounds available."})
                            continue
                        room["round_type"] = round_type
                        room["round_data"] = data
                        room["phase"] = "image"
                        room["question"] = str(data.get("question", "")).strip()[:200]
                        room["question_duration_seconds"] = float(data.get("duration_seconds", ROUND_DURATION_SECONDS))
                        room["timer_end"] = None
                        room["clicks"] = {}
                        room["centroid"] = None
                        room["solution"] = data.get("solution")
                        room["team_radius"] = _team_radius_for_solution(room["solution"])
                        room["active_team"] = 1 if room["round_index"] % 2 == 1 else 2
                        room["leader_id"] = None
                        room["join_allowed"] = True
                        room["game_started"] = True
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                        game_snapshot = _game_snapshot(room)
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    await _broadcast(room, {"type": "game_state", **game_snapshot})
                    continue

                if msg_type == "move_player":
                    player_id = msg.get("player_id")
                    team = msg.get("team")
                    if not isinstance(player_id, str) or team not in (1, 2):
                        await _safe_send(ws, {"type": "error", "message": "Player and destination team are required."})
                        continue
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        player = _find_player_by_id(room, player_id)
                        if not player:
                            await _safe_send(ws, {"type": "error", "message": "Player is no longer connected."})
                            continue
                        player["team"] = team
                        # A moved player must not retain a click or leader role from their old team.
                        room["clicks"].pop(player_id, None)
                        if room.get("leader_id") == player_id:
                            room["leader_id"] = None
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                        game_snapshot = _game_snapshot(room)
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    await _broadcast(room, {"type": "game_state", **game_snapshot})
                    continue

                if msg_type == "reset_game":
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        _cancel_timer(room)
                        room["join_allowed"] = True
                        room["game_started"] = False
                        room["round_index"] = 0
                        room["round_type"] = None
                        room["phase"] = "idle"
                        room["active_team"] = 1
                        room["question"] = ""
                        room["timer_end"] = None
                        room["clicks"] = {}
                        room["scores"] = {1: 0, 2: 0}
                        room["centroid"] = None
                        room["solution"] = None
                        room["team_radius"] = 0.06
                        room["round_data"] = None
                        room["leader_id"] = None
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                        game_snapshot = _game_snapshot(room)
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    await _broadcast(room, {"type": "game_state", **game_snapshot})
                    await _broadcast(room, {"type": "status", "message": "Game reset. Teams have been kept."})
                    continue

                if msg_type == "reveal_question":
                    round_type = _normalize_round_type(msg.get("round_type"))
                    requested_duration = msg.get("duration_seconds")
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        if room["phase"] != "image":
                            await _safe_send(ws, {"type": "error", "message": "Round not ready for question."})
                            continue
                        if round_type in ("individual_blind", "individual_seeing", "leader"):
                            room["round_type"] = round_type
                            room["leader_id"] = None
                        if requested_duration is not None:
                            try:
                                requested_duration = float(requested_duration)
                            except (TypeError, ValueError):
                                await _safe_send(ws, {"type": "error", "message": "Time limit must be between 5 and 600 seconds."})
                                continue
                            if not 5 <= requested_duration <= 600:
                                await _safe_send(ws, {"type": "error", "message": "Time limit must be between 5 and 600 seconds."})
                                continue
                            room["question_duration_seconds"] = requested_duration
                        room["phase"] = "question"
                        room["timer_end"] = _now() + room.get("question_duration_seconds", ROUND_DURATION_SECONDS)
                        room["last_activity"] = _now()
                        _cancel_timer(room)
                        room["timer_task"] = asyncio.create_task(_finish_round(room_id))
                        game_snapshot = _game_snapshot(room)
                    await _broadcast(room, {"type": "game_state", **game_snapshot})
                    continue

                if msg_type == "restart_round":
                    round_type = _normalize_round_type(msg.get("round_type"))
                    if round_type is None:
                        # Backward-compatible fallback for older host clients that do not send round_type on restart.
                        round_type = "individual_seeing"
                    if round_type is not None and round_type not in ("individual_blind", "individual_seeing", "leader"):
                        await _safe_send(
                            ws,
                            {
                                "type": "error",
                                "message": "Round type must be individual_blind, individual_seeing, or leader.",
                            },
                        )
                        continue
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        if room["round_index"] <= 0:
                            await _safe_send(ws, {"type": "error", "message": "No round to restart."})
                            continue
                        _cancel_timer(room)
                        room["phase"] = "image"
                        if round_type is not None:
                            room["round_type"] = round_type
                        round_data = room.get("round_data") or {}
                        room["question"] = str(round_data.get("question", "")).strip()[:200]
                        room["timer_end"] = None
                        room["clicks"] = {}
                        room["leader_id"] = None
                        room["centroid"] = None
                        room["last_activity"] = _now()
                        game_snapshot = _game_snapshot(room)
                    await _broadcast(room, {"type": "game_state", **game_snapshot})
                    continue

                await _safe_send(
                    ws,
                    {"type": "error", "message": "Host can only start_game, start_round, reveal_question, restart_round, move_player, or reset_game."},
                )
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
                        player = _find_player(room, ws)
                        if not player:
                            await _safe_send(ws, {"type": "error", "message": "Player not registered."})
                            continue
                        if room["game_started"] and player.get("team") is not None:
                            await _safe_send(ws, {"type": "error", "message": "Your player details are locked after the game has started."})
                            continue
                        player["name"] = name
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    continue

                if msg_type == "set_color":
                    color = msg.get("color")
                    if not isinstance(color, str) or not color.startswith("#"):
                        await _safe_send(ws, {"type": "error", "message": "Color is required."})
                        continue
                    if color.lower() == DISALLOWED_COLOR:
                        await _safe_send(ws, {"type": "error", "message": "Red is reserved for the solution."})
                        continue
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        player = _find_player(room, ws)
                        if not player:
                            await _safe_send(ws, {"type": "error", "message": "Player not registered."})
                            continue
                        if room["game_started"] and player.get("team") is not None:
                            await _safe_send(ws, {"type": "error", "message": "Your player details are locked after the game has started."})
                            continue
                        player["color"] = color
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    continue

                if msg_type == "set_team":
                    team = msg.get("team")
                    if team not in (1, 2):
                        await _safe_send(ws, {"type": "error", "message": "Team must be 1 or 2."})
                        continue
                    name = msg.get("name")
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        player = _find_player(room, ws)
                        if not player:
                            await _safe_send(ws, {"type": "error", "message": "Player not registered."})
                            continue
                        if room["game_started"] and player.get("team") is not None:
                            await _safe_send(ws, {"type": "error", "message": "Only the host can change teams after the game has started."})
                            continue
                        if isinstance(name, str) and name.strip():
                            player["name"] = name.strip()[:32]
                        if not player.get("name"):
                            await _safe_send(ws, {"type": "error", "message": "Set name first."})
                            continue
                        if not player.get("color"):
                            player["color"] = DEFAULT_COLORS[0]
                        player["team"] = team
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    continue

                if msg_type == "click":
                    x = msg.get("x")
                    y = msg.get("y")
                    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                        await _safe_send(ws, {"type": "error", "message": "Invalid click coordinates."})
                        continue
                    if x < 0 or x > 1 or y < 0 or y > 1:
                        await _safe_send(ws, {"type": "error", "message": "Click out of bounds."})
                        continue
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        if room["phase"] != "question":
                            await _safe_send(ws, {"type": "error", "message": "Round not accepting clicks."})
                            continue
                        player = _find_player(room, ws)
                        if not player:
                            await _safe_send(ws, {"type": "error", "message": "Player not registered."})
                            continue
                        if player.get("team") != room["active_team"]:
                            await _safe_send(ws, {"type": "error", "message": "Not your team's turn."})
                            continue
                        if room.get("round_type") == "leader":
                            leader_id = room.get("leader_id")
                            if leader_id is None:
                                room["leader_id"] = player["id"]
                            elif leader_id != player["id"]:
                                await _safe_send(ws, {"type": "error", "message": "Only the team leader can click in this round."})
                                continue
                            room["clicks"] = {room["leader_id"]: {"x": float(x), "y": float(y)}}
                        else:
                            room["clicks"][player["id"]] = {"x": float(x), "y": float(y)}
                        room["last_activity"] = _now()
                        leader_id = room.get("leader_id")
                        leader = _find_player_by_id(room, leader_id) if isinstance(leader_id, str) else None
                        click_message = {
                            "type": "click_update",
                            "player_id": player["id"],
                            "x": float(x),
                            "y": float(y),
                            "team": player.get("team"),
                            "color": player.get("color"),
                            "leader_id": leader_id,
                            "leader_name": leader.get("name") if leader else None,
                        }
                    await _broadcast(room, click_message)
                    continue

                await _safe_send(ws, {"type": "error", "message": "Player can only set_name, set_color, set_team, or click."})
                continue

    except WebSocketDisconnect:
        pass
    finally:
        if role and room_id:
            async with rooms_lock:
                room = rooms.get(room_id)
                if room:
                    if role == "host":
                        _cancel_timer(room)
                        room["host"] = None
                        room["join_allowed"] = False
                        await _broadcast(room, {"type": "status", "message": "Host disconnected. Room closed."})
                        rooms.pop(room_id, None)
                    else:
                        leaving_player = _find_player(room, ws)
                        leaving_player_id = leaving_player["id"] if leaving_player else None
                        room["players"] = [player for player in room["players"] if player["ws"] is not ws]
                        for player_id, click in list(room["clicks"].items()):
                            if not _find_player_by_id(room, player_id):
                                room["clicks"].pop(player_id, None)
                        if leaving_player_id and room.get("leader_id") == leaving_player_id:
                            room["leader_id"] = None
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                        await _broadcast(room, {"type": "lobby_state", **snapshot})
                        if room["phase"] != "idle":
                            await _broadcast(room, {"type": "game_state", **_game_snapshot(room)})
                        if room.get("host") is None and not room.get("players"):
                            rooms.pop(room_id, None)
