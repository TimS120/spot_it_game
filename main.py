import asyncio
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
try:
    from PIL import Image, UnidentifiedImageError
except ImportError:
    Image = None
    UnidentifiedImageError = Exception


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
STATIC_DIR = BASE_DIR / "static"
RESULTS_PATH = STATIC_DIR / "results.json"
IMAGES_DIR = STATIC_DIR / "images"
OPTIMIZED_IMAGES_DIR = IMAGES_DIR / "_optimized"
IMAGE_TARGET_SIZE = int(os.getenv("IMAGE_TARGET_SIZE", "1080"))
IMAGE_JPEG_QUALITY = int(os.getenv("IMAGE_JPEG_QUALITY", "82"))
PIL_AVAILABLE = Image is not None
RESAMPLE_LANCZOS = Image.Resampling.LANCZOS if PIL_AVAILABLE and hasattr(Image, "Resampling") else getattr(Image, "LANCZOS", None)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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


def _load_rounds() -> List[Dict[str, Any]]:
    if RESULTS_PATH.exists():
        try:
            raw = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [item for item in raw if isinstance(item, dict)]
            if isinstance(raw, dict):
                legacy_rounds = raw.get("rounds", [])
                if isinstance(legacy_rounds, list):
                    return [item for item in legacy_rounds if isinstance(item, dict)]
        except json.JSONDecodeError:
            return []
    return []


ROUNDS = _load_rounds()


def _is_remote_image(path: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9+.-]*://", path, flags=re.IGNORECASE))


def _resolve_local_image_path(path: str) -> Optional[Path]:
    candidate = path.strip().replace("\\", "/")
    if not candidate or _is_remote_image(candidate):
        return None
    if candidate.startswith("/"):
        candidate = candidate[1:]
    if candidate.startswith("static/"):
        candidate = candidate[len("static/") :]
    if candidate.startswith("images/"):
        candidate = candidate[len("images/") :]
    file_path = (IMAGES_DIR / candidate).resolve()
    images_root = IMAGES_DIR.resolve()
    if images_root not in file_path.parents and file_path != images_root:
        return None
    return file_path


def _letterbox_transform(source_width: int, source_height: int) -> Dict[str, float]:
    side = float(max(source_width, source_height))
    scale_x = float(source_width) / side
    scale_y = float(source_height) / side
    pad_x = (1.0 - scale_x) / 2.0
    pad_y = (1.0 - scale_y) / 2.0
    return {"scale_x": scale_x, "scale_y": scale_y, "pad_x": pad_x, "pad_y": pad_y}


def _apply_letterbox_solution(solution: Optional[Dict[str, Any]], transform: Dict[str, float]) -> Optional[Dict[str, Any]]:
    if not isinstance(solution, dict):
        return None
    x = float(solution.get("x", 0.5))
    y = float(solution.get("y", 0.5))
    r = float(solution.get("r", 0.06))
    return {
        "x": transform["pad_x"] + x * transform["scale_x"],
        "y": transform["pad_y"] + y * transform["scale_y"],
        "r": r * transform["scale_x"],
    }


def _derive_reveal_image_path(image_path: str) -> str:
    path = image_path.strip()
    if not path:
        return path
    # Keep query/hash suffixes intact.
    suffix = ""
    split_idx = min([idx for idx in [path.find("?"), path.find("#")] if idx != -1], default=-1)
    if split_idx != -1:
        suffix = path[split_idx:]
        path = path[:split_idx]
    dot = path.rfind(".")
    if dot <= 0:
        derived = f"{path}_1"
    else:
        derived = f"{path[:dot]}_1{path[dot:]}"
    return f"{derived}{suffix}"


