"""Run network faults against disposable services, never the paper deployment."""
import subprocess
import uuid

from verify_restore import DOCKER, ROOT


def main():
    compose = DOCKER + ["compose", "-p", "paper-fault-" + uuid.uuid4().hex[:12],
                        "-f", str(ROOT / "deploy/paper/compose.fault.yaml")]
    try:
        subprocess.run(compose + ["run", "--rm", "tests"], check=True, timeout=180)
    finally:
        subprocess.run(compose + ["down", "-v", "--remove-orphans"], check=True, timeout=60)


if __name__ == "__main__":
    main()
