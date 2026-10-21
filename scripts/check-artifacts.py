#!/usr/bin/env python3
"""Build every component from exactly the files its ExternalArtifact holds.

kustomize-controller builds a component inside its ExternalArtifact, not inside the checkout.
A path the artifact does not carry, such as a `../` reference out of the component directory,
builds locally and in validate.sh, and fails only in the cluster. So for every cluster, this
renders the root release (render.sh real-generate) and the per-component releases (real),
copies what each ArtifactGenerator copies (scripts/clusters.py mirrors source-watcher), and
runs `kustomize build` at each Kustomization's spec.path in that copy. A reserved directory
has no kustomization.yaml (kustomize-controller synthesizes one) and only has to be there.
It also checks that the root ArtifactGenerator copies every file the templates read from a
component: the root release renders from that artifact, not from the checkout.
"""
import concurrent.futures, os, pathlib, shutil, subprocess, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import clusters

ROOT = clusters.ROOT
RESERVED = ("namespaces", "artifacts", "substitution-secrets")


def render(mode, cluster):
    r = subprocess.run(["bash", "scripts/render.sh", mode, cluster], cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"{cluster}: render.sh {mode} failed:\n{r.stderr[-2000:]}")
    return [d for d in clusters.load_all(r.stdout) if d]


def check(cluster, ks, artifacts, tmp):
    name, path = ks["metadata"]["name"], ks["spec"]["path"].removeprefix("./")
    files = artifacts.get(ks["spec"]["sourceRef"]["name"])
    if files is None:
        return f"{cluster}: Kustomization {name}: no ArtifactGenerator emits {ks['spec']['sourceRef']['name']}"
    if isinstance(files, str):
        return f"{cluster}: Kustomization {name}: {files}"
    dest = pathlib.Path(tmp, cluster, name)  # per cluster: shared names would mask a missing bases entry
    for to, src in files.items():
        (dest / to).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / src, dest / to)
    if not (dest / path).is_dir():
        return f"{cluster}: Kustomization {name}: its artifact has no {path}"
    if not (dest / path / "kustomization.yaml").is_file():
        return None
    r = subprocess.run(["kustomize", "build", str(dest / path)], capture_output=True, text=True)
    if r.returncode:
        return (f"{cluster}: Kustomization {name} does not build from its artifact, which holds only "
                f"what its ArtifactGenerator copies:\n    " + r.stderr.strip().replace("\n", "\n    "))
    return None


def root_sees(cluster, tracked):
    """Files the generator reads from a component that the root ArtifactGenerator does not copy.

    The root release renders from its own artifact, not the checkout, so a file the templates
    read (kustomization.yaml, namespaces/, artifacts/, substitution-secrets/) that no copy
    carries changes the in-cluster render while CI, rendering the checkout, stays green.
    """
    _, gen = clusters.root_artifact(cluster)
    if gen is None:
        return [f"{cluster}: no root ArtifactGenerator"]
    seen = set()
    for spec in gen["spec"]["artifacts"]:
        try:
            seen |= set(clusters.artifact(spec, tracked).values())
        except ValueError as e:
            return [f"{cluster}: root ArtifactGenerator: source-watcher would fail: {e}"]
    missing = set()
    for comp, _ in clusters.components(cluster):
        for f in tracked:
            rel = f.removeprefix(comp + "/")
            if f.startswith(comp + "/") and f.endswith((".yaml", ".yml")) and (
                    rel == "kustomization.yaml"
                    or (rel.count("/") == 1 and rel.split("/")[0] in RESERVED)):
                if f not in seen:
                    missing.add(f)
    return [f"{cluster}: the root ArtifactGenerator does not copy {f}, which the generator reads "
            f"(add a glob to its copy list)" for f in sorted(missing)]


def main():
    tracked = clusters.tracked()
    failures, n = [], 0
    for cluster in clusters.clusters():
        failures += ["  " + e for e in root_sees(cluster, tracked)]
    with tempfile.TemporaryDirectory() as tmp, \
            concurrent.futures.ThreadPoolExecutor(max_workers=int(os.environ.get("JOBS") or 0) or os.cpu_count() or 4) as pool:
        jobs = []
        for cluster in clusters.clusters():
            artifacts = {}
            for gen in render("real-generate", cluster):
                if gen.get("kind") != "ArtifactGenerator":
                    continue
                for spec in gen["spec"]["artifacts"]:
                    try:
                        artifacts[spec["name"]] = clusters.artifact(spec, tracked)
                    except ValueError as e:
                        artifacts[spec["name"]] = f"source-watcher would fail: {e}"
            for ks in render("real", cluster):
                if ks.get("kind") == "Kustomization" and ks["spec"]["sourceRef"]["kind"] == "ExternalArtifact":
                    jobs.append(pool.submit(check, cluster, ks, artifacts, tmp))
        for job in jobs:
            n += 1
            if err := job.result():
                failures.append("  " + err)
    if failures:
        print("== artifact build failed ==", file=sys.stderr)
        print(*failures, sep="\n", file=sys.stderr)
        sys.exit(1)
    print(f"{n} Kustomization(s) build from their own artifact")


main()
