"""The deploy files must keep to the VM rules (SERVER_OPERATIONS.md section 2) and to each other."""
import re
from pathlib import Path

D = Path(__file__).resolve().parents[1] / "deploy"
PROD = ["prism-web.service", "prism-worker.service", "prism-mcp.service", "prism-health.service"]
STAGING = ["prism-staging-web.service", "prism-staging-worker.service"]
MUST = ["User=prism", "NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true", "PrivateTmp=true",
        "IPAddressDeny=169.254.169.254/32", "CapabilityBoundingSet=", "UMask=0077", "SystemCallFilter=@system-service"]


def test_units_are_isolated():
    for name in PROD + STAGING:
        t = (D / name).read_text()
        for m in MUST:
            assert m in t, f"{name}: missing {m}"
        assert "azureuser" not in t and "/home/" not in t, f"{name}: still points at azureuser/home"
        assert re.search(r"^MemoryMax=\d+M$", t, re.M), f"{name}: MemoryMax needs a unit"
        for port in re.findall(r"--port (\d+)", t):
            assert "--host 127.0.0.1" in t, f"{name}: binds beyond localhost"


def test_prod_units_use_prod_settings_and_staging_cant_see_prod():
    for name in PROD:
        t = (D / name).read_text()
        assert "EnvironmentFile=/etc/prism/prism.env" in t and "WorkingDirectory=/var/www/prism\n" in t
        assert "StateDirectory=prism\n" in t and "prism-staging" not in t
    for name in STAGING:
        t = (D / name).read_text()
        assert "InaccessiblePaths=/var/lib/prism" in t and "prism-staging.env" in t and "/opt/prism-staging-venv" in t


def test_cli_wrapper_matches_unit_sandbox():
    wrapper = (D / "prism").read_text()
    unit = (D / "prism-web.service").read_text()
    sandbox = unit.split("# ---- isolation")[1].split("[Install]")[0]
    for line in sandbox.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            assert f"-p {line} " in wrapper or f'-p "{line}" ' in wrapper, f"wrapper is missing {line}"


def test_ports_match_caddy_block():
    caddy = (D / "caddy_go_live.sh").read_text()
    assert "reverse_proxy 127.0.0.1:8040" in caddy and "reverse_proxy 127.0.0.1:8041" in caddy
    assert "--port 8040" in (D / "prism-web.service").read_text()
    assert "unsafe-inline'; style-src" not in caddy  # scripts: 'self' only


def test_retired_scripts_refuse_to_run():
    for name in ["setup_vm.sh", "go_live.sh"]:
        assert "exit 1" in (D / name).read_text()
