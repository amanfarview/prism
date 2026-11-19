#!/usr/bin/env bash
# Prism's routes in Caddy (jarvis.farviewequity.com site block). Safe to re-run: it replaces whatever sits between
# "# BEGIN prism" and "# END prism" (or adds the block), validates, reloads, then checks that Prism, Orbit and Flo
# all still answer. Any failure puts the previous Caddyfile back.
#   sudo bash /var/www/prism/deploy/caddy_go_live.sh            production block (/prism, /prism/mcp)
#   sudo bash /var/www/prism/deploy/caddy_go_live.sh --staging  adds the /prism-staging block too
# Refuses to publish anything that answers without sign-in.
set -euo pipefail
CF=/etc/caddy/Caddyfile; HOST=jarvis.farviewequity.com
STAGING=0; [ "${1:-}" = "--staging" ] && STAGING=1
ok() { echo "  OK  $*"; }
die() { echo "  STOPPED: $*"; exit 1; }
[ "$(id -u)" = 0 ] || die "run with sudo"
[ -f "$CF" ] || die "$CF not found"

echo "1/4 Sign-in required"
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8040/prism/ || true)
[ "$code" = 303 ] || die "Prism answered $code without asking for sign-in: not publishing it"
ok "production asks for sign-in"
if [ "$STAGING" = 1 ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8042/prism-staging/ || true)
  [ "$code" = 303 ] || die "staging answered $code without sign-in (is it running? sudo bash deploy/update.sh --staging)"
  ok "staging asks for sign-in"
fi

echo "2/4 Caddyfile"
BAK="$CF.bak-prism-$(date +%Y%m%d%H%M%S)"; cp -p "$CF" "$BAK"; ok "backup $BAK"
python3 - "$CF" "$HOST" "$STAGING" <<'PY'
import re, sys
path, host, staging = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
HEAD = """        header {
            X-Content-Type-Options nosniff
            X-Frame-Options DENY
            Referrer-Policy strict-origin-when-cross-origin
            Strict-Transport-Security "max-age=31536000"
            Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
            -Server
        }"""
block = f"""    # BEGIN prism (deploy/caddy_go_live.sh; edit there, not here)
    redir /prism /prism/ 308
    handle /prism/mcp* {{
        uri strip_prefix /prism
        request_body {{
            max_size 1MB
        }}
        reverse_proxy 127.0.0.1:8041 {{
            flush_interval -1
        }}
    }}
    handle /prism/* {{
        request_body {{
            max_size 25MB
        }}
{HEAD}
        reverse_proxy 127.0.0.1:8040
    }}
"""
if staging:
    block += f"""    redir /prism-staging /prism-staging/ 308
    handle /prism-staging/* {{
        request_body {{
            max_size 25MB
        }}
{HEAD}
        reverse_proxy 127.0.0.1:8042
    }}
"""
block += "    # END prism\n"
s = open(path).read()
m = re.search(r"^[ \t]*# BEGIN prism.*?^[ \t]*# END prism[^\n]*\n", s, re.M | re.S)
if m:
    s = s[:m.start()] + block + s[m.end():]
else:
    o = re.search(r"^[^#\n]*" + re.escape(host) + r"[^{\n]*\{[ \t]*$", s, re.M)
    if not o:
        sys.exit("site block for " + host + " not found")
    s = s[:o.end()] + "\n" + block + s[o.end() + 1:]
open(path, "w").write(s)
PY
if ! caddy validate --config "$CF" --adapter caddyfile >/tmp/caddy-validate.log 2>&1; then
  tail -5 /tmp/caddy-validate.log; cp -p "$BAK" "$CF"; die "caddy validate failed; Caddyfile left as it was"
fi
ok "caddy validate passed"

echo "3/4 Reload"
systemctl reload caddy; sleep 3
R=(--resolve "$HOST:443:127.0.0.1")
P=$(curl -s "${R[@]}" -o /dev/null -w '%{http_code}' "https://$HOST/prism/healthz" || echo 000)
PL=$(curl -s "${R[@]}" -o /dev/null -w '%{http_code}' "https://$HOST/prism/" || echo 000)
M=$(curl -s "${R[@]}" -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' "https://$HOST/prism/mcp" -d '{}' || echo 000)
O=$(curl -s "${R[@]}" "https://$HOST/orbit/api/health" || true)
F=$(curl -s "${R[@]}" -o /dev/null -w '%{http_code}' "https://$HOST/flo" || echo 000)
echo "  ..  prism healthz $P, signed-out $PL, mcp without token $M, flo $F, orbit $(echo "$O" | grep -q '"db":true' && echo ok || echo "?")"
if [ "$P" != 200 ] || [ "$PL" != 303 ] || [ "$M" != 401 ] || [ "$F" = 000 ] || [ "$F" -ge 500 ] || ! echo "$O" | grep -q '"db":true'; then
  cp -p "$BAK" "$CF"; systemctl reload caddy
  die "a check failed after the change, so the previous Caddyfile is back. Send Claude this output."
fi
ok "Prism, Orbit and Flo answer through Caddy"

echo "4/4 Public address"
PUB=$(curl -s -o /dev/null -w '%{http_code}' "https://$HOST/prism/healthz" || echo 000)
[ "$PUB" = 200 ] && ok "https://$HOST/prism/ is live" || echo "  ..  public check got $PUB (DNS or network outside this VM); routes are in place"
