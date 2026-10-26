#!/usr/bin/env python3
"""Decky plugin backend (decky/main.py) against the real site code: boots scripts/dev_server.js (api/*.js + the mock
storage library) and calls the panel's functions with a fake Deck home, over real HTTP. Also checks that the committed
decky/DeckStream.zip carries the current main.py, plugin.json and package.json."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("decky_main", ROOT / "decky" / "main.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# The zip Decky installs from the site must be the current source (scripts/build_decky.sh rebuilds it).
with zipfile.ZipFile(ROOT / "decky" / "DeckStream.zip") as z:
    names = set(z.namelist())
    assert "DeckStream/dist/index.js" in names, names
    for f in ("main.py", "plugin.json", "package.json"):
        assert z.read(f"DeckStream/{f}") == (ROOT / "decky" / f).read_bytes(), f"decky/DeckStream.zip is stale ({f}): run scripts/build_decky.sh"
    assert 'callable("send")' not in z.read("DeckStream/dist/index.js").decode(), "old send API"
assert json.loads((ROOT / "decky" / "plugin.json").read_text())["name"] == "DeckStream"

# Boot the dev server on a free port.
srv = subprocess.Popen(["node", "-e", "require('./scripts/dev_server').start({port:0}).then(s=>console.log(s.port))"],
                       cwd=ROOT, stdout=subprocess.PIPE, text=True, env={**os.environ, "DECKSTREAM_SECRET": "test-secret-123"})
try:
    port = int(srv.stdout.readline().strip())
    home = Path(tempfile.mkdtemp())
    cfg = home / ".config" / "deckstream"
    cfg.mkdir(parents=True)

    def set_config(secret="test-secret-123", key="mock-storage-key"):
        (cfg / "daemon_config.json").write_text(json.dumps({"server_url": f"http://127.0.0.1:{port}", "device_id": "deck-test",
                                                            "queue_secret": secret}))
        (cfg / "storage.conf").write_text(f"STORAGE_KEY='{key}'\nSTORAGE_WEBDAV_URL=http://nas.lan:8781\n" if key else "")
        m._QUEUE_CACHE["val"] = None

    set_config()
    no_wait = lambda s: None

    lib = m.library(home)
    assert lib["ok"], lib
    group = {it["name"]: it["group"] for it in lib["items"]}
    assert group["Cyberpunk 2077"] == "Ready to play", group
    assert group["Hollow Knight"] == "Still caching", group
    assert group["The Witcher 3 (GOG offline installer)"] == "Needs installing", group
    assert lib["items"][0]["name"] == "Hollow Knight", "newest first"

    res = m.search("hades", home)
    assert res["ok"], res
    first = res["results"][0]
    assert first["id"] == "102" and first["group"] == "Streams from your storage" and first["sendable"], first
    assert all(not r["sendable"] for r in res["results"][1:] if r["group"] == "On the Steam store")
    assert m.search("", home) == {"ok": False, "why": "Type a game to search for."}

    r = m.send_library("101", "Cyberpunk 2077", home, wait=no_wait)
    assert r["ok"] and "it streams" in r["text"], r
    r = m.send_library("105", "The Witcher 3", home, wait=no_wait)
    assert r["ok"] and "installs first" in r["text"], r
    r = m.send_library("107", "Hollow Knight", home, wait=no_wait)
    assert r["ok"] and "still preparing" in r["text"], r
    assert m.send_library("999", "Gone", home, wait=no_wait) == {"ok": False, "text": "Gone is no longer in your library."}
    assert not m.send_library("abc", "Bad", home)["ok"]

    q = m.queue(home, now=1e12)
    assert q["ok"], q
    sent = [j["name"] for j in q["jobs"]] + ([q["current"]["name"]] if q["current"] else [])
    assert "Cyberpunk 2077" in sent and "The Witcher 3" in sent, q
    jobs = {j["name"]: j for j in q["jobs"]}
    assert jobs["Cyberpunk 2077"]["mode"] == "Stream", jobs

    cancel = m.queue_action("cancel", next(j["id"] for j in q["jobs"] if j["name"] == "The Witcher 3"), home)
    assert cancel["ok"], cancel
    m._QUEUE_CACHE["val"] = None
    assert "The Witcher 3" not in [j["name"] for j in m.queue(home, now=2e12)["jobs"]]

    set_config(secret="wrong")
    assert "refused the queue password" in m.queue(home, now=3e12)["why"]
    assert "refused the queue password" in m.library(home)["why"]
    set_config(key="")
    assert m.library(home) == {"ok": False, "why": m.NO_KEY}

    assert m.status(home) == {"installed": False}   # no daemon installed in this home: said, not crashed on
finally:
    srv.terminate()
    srv.wait()

print("[ok] Decky panel: library groups, search, Stream/Install to Deck into the queue, Cancel, bad password, no key, zip current")
