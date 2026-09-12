#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu 24.04 box (Hetzner CPX/CX, Falkenstein or Nuremberg)
# into a Claude Code server you drive from your phone.
#
#   ssh root@<ip>
#   curl -fsSLo bootstrap.sh <this file>   # or scp it up
#   bash bootstrap.sh                      # add --with-ntfy for push notifications
#
# Idempotent: safe to re-run.
set -euo pipefail

APP_USER="${APP_USER:-claude}"
APP_DIR="/opt/ccremote"
ENV_FILE="/etc/ccremote.env"
NODE_MAJOR=22
WITH_NTFY=0
[[ "${1:-}" == "--with-ntfy" ]] && WITH_NTFY=1

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

# ---------------------------------------------------------------- packages ---
bold "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
  curl ca-certificates gnupg git jq unzip rsync \
  ufw tmux mosh ripgrep \
  bubblewrap socat \
  python3 python3-venv python3-pip \
  build-essential unattended-upgrades

# -------------------------------------------------------------------- swap ---
bold "Swap"
if swapon --show 2>/dev/null | grep -q '/swapfile'; then
  echo "   swapfile already active"
else
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "   2G swapfile active (a build that spikes gets slow instead of OOM-killed)"
fi

# -------------------------------------------------------------------- user ---
bold "Service user: $APP_USER"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$APP_USER"
  usermod -aG sudo "$APP_USER"
fi
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_HOME/projects" "$APP_HOME/.claude"

# Carry root's authorised keys over so you can ssh in directly as $APP_USER.
if [[ -f /root/.ssh/authorized_keys ]]; then
  install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$APP_HOME/.ssh"
  install -m 600 -o "$APP_USER" -g "$APP_USER" /root/.ssh/authorized_keys "$APP_HOME/.ssh/authorized_keys"
fi

# ------------------------------------------------------------------- node ----
bold "Node $NODE_MAJOR + Claude Code"
if ! command -v node >/dev/null || [[ "$(node -v | sed 's/v\([0-9]*\).*/\1/')" -lt 18 ]]; then
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y -qq nodejs
fi
npm install -g @anthropic-ai/claude-code >/dev/null
CLAUDE_BIN="$(command -v claude)"
echo "claude: $CLAUDE_BIN ($("$CLAUDE_BIN" --version 2>/dev/null || echo '?'))"

# ------------------------------------------------------------- tailscale -----
bold "Tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

# -------------------------------------------------------------- firewall -----
bold "Firewall"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow 60000:61000/udp comment 'mosh' >/dev/null
ufw allow in on tailscale0 >/dev/null
ufw --force enable >/dev/null
ufw status verbose | sed 's/^/   /'

# ---------------------------------------------------------------- ntfy -------
if [[ $WITH_NTFY -eq 1 ]]; then
  bold "ntfy (self-hosted push)"
  set +e
  NTFY_VER="$(curl -fsSL https://api.github.com/repos/binwiederhier/ntfy/releases/latest | jq -r .tag_name | sed 's/^v//')"
  if [[ -n "$NTFY_VER" && "$NTFY_VER" != "null" ]]; then
    curl -fsSLo /tmp/ntfy.deb \
      "https://github.com/binwiederhier/ntfy/releases/download/v${NTFY_VER}/ntfy_${NTFY_VER}_linux_amd64.deb" \
      && dpkg -i /tmp/ntfy.deb >/dev/null \
      && mkdir -p /etc/ntfy \
      && printf 'base-url: http://127.0.0.1:2586\nlisten-http: 127.0.0.1:2586\n' > /etc/ntfy/server.yml \
      && systemctl enable --now ntfy \
      && echo "   ntfy listening on 127.0.0.1:2586"
  fi
  [[ $? -ne 0 ]] && warn "ntfy install failed - notifications stay off, everything else works"
  set -e
fi

# ------------------------------------------------------------ orchestrator ---
bold "Orchestrator"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR" "$APP_DIR/data"

if [[ ! -d "$APP_DIR/orchestrator" ]]; then
  warn "No code in $APP_DIR yet."
  warn "From your Mac:  ./scripts/sync.sh $APP_USER@<tailscale-ip-or-name>"
  warn "Then re-run this script to finish the install."
fi

sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv" 2>/dev/null || true
if [[ -f "$APP_DIR/orchestrator/requirements.txt" ]]; then
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/orchestrator/requirements.txt"
fi

# ----------------------------------------------------------------- env -------
bold "Environment"
if [[ ! -f "$ENV_FILE" ]]; then
  TOKEN="$(openssl rand -hex 32)"
  cat > "$ENV_FILE" <<EOF
CCR_TOKEN=$TOKEN
CCR_BIND_HOST=127.0.0.1
CCR_BIND_PORT=8080
CCR_CLAUDE_BIN=$CLAUDE_BIN
CCR_DB=$APP_DIR/data/runs.sqlite3
CCR_TASKS=$APP_DIR/orchestrator/tasks.yaml
CCR_MAX_CONCURRENT=2
CCR_TURN_TIMEOUT=3600
CCR_NTFY_URL=$([[ $WITH_NTFY -eq 1 ]] && echo "http://127.0.0.1:2586" || echo "")
CCR_NTFY_TOPIC=$([[ $WITH_NTFY -eq 1 ]] && openssl rand -hex 8 || echo "")
CCR_NTFY_TOKEN=
CCR_PUBLIC_URL=
HOME=$APP_HOME
EOF
  echo "   wrote $ENV_FILE with a fresh token"
