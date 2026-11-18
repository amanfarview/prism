"""Read-only: find the Entra tenant/client id (and secret, if stored there) that Orbit
already uses, so Prism can sign in with the same "Farview Apps" registration.
Prints shell assignments; prints nothing for values it can't find.
"""
from __future__ import annotations

import glob
import os
import re
import shlex
import subprocess

GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def candidate_files() -> list[str]:
    files: list[str] = []
    try:
        units = subprocess.run(["systemctl", "list-unit-files", "--type=service", "--no-legend"],
                               capture_output=True, text=True, timeout=20).stdout.split()
    except Exception:
        units = []
    for u in [u for u in units if "orbit" in u.lower() and u.endswith(".service") and "staging" not in u.lower()]:
        try:
            body = subprocess.run(["systemctl", "cat", u], capture_output=True, text=True, timeout=20).stdout
        except Exception:
            continue
        for m in re.finditer(r"^EnvironmentFile=-?(.+)$", body, re.M):
            files.append(m.group(1).strip())
        for m in re.finditer(r"^WorkingDirectory=(.+)$", body, re.M):
            files.append(os.path.join(m.group(1).strip(), ".env"))
    home = os.path.expanduser("~")
    files += glob.glob(f"{home}/*orbit*/.env") + glob.glob(f"{home}/*/*orbit*/.env") + glob.glob(f"{home}/*orbit*/*/.env")
    seen, out = set(), []
    for f in files:
        f = os.path.realpath(f)
        if f not in seen and os.path.isfile(f) and os.access(f, os.R_OK) and "staging" not in f.lower():
            seen.add(f)
            out.append(f)
    return out


def parse(path: str) -> dict[str, str]:
    vals = {}
    for line in open(path, errors="ignore"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip().removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
    return vals


def main() -> None:
    tenant = client = secret = ""
    source = ""
    for f in candidate_files():
        vals = parse(f)
        for k, v in vals.items():
            ku = k.upper()
            if not tenant and "TENANT" in ku and GUID.match(v):
                tenant, source = v, f
            elif not client and ("CLIENT_ID" in ku or "APP_ID" in ku or "CLIENTID" in ku) and GUID.match(v):
                client, source = v, f
            elif not secret and ("CLIENT_SECRET" in ku or "CLIENTSECRET" in ku) and v and not GUID.match(v) \
                    and "vault" not in v.lower():
                secret = v
    if tenant:
        print(f"ORBIT_TENANT={shlex.quote(tenant)}")
    if client:
        print(f"ORBIT_CLIENT={shlex.quote(client)}")
    if secret:
        print(f"ORBIT_SECRET={shlex.quote(secret)}")
    if source:
        print(f"ORBIT_ENV_FILE={shlex.quote(source)}")


if __name__ == "__main__":
    main()
