#!/usr/bin/env bash
# Checks everything that has to be true for the phone to work while your
# own machine is switched off. Run it on the server after bootstrap.
#   bash /opt/ccremote/scripts/verify.sh
set -uo pipefail

PASS=0; FAIL=0; WARN=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n     -> %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n     -> %s\n' "$1" "$2"; WARN=$((WARN+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

ENV_FILE=${ENV_FILE:-/etc/ccremote.env}
[ -r "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a
PORT=${CCR_BIND_PORT:-8080}

head_ "1. Survives a reboot with nobody logged in"
for unit in ccremote tailscaled; do
  if systemctl is-enabled --quiet "$unit" 2>/dev/null; then ok "$unit is enabled (starts at boot)"
  else bad "$unit is NOT enabled" "systemctl enable $unit  -- otherwise it dies at the next reboot"; fi
  if systemctl is-active --quiet "$unit" 2>/dev/null; then ok "$unit is running now"
  else bad "$unit is not running" "systemctl status $unit"; fi
done

head_ "2. Reachable from the phone"
if command -v tailscale >/dev/null; then
  if tailscale status >/dev/null 2>&1; then
    ok "tailscale is up ($(tailscale ip -4 2>/dev/null | head -1))"
    if tailscale serve status 2>/dev/null | grep -q "$PORT"; then
      ok "tailscale serve is proxying port $PORT ($(tailscale serve status 2>/dev/null | grep -oE 'https://[^ ]+' | head -1))"
    else
      bad "tailscale serve is not configured" "tailscale serve --bg $PORT"
    fi
    if tailscale status --json 2>/dev/null | grep -q '"KeyExpiryDisabled": *true'; then
      ok "Tailscale key expiry is DISABLED (node will not drop off in 180 days)"
    else
      warn "Tailscale key expiry is still ENABLED" \
           "Admin console -> Machines -> this host -> Disable key expiry. Otherwise it silently leaves the tailnet in ~180 days, while you are away from your computer."
    fi
  else bad "tailscale is not connected" "tailscale up"; fi
else bad "tailscale is not installed" "curl -fsSL https://tailscale.com/install.sh | sh"; fi

head_ "3. Claude Code can actually run"
if command -v claude >/dev/null; then
  ok "claude installed ($(claude --version 2>/dev/null | head -1))"
else bad "claude not on PATH" "npm install -g @anthropic-ai/claude-code"; fi
APP_USER=${APP_USER:-claude}
APP_HOME=$(getent passwd "$APP_USER" 2>/dev/null | cut -d: -f6)
# `claude setup-token` stores nothing on disk: the token in the service's
# environment IS the login, so that is what to check for.
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  ok "CLAUDE_CODE_OAUTH_TOKEN is set in $ENV_FILE"
else
  bad "no CLAUDE_CODE_OAUTH_TOKEN in $ENV_FILE" \
      "sudo -iu $APP_USER; claude setup-token; then add CLAUDE_CODE_OAUTH_TOKEN=<token> to $ENV_FILE and restart"
fi
if [ -x /usr/local/bin/ccr-git ] && [ -x /usr/local/libexec/ccr-askpass ] && [ -r /etc/sudoers.d/ccremote ]; then
  ok "ccr-git helper installed (sessions can clone/pull your repos)"
else
  warn "ccr-git helper not installed" "re-run bootstrap.sh after scripts/sync.sh"
fi
command -v bwrap >/dev/null && ok "bubblewrap present (sandbox available)" \
  || warn "bubblewrap missing" "apt-get install -y bubblewrap socat"

head_ "4. Memory headroom"
if swapon --show 2>/dev/null | grep -q .; then ok "swap active ($(swapon --show=SIZE --noheadings | head -1 | tr -d ' '))"
else warn "no swap" "a build that spikes will be OOM-killed instead of getting slow"; fi
echo "     RAM: $(free -h | awk '/^Mem:/{print $2" total, "$7" available"}')"

head_ "5. The service answers"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null)
[ "$CODE" = "200" ] && ok "orchestrator answers on 127.0.0.1:$PORT" \
  || bad "orchestrator not answering (HTTP $CODE)" "journalctl -u ccremote -n 40"
if [ -n "${CCR_TOKEN:-}" ]; then
  C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "X-Token: $CCR_TOKEN" "http://127.0.0.1:$PORT/api/config")
  [ "$C" = "200" ] && ok "token authentication works" || bad "auth failed (HTTP $C)" "check CCR_TOKEN in $ENV_FILE"
  U=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/api/config")
  [ "$U" = "401" ] && ok "unauthenticated requests are rejected" || bad "API not protected (HTTP $U)" "CCR_TOKEN must be set"
else warn "CCR_TOKEN not readable here" "run as root, or export it first"; fi

head_ "6. Your project folders exist ON THIS MACHINE"
TASKS=${CCR_TASKS:-/opt/ccremote/orchestrator/tasks.yaml}
# PyYAML lives in the orchestrator's venv, not necessarily the system python.
PY=/opt/ccremote/.venv/bin/python; [ -x "$PY" ] || PY=python3
if [ -r "$TASKS" ]; then
  "$PY" - "$TASKS" <<'PY'
import os,sys,yaml
d=yaml.safe_load(open(sys.argv[1])) or {}
projs=(d.get("projects") or {})
if not projs: print("     (no projects configured)")
for k,v in projs.items():
    p=os.path.expanduser(os.path.expandvars(str((v or {}).get("path",""))))
    print(("  \033[32mPASS\033[0m  " if os.path.isdir(p) else "  \033[31mFAIL\033[0m  ")+f"{k}: {p}")
    if not os.path.isdir(p):
        print("     -> clone it here; folders on your laptop do not exist when your laptop is off")
PY
else warn "tasks.yaml not readable at $TASKS" "set CCR_TASKS"; fi

printf '\n\033[1m%d passed, %d failed, %d warnings\033[0m\n' "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -eq 0 ]; then
  printf '\nNow the only test that counts:\n  1. sudo reboot\n  2. close your laptop completely\n  3. from your phone, wifi OFF, cellular only, open the https://...ts.net URL and send a prompt\n\n'
fi
exit $(( FAIL > 0 ? 1 : 0 ))
