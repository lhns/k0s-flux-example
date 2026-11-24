#!/usr/bin/env python3
"""An image volume mounts directories only: a file subPath fails at container start.

containerd (internal/cri/server/container_image_mount.go) rejects a subPath on an image volume
that is not a directory ("only directory subpath is supported"); the pod never starts. The
subPath is not checked against the image, so a name with an extension (the last element ends in a
dot and up to eight letters or digits) is taken for a file; a reverse-DNS app id is not. Per
cluster, over every component build, this fails on such a mount.

KUSTOMIZE_BUILDS is validate.sh's build directory; without it every build is run here.
tests/check-image-subpaths.py runs the same rule over inline fixtures.
"""
import os, pathlib, re, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import clusters

ROOT = clusters.ROOT
FILE_LIKE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def pod_specs(node):
    """Every dict that is a pod spec (has volumes and containers), at any depth."""
    if isinstance(node, dict):
        if isinstance(node.get("containers"), list) and "volumes" in node:
            yield node
        for v in node.values():
            yield from pod_specs(v)
    elif isinstance(node, list):
        for v in node:
            yield from pod_specs(v)


def image_volumes(pod):
    return {v.get("name") for v in pod.get("volumes") or [] if isinstance(v, dict) and "image" in v}


def findings(docs, dir):
    """One line per file-like subPath on an image volume in one component's build."""
    out = []
    for d in docs:
        for pod in pod_specs(d):
            images = image_volumes(pod)
            for c in (pod.get("containers") or []) + (pod.get("initContainers") or []):
                for m in c.get("volumeMounts") or []:
                    sub = str(m.get("subPath") or "")
                    if m.get("name") in images and FILE_LIKE.search(sub.rstrip("/").rsplit("/", 1)[-1]):
                        out.append(f"  {dir}: {d.get('kind')}/{(d.get('metadata') or {}).get('name')}: "
                                   f"container {c.get('name')}: image volume {m['name']!r} subPath {sub!r} "
                                   f"looks like a file; mount the directory instead")
    return out


def build(dir):
    done = os.environ.get("KUSTOMIZE_BUILDS")
    if done:
        out = pathlib.Path(done, dir.replace("/", "%"))
        if out.is_file() and not out.with_name(out.name + ".failed").exists():
            return out.read_text(encoding="utf-8")
        return None  # a failed build is reported by the kustomize build step
    r = subprocess.run(["kustomize", "build", str(ROOT / dir)], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def main():
    failures, n = [], 0
    for cluster in clusters.clusters():
        for dir, _ in clusters.components(cluster):
            text = build(dir)
            if text is None:
                continue
            docs = [d for d in clusters.load_all(text) if isinstance(d, dict)]
            n += sum(1 for d in docs for p in pod_specs(d) for c in p.get("containers") or []
                     for m in c.get("volumeMounts") or [] if m.get("subPath") and m.get("name") in image_volumes(p))
            failures += findings(docs, dir)
    if failures:
        print("== image-volume subPath check failed ==", file=sys.stderr)
        print(*failures, sep="\n", file=sys.stderr)
        sys.exit(1)
    print(f"{n} image-volume subPath(s) are directories")


if __name__ == "__main__":
    main()
