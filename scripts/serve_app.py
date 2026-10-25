#!/usr/bin/env python3
"""
DeckStream Local Companion Web Server
Serves the local companion dashboard and mock telemetry endpoints for local testing.
"""

import http.server
import json
import os
import socketserver
import sys

PORT = 8765
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))

GAMES = [
    {
        "id": "cp77",
        "title": "Cyberpunk 2077",
        "size_gb": 69.2,
        "streamable": True,
        "status": "playing",
        "startup_gb": 12.8,
        "genre": "Action RPG"
    },
    {
        "id": "bg3",
        "title": "Baldur's Gate 3",
        "size_gb": 122.4,
        "streamable": True,
        "status": "ready",
        "startup_gb": 9.4,
        "genre": "CRPG"
    },
    {
        "id": "sifu",
        "title": "Sifu",
        "size_gb": 15.8,
        "streamable": True,
        "status": "ready",
        "startup_gb": 5.3,
        "genre": "Martial Arts"
    },
    {
        "id": "control",
        "title": "Control Ultimate Edition",
        "size_gb": 42.1,
        "streamable": True,
        "status": "ready",
        "startup_gb": 4.1,
        "genre": "Action Adventure"
    }
]

class DeckStreamHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = {
                "device": "Steam Deck OLED (1TB)",
                "daemon": "Active (deckstream.service)",
                "cache_used": "12.88 GB / 60 GB",
                "free_space": "277 GB (MicroSD EXT4)",
                "active_stream": "Cyberpunk 2077 (24.5 MB/s)",
                "warmup": True
            }
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return
        elif self.path == "/api/library":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(GAMES).encode("utf-8"))
            return
        return super().do_GET()

def run(port=PORT):
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", port), DeckStreamHandler) as httpd:
        print(f"🎮 DeckStream companion web app running at http://localhost:{port}")
        print("Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")

if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    run(p)
