#!/usr/bin/env bash
set -euo pipefail

REPO="Aniket-think41/skillshare-mcp"
VERSION="${1:-latest}"

# Detect OS/arch
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"

case "$OS" in
  linux)  TARGET="linux-amd64" ;;
  darwin) TARGET="macos-arm64" ;;
  mingw*|msys*|cygwin*) TARGET="windows-amd64.exe" ;;
  *) echo "Unsupported OS: $OS"; exit 1 ;;
esac

if [ "$VERSION" = "latest" ]; then
  URL="https://github.com/$REPO/releases/latest/download/skillshare-mcp-$TARGET"
else
  URL="https://github.com/$REPO/releases/download/$VERSION/skillshare-mcp-$TARGET"
fi

echo "Downloading skillshare-mcp for $OS/$ARCH..."
curl -fsSL "$URL" -o /tmp/skillshare-mcp
chmod +x /tmp/skillshare-mcp
sudo mv /tmp/skillshare-mcp /usr/local/bin/skillshare-mcp
echo "Installed skillshare-mcp to /usr/local/bin/skillshare-mcp"
