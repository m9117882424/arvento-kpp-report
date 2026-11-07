#!/usr/bin/env bash
set -Eeuo pipefail

# One-time Nginx wiring for the dedicated maksim geofence editor.
# Run as root on PRIMARY after deploying the editor-relative API URLs.
PROJECT_ROOT="${1:-/opt/arvento_report}"
NGINX_SITE="/etc/nginx/sites-available/bdd.equippulse.com"
BDD_PASSWORD_FILE="/etc/nginx/.htpasswd_bdd"
GEOS_PASSWORD_FILE="/etc/nginx/.htpasswd_bdd_geozones_maksim"
SNIPPET="/etc/nginx/snippets/arvento-bdd-geozones.conf"
TEMPLATE="$PROJECT_ROOT/deploy/nginx/bdd-geozones.conf.example"
INCLUDE="include /etc/nginx/snippets/arvento-bdd-geozones.conf;"

[[ "$(id -u)" == "0" ]] || { echo 'ERROR: run as root' >&2; exit 1; }
for path in "$NGINX_SITE" "$BDD_PASSWORD_FILE" "$TEMPLATE"; do
    [[ -f "$path" ]] || { echo "ERROR: missing $path" >&2; exit 1; }
done
command -v nginx >/dev/null || { echo 'ERROR: nginx unavailable' >&2; exit 1; }
[[ "$(grep -c '^maksim:' "$BDD_PASSWORD_FILE" || true)" == "1" ]] || {
    echo 'ERROR: expected exactly one maksim entry in existing BDD password file' >&2
    exit 1
}
[[ "$(grep -Ec '^[[:space:]]*location[[:space:]]+/[[:space:]]*\{' "$NGINX_SITE" || true)" == "1" ]] || {
    echo 'ERROR: cannot identify unique BDD root location; no changes made' >&2
    exit 1
}

if ! curl --fail --silent --max-time 10 http://127.0.0.1:18083/api/geofences >/dev/null; then
    echo 'ERROR: the local geofence editor is not healthy; no changes made' >&2
    exit 1
fi

install -d -m 0755 /etc/nginx/snippets
password_tmp="$(mktemp /etc/nginx/.bdd-geozones-password.XXXXXXXX)"
trap 'rm -f "$password_tmp"' EXIT
grep '^maksim:' "$BDD_PASSWORD_FILE" > "$password_tmp"
install -o root -g www-data -m 0640 "$password_tmp" "$GEOS_PASSWORD_FILE"
install -o root -g root -m 0644 "$TEMPLATE" "$SNIPPET"

if ! grep -Fq "$INCLUDE" "$NGINX_SITE"; then
    backup="$(mktemp "${NGINX_SITE}.before-geozones.XXXXXXXX")"
    cp -a "$NGINX_SITE" "$backup"
    sed -i "/^[[:space:]]*location[[:space:]]*\/ {/i\\    $INCLUDE" "$NGINX_SITE"
    if ! nginx -t; then
        cp -a "$backup" "$NGINX_SITE"
        nginx -t
        echo "ERROR: invalid configuration; restored from $backup" >&2
        exit 1
    fi
    if ! systemctl reload nginx; then
        cp -a "$backup" "$NGINX_SITE"
        nginx -t && systemctl reload nginx
        echo "ERROR: reload failed; original configuration restored from $backup" >&2
        exit 1
    fi
    echo "OK: installed maksim-only geofence route; previous Nginx config: $backup"
else
    nginx -t
    systemctl reload nginx
    echo 'OK: refreshed maksim password hash and Nginx snippet'
fi
echo 'Check: https://bdd.equippulse.com/geozones/'
