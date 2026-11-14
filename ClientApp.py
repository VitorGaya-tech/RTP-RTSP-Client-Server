"""LOCAL API and page of the client: the catalog that runs on the viewer's computer.

This computer has NO videos. Everything the page shows comes from the server's API (which
runs on another computer) through GET /videos, and clicking a video makes this app open the
player (AutoClient.py) right here, which requests the session via POST /iniciar_stream and
then talks RTSP/RTP directly to the server:

    browser --HTTP, loopback only--> ClientApp --GET/POST--> server API
                                        |
                                        +--opens AutoClient.py --RTSP/RTP--> server

Usage:
    python ClientApp.py http://SERVER_IP:8000
    python ClientApp.py http://SERVER_IP:8000 --port 9000 --no-browser

Security: the app listens only on 127.0.0.1 and, like any local server, must defend itself
against websites open in the same browser (see `guard` in create_app).
"""
import argparse
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from html import escape

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

DEFAULT_LOCAL_PORT = 9000

# A server that is down, or a slow network, must not leave the page hanging.
HTTP_TIMEOUT = 3.0

# Player this app opens. It sits next to this file and does not depend on the current folder.
AUTOCLIENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AutoClient.py")

PAGE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>RTP Streaming (client)</title>
    <meta name="token" content="__TOKEN__">
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #0f0f0f; color: white; margin: 0; padding: 20px; text-align: center; }
        h1 { color: #ff0000; }
        .grid { display: flex; flex-wrap: wrap; justify-content: center; gap: 20px; margin-top: 30px; }
        .card { background: #212121; padding: 15px; border-radius: 12px; cursor: pointer; width: 250px; transition: transform 0.2s; box-shadow: 0 4px 8px rgba(0,0,0,0.2); }
        .card:hover { transform: scale(1.05); background: #383838; }
        .live { border: 2px solid red; }
        .thumb { font-size: 60px; margin-bottom: 10px; }
        .title { font-size: 18px; font-weight: bold; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        #status { margin-top: 10px; color: #aaa; }
        #status.error { color: #ff6b6b; }
    </style>
</head>
<body>
    <h1>▶ Video Streaming (RTSP/RTP)</h1>
    <p>Server: <b>__SERVER__</b></p>
    <div id="status">Connecting to the server...</div>
    <div class="grid" id="catalog"></div>

    <script>
        const token = document.querySelector('meta[name=token]').content;
        const statusEl = document.getElementById('status');
        const catalog = document.getElementById('catalog');
        let lastList = null;

        function card(name, emoji, title, extra) {
            // textContent (not innerHTML): the file name comes from the server and must
            // never be interpreted as HTML.
            const d = document.createElement('div');
            d.className = 'card' + (extra ? ' ' + extra : '');
            d.onclick = () => openPlayer(name);
            const t = document.createElement('div'); t.className = 'thumb'; t.textContent = emoji;
            const n = document.createElement('div'); n.className = 'title'; n.textContent = title;
            d.append(t, n);
            return d;
        }

        function render(videos) {
            catalog.replaceChildren(card('live', '🔴', 'Live Broadcast (Webcam)', 'live'));
            videos.forEach(v => catalog.appendChild(card(v, '🎬', v)));
        }

        // Ask the server (through this app) what exists RIGHT NOW. Repeats every 5 s, so a
        // new video in the server's folder shows up by itself, and the server going down
        // becomes a warning.
        async function refresh() {
            try {
                const r = await fetch('/videos');
                const d = await r.json();
                if (!r.ok) throw new Error(d.detail || r.status);
                statusEl.className = ''; statusEl.textContent = '';
                const key = JSON.stringify(d.videos);
                if (key !== lastList) { lastList = key; render(d.videos); }
            } catch (e) {
                statusEl.className = 'error';
                statusEl.textContent = e.message.startsWith('Server') ? e.message : 'Server unavailable: ' + e.message;
                if (lastList === null) { lastList = ''; render([]); }
            }
        }

        // Ask THIS app to open the player on this machine (not on the server's).
        async function openPlayer(name) {
            const r = await fetch('/play/' + encodeURIComponent(name),
                                  { method: 'POST', headers: { 'X-Token': token } });
            if (!r.ok) {
                const d = await r.json().catch(() => ({}));
                statusEl.className = 'error'; statusEl.textContent = d.detail || ('Error ' + r.status);
            }
        }

        refresh();
        setInterval(refresh, 5000);
    </script>
</body>
</html>
"""


def normalize_url(url):
    """'192.168.0.10:8000' -> 'http://192.168.0.10:8000' (and strip the trailing slash)."""
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def create_app(server_url, local_port=DEFAULT_LOCAL_PORT):
    """Build the FastAPI app bound to ONE server. Returns (app, token)."""
    server_url = normalize_url(server_url)

    # Secret generated on every run and embedded in the page. Only whoever loaded this app's
    # page knows it, so another website open in the same browser cannot trigger the POST.
    token = secrets.token_urlsafe(16)
    allowed_hosts = {f"127.0.0.1:{local_port}", f"localhost:{local_port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    app = FastAPI()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Defenses of a local server: it only serves the browser itself, on this app's pages."""
        # A Host outside the list = DNS rebinding (a malicious site whose domain points to
        # 127.0.0.1 would make the browser talk to this app as if it were "the same site").
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"detail": "Invalid Host"}, status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:   # CSRF
                return JSONResponse({"detail": "Invalid Origin"}, status_code=403)
            if request.headers.get("x-token") != token:
                return JSONResponse({"detail": "Missing or invalid token"}, status_code=403)
        return await call_next(request)

    def server_videos():
        """GET /videos on the server. A network failure becomes 502 (no hang, no traceback leak)."""
        try:
            r = requests.get(f"{server_url}/videos", timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()["videos"]
        except (requests.RequestException, ValueError, KeyError) as e:
            raise HTTPException(status_code=502, detail=f"Server unavailable: {e.__class__.__name__}")

    @app.get("/", response_class=HTMLResponse)
    def page():
        return PAGE.replace("__TOKEN__", token).replace("__SERVER__", escape(server_url))

    @app.get("/videos")
    def videos():
        return {"servidor": server_url, "videos": server_videos()}

    @app.post("/play/{video}", status_code=202)
    def play(video: str):
        # Only opens what the SERVER confirms exists ('live' is the webcam, always valid).
        if video != "live" and video not in server_videos():
            raise HTTPException(status_code=404, detail="Video does not exist on the server")
        # Argument list (no shell) and sys.executable (the same Python as this app).
        subprocess.Popen([sys.executable, AUTOCLIENT, video, server_url])
        return {"status": "player started on this machine"}

    return app, token


def main():
    ap = argparse.ArgumentParser(description="Local catalog of the RTSP/RTP client")
    ap.add_argument("server_url", nargs="?", default=os.environ.get("SERVER_URL"),
                    help="URL of the server's API, e.g. http://192.168.0.10:8000")
    ap.add_argument("--port", type=int, default=DEFAULT_LOCAL_PORT, help="local port of the page")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = ap.parse_args()
    if not args.server_url:
        ap.error("provide the server URL (or set SERVER_URL)")

    app, _ = create_app(args.server_url, args.port)
    url = f"http://127.0.0.1:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    print(f"Catalog at {url}  (server: {normalize_url(args.server_url)})")
    # 127.0.0.1: the page can only be opened on this computer.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
