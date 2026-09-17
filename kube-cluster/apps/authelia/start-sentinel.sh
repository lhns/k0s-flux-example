#!/bin/sh
set -eu
NAME=authelia
HEADLESS=valkey-headless.authelia.svc.cluster.local
FQDN="${HOSTNAME}.${HEADLESS}"
CONF=/data/sentinel.conf
REPLICAS=3

# The config is REBUILT on every start, deliberately, and the monitored master is
# rediscovered rather than remembered.
#
# It used to be written once and left to gossip. That cannot self-heal: a sentinel
# adopts a peer's view only at a strictly HIGHER config epoch, so if a disrupted
# failover leaves two sentinels naming different masters at the SAME epoch, they
# disagree forever. The state sits on each sentinel's PVC, so it survives restarts
# too. That happened: one sentinel kept naming a demoted replica as master, and the
# Authelia pod sharing its node got that answer every time and crash-looped on
# "READONLY You can't write against a read only replica" for 27 hours.
#
# Rebuilding also resets current-epoch to 0, which is what makes a restarted
# sentinel accept the majority's higher-epoch config instead of fighting it.

# Only `sentinel myid` is carried across. Losing it would make the group treat this
# pod as a brand new sentinel on every restart while the old ids linger as
# known-sentinel entries, and a majority is counted over known sentinels -- enough
# phantoms and no failover can be authorised at all.
MYID=""
if [ -f "$CONF" ]; then
  MYID=$(sed -n 's/^sentinel myid \([0-9a-f]*\).*/\1/p' "$CONF" | head -1)
fi

# Ask the data nodes who is master, and believe them: role:master is ground truth,
# where any sentinel's answer is only an opinion. Fall back to a peer sentinel, then
# to valkey-0, which start-valkey.sh bootstraps as master on a cold start.
ask() { _h=$1; _p=$2; shift 2; timeout 3 valkey-cli -h "$_h" -p "$_p" "$@" 2>/dev/null || true; }
find_master() {
  i=0
  while [ "$i" -lt "$REPLICAS" ]; do
    h="valkey-${i}.${HEADLESS}"
    if ask "$h" 6379 info replication | tr -d '\r' | grep -qx 'role:master'; then
      echo "$h"; return
    fi
    i=$(( i + 1 ))
  done
  i=0
  while [ "$i" -lt "$REPLICAS" ]; do
    h="valkey-${i}.${HEADLESS}"
    if [ "$h" != "$FQDN" ]; then
      m=$(ask "$h" 26379 sentinel get-master-addr-by-name "$NAME" | head -1 | tr -d '\r')
      case "$m" in valkey-*) echo "$m"; return;; esac
    fi
    i=$(( i + 1 ))
  done
  echo "valkey-0.${HEADLESS}"
}
MASTER=$(find_master)
echo "sentinel: monitoring ${NAME} at ${MASTER}"

cat > "$CONF" <<CONFEOF
port 26379
dir /data
protected-mode no
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
sentinel announce-ip ${FQDN}
sentinel monitor ${NAME} ${MASTER} 6379 2
sentinel down-after-milliseconds ${NAME} 5000
sentinel failover-timeout ${NAME} 15000
sentinel parallel-syncs ${NAME} 1
CONFEOF
if [ -n "$MYID" ]; then echo "sentinel myid ${MYID}" >> "$CONF"; fi

exec valkey-sentinel "$CONF"
