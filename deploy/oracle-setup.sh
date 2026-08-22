#!/usr/bin/env bash
# Roadwatch on a free Oracle Cloud Always Free VM.
#
#   scp -r roadwatch/ ubuntu@<vm-ip>:~/
#   ssh ubuntu@<vm-ip>
#   sudo bash roadwatch/deploy/oracle-setup.sh yourname.duckdns.org <duckdns-token>
#
# Measured footprint: ~234MB with two feeds polling, ~24MB per additional
# camera at max_frame_width=640. The Always Free AMD shape has 1GB, so after
# Ubuntu and Docker there's roughly 490MB spare - comfortable.
#
# WHY HTTPS IS PART OF THIS AND NOT AN AFTERTHOUGHT
#
# The app is a PWA. Service workers and Add to Home Screen both hard-require a
# secure context, so plain http://<ip>:8000 gives you a webpage you cannot
# install and that will not work offline. A bare VM has no hostname and no
# certificate, which is exactly the gap Pages filled for free. Caddy plus a free
# DuckDNS hostname closes it: Caddy gets a Let's Encrypt certificate
# automatically and renews it without further involvement.
set -euo pipefail

DOMAIN="${1:-}"
DUCKDNS_TOKEN="${2:-}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "$DOMAIN" ]]; then
  echo "usage: sudo bash oracle-setup.sh <domain> [duckdns-token]" >&2
  echo "  Get a free subdomain at duckdns.org, or use any domain pointed here." >&2
  exit 1
fi

echo "==> Installing packages"
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 curl ufw

echo "==> Opening the firewall"
# THE ORACLE GOTCHA. Oracle's Ubuntu images ship iptables rules that DROP
# everything except SSH, and those rules are separate from the VCN Security
# List in the web console. Open only one of the two and the port stays shut
# while everything looks correctly configured - this is the single most common
# reason an Oracle VM appears unreachable.
#
# You must ALSO add ingress rules for TCP 80 and 443 in the console:
#   Networking > Virtual Cloud Networks > <vcn> > Security Lists > Default
# That part is web-only and cannot be scripted from in here.
iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT || true
iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT || true
netfilter-persistent save 2>/dev/null || iptables-save > /etc/iptables/rules.v4 2>/dev/null || true

if [[ -n "$DUCKDNS_TOKEN" ]]; then
  echo "==> Pointing $DOMAIN at this machine"
  SUB="${DOMAIN%%.duckdns.org}"
  curl -fsS "https://www.duckdns.org/update?domains=${SUB}&token=${DUCKDNS_TOKEN}&ip=" >/dev/null
  # Re-assert every 5 minutes. Oracle public IPs are stable in practice, but a
  # stale DNS record means the certificate renewal fails silently and the PWA
  # stops installing months later for no visible reason.
  cat >/etc/cron.d/duckdns <<EOF
*/5 * * * * root curl -fsS "https://www.duckdns.org/update?domains=${SUB}&token=${DUCKDNS_TOKEN}&ip=" >/dev/null 2>&1
EOF
fi

echo "==> Writing compose file"
cat >"${APP_DIR}/docker-compose.yml" <<EOF
services:
  app:
    build: .
    restart: always
    environment:
      PORT: "8000"
    volumes:
      # Your labelled events live here. This is the only thing in the project
      # that cannot be regenerated, so it must outlive the container.
      - ./data:/app/data
    expose: ["8000"]

  caddy:
    image: caddy:2-alpine
    restart: always
    ports: ["80:80", "443:443"]
    volumes:
      - ./deploy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on: [app]

volumes:
  caddy_data:
  caddy_config:
EOF

cat >"${APP_DIR}/deploy/Caddyfile" <<EOF
${DOMAIN} {
    encode gzip
    reverse_proxy app:8000
}
EOF

echo "==> Building and starting"
cd "$APP_DIR"
docker compose up -d --build

echo "==> Installing systemd unit so it survives reboots"
cat >/etc/systemd/system/roadwatch.service <<EOF
[Unit]
Description=Roadwatch
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${APP_DIR}
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable roadwatch.service

cat <<EOF

Done. https://${DOMAIN}

Remaining, and both are web-console only:
  1. Oracle console > Networking > VCN > Security Lists > Default:
     add ingress for TCP 80 and 443 from 0.0.0.0/0.
     Without this the port stays closed no matter what this script did.
  2. Confirm ${DOMAIN} resolves to this VM's public IP before expecting a
     certificate - Let's Encrypt validates over HTTP and will fail otherwise.

Then open https://${DOMAIN} in Safari on the iPhone and Add to Home Screen.
The Live tab will appear on its own once api/policy answers.

Logs:    docker compose logs -f app
Restart: systemctl restart roadwatch
EOF
