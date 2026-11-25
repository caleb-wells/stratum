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

# --- AWS credentials for the retrieval service only
# Containers cannot reach the instance metadata service (hop limit 1). The host fetches the
# role's short-lived credentials and writes them to a file that only rag-api mounts.
mkdir -p /opt/stratum/aws
cat > /usr/local/sbin/stratum-creds <<'CREDS'
#!/usr/bin/env bash
set -euo pipefail
umask 077
M=http://169.254.169.254/latest
T=$(curl -sf -X PUT "$M/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 120")
ROLE=$(curl -sf -H "X-aws-ec2-metadata-token: $T" "$M/meta-data/iam/security-credentials/")
curl -sf -H "X-aws-ec2-metadata-token: $T" "$M/meta-data/iam/security-credentials/$ROLE" | python3 -c '
import json, sys
c = json.load(sys.stdin)
print(json.dumps({"Version": 1, "AccessKeyId": c["AccessKeyId"], "SecretAccessKey": c["SecretAccessKey"],
                  "SessionToken": c["Token"], "Expiration": c["Expiration"]}))' > /opt/stratum/aws/creds.json.new
chown 10001 /opt/stratum/aws/creds.json.new && chmod 400 /opt/stratum/aws/creds.json.new
mv -f /opt/stratum/aws/creds.json.new /opt/stratum/aws/creds.json
CREDS
chmod 755 /usr/local/sbin/stratum-creds
printf '[default]\nregion = %s\ncredential_process = /bin/cat /run/aws/creds.json\n' "$AWS_REGION" > /opt/stratum/aws/config
chmod 755 /opt/stratum/aws && chmod 644 /opt/stratum/aws/config
cat > /etc/systemd/system/stratum-creds.service <<'UNIT'
[Unit]
Description=Refresh AWS credentials file for the Stratum retrieval service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/stratum-creds
UNIT
cat > /etc/systemd/system/stratum-creds.timer <<'UNIT'
[Unit]
Description=Refresh Stratum AWS credentials every 10 minutes
[Timer]
OnBootSec=30s
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
UNIT
# remove the firewall-based guard from an earlier version of this script, if present
systemctl disable --now stratum-imds-guard >/dev/null 2>&1 || true
rm -f /etc/systemd/system/stratum-imds-guard.service /usr/local/sbin/stratum-imds-guard
systemctl daemon-reload
systemctl enable --now stratum-creds.timer >/dev/null
/usr/local/sbin/stratum-creds

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
dc() { docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.server.yml --env-file .env "$@"; }
dc pull -q postgres rag-api open-webui
dc up -d --no-build postgres rag-api open-webui

# --- load the corpus (re-embeds; takes about a minute) and check retrieval
for i in $(seq 1 30); do dc exec -T rag-api python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)" 2>/dev/null && break; sleep 4; done
dc exec -T rag-api python -W ignore -m app.ingest.cli load
dc exec -T rag-api python -W ignore -m app.ingest.cli verify
# one real model call through the instance role, so a permissions problem shows up here
dc exec -T rag-api python -c "from app import bedrock, config; r = bedrock.claude().messages.create(model=config.ROUTER_MODEL, max_tokens=20, messages=[{'role':'user','content':'Reply with: ok'}]); print('bedrock ok:', r.stop_reason)"

# --- prove no public-facing container can reach instance credentials. Fails closed:
# only an explicit BLOCKED from a probe that really ran counts as a pass.
for svc in open-webui postgres; do
  if [ "$svc" = open-webui ]; then
    probe=$(dc exec -T open-webui python3 -c "
import urllib.request, urllib.error
req = urllib.request.Request('http://169.254.169.254/latest/api/token', method='PUT', headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'})
try:
    urllib.request.urlopen(req, timeout=4); print('REACHABLE')
except urllib.error.HTTPError: print('REACHABLE')
except Exception: print('BLOCKED')" 2>&1 | tail -1) || probe="PROBE_FAILED"
  else
    probe=$(dc exec -T postgres sh -c "if timeout 4 sh -c 'exec 3<>/dev/tcp/169.254.169.254/80 && printf \"PUT /latest/api/token HTTP/1.0\\r\\nX-aws-ec2-metadata-token-ttl-seconds: 60\\r\\n\\r\\n\" >&3 && head -c 12 <&3 | grep -q HTTP' 2>/dev/null; then echo REACHABLE; else echo BLOCKED; fi" 2>&1 | tail -1) || probe="PROBE_FAILED"
  fi
  if [ "$probe" != "BLOCKED" ]; then
    echo "FATAL: credential isolation check for $svc returned '$probe'; not exposing the site" >&2
    exit 1
  fi
done
echo "credential isolation ok"

# --- accounts and the chat plugin, BEFORE the site is reachable from outside
dc exec -T -e ADMIN_EMAIL -e ADMIN_PASSWORD -e ADMIN_NAME -e DEMO_EMAIL -e DEMO_PASSWORD -e DEMO_NAME \
  rag-api python -m app.bootstrap_webui

# --- now open the front door (Caddy gets its certificate once DNS points here)
dc --profile tls pull -q caddy
dc --profile tls up -d --no-build caddy

# --- nightly database dump to S3 (chat history + usage log)
cat > /etc/cron.d/stratum-backup <<CRON
15 9 * * * root cd /opt/stratum && for db in rag; do docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.server.yml --env-file .env exec -T postgres pg_dump -U rag \$db | gzip | aws s3 cp - s3://$BACKUP_BUCKET/postgres/\$(date +\%F).sql.gz --region $AWS_REGION; done; docker run --rm -v geo-rag_openwebui:/data:ro alpine tar czf - -C /data . | aws s3 cp - s3://$BACKUP_BUCKET/openwebui/\$(date +\%F).tgz --region $AWS_REGION
CRON
dnf install -y cronie >/dev/null 2>&1 || true
systemctl enable --now crond >/dev/null 2>&1 || true
echo "stratum setup finished"