else
  echo "   $ENV_FILE already exists, leaving it alone"
fi
# systemd reads EnvironmentFile as root before dropping to $APP_USER, so the
# session user never needs to read it -- and must not: it holds CCR_TOKEN and
# the Claude OAuth token, and every Claude Code session runs as $APP_USER.
chmod 600 "$ENV_FILE"; chown root:root "$ENV_FILE"

# ------------------------------------------------------------- ccr-git ------
# Lets a session clone/pull any of your GitHub repos without ever seeing the
# token. Installed root-owned outside $APP_DIR: the copy under $APP_DIR is
# writable by $APP_USER (sync.sh puts it there), and a script that runs as
# root through sudo must not be editable by the user who invokes it.
bold "ccr-git helper"
if [[ -f "$APP_DIR/scripts/ccr-git" ]]; then
  install -m 755 -o root -g root "$APP_DIR/scripts/ccr-git" /usr/local/bin/ccr-git
  install -d -m 755 /usr/local/libexec
  install -d -m 700 /etc/ccremote-git.d
  cat > /usr/local/libexec/ccr-askpass <<'EOF2'
#!/bin/sh
# GIT_ASKPASS helper for ccr-git: answers git's credential prompts from the
# token file named in CCR_TOKEN_FILE, so the token never appears in argv.
case "$1" in
  Username*) echo x-access-token ;;
  *) cat "$CCR_TOKEN_FILE" ;;
esac
EOF2
  chmod 700 /usr/local/libexec/ccr-askpass; chown root:root /usr/local/libexec/ccr-askpass
  echo "   /usr/local/bin/ccr-git installed; tokens go in /etc/ccremote-git.d/<owner>.token (root, 600)"
else
  warn "scripts/ccr-git not synced yet -- re-run after ./scripts/sync.sh to install it"
fi

# The ONLY things $APP_USER may run as root without a password: the git helper
# (validated arguments, token stays root-only) and restarting this service,
# which is what scripts/sync.sh does over ssh.
cat > /etc/sudoers.d/ccremote <<EOF
$APP_USER ALL=(root) NOPASSWD: /usr/local/bin/ccr-git
$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart ccremote
EOF
chmod 440 /etc/sudoers.d/ccremote
visudo -cf /etc/sudoers.d/ccremote >/dev/null || { rm -f /etc/sudoers.d/ccremote; warn "sudoers entry invalid, removed"; }

# --------------------------------------------------------------- systemd -----
bold "systemd unit"
cat > /etc/systemd/system/ccremote.service <<EOF
[Unit]
Description=Claude Code Remote orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python -m orchestrator.app
Restart=always
RestartSec=3
# No NoNewPrivileges= here: it forbids setuid for every child of the service,
# and "sudo ccr-git" from inside a session is exactly that. The sudoers file
# above is what limits what sudo can do instead.
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable ccremote >/dev/null
if [[ -f "$APP_DIR/orchestrator/app.py" ]]; then
  systemctl restart ccremote
  sleep 2
  systemctl is-active --quiet ccremote && echo "   ccremote is running" || warn "ccremote failed - journalctl -u ccremote -n 50"
fi

# ----------------------------------------------------------------- done ------
TOKEN_NOW="$(grep '^CCR_TOKEN=' "$ENV_FILE" | cut -d= -f2)"
cat <<EOF

$(bold "Done. Four manual steps left:")

1. Join the tailnet and put HTTPS in front of the app:
     tailscale up                  # do NOT add --accept-dns=false
     tailscale serve --bg 8080     # 1-2 min on first run; it is not hung
     tailscale serve status        # prints your https://<host>.<tailnet>.ts.net URL

   Then DISABLE KEY EXPIRY for this machine at
   https://login.tailscale.com/admin/machines
   Without it the box silently leaves the tailnet in ~180 days.

2. Log Claude Code in. Run these as SEPARATE interactive commands - piping
   them through ssh as a one-liner silently does nothing:
     sudo -iu $APP_USER
     claude setup-token
   It prints a token starting sk-ant-oat01-... Copy it, exit to root, then:
     echo 'CLAUDE_CODE_OAUTH_TOKEN=<paste>' >> $ENV_FILE
     systemctl restart ccremote
   setup-token does NOT store a login on disk - that env var IS the auth.

3. Put your repositories on THIS machine. Folders on your laptop do not exist
   when your laptop is off, so they have to live here:
     sudo -u $APP_USER git clone <repo> /home/$APP_USER/projects/<name>
   Anything under /home/$APP_USER/projects is reachable from the phone with no
   config change, and the phone can create folders and clone into them itself.
   tasks.yaml only needs editing to pin a shortcut or change the workspace root.

4. Verify, then prove it:
     bash $APP_DIR/scripts/verify.sh
   On your phone, open the https:// URL and paste this token:

     $TOKEN_NOW

EOF
