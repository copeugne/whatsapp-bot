#!/usr/bin/env bash
# =============================================================================
# WhatsApp Bot — VPS Deployment Script
# Target: wab.nix.zone (Oracle Linux on Oracle Cloud, behind Cloudflare proxy)
#
# This script:
#   1. Installs system dependencies (docker, uv, nginx)
#   2. Creates a dedicated service user
#   3. Sets up the application directory
#   4. Patches .env files for production (or generates fresh secrets)
#   5. Installs Cloudflare Origin Certificate for nginx TLS
#   6. Configures nginx as reverse proxy
#   7. Handles SELinux for nginx proxying
#   8. Configures firewall (firewalld)
#   9. Installs systemd service
#  10. Starts everything
#
# Usage (one command from your local machine):
#   bash scripts/push-and-deploy.sh
#
# Or manually:
#   1. SSH in and prepare the directory:
#        ssh -i .ssh/ssh-key-2025-09-30.key opc@141.145.220.34
#        sudo mkdir -p /opt/whatsapp-bot && sudo chown opc:opc /opt/whatsapp-bot
#
#   2. From the project directory on your local machine (whatsapp-bot/):
#        rsync -avz --rsh="ssh -i .ssh/ssh-key-2025-09-30.key" \
#          --exclude=.venv --exclude=.git --exclude=__pycache__ \
#          . opc@141.145.220.34:/opt/whatsapp-bot/
#
#   3. SSH in and run:
#        ssh -i .ssh/ssh-key-2025-09-30.key opc@141.145.220.34
#        sudo bash /opt/whatsapp-bot/scripts/deploy.sh
#
# Prerequisites:
#   - Oracle Cloud VPS with Oracle Linux 8 or 9
#   - Cloudflare DNS: A record wab.nix.zone -> VPS IP (Proxied)
#   - Cloudflare SSL/TLS mode: Full (strict)
#   - Cloudflare Origin Certificate (generated in dashboard)
#   - OCI Security List: open ingress for TCP 80 and 443 (0.0.0.0/0)
# =============================================================================

set -euo pipefail

