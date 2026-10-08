#!/bin/sh
# Hand the primary role over before this pod stops; valkey has no failover on shutdown
# outside cluster mode. Never blocks shutdown: always exits 0.
NAME=authelia
FQDN="${HOSTNAME}.valkey-headless.authelia.svc.cluster.local"
S="valkey-cli -p 26379"
V="valkey-cli -p 6379"
primary() { timeout 3 $S sentinel get-master-addr-by-name "$NAME" 2>/dev/null | head -1 | tr -d '\r'; }

[ "$(primary)" = "$FQDN" ] || exit 0
timeout 3 $V info replication | tr -d '\r' | grep -q '^connected_slaves:[1-9]' || exit 0

ver=$(timeout 3 $V info server | tr -d '\r' | sed -n 's/^valkey_version:\(.*\)/\1/p')
echo "preStop: $FQDN is primary (valkey $ver), handing over"
if [ "$(printf '%s\n9.0.5\n' "$ver" | sort -V | head -1)" = "9.0.5" ]; then
  timeout 3 $S sentinel failover "$NAME" coordinated
else
  timeout 3 $V client pause 20000 write
  timeout 3 $S sentinel failover "$NAME"
fi

i=0
while [ "$i" -lt 40 ]; do
  p=$(primary)
  if [ -n "$p" ] && [ "$p" != "$FQDN" ]; then
    echo "preStop: new primary $p"
    timeout 3 $V client unpause >/dev/null 2>&1
    exit 0
  fi
  sleep 0.5
  i=$((i + 1))
done
echo "preStop: no handover within 20s"
timeout 3 $V client unpause >/dev/null 2>&1
exit 0
