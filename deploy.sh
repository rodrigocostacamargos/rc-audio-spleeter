#!/usr/bin/env bash
# deploy.sh — Envia o código local do rc-audio-spleeter para o Hetzner e reinicia o container.
set -euo pipefail

SERVER_IP="91.99.194.219"
SERVER_USER="root"
REMOTE_DIR="/root/spleeter"
SSH_KEY="$HOME/.ssh/hetzner_id_rsa"
SSH_OPTS="-i $SSH_KEY -o ConnectTimeout=10 -o StrictHostKeyChecking=no"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
echo "==> Verificando conexão SSH..."
ssh $SSH_OPTS "$SERVER_USER@$SERVER_IP" "echo 'OK'" > /dev/null

# ---------------------------------------------------------------------------
echo "==> Empacotando código..."
TAR="spleeter-$(date +%Y%m%d-%H%M%S).tar.gz"
tar -czf "$TAR" -C "$SCRIPT_DIR" \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.venv' \
    --exclude='stems' \
    --exclude='musicas' \
    --exclude='output' \
    --exclude='logs' \
    --exclude='.env' \
    --exclude='*.tar.gz' \
    --exclude='.pytest_cache' \
    . || true   # ignora aviso "file changed as we read it"
echo "   Package: $TAR ($(du -h "$TAR" | cut -f1))"

# ---------------------------------------------------------------------------
echo "==> Enviando para o servidor..."
scp $SSH_OPTS "$TAR" "$SERVER_USER@$SERVER_IP:/tmp/$TAR"
rm -f "$TAR"

# ---------------------------------------------------------------------------
echo "==> Extraindo e rebuilding no servidor..."
ssh $SSH_OPTS "$SERVER_USER@$SERVER_IP" bash -s << EOF
    set -e
    mkdir -p $REMOTE_DIR
    tar -xzf /tmp/$TAR -C $REMOTE_DIR
    rm -f /tmp/$TAR
    cd $REMOTE_DIR
    docker compose build
    docker compose up -d
    echo ""
    docker compose ps
EOF

# ---------------------------------------------------------------------------
echo ""
echo "==> Deploy concluído. Aguardando startup..."
sleep 3
curl -sf --max-time 10 "http://$SERVER_IP:8001/" > /dev/null \
    && echo "    http://$SERVER_IP:8001/ respondendo OK" \
    || echo "    AVISO: servidor ainda não respondeu (pode estar iniciando)"