# --- Configuration -----------------------------------------------------------
DOMAIN="${DOMAIN:-wab.nix.zone}"
VPS_IP="${VPS_IP:-YOUR_VPS_IP}"
APP_DIR="/opt/whatsapp-bot"
APP_USER="whatsapp-bot"
APP_GROUP="whatsapp-bot"
CF_CERT_DIR="/etc/ssl/cloudflare"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log()   { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[x]${NC} $*" >&2; }
info()  { echo -e "${BLUE}[i]${NC} $*"; }
header(){ echo -e "\n${BLUE}--- $* ---${NC}\n"; }

# --- Pre-flight checks -------------------------------------------------------
header "Pre-flight checks"

if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (sudo bash $0)"
    exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
    err "Project not found at $APP_DIR"
    err "First create the directory:"
    err "  sudo mkdir -p ${APP_DIR} && sudo chown opc:opc ${APP_DIR}"
    err "Then rsync from your local machine (from the whatsapp-bot/ directory):"
    err "  rsync -avz --rsh='ssh -i .ssh/ssh-key-2025-09-30.key' \\"
    err "    --exclude=.venv --exclude=.git --exclude=__pycache__ \\"
    err "    . opc@${VPS_IP}:${APP_DIR}/"
    exit 1
fi

if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
    err "$APP_DIR/pyproject.toml not found — is this the right directory?"
    exit 1
fi

# Detect OS
if [[ -f /etc/oracle-release ]]; then
    log "Detected Oracle Linux"
elif [[ -f /etc/redhat-release ]]; then
    log "Detected RHEL-based OS"
else
    warn "Expected Oracle Linux but got something else — script may need adjustments"
fi

log "Pre-flight checks passed"

# --- Step 1: Install system dependencies --------------------------------------
header "Step 1: System dependencies"

# Enable EPEL
if ! rpm -q oracle-epel-release-el9 &>/dev/null && ! rpm -q oracle-epel-release-el8 &>/dev/null; then
    dnf install -y oracle-epel-release-el9 2>/dev/null \
    || dnf install -y oracle-epel-release-el8 2>/dev/null \
    || dnf install -y epel-release 2>/dev/null \
    || warn "Could not install EPEL — some packages may not be available"
fi
log "EPEL repository enabled"

# Core tools
dnf install -y curl wget bind-utils jq unzip tar >/dev/null 2>&1
log "Core tools installed"

# Docker
if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    dnf install -y dnf-utils >/dev/null 2>&1
    dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo 2>/dev/null \
    || dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo 2>/dev/null
    dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null 2>&1
    systemctl enable --now docker
    log "Docker installed ($(docker --version))"
else
    log "Docker already installed ($(docker --version))"
fi

# Docker Compose plugin
if ! docker compose version &>/dev/null; then
    dnf install -y docker-compose-plugin >/dev/null 2>&1
    log "Docker Compose plugin installed"
else
    log "Docker Compose already available"
fi

# Nginx
if ! command -v nginx &>/dev/null; then
    dnf install -y nginx >/dev/null 2>&1
    systemctl enable nginx
    log "Nginx installed"
else
    log "Nginx already installed"
fi

# uv (Python package manager)
# Find the real uv binary (skip symlinks that may point to inaccessible paths)
UV_SRC=""
for p in /root/.local/bin/uv /home/opc/.local/bin/uv; do
    if [[ -f "$p" ]]; then UV_SRC="$p"; break; fi
done

if [[ -z "$UV_SRC" ]]; then
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    for p in /root/.local/bin/uv /home/opc/.local/bin/uv; do
        if [[ -f "$p" ]]; then UV_SRC="$p"; break; fi
    done
fi

# Copy to /usr/local/bin/ so all users (including whatsapp-bot) can run it
rm -f /usr/local/bin/uv /usr/local/bin/uvx
cp "$UV_SRC" /usr/local/bin/uv
chmod 755 /usr/local/bin/uv
UV_DIR=$(dirname "$UV_SRC")
if [[ -f "$UV_DIR/uvx" ]]; then
    cp "$UV_DIR/uvx" /usr/local/bin/uvx
    chmod 755 /usr/local/bin/uvx
fi
hash -r
log "uv installed ($(/usr/local/bin/uv --version))"

# --- Step 2: Create service user ----------------------------------------------
header "Step 2: Service user"

if ! id "$APP_USER" &>/dev/null; then
    useradd --system --shell /usr/sbin/nologin --home-dir /home/$APP_USER --create-home "$APP_USER"
    log "Created user $APP_USER"
else
    log "User $APP_USER already exists"
    gpasswd -d "$APP_USER" docker 2>/dev/null || true
fi

# --- Step 3: Set up application directory -------------------------------------
header "Step 3: Application directory"

mkdir -p "$APP_DIR"/{data,logs,config}
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
chmod +x "$APP_DIR"/scripts/*.sh 2>/dev/null || true

log "Application directory ready at $APP_DIR"

# --- Step 4: Production secrets -----------------------------------------------
header "Step 4: Production secrets"

generate_secret() {
    python3 -c "import secrets; print(secrets.token_urlsafe(32))"
}

generate_fernet_key() {
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null \
    || uv run python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
}

generate_password() {
    python3 -c "import secrets; print(secrets.token_hex(16))"
}

# Helper: set a key=value in a file (patch if exists, append if not)
patch_env() {
    local file="$1" key="$2" value="$3"
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$file"
    else
        echo "${key}=${value}" >> "$file"
    fi
}

if [[ -f "$APP_DIR/.env" ]]; then
    log ".env found (copied from dev) — patching for production"

    patch_env "$APP_DIR/.env" "BOT_FT_REDIRECT_URI" "https://${DOMAIN}/oauth/callback"
    log "  BOT_FT_REDIRECT_URI -> https://${DOMAIN}/oauth/callback"

    patch_env "$APP_DIR/.env" "BOT_WEBHOOK_PORT" "8090"
    log "  BOT_WEBHOOK_PORT -> 8090"

    patch_env "$APP_DIR/.env" "BOT_WEBHOOK_HOST" "172.17.0.1"
    log "  BOT_WEBHOOK_HOST -> Docker bridge only"

    patch_env "$APP_DIR/.env" "BOT_SCRAPER_ENABLED" "true"
    log "  BOT_SCRAPER_ENABLED -> true"

    # Preserve the operator-selected destination group.

    chmod 600 "$APP_DIR/.env"
    chown "$APP_USER:$APP_GROUP" "$APP_DIR/.env"

    if [[ -f "$APP_DIR/evolution.env" ]]; then
        patch_env "$APP_DIR/evolution.env" "SERVER_URL" "https://${DOMAIN}"
        log "  SERVER_URL -> https://${DOMAIN} (evolution.env)"

        # Update webhook URL to use port 8090
        WEBHOOK_SECRET=$(grep "^BOT_WEBHOOK_SECRET=" "$APP_DIR/.env" | cut -d= -f2-)
        if [[ -n "$WEBHOOK_SECRET" ]]; then
            patch_env "$APP_DIR/evolution.env" "WEBHOOK_GLOBAL_URL" "http://host.docker.internal:8090/webhook?token=${WEBHOOK_SECRET}"
            log "  WEBHOOK_GLOBAL_URL -> port 8090 (evolution.env)"
        fi

        patch_env "$APP_DIR/evolution.env" "LOG_LEVEL" "ERROR,WARN"
        patch_env "$APP_DIR/evolution.env" "LOG_COLOR" "false"

        chmod 600 "$APP_DIR/evolution.env"
        chown "$APP_USER:$APP_GROUP" "$APP_DIR/evolution.env"
    fi

    log "Production patches applied — all existing secrets preserved"
else
    info "No .env found — generating fresh production secrets"

    NEW_WEBHOOK_SECRET=$(generate_secret)
    NEW_ENCRYPTION_KEY=$(generate_fernet_key)
    NEW_EVOLUTION_API_KEY=$(generate_secret)
    NEW_POSTGRES_PASSWORD=$(generate_password)
    NEW_REDIS_PASSWORD=$(generate_secret)

    cat > "$APP_DIR/.env" <<ENVEOF
# Bot Service — Production config for $DOMAIN
# Generated: $(date -Iseconds)

BOT_EVOLUTION_API_URL=http://localhost:18080
BOT_EVOLUTION_API_KEY=${NEW_EVOLUTION_API_KEY}
BOT_WEBHOOK_HOST=172.17.0.1
BOT_WEBHOOK_PORT=8090
BOT_INSTANCE_NAME=default

BOT_FT_CLIENT_ID=                # FILL IN: 42 OAuth app client ID
BOT_FT_CLIENT_SECRET=            # FILL IN: 42 OAuth app client secret
BOT_FT_CAMPUS_ID=1
BOT_FT_REDIRECT_URI=https://${DOMAIN}/oauth/callback
BOT_SCRAPER_ENABLED=true
BOT_SCRAPER_INTERVAL_SECONDS=300
BOT_WHATSAPP_GROUP_JID=          # FILL IN: target group JID
BOT_SEEN_EVENTS_PATH=data/seen_events.json

BOT_GEMINI_API_KEY=              # FILL IN: Google AI Studio API key

BOT_WEBHOOK_SECRET=${NEW_WEBHOOK_SECRET}
BOT_TOKEN_ENCRYPTION_KEY=${NEW_ENCRYPTION_KEY}
ENVEOF
    chmod 600 "$APP_DIR/.env"
    chown "$APP_USER:$APP_GROUP" "$APP_DIR/.env"
    log "Generated $APP_DIR/.env with fresh secrets"
    warn "You MUST fill in: BOT_FT_CLIENT_ID, BOT_FT_CLIENT_SECRET, BOT_WHATSAPP_GROUP_JID, BOT_GEMINI_API_KEY"

    cat > "$APP_DIR/evolution.env" <<EVOEOF
# Evolution API — Production config
# Generated: $(date -Iseconds)

POSTGRES_DB=evolution
POSTGRES_USER=evolution
POSTGRES_PASSWORD=${NEW_POSTGRES_PASSWORD}

SERVER_URL=https://${DOMAIN}
AUTHENTICATION_API_KEY=${NEW_EVOLUTION_API_KEY}

DATABASE_PROVIDER=postgresql
DATABASE_CONNECTION_URI=postgresql://\${POSTGRES_USER}:\${POSTGRES_PASSWORD}@postgres:5432/\${POSTGRES_DB}?schema=public
DATABASE_CONNECTION_CLIENT_NAME=evolution_v2
DATABASE_SAVE_DATA_INSTANCE=true
DATABASE_SAVE_DATA_NEW_MESSAGE=true
DATABASE_SAVE_MESSAGE_UPDATE=true
DATABASE_SAVE_DATA_CONTACTS=true
DATABASE_SAVE_DATA_CHATS=true
DATABASE_SAVE_DATA_LABELS=true
DATABASE_SAVE_DATA_HISTORIC=true
DATABASE_SAVE_IS_ON_WHATSAPP=true
DATABASE_SAVE_IS_ON_WHATSAPP_DAYS=7
DATABASE_DELETE_MESSAGE=true

CACHE_REDIS_ENABLED=true
REDIS_PASSWORD=${NEW_REDIS_PASSWORD}
CACHE_REDIS_URI=redis://:${NEW_REDIS_PASSWORD}@redis:6379/6
CACHE_REDIS_TTL=604800
CACHE_REDIS_PREFIX_KEY=evolution
CACHE_REDIS_SAVE_INSTANCES=false
CACHE_LOCAL_ENABLED=false

WEBHOOK_GLOBAL_ENABLED=true
WEBHOOK_GLOBAL_URL=http://host.docker.internal:8090/webhook?token=${NEW_WEBHOOK_SECRET}
WEBHOOK_GLOBAL_WEBHOOK_BY_EVENTS=false
WEBHOOK_EVENTS_MESSAGES_UPSERT=true
WEBHOOK_EVENTS_MESSAGES_UPDATE=true
WEBHOOK_EVENTS_CONNECTION_UPDATE=true
WEBHOOK_EVENTS_QRCODE_UPDATED=true
WEBHOOK_EVENTS_MESSAGES_SET=false
WEBHOOK_EVENTS_MESSAGES_EDITED=false
WEBHOOK_EVENTS_MESSAGES_DELETE=false
WEBHOOK_EVENTS_SEND_MESSAGE=false
WEBHOOK_EVENTS_CONTACTS_SET=false
WEBHOOK_EVENTS_CONTACTS_UPSERT=false
WEBHOOK_EVENTS_CONTACTS_UPDATE=false
WEBHOOK_EVENTS_PRESENCE_UPDATE=false
WEBHOOK_EVENTS_CHATS_SET=false
WEBHOOK_EVENTS_CHATS_UPSERT=false
WEBHOOK_EVENTS_CHATS_UPDATE=false
WEBHOOK_EVENTS_CHATS_DELETE=false
WEBHOOK_EVENTS_GROUPS_UPSERT=false
WEBHOOK_EVENTS_GROUPS_UPDATE=false
WEBHOOK_EVENTS_GROUP_PARTICIPANTS_UPDATE=false
WEBHOOK_EVENTS_LABELS_EDIT=false
WEBHOOK_EVENTS_LABELS_ASSOCIATION=false
WEBHOOK_EVENTS_CALL=false

QRCODE_LIMIT=30
CONFIG_SESSION_PHONE_CLIENT=WhatsApp Bot
CONFIG_SESSION_PHONE_NAME=Chrome

LOG_LEVEL=ERROR,WARN
LOG_COLOR=false
LOG_BAILEYS=error

TELEMETRY_ENABLED=false

TYPEBOT_ENABLED=false
CHATWOOT_ENABLED=false
OPENAI_ENABLED=false
DIFY_ENABLED=false
N8N_ENABLED=false
EVOAI_ENABLED=false
RABBITMQ_ENABLED=false
SQS_ENABLED=false
WEBSOCKET_ENABLED=false
S3_ENABLED=false
EVOEOF
    chmod 600 "$APP_DIR/evolution.env"
    chown "$APP_USER:$APP_GROUP" "$APP_DIR/evolution.env"
    log "Generated $APP_DIR/evolution.env with fresh secrets"

    if [[ -f "$APP_DIR/docker-compose.yml" ]]; then
        log "Compose credentials are supplied through evolution.env"
    fi
fi

# --- Step 5: Cloudflare Origin Certificate ------------------------------------
header "Step 5: Cloudflare Origin Certificate"

mkdir -p "$CF_CERT_DIR"

openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
    -subj "/CN=$DOMAIN" \
    -keyout "$CF_CERT_DIR/$DOMAIN.key" \
    -out "$CF_CERT_DIR/$DOMAIN.pem" >/dev/null 2>&1
chmod 644 "$CF_CERT_DIR/$DOMAIN.pem"
chmod 600 "$CF_CERT_DIR/$DOMAIN.key"
log "Origin TLS certificate generated at $CF_CERT_DIR/"

# --- Step 6: Configure nginx --------------------------------------------------
header "Step 6: Nginx configuration"

NGINX_CONF="/etc/nginx/conf.d/${DOMAIN}.conf"
cp "$APP_DIR/deploy/nginx/wab.nix.zone.conf" "$NGINX_CONF"

# Remove the default server block if present
if [[ -f /etc/nginx/conf.d/default.conf ]]; then
    mv /etc/nginx/conf.d/default.conf /etc/nginx/conf.d/default.conf.disabled
fi

nginx -t
systemctl restart nginx
log "Nginx configured and running for $DOMAIN"

# --- Step 7: SELinux ----------------------------------------------------------
header "Step 7: SELinux"

if command -v getenforce &>/dev/null && [[ "$(getenforce)" != "Disabled" ]]; then
    setsebool -P httpd_can_network_connect 1
    log "SELinux: httpd_can_network_connect = on (nginx can proxy to bot)"

    if command -v restorecon &>/dev/null; then
        restorecon -Rv "$CF_CERT_DIR" >/dev/null 2>&1
    fi
else
    log "SELinux disabled or not present — no action needed"
fi

# --- Step 8: Firewall (firewalld) ---------------------------------------------
header "Step 8: Firewall (firewalld)"

if command -v firewall-cmd &>/dev/null; then
    systemctl enable --now firewalld 2>/dev/null || true

    firewall-cmd --permanent --add-service=ssh   >/dev/null 2>&1
    firewall-cmd --permanent --add-service=http  >/dev/null 2>&1
    firewall-cmd --permanent --add-service=https >/dev/null 2>&1
    firewall-cmd --reload >/dev/null 2>&1

    log "firewalld configured (SSH + HTTP + HTTPS)"
    firewall-cmd --list-services
else
    warn "firewalld not found — configure your OS firewall manually"
fi

echo ""
warn "REMINDER: Also open ports 80 and 443 in your OCI Security List"
warn "  OCI Console -> Networking -> Virtual Cloud Networks -> Security Lists"
warn "  Add ingress rules: TCP 80 and TCP 443 from 0.0.0.0/0"

# --- Step 9: Systemd service --------------------------------------------------
header "Step 9: Systemd service"

cp "$APP_DIR/deploy/systemd/whatsapp-bot.service" /etc/systemd/system/whatsapp-bot.service
systemctl daemon-reload
log "Systemd service installed"

# --- Step 10: Python dependencies ---------------------------------------------
header "Step 10: Python dependencies"

su -s /bin/bash "$APP_USER" -c "cd $APP_DIR && /usr/local/bin/uv sync --frozen 2>&1"
log "Python dependencies installed"

# --- Step 11: Start Docker services -------------------------------------------
header "Step 11: Docker services"

cd "$APP_DIR"
docker compose --env-file "$APP_DIR/evolution.env" down 2>/dev/null || true
docker compose --env-file "$APP_DIR/evolution.env" up -d
log "Docker services started (Evolution API + Postgres + Redis)"

info "Waiting for Evolution API..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:18080/ >/dev/null 2>&1; then
        log "Evolution API is ready"
        break
    fi
    if [[ $i -eq 30 ]]; then
        warn "Evolution API did not respond after 30s — check: docker compose logs"
    fi
    sleep 1
done

# --- Step 12: Start the bot ---------------------------------------------------
header "Step 12: Start the bot"

# Stop any existing instance
systemctl stop whatsapp-bot 2>/dev/null || true
sleep 1

systemctl enable --now whatsapp-bot
sleep 3

if systemctl is-active --quiet whatsapp-bot; then
    log "WhatsApp Bot service is running"
else
    err "Bot service failed to start!"
    err "Check logs: journalctl -u whatsapp-bot -n 50"
fi

# --- Final summary ------------------------------------------------------------
header "Deployment complete!"

echo ""
log "Domain:     https://$DOMAIN"
log "Health:     https://$DOMAIN/health"
log "OAuth:      https://$DOMAIN/oauth/callback"
log "Bot logs:   journalctl -u whatsapp-bot -f"
log "Bot status: systemctl status whatsapp-bot"
log "Docker:     docker compose -f $APP_DIR/docker-compose.yml logs -f"
echo ""

if grep -q "# FILL IN" "$APP_DIR/.env" 2>/dev/null; then
    warn "--- ACTION REQUIRED ---"
    warn "Edit $APP_DIR/.env and fill in the following:"
    grep "# FILL IN" "$APP_DIR/.env" | while read -r line; do
        warn "  -> ${line%%=*}"
    done
    warn "Then restart: sudo systemctl restart whatsapp-bot"
    echo ""
fi

info "Next steps:"
info "  1. Ensure OCI Security List allows TCP 80 + 443 inbound"
info "  2. Set Cloudflare SSL/TLS mode to Full (strict)"
info "  3. Verify: curl https://$DOMAIN/health"
info "  4. Scan the QR code to connect WhatsApp (check Evolution API logs)"
info "  5. Re-authorize your 42 OAuth token"
echo ""
