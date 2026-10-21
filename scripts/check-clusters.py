#!/usr/bin/env python3
"""Each cluster reads only its own directory, and shared/ holds only plain bases.

A cluster is any top-level <dir>/flux-system/kustomization.yaml (scripts/clusters.py). For each one:

1. gotk-sync.yaml syncs ./<dir>/flux-system. Pointing at another cluster's path makes this
   cluster apply that cluster's bootstrap.
2. Every group base and k0sUpgrade path is under <dir>/. A group in another cluster's tree
   deploys that cluster's components here. No component (<dir>/*/*/kustomization.yaml) sits
   outside every group base, where nothing would ever render it.
3. The root ArtifactGenerator copies only Chart.yaml, templates/ and paths under <dir>/. A
   `*/...` wildcard would put every cluster's files into each root release.
4. .sops.yaml has a rule for <dir>/ that matches no other cluster's path, with recipients no
   other cluster's rule has: one age key per cluster.

And for shared/:

5. No reserved directory (namespaces/, artifacts/, substitution-secrets/) and no
   flux-component-generator/ annotation: the generator only reads those from a cluster's
   component. No .sops.yaml rule matches it; scripts/check-sops.py refuses encrypted files.
"""
import pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import clusters

ROOT = clusters.ROOT
RESERVED = ("namespaces", "artifacts", "substitution-secrets")
failures = []

names = clusters.clusters()
rules = (clusters.load((ROOT / ".sops.yaml").read_text(encoding="utf-8")) or {}).get("creation_rules") or []


def rule_for(rel):
    """sops picks the first rule whose path_regex is found anywhere in the path."""
    return next((r for r in rules if r.get("path_regex") is None or re.search(r["path_regex"], rel)), None)


def recipients(rule):
    return frozenset(r.strip() for r in str((rule or {}).get("age") or "").split(",") if r.strip())


keys = {}
for c in names:
    fs = f"{c}/flux-system"
    for f in ("gotk-components.yaml", "gotk-sync.yaml"):
        if not (ROOT / fs / f).is_file():
            failures.append(f"  {fs}: no {f}")

    # 1
    if (ROOT / fs / "gotk-sync.yaml").is_file():
        sync = [d for d in clusters.docs(f"{fs}/gotk-sync.yaml")
                if d.get("kind") == "Kustomization" and d["metadata"]["name"] == "flux-system"]
        path = sync[0]["spec"].get("path") if sync else None
        if path != f"./{fs}":
            failures.append(f"  {fs}/gotk-sync.yaml: Kustomization flux-system syncs {path!r}, not './{fs}'")

    # 2
    rel_path, _ = clusters.root_release(c)
    values = clusters.values(c)
    for g in values.get("groups") or []:
        if not g["base"].startswith(f"{c}/"):
            failures.append(f"  {rel_path}: group base {g['base']} is outside {c}/")
    # A component directory outside every group is never rendered, and nothing else notices.
    bases = {g["base"] for g in values.get("groups") or []}
    for k in sorted((ROOT / c).glob("*/*/kustomization.yaml")):
        parent = k.parent.parent.relative_to(ROOT).as_posix()
        if parent not in bases:
            failures.append(f"  {k.parent.relative_to(ROOT).as_posix()}: {parent} is no group base in {rel_path}, "
                            f"so this component is never deployed")
    for key in ("k0sctlPath", "path"):
        p = (values.get("k0sUpgrade") or {}).get(key)
        if p and not p.startswith(f"{c}/"):
            failures.append(f"  {rel_path}: k0sUpgrade.{key} {p} is outside {c}/")

    # 3
    art_path, gen = clusters.root_artifact(c)
    if rel_path and gen is None:
        failures.append(f"  {fs}: a generator root HelmRelease but no root ArtifactGenerator")
    for art in ((gen or {}).get("spec") or {}).get("artifacts") or []:
        for op in art.get("copy") or []:
            src = op["from"].removeprefix("@repo/")
            if src not in ("Chart.yaml", "templates") and not src.startswith(f"{c}/"):
                failures.append(f"  {art_path}: copies {op['from']}, "
                                f"outside Chart.yaml, templates/ and {c}/")

    # 4
    rule = rule_for(f"{c}/x.yaml")
    if rule is None or rule.get("path_regex") is None:
        failures.append(f"  .sops.yaml: no rule with a path_regex for {c}/")
        continue
    for other in names:
        if other != c and re.search(rule["path_regex"], f"{other}/x.yaml"):
            failures.append(f"  .sops.yaml: {c}'s rule {rule['path_regex']!r} also matches {other}/")
    for other, have in keys.items():
        if have & recipients(rule):
            failures.append(f"  .sops.yaml: {c} and {other} share an age recipient; each cluster has its own key")
    keys[c] = recipients(rule)

# 5
shared = ROOT / "shared"
if rule_for("shared/x.yaml") is not None:
    failures.append("  .sops.yaml: a rule matches shared/, where nothing may be encrypted")
for p in sorted(shared.rglob("*")) if shared.is_dir() else []:
    rel = p.relative_to(ROOT).as_posix()
    if p.is_dir() and p.name in RESERVED:
        failures.append(f"  {rel}: reserved directories only work in a cluster's component")
    if p.name == "kustomization.yaml" and "flux-component-generator/" in p.read_text(encoding="utf-8"):
        failures.append(f"  {rel}: generator annotations go on the cluster's component, not the base")

if not names:
    failures.append("  no */flux-system/kustomization.yaml -- no cluster found")
if failures:
    print("== cluster isolation check failed ==", file=sys.stderr)
    print(*failures, sep="\n", file=sys.stderr)
    sys.exit(1)
print(f"{len(names)} cluster(s) isolated: {', '.join(names)}")
