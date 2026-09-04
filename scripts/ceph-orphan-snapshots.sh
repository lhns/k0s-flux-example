#!/usr/bin/env bash
# Reclaim ceph-csi snapshot clones that no Kubernetes object references any more.
#
# These exist because both VolumeSnapshotClasses were deletionPolicy: Retain until eec87e8.
# Under Retain, deleting a VolumeSnapshot keeps the Ceph snapshot and nothing reaps it --
# pv-reaper watches PVs, not VolumeSnapshotContents.
#
# Layout, which the deletion order follows: ceph-csi stores a snapshot as a clone of a
# snapshot on the source volume, and that clone carries a snapshot of its own with the same
# name (csi-snap-X@csi-snap-X). `rbd rm` refuses while that exists, so it is purged first.
# Removing the clone then releases its parent on the source volume, which Ceph holds in the
# trash namespace -- invisible to `rbd snap ls` (use --all) and reported as 0 B by `rbd du`.
# Judge the result by `ceph df`, not per-image numbers.
#
# Read-only by default. Nothing is deleted without --apply.
set -euo pipefail

CEPH_HOST="${CEPH_HOST:-root@10.20.2.101}"
POOL="${POOL:-rbd.kube}"
RADOS_NS="${RADOS_NS:-kube}"
APPLY=0

case "${1:-}" in
  --apply) APPLY=1 ;;
  -h|--help) echo "usage: $0 [--apply]"; exit 0 ;;
  "") ;;
  *) echo "usage: $0 [--apply]"; exit 2 ;;
esac

# -n keeps ssh off our stdin, which matters inside loops. ceph_pipe is the exception: it
# feeds a script to `bash -s`, so it must NOT have -n or the remote reads an empty script.
ceph_cmd()  { ssh -n -o BatchMode=yes "$CEPH_HOST" "$@"; }
ceph_pipe() { ssh -o BatchMode=yes "$CEPH_HOST" "$@"; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "pool $POOL (rados namespace $RADOS_NS) on $CEPH_HOST"
[ "$APPLY" -eq 1 ] && echo "MODE: APPLY -- deletions are permanent" || echo "MODE: dry run"
echo

# One listing gives every image, its snapshots and its parent. Doing this once matters:
# `rbd children` scans the pool per call, so asking per-image is O(images^2) and does not
# finish on a pool with ~1000 images.
echo "listing pool (one pass)..."
ceph_cmd "rbd ls -l -p $POOL --namespace $RADOS_NS --format json" > "$tmp/ls.json"

# Snapshot UUIDs Kubernetes still references. An RBD CSI handle ends in the image UUID.
kubectl get volumesnapshotcontent -o jsonpath='{range .items[*]}{.status.snapshotHandle}{"\n"}{end}' \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' \
  | sort -u > "$tmp/live.txt"

python3 - "$tmp/ls.json" "$tmp/live.txt" "$tmp/orphans.txt" <<'PY'
import json, re, sys
entries = json.load(open(sys.argv[1]))
live = {l.strip() for l in open(sys.argv[2]) if l.strip()}

# Anything that is somebody's parent is still depended on -- a restored PVC, typically.
parents = set()
for e in entries:
    p = e.get('parent')
    if p:
        parents.add(p.get('image'))

images, sizes = set(), {}
for e in entries:
    if e.get('snapshot'):
        continue
    n = e['image']
    if n.startswith('csi-snap-'):
        images.add(n)
        sizes[n] = e.get('size', 0)

pat = re.compile(r'^csi-snap-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
orphans, skipped = [], []
for n in sorted(images):
    if not pat.match(n):            # never act on an unexpected name
        continue
    if n[len('csi-snap-'):] in live:
        continue
    if n in parents:                # has children; something restored from it
        skipped.append(n)
        continue
    orphans.append(n)

with open(sys.argv[3], 'w') as f:
    for n in orphans:
        f.write(n + '\n')

print(f"  clones in Ceph:            {len(images)}")
print(f"  referenced by Kubernetes:  {len(live)}")
print(f"  skipped (have children):   {len(skipped)}")
print(f"  orphaned:                  {len(orphans)}")
print(f"  their provisioned total:   {sum(sizes[n] for n in orphans)/1024**3:.1f} GiB")
for n in skipped[:10]:
    print(f"    SKIP {n}")
PY

count=$(wc -l < "$tmp/orphans.txt")
[ "$count" -eq 0 ] && { echo; echo "nothing to do"; exit 0; }

stored() {
  ceph_cmd "ceph df --format json" | python3 -c "
import json,sys
for p in json.load(sys.stdin)['pools']:
    if p['name']=='$POOL': print(p['stats']['stored'])
"
}

before=$(stored)
echo
echo "  $POOL stored before: $((before / 1024 / 1024 / 1024)) GiB"
echo

if [ "$APPLY" -eq 0 ]; then
  echo "  would delete $count clones. First 10:"
  head -10 "$tmp/orphans.txt" | sed 's/^/    /'
  echo
  echo "  re-run with --apply"
  exit 0
fi

# The whole loop runs in ONE ssh session: per-image round trips would be ~5000 calls.
# csi.snapname must be read before its metadata object is removed -- it is the key in
# csi.snaps.default. Failures leave the image intact and move on.
{
  printf "POOL=%q\nNS=%q\n" "$POOL" "$RADOS_NS"
  cat <<'REMOTE'
set -u
ok=0; fail=0
process_one() {
  img="$1"
  uuid="${img#csi-snap-}"
  snapname=$(rados -p "$POOL" --namespace "$NS" getomapval "csi.snap.$uuid" csi.snapname /dev/stdout 2>/dev/null \
    | grep -oE 'snapshot-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' || true)
  rbd snap unprotect "$POOL/$img@$img" --namespace "$NS" >/dev/null 2>&1 || true
  rbd snap purge "$POOL/$img" --namespace "$NS" >/dev/null 2>&1 || true
  if rbd rm "$POOL/$img" --namespace "$NS" >/dev/null 2>&1; then
    rados -p "$POOL" --namespace "$NS" rm "csi.snap.$uuid" >/dev/null 2>&1 || true
    [ -n "$snapname" ] && rados -p "$POOL" --namespace "$NS" rmomapkey csi.snaps.default "csi.snap.$snapname" >/dev/null 2>&1 || true
    ok=$((ok+1))
    [ $((ok % 25)) -eq 0 ] && echo "  deleted $ok..."
  else
    fail=$((fail+1))
    echo "  FAILED (left intact): $img"
  fi
}
REMOTE
  # The list travels inside the script: `bash -s` already owns stdin.
  echo "while read -r img; do [ -n \"\$img\" ] && process_one \"\$img\"; done <<'IMGLIST'"
  cat "$tmp/orphans.txt"
  echo "IMGLIST"
  echo 'echo "  deleted $ok, failed $fail"'
} | ceph_pipe "bash -s" || true

after=$(stored)
echo
echo "  $POOL stored after: $((after / 1024 / 1024 / 1024)) GiB"
echo "  reclaimed: $(( (before - after) / 1024 / 1024 / 1024 )) GiB stored (x3 raw)"
echo
echo "Parent trash snapshots are released as their last child goes. Confirm with:"
echo "  rbd snap ls $POOL/<csi-vol-...> --namespace $RADOS_NS --all"
