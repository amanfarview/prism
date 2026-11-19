# Deploying Prism on the Jarvis VM

Follows `SERVER_OPERATIONS.md` (Farview app store workspace): own service user, 127.0.0.1 only, settings
outside the code, git-first deploys. Same shape as Orbit.

## Layout

| What | Where | Owner / mode |
|---|---|---|
| Code (main) | `/var/www/prism` | azureuser:prism 750. The app reads it and can never change it |
| Python | `/opt/prism-venv` | root, read-only to the app |
| Settings | `/etc/prism/prism.env` | root:root 600. systemd loads it; the app can't read the file |
| Secrets | `/var/lib/prism/secrets.json` | prism:prism 600 (provider keys, Entra secret, Teams webhook) |
| Database | Azure SQL `jarvis`, schema `prism`, login `prismapp` (SQLite at `/var/lib/prism/prism.db` until the move) | |
| Services | `prism-web` 127.0.0.1:8040, `prism-worker`, `prism-mcp` 127.0.0.1:8041, `prism-health.timer` | user prism, sandboxed |
| CLI | `sudo prism <command>` (`/usr/local/bin/prism`) | runs as prism in the same sandbox |
| Staging | `/var/www/prism-staging` (dev), `/opt/prism-staging-venv`, `/etc/prism/prism-staging.env`, `/var/lib/prism-staging`, 127.0.0.1:8042, schema `prism_staging` | can't see production's keys; never spends |
| Deploy log | `/var/log/prism-deploys.log` | |

Every Prism unit blocks Azure's metadata endpoint (`IPAddressDeny=169.254.169.254/32`), drops all capabilities,
filters system calls and has a memory ceiling. `tests/test_deploy.py` fails CI if a unit loses any of that.

### Why not Key Vault

Any process on the VM, under any user, can ask `169.254.169.254` for the VM identity's token, and with it read
every Key Vault secret that identity can read. On a box shared by six apps that isolates nothing, and Prism's
site audit fetches outside URLs, so an SSRF bug could do the same. Prism keeps its keys in its own 600 file and
is blocked from the metadata endpoint twice (systemd and `prism/netguard.py`).

## Commands

```bash
sudo bash /var/www/prism/deploy/update.sh               # deploy latest main (PR merged, CI green)
sudo bash /var/www/prism/deploy/update.sh --staging     # deploy latest dev to /prism-staging
sudo bash /var/www/prism/deploy/update.sh --rollback    # back to the version before the last deploy
sudo prism selfcheck                                    # sign-in, DB, secrets file, metadata blocked, isolation
sudo prism set-key openai                               # also: entra, teams, anthropic, gemini, ...
sudo prism estimate amplience                           # free
sudo prism token "Claude MCP"                           # read token, printed once
journalctl -u prism-web -n 100 --no-pager
```

## First install / move from the old layout

`deploy/install.sh` (safe to re-run; deletes nothing; restores the old services if the new ones fail).
Then `deploy/caddy_go_live.sh`. Azure SQL: `deploy/prism_schema.sql`, then `deploy/azure_sql.py login|test|move`.
Staging: `deploy/staging_setup.sh`. Server health card: `deploy/patch_server_healthcheck.py --apply`.

## Rules

1. Changes go laptop → `dev` → staging → PR to `main` (CI green) → `update.sh`. Never edit code on the server.
2. Never run Prism as azureuser, never put a `.env` in `/var/www/prism`, never print a secret (compare with `sha256sum`).
3. Prism only ever adds tables (`prism init`). A column change needs a written migration; ask before shipping one.
4. Dependabot PRs: merge when CI is green, deploy to staging first.
