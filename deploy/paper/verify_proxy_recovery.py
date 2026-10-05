"""Exercise the actual web image against disposable changing backend addresses."""
import json
import subprocess
import time
import uuid

from verify_restore import DOCKER, run

SERVER = '''
import http.server, json, sys
generation = sys.argv[1]
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"generation": generation}).encode())
    def log_message(self, *args):
        pass
http.server.HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
'''


def main():
    prefix = "paper-proxy-drill-" + uuid.uuid4().hex[:10]
    network, backend, web, holder = [prefix + suffix for suffix in ("-net", "-backend", "-web", "-holder")]
    owned = []
    run(DOCKER + ["network", "create", "--internal", network])
    try:
        def start_backend(generation):
            run(DOCKER + ["run", "-d", "--name", backend, "--network", network,
                          "--network-alias", "backend", "--entrypoint", "python",
                          "frozen-stock-paper:local", "-c", SERVER, generation])
            owned.append(backend)

        def address():
            return run(DOCKER + ["inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", backend]).decode().strip()

        def wait_for(generation):
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                result = subprocess.run(DOCKER + ["exec", web, "wget", "-qO-", "-T", "2", "http://127.0.0.1:8080/api/health"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
                if result.returncode == 0 and json.loads(result.stdout).get("generation") == generation:
                    return
                time.sleep(1)
            raise RuntimeError("Proxy did not recover to the expected backend")

        def wait_health(status):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = run(DOCKER + ["inspect", "--format", "{{.State.Health.Status}}", web]).decode().strip()
                if state == status:
                    return
                time.sleep(1)
            raise RuntimeError("Proxy health check did not reflect backend availability")

        start_backend("first")
        first_ip = address()
        web_id = run(DOCKER + ["run", "-d", "--name", web, "--network", network,
                              "-e", "LOCAL_VIEWER_KEY=isolated-proxy-fixture",
                              "--health-cmd", "wget -q -O /dev/null http://127.0.0.1:8080/api/health",
                              "--health-interval", "1s", "--health-timeout", "3s", "--health-retries", "1",
                              "frozen-stock-web:local"]).decode().strip()
        owned.append(web)
        wait_for("first")
        wait_health("healthy")
        run(DOCKER + ["rm", "-f", backend])
        owned.remove(backend)
        wait_health("unhealthy")
        # Occupy the old address so a restart cannot accidentally reuse it.
        run(DOCKER + ["run", "-d", "--name", holder, "--network", network, "--ip", first_ip,
                      "--entrypoint", "python", "frozen-stock-paper:local", "-c", "import time; time.sleep(120)"])
        owned.append(holder)
        start_backend("second")
        if address() == first_ip:
            raise RuntimeError("Backend address did not change")
        wait_for("second")
        wait_health("healthy")
        actual_id = run(DOCKER + ["inspect", "--format", "{{.Id}}", web]).decode().strip()
        if actual_id != web_id:
            raise RuntimeError("Web container unexpectedly changed")
        print(json.dumps({"status": "passed", "backend_ip_changed": True, "web_restarted": False,
                          "health_detected_outage_and_recovery": True,
                          "production_containers_touched": False}))
    finally:
        for name in reversed(owned):
            run(DOCKER + ["rm", "-f", name])
        run(DOCKER + ["network", "rm", network])


if __name__ == "__main__":
    main()
