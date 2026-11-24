#!/usr/bin/env bash
# Idempotently create phylipppp-zzy/daily-radar, enable GitHub Actions Pages, push main, and verify HTTP 200.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_SLUG="${DAILY_RADAR_REPO_SLUG:-phylipppp-zzy/daily-radar}"
PUBLIC_URL="${DAILY_RADAR_PUBLIC_URL:-https://phylipppp-zzy.github.io/daily-radar}"
SSH_REMOTE="ssh://git@ssh.github.com:443/${REPO_SLUG}.git"

cd "${REPO_DIR}"

echo "[1/6] Running pre-publish secret & zero-binary scans..."
python3 -m daily_radar scan-secrets --target "${REPO_DIR}" --include-tests
if [[ -d "${REPO_DIR}/_site" ]]; then
  python3 -m daily_radar scan-secrets --target "${REPO_DIR}/_site"
fi

echo "[2/6] Ensuring public GitHub repository ${REPO_SLUG} exists..."
if gh repo view "${REPO_SLUG}" >/dev/null 2>&1; then
  echo "  Repository ${REPO_SLUG} already exists."
else
  gh repo create "${REPO_SLUG}" \
    --public \
    --description "Daily Research & Industry Radar (GUI-RL · OSSL/OOD · Multimodal & RL Infra)" \
    --homepage "${PUBLIC_URL}/"
fi

echo "[3/6] Configuring git remote origin -> ${SSH_REMOTE}..."
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "${SSH_REMOTE}"
else
  git remote add origin "${SSH_REMOTE}"
fi

echo "[4/6] Pushing main branch to ${REPO_SLUG}..."
pushed=0
for attempt in 1 2 3 4; do
  if git push -u origin main; then
    pushed=1
    break
  fi
  echo "  git push attempt ${attempt} failed; retrying in $((attempt * 5))s..." >&2
  sleep $((attempt * 5))
done
if [[ "${pushed}" -ne 1 ]]; then
  echo "ERROR: Failed to push main branch after 4 attempts." >&2
  exit 1
fi

echo "[5/6] Configuring GitHub Pages source = GitHub Actions (build_type=workflow)..."
if gh api "repos/${REPO_SLUG}/pages" >/dev/null 2>&1; then
  current_type="$(gh api "repos/${REPO_SLUG}/pages" --jq '.build_type // ""')"
  if [[ "${current_type}" != "workflow" ]]; then
    gh api "repos/${REPO_SLUG}/pages" -X PUT -f build_type=workflow >/dev/null
  fi
else
  gh api "repos/${REPO_SLUG}/pages" -X POST -f build_type=workflow >/dev/null
  sleep 2
  gh workflow run deploy-pages.yml --repo "${REPO_SLUG}"
fi

echo "[6/6] Waiting for GitHub Pages (${PUBLIC_URL}/) to return HTTP 200..."
for i in $(seq 1 24); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "${PUBLIC_URL}/" || true)"
  if [[ "${code}" == "200" ]]; then
    echo "OK: Live at ${PUBLIC_URL}/"
    exit 0
  fi
  sleep 10
done

echo "WARN: Pushed & enabled GitHub Pages, but ${PUBLIC_URL}/ is still building (last HTTP ${code:-000}). Check: gh run list --repo ${REPO_SLUG}" >&2
exit 0
