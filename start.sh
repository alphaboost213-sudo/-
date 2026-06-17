#!/bin/bash
set -e

echo "[start.sh] Запуск ImgUniq..."

# ── WireGuard ──────────────────────────────────────────────────────────────────
WG_CONF="/app/wg0.conf"

if [ -f "$WG_CONF" ]; then
    echo "[start.sh] Устанавливаем wireguard-tools через apt..."
    apt-get update -qq 2>/dev/null && apt-get install -y -qq wireguard-tools iproute2 iptables 2>/dev/null || {
        echo "[start.sh] apt недоступен, пробуем через pip-free установку..."
    }

    echo "[start.sh] Поднимаем WireGuard туннель..."
    echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null || true

    wg-quick up "$WG_CONF" 2>&1 || {
        echo "[start.sh] wg-quick up упал, пробуем вручную через ip/wg..."

        WG_IF="wg0"
        PRIVATE_KEY=$(grep PrivateKey "$WG_CONF" | awk '{print $3}')
        PEER_PUB=$(grep -A10 "\[Peer\]" "$WG_CONF" | grep PublicKey | awk '{print $3}')
        ENDPOINT=$(grep Endpoint "$WG_CONF" | awk '{print $3}')
        ADDRESS=$(grep Address "$WG_CONF" | awk '{print $3}')
        MTU=$(grep MTU "$WG_CONF" | awk '{print $3}')
        KEEPALIVE=$(grep PersistentKeepalive "$WG_CONF" | awk '{print $3}')

        ip link add dev $WG_IF type wireguard 2>/dev/null || true
        echo "$PRIVATE_KEY" | wg set $WG_IF private-key /dev/stdin
        wg set $WG_IF peer "$PEER_PUB" \
            endpoint "$ENDPOINT" \
            allowed-ips "0.0.0.0/0,::/0" \
            persistent-keepalive "${KEEPALIVE:-20}"
        ip addr add "${ADDRESS}/32" dev $WG_IF 2>/dev/null || true
        ip link set mtu "${MTU:-1298}" up dev $WG_IF
        echo "[start.sh] WireGuard поднят вручную"
    }

    sleep 2
    echo "[start.sh] WireGuard статус:"
    wg show 2>/dev/null || true
else
    echo "[start.sh] wg0.conf не найден, пропускаем WireGuard"
fi

# ── Gunicorn ───────────────────────────────────────────────────────────────────
echo "[start.sh] Запускаем gunicorn..."
exec gunicorn main:app \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers 2 \
    --timeout 240 \
    --access-logfile - \
    --error-logfile -
