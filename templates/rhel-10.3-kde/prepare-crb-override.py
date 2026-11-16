#!/usr/bin/env python3
"""Create an image-builder repo override with RHEL 10.3 CRB.

image-builder 79 embeds BaseOS/AppStream but not CodeReady Builder. KDE needs
CRB for appstream-qt, libdbusmenu and other dependencies. The generated file
uses the host subscription via ``rhsm: true`` and is picked up by vEMU from
``~/.config/vemu/image-builder/repositories``.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

URL = (
    "https://raw.githubusercontent.com/osbuild/image-builder/main/"
    "data/repositories/rhel-10.3.json"
)
DEST = (
    Path.home()
    / ".config/vemu/image-builder/repositories/rhel-10.3.json"
)


def main() -> None:
    with urllib.request.urlopen(URL, timeout=30) as response:
        data = json.load(response)
    changed = 0
    for arch, repos in data.items():
        if not isinstance(repos, list):
            continue
        if any(repo.get("name") == "crb" for repo in repos):
            continue
        base = next(
            (repo for repo in repos if repo.get("name") == "baseos"), None
        )
        if base is None:
            continue
        repos.append(
            {
                "name": "crb",
                "baseurl": (
                    "https://cdn.redhat.com/content/dist/rhel10/10.3/"
                    f"{arch}/codeready-builder/os"
                ),
                "gpgkey": base["gpgkey"],
                "rhsm": True,
                "check_gpg": True,
            }
        )
        changed += 1
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {DEST} ({changed} arch override(s))")


if __name__ == "__main__":
    main()
