# spot_it_game
Spot It party game (FastAPI + WebSockets).

## Quick start
1) Install dependencies:
```bash
pip install fastapi uvicorn
```
2) Run the server:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
3) Open the host view (creates a room automatically):
```
http://localhost:8000
```
4) Share the invite link printed in the server console (or the invite path shown in the UI).

## Images + results
Place round images in `static/images/` with the naming convention:
- Main image: `1.png` (or `.jpg`/`.jpeg`)
- Reveal image: `1_2.png` (or `.jpg`/`.jpeg`)

Define the solution circle for each round in `results.json` using normalized coordinates (0-1):
```json
{
  "1": { "solution": { "x": 0.5, "y": 0.5, "r": 0.08 } }
}
```

## Gameplay flow
- Host opens `/` and gets a room automatically.
- Players join using `/?room=ROOM_ID`, set name + color, then pick a team.
- Host starts a round (duration in seconds).
- Team on turn clicks the image (each player sees only their own circle; the other team sees all circles).
- When time ends, the reveal image shows and the team centroid + solution circle are displayed.

## Sharing outside your network (optional)
If you want to share it publicly, use Cloudflare Tunnel:
```bash
cloudflared tunnel --url http://localhost:8000
```

Notes:
- The server prints an invite link using localhost; replace the host with the tunnel URL when sharing.
- Anyone with the URL can access your server.
