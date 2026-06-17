#!/bin/bash
set -e

echo "[start.sh] Запуск ImgUniq..."

# ── WireGuard ──────────────────────────────────────────────────────────────────
WG_CONF="/app/wg0.conf"

if [ -f "$WG_CONF" ]; then
    echo "[start.sh] Поднимаем WireGuard туннель..."

    # Устанавливаем wireguard-tools если нет
    if ! command -v wg-quick &>/dev/null; then
        echo "[start.sh] Устанавливаем wireguard-tools..."
        apt-get update -qq && apt-get install -y -qq wireguard-tools iproute2 iptables 2>/dev/null || true
    fi

    # Включаем ip_forward
    echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null || true

    # Поднимаем туннель
    wg-quick up "$WG_CONF" 2>&1 || {
        echo "[start.sh] wg-quick up упал, пробуем вручную..."
        
        WG_IF="wg0"
        PRIVATE_KEY=$(grep PrivateKey "$WG_CONF" | awk '{print $3}')
        PEER_PUB=$(grep -A5 "\[Peer\]" "$WG_CONF" | grep PublicKey | awk '{print $3}')
        ENDPOINT=$(grep Endpoint "$WG_CONF" | awk '{print $3}')
        ALLOWED=$(grep AllowedIPs "$WG_CONF" | awk '{print $3,$4}' | tr -d ' ')
        ADDRESS=$(grep Address "$WG_CONF" | awk '{print $3}')
        MTU=$(grep MTU "$WG_CONF" | awk '{print $3}')
        KEEPALIVE=$(grep PersistentKeepalive "$WG_CONF" | awk '{print $3}')

        ip link add dev $WG_IF type wireguard 2>/dev/null || true
        echo "$PRIVATE_KEY" | wg set $WG_IF private-key /dev/stdin
        wg set $WG_IF peer "$PEER_PUB" \
            endpoint "$ENDPOINT" \
            allowed-ips "0.0.0.0/0,::/0" \
            persistent-keepalive "${KEEPALIVE:-20}"
        ip addr add "$ADDRESS" dev $WG_IF 2>/dev/null || true
        ip link set mtu "${MTU:-1298}" up dev $WG_IF
        ip route add default dev $WG_IF 2>/dev/null || true
        echo "[start.sh] WireGuard поднят вручную"
    }

    # Ждём чуть-чуть пока туннель установится
    sleep 2
    echo "[start.sh] WireGuard статус:"
    wg show 2>/dev/null || true
else
    echo "[start.sh] WireGuard конфиг не найден ($WG_CONF), пропускаем."
fi

# ── Gunicorn ───────────────────────────────────────────────────────────────────
echo "[start.sh] Запускаем gunicorn..."
exec gunicorn main:app \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers 2 \
    --timeout 240 \
    --access-logfile - \
    --error-logfile -
