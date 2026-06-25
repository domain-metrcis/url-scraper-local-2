#!/usr/bin/env bash
# One-time setup: download ungoogled-chromium and clone cf-autoclick into vendor/.
# Idempotent — re-running is safe and fast.
#
# Usage:
#   bash tools/setup_vendor.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
VENDOR_DIR="${PROJECT_DIR}/vendor"

# ─── Pinned versions ────────────────────────────────────────────────────────
CHROMIUM_VERSION="149.0.7827.53-1"
CHROMIUM_TARBALL_URL="https://github.com/ungoogled-software/ungoogled-chromium-portablelinux/releases/download/${CHROMIUM_VERSION}/ungoogled-chromium-${CHROMIUM_VERSION}-x86_64_linux.tar.xz"
CHROMIUM_TARBALL="${VENDOR_DIR}/ungoogled-chromium.tar.xz"
CHROMIUM_DIR="${VENDOR_DIR}/ungoogled-chromium"
CHROMIUM_INNER="${VENDOR_DIR}/ungoogled-chromium-${CHROMIUM_VERSION}-x86_64_linux"

EXTENSION_URL="https://github.com/tenacious6/cf-autoclick.git"
EXTENSION_DIR="${VENDOR_DIR}/cf-autoclick"

mkdir -p "${VENDOR_DIR}"

# ─── 1. ungoogled-chromium binary ───────────────────────────────────────────
if [[ -x "${CHROMIUM_DIR}/chrome" ]]; then
  echo "[*] ungoogled-chromium already present — skipping"
else
  if [[ ! -f "${CHROMIUM_TARBALL}" ]]; then
    echo "[*] Downloading ungoogled-chromium ${CHROMIUM_VERSION}..."
    curl -fL --progress-bar -o "${CHROMIUM_TARBALL}" "${CHROMIUM_TARBALL_URL}"
  fi

  echo "[*] Extracting tarball..."
  tar -xJf "${CHROMIUM_TARBALL}" -C "${VENDOR_DIR}"

  if [[ ! -d "${CHROMIUM_INNER}" ]]; then
    echo "❌ Expected dir ${CHROMIUM_INNER} not found." >&2; exit 1
  fi

  mv "${CHROMIUM_INNER}" "${CHROMIUM_DIR}"
  chmod +x "${CHROMIUM_DIR}/chrome" "${CHROMIUM_DIR}/chromedriver" 2>/dev/null || true

  "${CHROMIUM_DIR}/chrome" --version || {
    echo "❌ chrome --version failed. Install missing libs:" >&2
    echo "  sudo apt install -y libnss3 libatk-bridge2.0-0 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64" >&2
    exit 1
  }
  echo "✅ Chromium ready at ${CHROMIUM_DIR}/chrome"
fi

# ─── 2. cf-autoclick extension ──────────────────────────────────────────────
if [[ -f "${EXTENSION_DIR}/manifest.json" ]]; then
  echo "[*] cf-autoclick already present — skipping"
else
  echo "[*] Cloning cf-autoclick..."
  git clone --depth 1 "${EXTENSION_URL}" "${EXTENSION_DIR}"
  [[ -f "${EXTENSION_DIR}/manifest.json" ]] || { echo "❌ Extension missing manifest.json" >&2; exit 1; }
  echo "✅ Extension ready at ${EXTENSION_DIR}"
fi

# ─── Done ───────────────────────────────────────────────────────────────────
echo ""
echo "✅ VENDOR SETUP COMPLETE"
echo "  Chromium:  ${CHROMIUM_DIR}/chrome"
echo "  Extension: ${EXTENSION_DIR}"
echo ""
echo "Run: python3 url_scraper.py --workers 1"
