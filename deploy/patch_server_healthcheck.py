"""Add Prism to the server-wide health check (/usr/local/bin/farview-health-check.sh, 08/13/18 UTC to the Teams
"Apps & AI health" channel): service status, runs-as-prism check, /etc/prism/prism.env permissions, card line.

  sudo python3 /var/www/prism/deploy/patch_server_healthcheck.py           show what would change
  sudo python3 /var/www/prism/deploy/patch_server_healthcheck.py --apply   change it (backup first, bash -n checked)

Each edit is anchored on a line the script already has; a missing anchor skips that edit and says so. Safe to re-run.
"""
import shutil
import subprocess
import sys
import time

PATH = "/usr/local/bin/farview-health-check.sh"
EDITS = [
    ('PIQ=$(svc_status "portfolioiq.service")',
     'PIQ=$(svc_status "portfolioiq.service")\nPRISM=$(svc_status "prism-web.service")\nPRISMW=$(svc_status "prism-worker.service")',
     "PRISM=$(svc_status"),
    ('"portfolioiq.service:$PIQ"', '"portfolioiq.service:$PIQ" "prism-web.service:$PRISM" "prism-worker.service:$PRISMW"',
     '"prism-web.service:$PRISM"'),
    ('check_unit_user "portfolioiq.service"      "pfiq"',
     'check_unit_user "portfolioiq.service"      "pfiq"\ncheck_unit_user "prism-web.service"        "prism"\n'
     'check_unit_user "prism-worker.service"     "prism"\ncheck_unit_user "prism-mcp.service"        "prism"',
     'check_unit_user "prism-web.service"'),
    ('    "/etc/orbit/orbit.env"\n', '    "/etc/orbit/orbit.env"\n    "/etc/prism/prism.env"\n', '"/etc/prism/prism.env"'),
    ('team = """$TEAM"""; jarvis = """$JARVIS"""; piq = """$PIQ"""',
     'team = """$TEAM"""; jarvis = """$JARVIS"""; piq = """$PIQ"""; prism = """$PRISM"""', 'prism = """$PRISM"""'),
    ('{icon(piq)} PortfolioIQ"', '{icon(piq)} PortfolioIQ  |  {icon(prism)} Prism"', "{icon(prism)} Prism"),
]


def main() -> None:
    apply = "--apply" in sys.argv
    s = open(PATH).read()
    new = s
    for anchor, repl, marker in EDITS:
        if marker in new:
            print(f"  ok    already there: {marker}")
        elif anchor in new:
            new = new.replace(anchor, repl, 1)
            print(f"  add   {marker}")
        else:
            print(f"  skip  anchor not found: {anchor[:60]}")
    if new == s:
        print("Nothing to change.")
        return
    if not apply:
        print("Dry run. Re-run with --apply to write it.")
        return
    bak = f"{PATH}.bak-prism-{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(PATH, bak)
    open(PATH, "w").write(new)
    if subprocess.run(["bash", "-n", PATH]).returncode != 0:
        shutil.copy2(bak, PATH)
        sys.exit("bash -n failed: the original is back")
    print(f"Written (backup {bak}). Test it: sudo {PATH}")


if __name__ == "__main__":
    main()