def _optimize_round_image(path: str) -> Tuple[Optional[str], Optional[Dict[str, float]]]:
    local_path = _resolve_local_image_path(path)
    if not local_path or not local_path.exists() or not PIL_AVAILABLE:
        return None, None
    try:
        stat = local_path.stat()
        cache_key = hashlib.sha256(
            f"{local_path.as_posix()}|{stat.st_mtime_ns}|{stat.st_size}|{IMAGE_TARGET_SIZE}|{IMAGE_JPEG_QUALITY}".encode(
                "utf-8"
            )
        ).hexdigest()[:12]
        OPTIMIZED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        out_name = f"{local_path.stem}.{cache_key}.jpg"
        out_path = OPTIMIZED_IMAGES_DIR / out_name
        with Image.open(local_path) as src:
            src_rgb = src.convert("RGB")
            source_width, source_height = src_rgb.size
            side = max(source_width, source_height)
            square = Image.new("RGB", (side, side), (0, 0, 0))
            offset = ((side - source_width) // 2, (side - source_height) // 2)
            square.paste(src_rgb, offset)
            if side != IMAGE_TARGET_SIZE:
                if RESAMPLE_LANCZOS is not None:
                    square = square.resize((IMAGE_TARGET_SIZE, IMAGE_TARGET_SIZE), RESAMPLE_LANCZOS)
                else:
                    square = square.resize((IMAGE_TARGET_SIZE, IMAGE_TARGET_SIZE))
            if not out_path.exists():
                square.save(out_path, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True, progressive=True)
        optimized_relative = f"_optimized/{out_name}"
        transform = _letterbox_transform(source_width, source_height)
        return optimized_relative, transform
    except (OSError, UnidentifiedImageError, ValueError):
        return None, None


def _prepare_round_assets() -> None:
    if not PIL_AVAILABLE:
        print("Pillow not installed. Skipping image optimization; serving original files.")
    for round_data in ROUNDS:
        transform = None
        image_path = round_data.get("image")
        if not isinstance(image_path, str) or not image_path.strip():
            continue
        if not isinstance(round_data.get("reveal_image"), str) or not round_data.get("reveal_image", "").strip():
            round_data["reveal_image"] = _derive_reveal_image_path(image_path)
        for image_key in ("image", "reveal_image"):
            raw_path = round_data.get(image_key)
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            optimized_path, image_transform = _optimize_round_image(raw_path)
            if optimized_path:
                round_data[image_key] = optimized_path
                if transform is None and image_transform:
                    transform = image_transform
        if transform:
            mapped_solution = _apply_letterbox_solution(round_data.get("solution"), transform)
            if mapped_solution is not None:
                round_data["solution"] = mapped_solution


_prepare_round_assets()


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
        "timer_end": None,
        "clicks": {},
        "scores": {1: 0, 2: 0},
        "centroid": None,
        "solution": None,
        "team_radius": 0.06,
        "round_data": None,
        "timer_task": None,
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


def _round_data(round_index: int) -> Optional[Dict[str, Any]]:
    if round_index <= 0 or round_index > len(ROUNDS):
        return None
    return ROUNDS[round_index - 1]


def _team_radius_for_solution(solution: Optional[Dict[str, Any]]) -> float:
    if isinstance(solution, dict):
        raw = solution.get("r")
        if isinstance(raw, (int, float)):
            return float(raw)
    return 0.06


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
                        if not room["join_allowed"]:
                            await _safe_send(ws, {"type": "error", "message": "Joining is closed."})
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
                    await _broadcast(room, {"type": "status", "message": "Joining closed."})
                    continue

                if msg_type == "start_round":
                    round_type = msg.get("round_type")
                    if round_type not in ("individual", "leader"):
                        await _safe_send(ws, {"type": "error", "message": "Round type must be individual or leader."})
                        continue
                    if round_type == "leader":
                        await _safe_send(ws, {"type": "error", "message": "Leader round not implemented yet."})
                        continue
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        _cancel_timer(room)
                        room["round_index"] += 1
                        data = _round_data(room["round_index"])
                        if not data:
                            room["round_index"] -= 1
                            await _safe_send(ws, {"type": "error", "message": "No more rounds available."})
                            continue
                        room["round_type"] = round_type
                        room["round_data"] = data
                        room["phase"] = "image"
                        room["question"] = str(data.get("question", "")).strip()[:200]
                        room["timer_end"] = None
                        room["clicks"] = {}
                        room["centroid"] = None
                        room["solution"] = data.get("solution")
                        room["team_radius"] = _team_radius_for_solution(room["solution"])
                        room["active_team"] = 1 if room["round_index"] % 2 == 1 else 2
                        room["join_allowed"] = False
                        room["game_started"] = True
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                        game_snapshot = _game_snapshot(room)
                    await _broadcast(room, {"type": "join_closed"})
                    await _broadcast(room, {"type": "lobby_state", **snapshot})
                    await _broadcast(room, {"type": "game_state", **game_snapshot})
                    continue

                if msg_type == "reveal_question":
                    async with rooms_lock:
                        room = rooms.get(room_id)
                        if not room:
                            await _safe_send(ws, {"type": "error", "message": "Room no longer exists."})
                            continue
                        if room["phase"] != "image":
                            await _safe_send(ws, {"type": "error", "message": "Round not ready for question."})
                            continue
                        room["phase"] = "question"
                        room["timer_end"] = _now() + ROUND_DURATION_SECONDS
                        room["last_activity"] = _now()
                        _cancel_timer(room)
                        room["timer_task"] = asyncio.create_task(_finish_round(room_id))
                        game_snapshot = _game_snapshot(room)
                    await _broadcast(room, {"type": "game_state", **game_snapshot})
                    continue

                if msg_type == "restart_round":
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
                        round_data = room.get("round_data") or {}
                        room["question"] = str(round_data.get("question", "")).strip()[:200]
                        room["timer_end"] = None
                        room["clicks"] = {}
                        room["centroid"] = None
                        room["last_activity"] = _now()
                        game_snapshot = _game_snapshot(room)
                    await _broadcast(room, {"type": "game_state", **game_snapshot})
                    continue

                await _safe_send(
                    ws,
                    {"type": "error", "message": "Host can only start_game, start_round, reveal_question, restart_round."},
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
                        if not room["join_allowed"]:
                            await _safe_send(ws, {"type": "error", "message": "Joining is closed."})
                            continue
                        player = _find_player(room, ws)
                        if not player:
                            await _safe_send(ws, {"type": "error", "message": "Player not registered."})
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
                        if not room["join_allowed"]:
                            await _safe_send(ws, {"type": "error", "message": "Joining is closed."})
                            continue
                        player = _find_player(room, ws)
                        if not player:
                            await _safe_send(ws, {"type": "error", "message": "Player not registered."})
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
                        room["clicks"][player["id"]] = {"x": float(x), "y": float(y)}
                        room["last_activity"] = _now()
                        click_message = {
                            "type": "click_update",
                            "player_id": player["id"],
                            "x": float(x),
                            "y": float(y),
                            "team": player.get("team"),
                            "color": player.get("color"),
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
                        room["players"] = [player for player in room["players"] if player["ws"] is not ws]
                        for player_id, click in list(room["clicks"].items()):
                            if not _find_player_by_id(room, player_id):
                                room["clicks"].pop(player_id, None)
                        room["last_activity"] = _now()
                        snapshot = _room_snapshot(room)
                        await _broadcast(room, {"type": "lobby_state", **snapshot})
                        if room["phase"] != "idle":
                            await _broadcast(room, {"type": "game_state", **_game_snapshot(room)})
                        if room.get("host") is None and not room.get("players"):
                            rooms.pop(room_id, None)
