# Deploy Prism on the Jarvis VM

Two scripts do the work. Both are safe to re-run and never delete anything.

- `deploy/setup_vm.sh`: venv, packages, tests, `.env`, database, the four portcos, systemd
  services. Runs Prism privately on localhost (reach it with an SSH tunnel). Finds the Key Vault
  Orbit uses; if the VM's identity can't reach it, keys go in `data/secrets.json` (chmod 600).
- `deploy/go_live.sh`: Microsoft sign-in with the "Farview Apps" registration (details read from
  Orbit's config), nginx include next to `/orbit` (backed up, auto-restored if the test fails),
  public at https://jarvis.farviewequity.com/prism.

Database: SQLite in `data/prism.db` to start. To move to the shared Azure SQL later, set
`PRISM_DATABASE_URL` in `.env` to Orbit's server/database, then `venv/bin/python -m prism.cli init`.

Updates later: `deploy/update.sh` (pull, install, test, restart).

MCP for Claude Code: create a read token in Command Centre > API tokens, then
`claude mcp add --transport http prism https://jarvis.farviewequity.com/prism/mcp/ --header "Authorization: Bearer <token>"`
