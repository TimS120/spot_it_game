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
5) Use the host controls to start a round and start the fixed 60s question timer.

## Player flow
1) Open the invite link.
2) Enter a name.
3) Pick a color.
4) Click Team 1 or Team 2.
5) During the round, click once on the image to place your circle.

## Assets (required)
- Add round images under `static/images`:
  - normal image: `1.png`
  - reveal image: `1_1.png` (same name + `_1` before extension)
- Edit `static/results.json` as a plain array of rounds with only:
  - `question`
  - `image`
  - `solution` (`x`, `y`, `r`)
- Install Pillow to enable automatic image optimization:
```bash
pip install pillow
```
- On server startup, local round images are converted to square `1080x1080` JPEGs with black padding (no cropping) and served from an internal optimized cache.

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
