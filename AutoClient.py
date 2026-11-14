import os
import socket
import sys
from urllib.parse import quote, urlparse

import requests
from tkinter import Tk
from Client import Client # Imports your original client, untouched

# Where the server's API is. By default it is this same machine (the previous behavior);
# to use ANOTHER computer, pass the URL as the 2nd argument or in the SERVER_URL variable:
#     python AutoClient.py video.avi http://192.168.0.10:8000
DEFAULT_SERVER_URL = "http://127.0.0.1:8000"

def free_udp_port():
    """Ask the OS for a free UDP port for this client to receive the RTP on.

    The client itself picks the receiving port and tells it to the server in the SETUP.
    Previously, the server picked the port of ANOTHER machine, which does not work over a network.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def main():
    # Check whether the video name was passed by the browser (via the API)
    if len(sys.argv) > 1:
        chosen_video = sys.argv[1]
    else:
        print("Usage: python AutoClient.py video_name [http://SERVER_IP:8000]")
        return

    api_url = (sys.argv[2] if len(sys.argv) > 2
               else os.environ.get("SERVER_URL", DEFAULT_SERVER_URL)).rstrip("/")

    print(f"Preparing session for {chosen_video} at {api_url}...")
    try:
        session_response = requests.post(
            f"{api_url}/iniciar_stream/{quote(chosen_video, safe='')}", timeout=5)
    except requests.RequestException as e:
        print(f"Could not reach the server ({api_url}): {e}")
        return

    if session_response.status_code != 200:
        print(f"Error starting the stream ({session_response.status_code}).")
        return

    session_data = session_response.json()

    # Start the graphical interface right away!
    root = Tk()
    # The RTSP server address is the SAME one we just used to reach the API. The
    # "ip_servidor" the API returns ("127.0.0.1") only holds on the server's own machine;
    # on another computer, 127.0.0.1 would be the client itself.
    ip = urlparse(api_url).hostname
    rtsp_port = session_data["porta_rtsp"]
    rtp_port = free_udp_port()
    filename = session_data["video"]

    app = Client(root, ip, rtsp_port, rtp_port, filename)
    app.master.title(f"Streaming: {filename}")
    root.mainloop()

if __name__ == "__main__":
    main()
