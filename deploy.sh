#!/usr/bin/env bash
# Bootstraps the web dashboard on a fresh Debian/Ubuntu machine (e.g. a rented cloud GPU/CPU box).
#
# Usage (from inside a checkout):
#   ./deploy.sh [--port=8080] [--tailscale-key=tskey-...] [--hostname=av1-encoder]
# Or let the script clone the repository first:
#   ./deploy.sh --repo=https://github.com/<user>/<repo>.git
set -euo pipefail

PORT=8080
TS_KEY=""
TS_HOSTNAME="av1-encoder"
REPO_URL=""

for arg in "$@"; do
  case $arg in
    --port=*)          PORT="${arg#*=}" ;;
    --tailscale-key=*) TS_KEY="${arg#*=}" ;;
    --hostname=*)      TS_HOSTNAME="${arg#*=}" ;;
    --repo=*)          REPO_URL="${arg#*=}" ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo &> /dev/null; then
    SUDO="sudo"
fi

echo "[1/4] Installing system packages (FFmpeg, MKVToolNix, Python)..."
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq ffmpeg mkvtoolnix python3 python3-venv python3-pip git curl

if [ ! -f "web_app.py" ]; then
    if [ -z "$REPO_URL" ]; then
        echo "Run this script from the project folder, or pass --repo=<git url> to clone it." >&2
        exit 1
    fi
    DIR="$(basename "$REPO_URL" .git)"
    [ -d "$DIR" ] || git clone "$REPO_URL" "$DIR"
    cd "$DIR"
fi

if [ -n "$TS_KEY" ]; then
    echo "[2/4] Joining Tailscale network as '$TS_HOSTNAME'..."
    if ! command -v tailscale &> /dev/null; then
        curl -fsSL https://tailscale.com/install.sh | $SUDO sh
    fi
    $SUDO tailscale up --authkey="$TS_KEY" --hostname="$TS_HOSTNAME"
else
    echo "[2/4] Skipping Tailscale (no --tailscale-key given)."
fi

echo "[3/4] Creating Python virtual environment..."
[ -d ".venv" ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "[4/4] Starting web dashboard on port $PORT..."
if [ -n "$TS_KEY" ]; then
    echo "Dashboard: http://$TS_HOSTNAME:$PORT (Tailscale)"
fi
echo "Dashboard: http://localhost:$PORT"
# Binds to all interfaces so the dashboard is reachable remotely. The app has no
# authentication, so only expose it on a private network such as a Tailscale tailnet.
exec python3 web_app.py --host 0.0.0.0 --port "$PORT"
