#!/usr/bin/env python3
"""Every container image reference must carry a digest: `repo:tag@sha256:...`.

A tag alone is mutable. With imagePullPolicy IfNotPresent a node keeps the build it first pulled
when upstream re-pushes the tag (a security rebuild goes unnoticed), and Renovate raises digest
updates only for references that already carry a digest. See docs/adr/0027.

Checked shapes: `image: <ref>` (containers, initContainers, Grafana-style sidecars), image
volumes and ImageComposition layers (`image: {reference|ref: <ref>}`), a Grafana CR's
`spec.version`, kustomize `images:` entries, and chart values `image: {repository, tag|digest}`.
`${VAR}` references are substituted at apply time and cannot be pinned here.

  check-image-pins.py            verify (scripts/validate.sh)
  check-image-pins.py --list     print every reference found with its status
"""
import fnmatch
import pathlib
import re
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import clusters

ROOT = clusters.ROOT
SCOPE = ("kube-cluster/", "kube-test-cluster/", "shared/")
DIGEST = re.compile(r"@sha256:[a-f0-9]{64}$")

# (path glob, reason). A whole file is exempt only where its content is not ours to edit.
ALLOW_FILES = [
    ("*/flux-system/gotk-components.yaml",
     "flux install output, regenerated whole by gen-flux.sh (the lint checks it)"),
    ("*/infra/barman-cloud-plugin/manifest.yaml",
     "whole-file vendored release; the image is also base64 inside a Secret, so it cannot be pinned in place (renovate.json)"),
    ("kube-cluster/infra/multus/upstream.yaml",
     "vendored unmodified; kustomization.yaml's images: entry sets the real tag and digest"),
]
# (reference as written, reason): deliberate exceptions.
ALLOW_REFS = {
    "alpine/k8s": "no tag on purpose; each cluster's k0s-upgrade overlay sets tag and digest",
}


def refs(node, path=()):
    """(kind, reference) for every image reference in a parsed document."""
    if isinstance(node, list):
        for item in node:
            yield from refs(item, path)
    elif isinstance(node, dict):
        for key, val in node.items():
            if key == "image" and isinstance(val, str):
                yield "image", val
            elif key == "image" and isinstance(val, dict):
                for k in ("reference", "ref"):
                    if isinstance(val.get(k), str):
                        yield "volume", val[k]
                repo = val.get("repository")
                if isinstance(repo, str):
                    tag, digest = str(val.get("tag") or ""), val.get("digest")
                    yield "chart", repo + (f":{tag}" if tag else "") + (f"@{digest}" if digest else "")
            elif key == "images" and isinstance(val, list) and node.get("kind") == "Kustomization":
                for img in val:
                    if isinstance(img, dict) and "name" in img:
                        base = img.get("newName") or img["name"]
                        tag = str(img.get("newTag") or "")
                        if img.get("digest"):
                            yield "kustomize", f"{base}:{tag}@{img['digest']}" if tag else f"{base}@{img['digest']}"
                        elif tag:
                            yield "kustomize", f"{base}:{tag}"
                        else:
                            yield "kustomize", base
            elif key == "spec" and isinstance(val, dict) and node.get("kind") == "Grafana"                     and isinstance(val.get("version"), str) and "/" in val["version"]:
                yield "grafana", val["version"]
            if isinstance(val, (dict, list)):
                yield from refs(val, path + (key,))


def scan():
    for rel in sorted(clusters.tracked()):
        if not rel.startswith(SCOPE) or not rel.endswith((".yaml", ".yml")):
            continue
        reason = next((r for g, r in ALLOW_FILES if fnmatch.fnmatch(rel, g)), None)
        try:
            docs = [d for d in clusters.load_all((ROOT / rel).read_text(encoding="utf-8")) if d]
        except yaml.YAMLError:
            continue
        seen = set()
        for doc in docs:
            for kind, ref in refs(doc):
                if "${" in ref or (rel, ref) in seen:
                    continue
                seen.add((rel, ref))
                yield rel, kind, ref, reason


def main():
    bad = []
    for rel, kind, ref, reason in scan():
        ok = bool(DIGEST.search(ref))
        note = reason or ALLOW_REFS.get(ref)
        if "--list" in sys.argv:
            print(("ok     " if ok else "allow  " if note else "UNPIN  ") + f"{rel}: {ref}")
        if not ok and not note:
            bad.append((rel, ref))
    if bad:
        for rel, ref in bad:
            print(f"{rel}: {ref} has no @sha256 digest", file=sys.stderr)
        sys.exit("Pin each with tag@sha256:<index digest> (crane digest <ref>), or add a reasoned "
                 "entry to ALLOW_* in scripts/check-image-pins.py")
    print("every image reference carries a digest")


if __name__ == "__main__":
    main()
