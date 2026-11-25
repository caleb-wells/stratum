#!/usr/bin/env bash
# Runs on the EC2 instance (first boot, and again for each redeploy). Idempotent.
# Expects /opt/stratum/config.env to be loaded into the environment by the caller.
set -euo pipefail
cd /opt/stratum
PREFIX=/stratum
export AWS_DEFAULT_REGION="$AWS_REGION"

# --- Docker + Compose
if ! command -v docker >/dev/null; then
  dnf install -y docker
  systemctl enable --now docker
fi
if ! docker compose version >/dev/null 2>&1; then
  mkdir -p /usr/local/lib/docker/cli-plugins
  curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi
# a little swap keeps a small instance steady while images are pulled
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none && chmod 600 /swapfile && mkswap /swapfile >/dev/null
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi
swapon -a || true

# --- only the retrieval service may reach the instance's AWS credentials
# The instance metadata hop limit is 2 so that a container can use the instance role. Without
# this rule every container could, including the internet-facing chat UI. The rule lets one
# fixed container address through and drops the rest, and is re-applied at every boot.
cat > /usr/local/sbin/stratum-imds-guard <<'GUARD'
#!/usr/bin/env bash
set -euo pipefail
iptables -N DOCKER-USER 2>/dev/null || true
while iptables -D DOCKER-USER -d 169.254.169.254 -j DROP 2>/dev/null; do :; done
while iptables -D DOCKER-USER -s 172.28.0.10 -d 169.254.169.254 -j ACCEPT 2>/dev/null; do :; done
iptables -I DOCKER-USER 1 -d 169.254.169.254 -j DROP
iptables -I DOCKER-USER 1 -s 172.28.0.10 -d 169.254.169.254 -j ACCEPT
GUARD
chmod 755 /usr/local/sbin/stratum-imds-guard
cat > /etc/systemd/system/stratum-imds-guard.service <<'UNIT'
[Unit]
Description=Allow only the Stratum retrieval container to reach instance credentials
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/stratum-imds-guard
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable stratum-imds-guard >/dev/null 2>&1
systemctl restart stratum-imds-guard

# --- secrets: created once on this machine, stored encrypted in Parameter Store
secret() {  # secret <name> <bytes>
  local value
  if value=$(aws ssm get-parameter --name "$PREFIX/$1" --with-decryption --query Parameter.Value --output text 2>/dev/null); then
    printf '%s' "$value"; return
  fi
  value=$(openssl rand -base64 "$2" | tr -d '/+=\n')
  aws ssm put-parameter --name "$PREFIX/$1" --type SecureString --value "$value" >/dev/null
  printf '%s' "$value"
}
umask 077
cat > .env <<ENV
$(cat config.env)
POSTGRES_PASSWORD=$(secret postgres-password 24)
INTERNAL_API_KEY=$(secret internal-api-key 36)
WEBUI_SECRET_KEY=$(secret webui-secret-key 48)
ADMIN_PASSWORD=$(secret admin-password 18)
DEMO_PASSWORD=$(secret guest-password 18)
ENV
set -a; . ./.env; set +a

# --- pull images and start everything except the public proxy
registry="${RAG_API_IMAGE%%/*}"
aws ecr get-login-password | docker login --username AWS --password-stdin "$registry" >/dev/null
dc() { docker compose -f deploy/docker-compose.yml --env-file .env "$@"; }
dc pull -q postgres rag-api open-webui
dc up -d --no-build postgres rag-api open-webui

# --- load the corpus (re-embeds; takes about a minute) and check retrieval
for i in $(seq 1 30); do dc exec -T rag-api python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)" 2>/dev/null && break; sleep 4; done
dc exec -T rag-api python -W ignore -m app.ingest.cli load
dc exec -T rag-api python -W ignore -m app.ingest.cli verify
# one real model call through the instance role, so a permissions problem shows up here
dc exec -T rag-api python -c "from app import bedrock, config; r = bedrock.claude().messages.create(model=config.ROUTER_MODEL, max_tokens=20, messages=[{'role':'user','content':'Reply with: ok'}]); print('bedrock ok:', r.stop_reason)"

# prove the chat UI container cannot reach instance credentials; stop if it can
if dc exec -T open-webui python3 -c "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://169.254.169.254/latest/api/token', method='PUT', headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'}), timeout=3)" 2>/dev/null; then
  echo "FATAL: the chat UI container can reach instance credentials; not exposing the site" >&2
  exit 1
fi
echo "credential isolation ok"

# --- accounts and the chat plugin, BEFORE the site is reachable from outside
dc exec -T -e ADMIN_EMAIL -e ADMIN_PASSWORD -e ADMIN_NAME -e DEMO_EMAIL -e DEMO_PASSWORD -e DEMO_NAME \
  rag-api python -m app.bootstrap_webui

# --- now open the front door (Caddy gets its certificate once DNS points here)
dc --profile tls pull -q caddy
dc --profile tls up -d --no-build caddy

# --- nightly database dump to S3 (chat history + usage log)
cat > /etc/cron.d/stratum-backup <<CRON
15 9 * * * root cd /opt/stratum && for db in rag; do docker compose -f deploy/docker-compose.yml --env-file .env exec -T postgres pg_dump -U rag \$db | gzip | aws s3 cp - s3://$BACKUP_BUCKET/postgres/\$(date +\%F).sql.gz --region $AWS_REGION; done; docker run --rm -v geo-rag_openwebui:/data:ro alpine tar czf - -C /data . | aws s3 cp - s3://$BACKUP_BUCKET/openwebui/\$(date +\%F).tgz --region $AWS_REGION
CRON
dnf install -y cronie >/dev/null 2>&1 || true
systemctl enable --now crond >/dev/null 2>&1 || true
echo "stratum setup finished"
