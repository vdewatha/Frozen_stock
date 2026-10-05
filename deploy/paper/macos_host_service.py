"""Manage login startup for the existing paper VM; no broker or Docker writes."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

PREFIX = "com.frozenstock.paper"
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def definitions(home, colima):
    logs = home / "Library/Logs/FrozenStock"
    base = {"RunAtLoad": True, "ThrottleInterval": 60,
            "EnvironmentVariables": {"PATH": PATH, "HOME": str(home)}}
    return {
        PREFIX + ".startup": {**base, "Label": PREFIX + ".startup",
            "ProgramArguments": [str(colima), "start", "--profile", "frozen-stock",
                                 "--activate=false", "--save-config=false"],
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": str(logs / "startup.log"),
            "StandardErrorPath": str(logs / "startup.log")},
        PREFIX + ".ac-awake": {**base, "Label": PREFIX + ".ac-awake",
            "ProgramArguments": ["/usr/bin/caffeinate", "-s"],
            "KeepAlive": True},
    }


def run(args, *, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def install(home, colima):
    if not (home / ".colima/frozen-stock/colima.yaml").is_file():
        raise RuntimeError("Existing frozen-stock profile required; no new VM will be created")
    domain = f"gui/{os.getuid()}"
    agents = home / "Library/LaunchAgents"
    logs = home / "Library/Logs/FrozenStock"
    agents.mkdir(parents=True, exist_ok=True)
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    configs = definitions(home, colima)
    # Refuse conflicting definitions before changing any service.
    for label, config in configs.items():
        path = agents / f"{label}.plist"
        if path.is_symlink() or (path.exists() and plistlib.loads(path.read_bytes()) != config):
            raise RuntimeError(f"Existing service differs: {label}")
    for label, config in configs.items():
        path = agents / f"{label}.plist"
        if not path.exists():
            with path.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                plistlib.dump(config, stream)
        run(["/usr/bin/plutil", "-lint", str(path)])
        if run(["/bin/launchctl", "print", f"{domain}/{label}"], check=False).returncode:
            run(["/bin/launchctl", "bootstrap", domain, str(path)])
        print(f"Installed {label}")


def uninstall(home):
    domain = f"gui/{os.getuid()}"
    for suffix in ("startup", "ac-awake"):
        label = f"{PREFIX}.{suffix}"
        path = home / "Library/LaunchAgents" / f"{label}.plist"
        if path.is_symlink():
            raise RuntimeError("Refusing symlinked service definition")
        if path.exists() and plistlib.loads(path.read_bytes()).get("Label") != label:
            raise RuntimeError("Service identity mismatch")
        if run(["/bin/launchctl", "print", f"{domain}/{label}"], check=False).returncode == 0:
            run(["/bin/launchctl", "bootout", f"{domain}/{label}"])
        path.unlink(missing_ok=True)
        print(f"Removed {label}; VM and containers were not stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise RuntimeError("This helper requires macOS with a logged-in GUI user")
    if args.action == "uninstall":
        uninstall(Path.home())
    else:
        colima = shutil.which("colima")
        if not colima:
            raise RuntimeError("Existing Colima installation required")
        install(Path.home(), Path(colima))


if __name__ == "__main__":
    main()
