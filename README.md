# RTSP/RTP Video Streaming (Client–Server)

A small video streaming system written in Python. A **server** holds the videos (and can
share its webcam); one or more **clients**, on the same machine or on other computers,
pick a video from a web catalog and watch it in a desktop player.

- **Control channel:** RTSP over TCP (`SETUP`, `PLAY`, `PAUSE`, `TEARDOWN`)
- **Media channel:** RTP over UDP, one JPEG (Motion-JPEG, payload type 26) per packet
- **Catalog / signaling:** HTTP API built with FastAPI + Uvicorn
- **Player:** Tkinter window; the server uses OpenCV to read files and the webcam

## How it works

```
 CLIENT COMPUTER                                         SERVER COMPUTER
 ┌─────────────┐   HTTP (loopback)   ┌──────────────┐  GET /videos           ┌────────────────┐
 │  Browser    │ ──────────────────► │ ClientApp.py │ ─────────────────────► │  api_server.py │
 │ (catalog)   │                     │ 127.0.0.1:9000│  POST /iniciar_stream  │  0.0.0.0:8000  │
 └─────────────┘                     └──────┬───────┘                        └───────┬────────┘
                                            │ opens                                  │ starts with the API
                                     ┌──────▼───────┐   RTSP / TCP 8554      ┌───────▼────────┐
                                     │ AutoClient.py│ ◄────────────────────► │   Server.py    │
                                     │  + Client.py │   RTP / UDP (random)   │ + ServerWorker │
                                     │  (Tkinter)   │ ◄───────────────────── │  + VideoStream │
                                     └──────────────┘                        └────────────────┘
```

1. The client app (`ClientApp.py`) asks the server's API for the list of videos (`GET /videos`)
   and shows it as a web page. The page is dynamic: it refreshes every 5 s, so new files on
   the server appear by themselves.
2. Clicking a video opens the player (`AutoClient.py`) **on the client computer**. The player
   calls `POST /iniciar_stream/<video>` to validate the request and learn the RTSP port.
3. The player connects to the RTSP server (TCP 8554), sends `SETUP` with the UDP port it
   chose for receiving, then `PLAY`. The server starts sending one RTP packet per frame to
   that port, paced at the video's native frame rate.
4. `PAUSE` stops the sender, `TEARDOWN` ends the session and releases the file or camera.

A single RTSP server handles many clients at once (one `ServerWorker` per session, up to
32). The webcam is shared: one capture thread feeds every "live" viewer.

## Project layout

```
.
├── api_server.py          # Server side: FastAPI app + starts the RTSP server
├── Server.py              # Server side: RTSP listener, one worker per client
├── ServerWorker.py        # Server side: one RTSP session + RTP sender thread
├── VideoStream.py         # Server side: video file / shared webcam frame sources
├── ClientApp.py           # Client side: local catalog (API + web page)
├── AutoClient.py          # Client side: opens a session and starts the player
├── Client.py              # Client side: Tkinter player, RTSP client, RTP receiver
├── ClientLauncher.py      # Client side: manual launcher, useful for debugging
├── RtpPacket.py           # Shared: RTP packet encode/decode
├── RtpStats.py            # Shared: loss / fps / bitrate statistics
├── requirements-server.txt
├── requirements-client.txt
└── videos/                # Put your videos here (on the SERVER)
```

## Requirements

- Python 3 (tested with 3.13)
- **Server:** `pip install -r requirements-server.txt`
- **Client:** `pip install -r requirements-client.txt`
  - `tkinter` is not installed by pip. It ships with Python on Windows and macOS; on
    Linux install it with `sudo apt install python3-tk`.

## Quick start

### Both on the same computer

```bash
# Terminal 1 (server)
python -m uvicorn api_server:app --host 0.0.0.0 --port 8000

# Terminal 2 (client)
python ClientApp.py http://127.0.0.1:8000
```

The catalog opens at `http://127.0.0.1:9000`. Put `.mp4`, `.avi`, `.mjpeg` or `.mjpg` files in
`videos/` (the folder is created on first run).

### Two computers

1. **Server computer**: find its IP (`ipconfig` on Windows, `ip a` on Linux) and run

   ```bash
   python -m uvicorn api_server:app --host 0.0.0.0 --port 8000
   ```

   Allow **TCP 8000** and **TCP 8554** through the firewall.
2. **Client computer**: clone the repository, install the client requirements and run

   ```bash
   python ClientApp.py http://SERVER_IP:8000
   ```

   Allow Python through the client's firewall as well: the video arrives over **UDP** on a
   port the player picks automatically.

If the computers are connected directly by an Ethernet cable without DHCP, give each one a
static IP in the same subnet (for example `192.168.50.1` and `192.168.50.2`, mask
`255.255.255.0`).

> **Use the real IP, not `0.0.0.0`.** `--host 0.0.0.0` only tells the server to listen on
> every network interface. It is not an address you can open: use `127.0.0.1` on the same
> machine, or the server's real IP from another computer.

### Live webcam

The first card in the catalog streams the server's webcam (the capture backend is
Windows/DirectShow). Several clients can watch it at the same time.

## Configuration

| Setting | Where | Default |
|---|---|---|
| RTSP port | env `RTSP_PORT` (server) | `8554` |
| API port | `--port` of uvicorn (server) | `8000` |
| Max simultaneous sessions | `MAX_SESSIONS` in `Server.py` | `32` |
| Local catalog port | `--port` of `ClientApp.py` | `9000` |
| Server URL | argument of `ClientApp.py` / `AutoClient.py`, or env `SERVER_URL` | `http://127.0.0.1:8000` (AutoClient) |
| Skip opening the browser | `--no-browser` | off |

## Statistics

When the player window is closed it shows the frames received, packet loss, average frame
rate, average data rate, playback time and late/out-of-order packets. While playing, one
line per second is printed in the console, for example `[RTP] 30.0 fps | 7.46 Mbps | loss 0.00% (0/61)`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Page says *Server unavailable* | Wrong server URL, server not running, or firewall blocking TCP 8000. Test with `http://SERVER_IP:8000/videos` in a browser. |
| Browser says invalid address | You opened `http://0.0.0.0:...`. Use `127.0.0.1` or the server's real IP. |
| Player opens but the video never shows | Firewall on the client is blocking UDP, or TCP 8554 is blocked on the server. |
| `Address already in use` on the server | Another process uses port 8554 (or 8000). Change it with `RTSP_PORT` / `--port`. |
| `No module named tkinter` | Install it (`sudo apt install python3-tk` on Linux); reinstall Python with Tcl/Tk on Windows. |

## Security notes and limitations

- **There is no encryption or authentication yet.** RTSP and RTP travel in clear text and
  anyone who can reach the server's ports can watch the videos and the webcam. Use it only
  on a trusted network (for example a direct cable or a private LAN), and never expose
  ports 8000/8554 to the Internet.
- The server only accepts plain file names inside `videos/` (no `../` or absolute paths).
- `ClientApp.py` listens only on `127.0.0.1` and checks the `Host` header, the `Origin`
  header and a per-run token, so other websites open in your browser cannot make it launch
  the player.
- The catalog page served by `api_server.py` itself (`/`) and its `/play_local` endpoint open
  the player on the **server** machine; they are meant for single-computer use.
- Planned: TLS for RTSP/HTTP, per-session payload encryption, and signed session tickets.
- Each frame is sent in a single UDP datagram, so the frame size is limited (~64 KB). Frames
  are resized to 384x288 JPEG at quality 50 to stay well below that.

## License

Add the license of your choice (for example MIT) as a `LICENSE` file.
