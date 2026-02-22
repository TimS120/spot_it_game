# Spot It Online Lobby
Browser-based lobby for a Spot It game (FastAPI + WebSockets).

## Host flow
1) Start the server:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
2) Open `http://localhost:8000` in your browser.
3) A room is created automatically and the invite link is shown on the page.
4) The server console also prints the invite link when the room is created.
5) When teams are ready, click "Start game" (placeholder loop).

## Player flow
1) Open the invite link.
2) Enter a name.
3) Click Team 1 or Team 2.

## Cloudflare tunnel (online usage)
1) Download cloudflared:
   - Windows: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/
2) Run your server locally:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
3) In a second terminal, start the tunnel:
```bash
cloudflared tunnel --url http://localhost:8000
```
4) cloudflared prints a public HTTPS URL (example: https://random.trycloudflare.com).
5) Open that URL, let it create a room, and copy the invite link to share.

Notes:
- Keep the tunnel command running while others use the game.
- Anyone with the URL can access your server.
