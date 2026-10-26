#!/usr/bin/env python3
"""Write daemon/manifest.json: the list of files the installer ships, with SHA-256 and file mode.

  python3 scripts/make_manifest.py            regenerate daemon/manifest.json (and sign it when you have a key)
  python3 scripts/make_manifest.py --check    exit 1 if the committed manifest is stale, or if self-update is set up
                                              (daemon/update_signers exists) and the signature is missing or wrong
  python3 scripts/make_manifest.py --dir DIR  use another folder (tests)

The version is derived from the file contents, so it changes exactly when something shipped changes.

Self-update is OFF until you set it up, because it needs a signing key that only you hold:

  ssh-keygen -t ed25519 -N '' -C deckstream -f ~/.config/deckstream-signing/update_key
  echo "deckstream namespaces=\"deckstream-manifest\" $(cat ~/.config/deckstream-signing/update_key.pub)" > daemon/update_signers
  python3 scripts/make_manifest.py            # now also writes daemon/manifest.json.sig

The Deck pins daemon/update_signers at its first install and from then on installs only manifests signed by that key
(DECKSTREAM_SIGNING_KEY overrides the key path). Without update_signers the installer still works; Decks just do not
update themselves, and you re-run the install command to update.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SHIPPED = [
    "deckstream-daemon.py", "parallel_download.py", "stream-runner.sh", "cloud-install.sh", "game-launcher.sh",
    "umu-test.sh", "saves.py", "parallel_unzip.py", "doctor.py", "rollback.sh", "warmup.py", "startup_profile.py",
    "umu_gameid.py", "focus_game.sh", "launch_report.py", "language_fix.py", "suspend-resume-hook.sh",
    "proton_matcher.py", "demand_gate.py", "hedge_proxy.py", "deck_preset.py",
    "install.sh",       # listed so a self-update can check the installer it runs against the signed manifest
]
NAMESPACE = "deckstream-manifest"
SIGNER_ID = "deckstream"
MODULES = {"parallel_download.py"}      # imported, not run: not executable


def build(folder):
    files = {}
    for name in SHIPPED:
        data = (folder / name).read_bytes()
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "mode": "644" if name in MODULES else "755", "bytes": len(data)}
    combined = hashlib.sha256("".join(f"{n}:{files[n]['sha256']}" for n in sorted(files)).encode()).hexdigest()
    version = "v" + combined[:10]
    # 'released' orders versions (the version is a content hash): the Deck refuses a signed manifest that is not newer
    # than the installed one, so an old signed manifest cannot be replayed to downgrade it. Kept while nothing changes,
    # so regenerating (and --check) is stable.
    try:
        prev = json.loads((folder / "manifest.json").read_text())
    except (OSError, ValueError):
        prev = {}
    released = prev.get("released") if prev.get("version") == version else None
    if not isinstance(released, int):
        released = max(int(time.time()), int(prev.get("released") or 0) + 1)
    return {"version": version, "released": released, "files": dict(sorted(files.items()))}


def signing_key():
    return Path(os.environ.get("DECKSTREAM_SIGNING_KEY") or Path.home() / ".config" / "deckstream-signing" / "update_key")


def sign(target):
    """Write target.sig with the update key; returns False when the key is not on this machine."""
    key = signing_key()
    if not key.exists():
        return False
    sig = Path(str(target) + ".sig")
    sig.unlink(missing_ok=True)       # ssh-keygen will not overwrite
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(target)],
                   check=True, stdin=subprocess.DEVNULL, capture_output=True)
    return True


def verify(target, signers):
    """True when target.sig is a valid signature of target by a key in the signers file."""
    sig = Path(str(target) + ".sig")
    if not sig.exists() or not Path(signers).exists():
        return False
    with open(target, "rb") as f:
        r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", SIGNER_ID, "-n", NAMESPACE,
                            "-s", str(sig)], stdin=f, capture_output=True)
    return r.returncode == 0


def main(argv):
    folder = Path(argv[argv.index("--dir") + 1]) if "--dir" in argv else Path(__file__).resolve().parent.parent / "daemon"
    text = json.dumps(build(folder), indent=2) + "\n"
    target = folder / "manifest.json"
    signers = folder / "update_signers"
    if "--check" in argv:
        if not target.exists() or target.read_text() != text:
            print("daemon/manifest.json is stale: run  python3 scripts/make_manifest.py")
            return 1
        if not signers.exists():
            print("manifest is current (self-update is off: no daemon/update_signers)")
            return 0
        if not verify(target, signers):
            print("daemon/manifest.json.sig is missing or does not match daemon/update_signers: run  "
                  "python3 scripts/make_manifest.py  on the machine that holds the update key")
            return 1
        print("manifest is current and signed")
        return 0
    if not target.exists() or target.read_text() != text:
        target.write_text(text)
    signed = False
    if signers.exists():
        signed = verify(target, signers) or sign(target)
    print(f"wrote {target} ({len(SHIPPED)} files)" + (", signed" if signed else
          ", unsigned (self-update off)" if not signers.exists() else ", NOT signed (no update key on this machine)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
