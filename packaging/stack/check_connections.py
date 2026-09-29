"""Check a running package without printing credentials or submitting translation."""

import argparse
from pathlib import Path
import subprocess
import sys


PROBE = r'''
import importlib.util
import json
import os
from pathlib import Path
import sys
sys.path[:0] = ["/app/bazarr/libs", "/app/bazarr/custom_libs"]
import requests
import yaml

if os.geteuid() == 0:
    os.setgid(int(os.environ.get("PGID", "1000")))
    os.setuid(int(os.environ.get("PUID", "1000")))
config = yaml.safe_load(Path("/config/config/config.yaml").read_text())
spec = importlib.util.spec_from_file_location("stack_crypto", "/app/bazarr/bazarr/secret_store/crypto.py")
crypto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crypto)
api_key = crypto.decrypt_secret(config["auth"]["apikey"], config["general"]["secrets_encryption_key"])
session = requests.Session()
session.trust_env = False
response = session.get("http://127.0.0.1:6767/api/translator/status",
                       headers={"X-API-KEY": api_key}, timeout=30, allow_redirects=False)
assert response.status_code == 200, "Bazarr translator connection failed: HTTP " + str(response.status_code)
assert isinstance(response.json(), dict), "Translator status was not JSON"
unauthenticated = session.get("http://subtitle-translator:8765/api/v1/status", timeout=15, allow_redirects=False)
assert unauthenticated.status_code in (401, 403), "Translator accepted a request without authentication"
helper = session.get("http://flaresolverr:8191/health", timeout=15, allow_redirects=False)
assert helper.status_code == 200 and helper.json()["status"] == "ok", "FlareSolverr is not ready"
rendered = session.post("http://flaresolverr:8191/v1", timeout=90,
    json={"cmd": "request.get", "url": "http://bazarr:6767/_supervisor/status", "maxTimeout": 60000},
    allow_redirects=False)
assert rendered.status_code == 200, "FlareSolverr browser request failed"
result = rendered.json()
assert result["status"] == "ok" and result["solution"]["status"] == 200, "FlareSolverr did not load Bazarr"
assert "running" in result["solution"]["response"], "Unexpected rendered response"
expected = ("legendasdivx", "napiprojekt", "opensubtitles", "prijevodionline", "sub_scene",
            "subs4series", "turkcealtyaziorg", "wizdom", "yavkanet")
assert all(config[p]["flaresolverr_url"] == "http://flaresolverr:8191/v1" for p in expected), "Provider helper URL missing"
print(json.dumps({"bazarr_translator_proxy": response.status_code,
                  "unauthenticated_translator": unauthenticated.status_code,
                  "flaresolverr_health": helper.status_code,
                  "flaresolverr_browser_request": result["solution"]["status"],
                  "preconfigured_provider_count": len(expected),
                  "onboarding_complete": config["general"]["setup_complete"],
                  "paid_requests": 0}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="bazarr-plus-stack")
    args = parser.parse_args()
    command = ["docker", "compose", "-p", args.project, "-f",
               str(Path(__file__).with_name("compose.yaml")), "exec", "-T", "bazarr", "python", "-c", PROBE]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        # A dependency exception could include headers or configuration. Keep it private.
        print("Stack connection check failed. Inspect service health and settings locally.", file=sys.stderr)
        return 1
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
