#!/usr/bin/env bash
# Retired 19 Nov 2026. This script switched on sign-in by reading Orbit's Entra settings (find_orbit_config.py),
# wrote them into /home/azureuser/prism/.env and added an nginx include (nginx isn't on this VM; Caddy is).
# Prism now keeps its own settings in /etc/prism/prism.env and never reads another app's config.
# Use instead:
#   sudo bash /var/www/prism/deploy/install.sh        install, or move an old install to the new layout
#   sudo bash /var/www/prism/deploy/caddy_go_live.sh  publish through Caddy (sign-in required)
#   sudo bash /var/www/prism/deploy/update.sh         every deploy after that
echo "go_live.sh is retired: see the comment at the top of deploy/go_live.sh (use deploy/install.sh)." >&2
exit 1
