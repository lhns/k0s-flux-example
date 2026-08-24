#!/bin/sh
set -eu
NAME=authelia
HEADLESS=valkey-headless.authelia.svc.cluster.local
FQDN="${HOSTNAME}.${HEADLESS}"
CONF=/data/sentinel.conf
# Write the config ONCE (first boot) so Sentinel keeps its persisted identity (myid)
# and — after any failover — the current master it rewrote into this file. On first
# bootstrap the master is valkey-0 (see start-valkey.sh); Sentinel then tracks reality
# via gossip. announce-hostnames so failovers advertise stable FQDNs, not dead IPs.
if [ ! -f "$CONF" ]; then
  cat > "$CONF" <<EOF
port 26379
dir /data
protected-mode no
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
sentinel announce-ip ${FQDN}
sentinel monitor ${NAME} valkey-0.${HEADLESS} 6379 2
sentinel down-after-milliseconds ${NAME} 5000
sentinel failover-timeout ${NAME} 15000
sentinel parallel-syncs ${NAME} 1
EOF
fi
exec valkey-sentinel "$CONF"
