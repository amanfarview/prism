#!/usr/bin/env bash
# Retired 19 Nov 2026. This script built the first install: Prism as azureuser in /home/azureuser/prism, with its
# .env and database inside the app folder. That breaks the VM rules in SERVER_OPERATIONS.md (own service user,
# settings outside the code, nothing in /home/azureuser), and re-running it would put the old units back.
# Use instead:
#   sudo bash /var/www/prism/deploy/install.sh        install, or move an old install to the new layout
#   sudo bash /var/www/prism/deploy/caddy_go_live.sh  publish through Caddy (sign-in required)
#   sudo bash /var/www/prism/deploy/update.sh         every deploy after that
echo "setup_vm.sh is retired: see the comment at the top of deploy/setup_vm.sh (use deploy/install.sh)." >&2
exit 1
