from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
import threading
import os
from Server import Server # Importing your adapted Server class
import subprocess
from fastapi.responses import HTMLResponse

# RTSP server port. Fixed, so the firewall needs a single rule and the client knows where
# to connect. It can be changed without editing the code: RTSP_PORT=9000 uvicorn ...
RTSP_PORT = int(os.environ.get("RTSP_PORT", 8554))

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start ONE RTSP server together with the API and shut it down when the API stops.

    Previously, each call to /iniciar_stream created a new Server (random port, a single
    client, never shut down). Now there is only one, serving all clients. If the port is
    busy, Server() raises OSError here and the API does not even start, which is better
    than starting "healthy" with nobody to stream.
    """
    rtsp_server = Server(RTSP_PORT)
    thread = threading.Thread(target=rtsp_server.run, daemon=True, name="rtsp-server")
    thread.start()
    app.state.rtsp_server = rtsp_server
    try:
        yield
    finally:
        rtsp_server.stop()  # ends the active sessions and releases the webcam
        thread.join(timeout=3)

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
def web_interface():
    """HTML page that acts as the YouTube-style catalog"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>RTP Streaming</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #0f0f0f; color: white; margin: 0; padding: 20px; text-align: center; }
            h1 { color: #ff0000; }
            .grid { display: flex; flex-wrap: wrap; justify-content: center; gap: 20px; margin-top: 30px; }
            .card { background: #212121; padding: 15px; border-radius: 12px; cursor: pointer; width: 250px; transition: transform 0.2s; box-shadow: 0 4px 8px rgba(0,0,0,0.2); }
            .card:hover { transform: scale(1.05); background: #383838; }
            .thumb { font-size: 60px; margin-bottom: 10px; }
            .title { font-size: 18px; font-weight: bold; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        </style>
    </head>
    <body>
        <h1>▶ Video Streaming (RTSP/RTP)</h1>
        <p>Select one video to the native system player:</p>
        <div class="grid" id="catalogo">
            <div class="card" onclick="abrirPlayer('live')" style="border: 2px solid red;">
                <div class="thumb">🔴</div>
                <div class="title">Live Broadcast (Webcam)</div>
            </div>
        </div>

        <script>
            // Fetch the list of videos from our API
            fetch('/videos')
                .then(r => r.json())
                .then(data => {
                    const div = document.getElementById('catalogo');
                    data.videos.forEach(v => {
                        div.innerHTML += `
                            <div class="card" onclick="abrirPlayer('${v}')">
                                <div class="thumb">🎬</div>
                                <div class="title">${v}</div>
                            </div>
                        `;
                    });
                });

            // Tell the server to open the Tkinter player on the computer
            function abrirPlayer(nomeVideo) {
                fetch('/play_local/' + nomeVideo, { method: 'POST' });
            }
        </script>
    </body>
    </html>
    """
    return html

@app.post("/play_local/{video_name}")
def launch_local_client(video_name: str):
    """The API runs a terminal command on the backend to open the Tkinter interface"""
    subprocess.Popen(["python", "AutoClient.py", video_name])
    return {"status": "Player opened!"}

# Folder where your videos live
VIDEOS_DIR = "videos/"

@app.get("/videos")
def list_videos():
    """Return all the videos available in the folder"""
    if not os.path.exists(VIDEOS_DIR):
        os.makedirs(VIDEOS_DIR) # Create the folder if it does not exist
        return {"videos": []}

    # Updated to list .mp4, .avi, .mjpeg and .mjpg files
    allowed_extensions = ('.mp4', '.avi', '.mjpeg', '.mjpg', '.Mjpeg')
    files = [f for f in os.listdir(VIDEOS_DIR) if f.endswith(allowed_extensions)]
    return {"videos": files}

@app.post("/iniciar_stream/{video_name}")
def start_session(video_name: str):
    """Validate the request and tell the client where the RTSP server is.

    It no longer creates anything: the server is already up (see lifespan). The client
    connects to the fixed port and picks its own RTP port, which it tells the server in the SETUP.
    """
    if video_name != 'live':
        # Only a plain name; paths ("..", "\\", ":") do not escape the videos folder.
        if (video_name in ('.', '..') or video_name != os.path.basename(video_name)
                or '\\' in video_name or ':' in video_name):
            raise HTTPException(status_code=404, detail="Video not found")
        video_path = os.path.join(VIDEOS_DIR, video_name)
        if not os.path.exists(video_path):
            raise HTTPException(status_code=404, detail="Video not found")

    return {
        "status": "success",
        "video": video_name,
        "ip_servidor": "127.0.0.1",
        "porta_rtsp": RTSP_PORT
    }
