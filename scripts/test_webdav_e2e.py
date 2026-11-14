#!/usr/bin/env python3
"""Local storage contract -> signed ranges and real WebDAV -> VFS/cache integration.

Generated fixture bytes only; this does not launch a game or measure a Deck.
"""
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

import replay_rig as rig

ROOT = Path(__file__).resolve().parent.parent


def main():
    rclone = shutil.which("rclone")
    if not rclone:
        print("[skip] WebDAV integration requires rclone on PATH")
        return
    with tempfile.TemporaryDirectory() as work:
        tmp = Path(work)
        data = tmp / "library"
        game = data / "Example Game"
        game.mkdir(parents=True)
        payload = bytes(range(256)) * (16 * 1024)
        (game / "Game.exe").write_bytes(payload)
        env = {**os.environ, "STORAGE_KEY": "local-fixture-only", "DECKSTREAM_FILL_PAD_KB": "0"}
        node = subprocess.Popen([
            "node", "-e",
            "const s=require(process.argv[1]).create({dir:process.argv[2],key:process.env.STORAGE_KEY});"
            "s.listen(0,'127.0.0.1',()=>console.log(s.address().port));",
            str(ROOT / "scripts/storage_server.js"), str(data)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        upstream = vfs = proxy = None
        try:
            import select
            assert select.select([node.stdout], [], [], 10)[0], "storage server startup timed out"
            port = int(node.stdout.readline().strip())
            base = f"http://127.0.0.1:{port}"
            def api(path, body=None):
                req = urllib.request.Request(base + "/v1/api/" + path,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={"Authorization": "Bearer " + env["STORAGE_KEY"], "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as response:
                    result = json.load(response)
                    assert result["success"], result
                    return result["data"]
            try:
                urllib.request.urlopen(base + "/v1/api/games/mylist", timeout=10)
                raise AssertionError("unauthenticated library access accepted")
            except urllib.error.HTTPError as error:
                assert error.code == 401
            library = api("games/mylist")
            assert len(library) == 1 and library[0]["name"] == "Example Game", library
            item = library[0]
            mounted = api("games/mount", {"path": item["name"]})
            assert mounted["id"] == item["id"]
            link = api(f"games/requestdl?id={item['id']}&file_id={item['files'][0]['id']}")
            with urllib.request.urlopen(urllib.request.Request(link, headers={"Range": "bytes=4096-8191"}), timeout=10) as response:
                assert response.status == 206
                assert response.headers["Content-Range"] == f"bytes 4096-8191/{len(payload)}"
                assert response.read() == payload[4096:8192]
            print("[ok] authenticated real-folder discovery, mount selection and signed HTTP range bytes")

            upstream, up_port = rig.start_upstream(rclone, str(data))
            counters = rig.Counters()
            proxy = rig.make_proxy(up_port, 0, 0, counters, rig.proxy_paths(["Example Game/Game.exe"]))
            vfs, vfs_port, _ = rig.start_vfs(rclone, proxy.server_address[1], {}, str(tmp / "vfs-cache"))
            conn = http.client.HTTPConnection("127.0.0.1", vfs_port, timeout=20)
            try:
                conn.request("GET", "/Example%20Game/Game.exe", headers={"Range": "bytes=12345-77880"})
                response = conn.getresponse()
                assert response.status == 206
                assert response.read() == payload[12345:77881], "VFS returned different fixture bytes"
            finally:
                conn.close()
            rig.stop(vfs)
            vfs = None
            print("[ok] real WebDAV and rclone HTTP VFS return identical selected bytes")

            # Exercise the shipped fill-cache CLI, separately from any running VFS cache.
            cache = tmp / "prefill"
            profile = tmp / "profile.json"
            profile.write_text(json.dumps({"ranges": [{"f": "Game.exe", "o": 4096, "n": 65536, "t": 0}]}))
            args = [sys.executable, str(ROOT / "daemon/startup_profile.py"), "fill-cache",
                    "--cache-dir", str(cache), "--ns", "fixture", "--folder", item["name"],
                    "--profile", str(profile), "--webdav-url", f"http://127.0.0.1:{up_port}",
                    "--workers", "1", "--piece-mb", "1"]
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stdout + result.stderr
            cached = cache / "vfs/fixture" / item["name"] / "Game.exe"
            with cached.open("rb") as f:
                f.seek(4096)
                assert f.read(65536) == payload[4096:69632]
            meta = json.loads((cache / "vfsMeta/fixture" / item["name"] / "Game.exe").read_text())
            assert meta["Size"] == len(payload) and meta["Rs"], meta
            repeated = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
            assert repeated.returncode == 0 and "0 pieces" in repeated.stdout, repeated.stdout + repeated.stderr
            print("[ok] shipped fill-cache CLI writes matching sparse bytes and metadata; repeated fill has 0 pieces")
        finally:
            rig.stop(vfs)
            rig.stop(upstream)
            if proxy:
                proxy.shutdown()
                proxy.server_close()
            rig.stop(node)
    print("WEBDAV E2E TESTS PASSED (localhost fixture; no hardware launch)")


if __name__ == "__main__":
    main()
