#!/bin/sh
set -eu
HEADLESS=valkey-headless.authelia.svc.cluster.local
FQDN="${HOSTNAME}.${HEADLESS}"
CONF=/data/valkey.conf
cat > "$CONF" <<EOF
bind 0.0.0.0
protected-mode no
port 6379
dir /data
appendonly yes
appendfsync everysec
save 60 1000
replica-announce-ip ${FQDN}
replica-announce-port 6379
EOF
# pod-0 bootstraps as master; others start as replicas of pod-0. After the first
# election Sentinel owns the master role and REPLICAOFs any restarted pod to the
# current master at runtime, so a stale replicaof self-heals in seconds (clients
# always reach the true master via Sentinel).
if [ "$HOSTNAME" != "valkey-0" ]; then
  echo "replicaof valkey-0.${HEADLESS} 6379" >> "$CONF"
fi
exec valkey-server "$CONF"
