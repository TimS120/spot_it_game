# Spot It Online
Browser-based Spot It game with FastAPI + WebSockets.

## Host flow
1) Start the server:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
2) Open `http://localhost:8000` in your browser.
3) A room is created automatically and the invite link is shown on the page.
4) The server console also prints the invite link when the room is created.
5) Use **Start game** to lock assigned players' names, colors, and teams without starting a round. The roster includes controls to move any player between teams, before or during a game. The host can reset the game at any time; this clears scores and round progress but keeps the teams.

## Player flow
1) Open the invite link.
2) Enter a name.
3) Pick a color.
4) Click Team 1 or Team 2.
5) Players can join at any point, including while a round is in progress. During the round, click once on the image to place your circle.

## Create game-sets

Open `http://localhost:8000/editor` while the server is running. Create a named game-set, then add questions with:

- required question text;
- a normal question image and a reveal/answer image;
- a default round type; and
- a per-question time limit (5–600 seconds; default 60 seconds); and
- a solution circle, placed by clicking the normal image, then dragged and resized as needed.

Use the up/down controls to change question order. Game-sets are saved under `data/game_sets/`, one folder per set, and are intentionally ignored by Git. From the host page, select a game-set before starting the game. Its saved questions are copied into the room and remain unchanged for that game; the host can temporarily change each round type.

The host always sees the saved default round type beside the type selector, even after choosing a temporary override.

For either image, use **Crop image** to move and resize an orange crop window, optionally lock it to a 1:1 aspect ratio, then choose **Apply crop**. Cropping a question image intentionally clears its solution circle so it can be placed accurately again. The creation-only crosshair is a positioning aid and is not shown while playing.

An intentionally tiny, complete starter set is included in `example-game-set/`; copy it to `data/game_sets/example` to use it.

Supported upload formats are PNG, JPG/JPEG, WebP, GIF, and BMP. The editor automatically converts every upload to an optimized PNG, preserving its aspect ratio while resizing large uploads to a maximum side length of 1920 pixels (stored images are limited to 15 MB). Install Pillow to validate uploads:
```bash
pip install pillow
```

## Cloudflare tunnel (online usage)
### Fully automatic host startup (recommended)
Use this when the host starts locally on `localhost`, but players must join via public link.

1) Install cloudflared (one-time):
   - Windows: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/
2) Start the game server:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
3) Open `http://localhost:8000` on the host machine.
4) Wait for room creation. The server and host page now show a public invite URL like:
   - `https://<random>.trycloudflare.com/?room=<room_id>`
5) Share that invite URL with players.

Notes:
- Host keeps admin rights by opening without `?room=...`.
- Players always join with restricted rights via the shared `?room=...` link.
- Keep this terminal running during the game.
- To disable autostart and use local/manual mode: set `CLOUDFLARED_AUTOSTART=false`.

Manual fallback: start cloudflared yourself
1) Run server:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
2) In a second terminal:
```bash
cloudflared tunnel --url http://localhost:8000
```
3) Open the printed `https://...trycloudflare.com` URL as host and create the room there.

Optional env vars:
- `PUBLIC_BASE_URL=https://your-public-url` forces invite links to that URL.
- `CLOUDFLARED_AUTOSTART=false` disables automatic cloudflared startup.
- `CLOUDFLARED_BIN=cloudflared` sets executable name/path.
- `CLOUDFLARED_LOCAL_URL=http://127.0.0.1:8000` sets local target for tunnel.
- `CLOUDFLARED_TIMEOUT_SECONDS=20` timeout for discovering tunnel URL.

Notes:
- Keep the tunnel process running while players are connected.
- Anyone with the URL can access your server.
