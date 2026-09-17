#!/usr/bin/env python3
"""Model the generator's Helm release Secret and fail before it hits the apiserver cap.

Helm stores a release as base64(gzip(JSON)) in a Secret, and the release embeds the whole
chart file by file. The apiserver caps a Secret at 1 MiB; there is no flag that raises it.
This once reached 99.1% and froze every generated Kustomization with no warning.

Models the ROOT release: the chart files its GitRepository admits, plus the rendered
manifest. Per-component releases are bounded by one component and are not the risk.
"""
import base64, gzip, pathlib, re, subprocess, sys

LIMIT = 1048576
BUDGET = 0.70
# Model vs the live release Secret, measured against a known-good reconcile. The model
# omits Helm's per-release metadata, which is constant and does not scale with components.
CALIBRATION = 1.047

root = pathlib.Path(__file__).resolve().parent.parent

def tracked():
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True)
    return out.stdout.splitlines()

# Mirror of the `ignore` on the generators GitRepository. Keep the two in step: a path
# admitted there and not here is measured as free.
KEEP = re.compile(r"(^Chart\.yaml$)|(^templates/)|((^|/)k0sctl\.yaml$)|((^|/)kustomization\.yaml$)")

def main():
    # Values come from the live HelmRelease so this cannot drift from what is deployed.
    import json, yaml
    rel = None
    for doc in yaml.safe_load_all((root / "kube-cluster/flux-system/generators.yaml").read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "HelmRelease":
            rel = doc
    if rel is None:
        sys.exit("no HelmRelease in kube-cluster/flux-system/generators.yaml")
    values = json.dumps(rel["spec"]["values"])
    manifest = subprocess.run(
        ["helm", "template", "generators", ".", "-f", "-"],
        cwd=root, input=values, capture_output=True, text=True,
    )
    if manifest.returncode != 0:
        print(manifest.stderr[-2000:], file=sys.stderr)
        sys.exit("could not render the generator chart")

    buf = bytearray()
    files = [f for f in tracked() if KEEP.search(f)]
    for f in files:
        p = root / f
        if not p.is_file():
            continue
        buf += b'{"name":"' + f.encode() + b'","data":"'
        buf += base64.b64encode(p.read_bytes())
        buf += b'"},'
    buf += manifest.stdout.encode()

    stored = int(len(gzip.compress(bytes(buf), 9)) * 4 / 3 * CALIBRATION)
    pct = stored * 100.0 / LIMIT
    print(f"generator release ~{stored} bytes, {pct:.1f}% of 1 MiB ({len(files)} chart files)")
    if stored > LIMIT * BUDGET:
        sys.exit(
            f"generator release is at {pct:.1f}% of the 1 MiB Secret cap (budget {BUDGET:.0%}).\n"
            "  It fails silently at 100%: the release stops applying and every generated\n"
            "  Kustomization freezes at its last content. See docs/generator-chart-size.md."
        )

main()
