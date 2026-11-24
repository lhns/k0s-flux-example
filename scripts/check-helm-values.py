#!/usr/bin/env python3
"""Render every HelmRelease whose chartRef is a digest-pinned OCIRepository with its inline
values, and schema-check the output as validate.sh does the other manifests.

helm-controller validates nothing up front: a values typo against the chart's
values.schema.json, or a template that no longer renders, only shows as a failed release in
the cluster. valuesFrom and Flux ${...} substitution are not applied. Needs the network on a
cold cache; charts are cached by digest in .cache/charts.
"""
import concurrent.futures, os, pathlib, subprocess, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import clusters, yaml

root = clusters.ROOT
charts = root / ".cache" / "charts"
kc_cache = root / ".cache" / "kubeconform"


def documents():
    for c in clusters.clusters():
        for p in sorted((root / c).rglob("*.yaml")):
            try:
                docs = list(yaml.load_all(p.read_text(encoding="utf-8"), Loader=clusters.Loader))
            except yaml.YAMLError:
                continue  # yamllint reports it
            for d in docs:
                if isinstance(d, dict):
                    yield p.relative_to(root), d


def kube_version(cluster):
    """The Kubernetes version the cluster runs, from its k0sctl.yaml: charts gate on kubeVersion."""
    f = root / cluster / "k0sctl.yaml"
    for d in yaml.load_all(f.read_text(encoding="utf-8"), Loader=clusters.Loader):
        v = ((d or {}).get("spec") or {}).get("k0s", {}).get("version")
        if v:
            return v.split("+")[0]
    sys.exit(f"{f}: no spec.k0s.version")


def releases():
    docs = list(documents())
    sources = {}
    for _, d in docs:
        if d.get("kind") == "OCIRepository":
            m = d["metadata"]
            sources[(m.get("namespace"), m["name"])] = d["spec"]
    for path, d in docs:
        if d.get("kind") != "HelmRelease":
            continue
        m, spec = d["metadata"], d.get("spec") or {}
        ref = spec.get("chartRef") or {}
        if ref.get("kind") != "OCIRepository":
            continue
        src = sources.get((ref.get("namespace", m.get("namespace")), ref["name"]))
        if src is None:
            sys.exit(f"{path}: HelmRelease {m['name']} names an OCIRepository that is not in the repo")
        r = src.get("ref") or {}
        if not r.get("digest") or not r.get("tag"):
            sys.exit(f"{path}: OCIRepository {ref['name']} needs ref.tag and ref.digest")
        yield path, m, spec, src["url"], r["tag"], r["digest"], kube_version(path.parts[0])


def render(item):
    path, m, spec, url, tag, digest, kube = item
    name = f"{m.get('namespace')}/{m['name']} ({path})"
    with tempfile.TemporaryDirectory() as tmp:
        vals = pathlib.Path(tmp, "values.yaml")
        vals.write_text(yaml.safe_dump(spec.get("values") or {}))
        charts.mkdir(parents=True, exist_ok=True)
        # Helm 3.16 cannot pull url@digest: pull the tag and compare the digest it reports.
        # Cached by digest, so a cache hit is the pinned content.
        tgz = charts / (digest.replace(":", "-") + "-" + url.rstrip("/").rsplit("/", 1)[1] + ".tgz")
        if not tgz.exists():
            r = subprocess.run(["helm", "pull", url, "--version", tag, "-d", tmp], capture_output=True, encoding="utf-8")
            pulled = list(pathlib.Path(tmp).glob("*.tgz"))
            if r.returncode or not pulled:
                return name, None, r.stderr.strip()
            if f"Digest: {digest}" not in r.stdout + r.stderr:
                return name, None, f"{url}:{tag} is not {digest}: {r.stdout.strip()}"
            pulled[0].replace(tgz)
        r = subprocess.run(
            ["helm", "template", m["name"], str(tgz), "--namespace", m.get("namespace", "default"),
             "--include-crds", "--kube-version", kube, "-f", str(vals)],
            capture_output=True, encoding="utf-8")
        if r.returncode:
            return name, None, r.stderr.strip()
        return name, r.stdout, ""


def main():
    items = list(releases())
    if not items:
        sys.exit("no HelmRelease with an OCIRepository chartRef found")
    jobs = int(os.environ.get("JOBS", "8"))
    failed = False
    with concurrent.futures.ThreadPoolExecutor(jobs) as ex:
        for name, out, err in ex.map(render, items):
            if out is None:
                failed = True
                print(f"FAIL {name}: helm\n  " + err.replace("\n", "\n  "), file=sys.stderr)
                continue
            kc_cache.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(
                ["kubeconform", "-ignore-missing-schemas", "-strict", "-cache", str(kc_cache),
                 "-schema-location", "default",
                 "-schema-location", "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
                 "-summary"],
                input=out, capture_output=True, encoding="utf-8")
            # A schema that could not be downloaded stays unchecked, as in check_kubeconform.
            bad = [l for l in (r.stdout + r.stderr).splitlines()
                   if ("is invalid" in l or "failed validation" in l) and "failed downloading schema" not in l]
            if r.returncode and not bad and "Summary" not in r.stdout:
                bad = [r.stderr.strip() or "kubeconform did not run"]
            if bad:
                failed = True
                print(f"FAIL {name}: kubeconform\n  " + "\n  ".join(bad), file=sys.stderr)
            else:
                print(f"{name}: OK")
    sys.exit(1 if failed else 0)


main()
