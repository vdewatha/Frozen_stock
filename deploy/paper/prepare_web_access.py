"""Provision only the viewer credential to the loopback web proxy."""
import os
from pathlib import Path
import re

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def main():
    config = {**dotenv_values(ROOT / "deploy/paper/paper.env", interpolate=False),
              **dotenv_values(ROOT / ".env", interpolate=False)}
    key = config.get("AUTH_VIEWER_KEY", "") or ""
    if not re.fullmatch(r"[A-Za-z0-9_+/=-]{32,256}", key):
        raise ValueError("A valid local viewer key is required")
    os.umask(0o077)
    path = ROOT / "deploy/paper/web.env"
    with path.open("w") as output:
        os.chmod(path, 0o600)
        output.write("LOCAL_VIEWER_KEY=" + key + "\n")
    print("Private read-only web access configured; no broker credentials exported.")


if __name__ == "__main__":
    main()
