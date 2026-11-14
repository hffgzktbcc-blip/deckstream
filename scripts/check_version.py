#!/usr/bin/env python3
"""Keep the release version in one agreed place.

The site (package.json) and the Decky panel (decky/package.json) share one semantic version, and CHANGELOG.md has an
"Unreleased" section plus a section for every released version. With --tag vX.Y.Z (the release workflow) it also
checks the tag matches that version and that CHANGELOG.md has a "## X.Y.Z" section to use as release notes.

  python3 scripts/check_version.py                 # consistency check (part of run_all_tests.sh)
  python3 scripts/check_version.py --tag v1.1.0    # before a release build
  python3 scripts/check_version.py --notes 1.1.0   # print that version's changelog section
"""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def versions():
    return {p: json.loads((ROOT / p).read_text())["version"] for p in ("package.json", "decky/package.json")}


def changelog_section(version):
    text = (ROOT / "CHANGELOG.md").read_text()
    m = re.search(rf"^## \[?{re.escape(version)}\]?[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else None


def check(tag=None):
    errors = []
    vs = versions()
    if len(set(vs.values())) != 1:
        errors.append(f"versions differ: {vs}")
    version = vs["package.json"]
    if not SEMVER.match(version):
        errors.append(f"package.json version {version!r} is not X.Y.Z")
    text = (ROOT / "CHANGELOG.md").read_text()
    if not re.search(r"^## Unreleased\s*$", text, re.M):
        errors.append("CHANGELOG.md has no '## Unreleased' section")
    if changelog_section(version) is None:
        errors.append(f"CHANGELOG.md has no '## {version}' section for the current version")
    if tag is not None:
        if tag != f"v{version}":
            errors.append(f"tag {tag!r} does not match version v{version}; bump package.json and decky/package.json")
        elif not changelog_section(version):
            errors.append(f"CHANGELOG.md section '## {version}' is empty")
    return errors


def main(argv):
    if len(argv) == 2 and argv[0] == "--notes":
        notes = changelog_section(argv[1])
        if notes is None:
            sys.exit(f"no changelog section for {argv[1]}")
        print(notes)
        return
    tag = argv[1] if len(argv) == 2 and argv[0] == "--tag" else None
    if argv and tag is None:
        sys.exit(__doc__)
    errors = check(tag)
    for e in errors:
        print("version:", e, file=sys.stderr)
    if errors:
        sys.exit(1)
    print(f"version {versions()['package.json']} consistent")


if __name__ == "__main__":
    main(sys.argv[1:])
