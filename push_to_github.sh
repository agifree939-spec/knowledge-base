#!/bin/bash
set -e

TOKEN_FILE="/tmp/gh_token.txt"
TOKEN=*** "$TOKEN_FILE" | tr -d '\n')
REPO="agifree939-spec/knowledge-base"

echo "=== Step 1: Create repo on GitHub ==="
HTTP_CODE=$(curl -s -o /tmp/gh_create_response.json -w "%{http_code}" \
  -H "Authorization: Bearer *** \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/user/repos \
  -d '{"name":"knowledge-base","description":"Personal knowledge base for tweets and articles","private":false,"auto_init":false}')

echo "Create repo HTTP: $HTTP_CODE"
cat /tmp/gh_create_response.json | python3 -m json.tool 2>/dev/null | head -10

echo ""
echo "=== Step 2: Push code ==="
cd /opt/data/projects/knowledge-base

git remote remove origin 2>/dev/null || true
git remote add origin "https://agifree939-spec:${TOKEN}@github.com/${REPO}.git"
git branch -M main
git push -u origin main --force 2>&1

echo ""
echo "=== DONE ==="
