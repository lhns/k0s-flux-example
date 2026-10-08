#!/usr/bin/env python3
"""Model the generator's Helm release Secret and fail before it hits the apiserver cap.

Helm stores a release as base64(gzip(JSON)) in a Secret, and the release embeds the whole
chart file by file. The apiserver caps a Secret at 1 MiB; there is no flag that raises it.
This once reached 99.1% and froze every generated Kustomization with no warning.

Models each cluster's ROOT release: the chart files its root ArtifactGenerator
(scripts/clusters.py) copies, plus the rendered manifest.
Per-component releases are bounded by one component and are not the risk.
"""
import base64, gzip, json, pathlib, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import clusters

LIMIT = 1048576
BUDGET = 0.70
# Model vs the live release Secret, measured against a known-good reconcile. The model
# omits Helm's per-release metadata, which is constant and does not scale with components.
CALIBRATION = 1.047

root = clusters.ROOT


def chart_files(cluster, tracked):
    """What the cluster's root ArtifactGenerator copies, read from its copy rules."""
    path, gen = clusters.root_artifact(cluster)
    if gen is None or len(gen["spec"]["artifacts"]) != 1:
        sys.exit(f"{cluster}/flux-system: expected a root ArtifactGenerator with one artifact")
    return sorted(clusters.artifact(gen["spec"]["artifacts"][0], tracked).values())


def check(cluster, tracked):
    # Values come from the live HelmRelease so this cannot drift from what is deployed.
    values = json.dumps(clusters.values(cluster))
    manifest = subprocess.run(
        ["helm", "template", "generators", ".", "-f", "-"],
        cwd=root, input=values, capture_output=True, text=True,
    )
    if manifest.returncode != 0:
        print(manifest.stderr[-2000:], file=sys.stderr)
        sys.exit(f"{cluster}: could not render the generator chart")

    buf = bytearray()
    files = chart_files(cluster, tracked)
    for f in files:
        buf += b'{"name":"' + f.encode() + b'","data":"'
        buf += base64.b64encode((root / f).read_bytes())
        buf += b'"},'
    buf += manifest.stdout.encode()

    stored = int(len(gzip.compress(bytes(buf), 9)) * 4 / 3 * CALIBRATION)
    pct = stored * 100.0 / LIMIT
    print(f"{cluster}: generator release ~{stored} bytes, {pct:.1f}% of 1 MiB ({len(files)} chart files)")
    if stored > LIMIT * BUDGET:
        sys.exit(
            f"{cluster}: generator release is at {pct:.1f}% of the 1 MiB Secret cap (budget {BUDGET:.0%}).\n"
            "  It fails silently at 100%: the release stops applying and every generated\n"
            "  Kustomization freezes at its last content. See docs/generator-chart-size.md."
        )


tracked = clusters.tracked()
for c in clusters.clusters():
    if clusters.root_release(c)[1] is not None:
        check(c, tracked)
