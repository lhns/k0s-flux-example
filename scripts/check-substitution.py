#!/usr/bin/env python3
"""Check the two ways a substituted config can silently go wrong.

1. A substituted credential applied as a ConfigMap.

   A component with a substitution-secrets/ dir has envsubst run over its whole build,
   so a ConfigMap holding one of its ${KEY} placeholders receives the real value at
   apply time. That is only safe for a ConfigMap the generator turns into a Secret,
   which is the generators.example.com/as-secret annotation. Misspell the annotation and
   the credential is applied in the clear with nothing to notice it.

2. A marked generator that produces an object nothing can reach.

   The as-secret patch matches on kind and annotation, never on name, so a hashed name
   still gets converted. What breaks is the consumer: kustomize rewrites hashed names
   only where it recognises a ConfigMap reference, and the workload refers to this as a
   Secret. So the object lands as <name>-<hash> and the pod cannot mount <name>.
   disableNameSuffixHash cannot be patched in, because it is consumed during the
   component build while the Flux Kustomization's patches run after it. So check it.

Key names are read straight from the SOPS files; only the values are encrypted.
"""
import pathlib, re, subprocess, sys, yaml

ANNOTATION = "generators.example.com/as-secret"
KEY_RE = re.compile(r'^\s{2,}([A-Z][A-Z0-9_]*):', re.M)
failures = []


def marked_generators(component: pathlib.Path):
    """configMapGenerator entries in this component that ask to become Secrets."""
    kust = component / "kustomization.yaml"
    if not kust.exists():
        return []
    doc = yaml.safe_load(kust.read_text(encoding="utf-8")) or {}
    out = []
    for entry in doc.get("configMapGenerator") or []:
        options = entry.get("options") or {}
        if str((options.get("annotations") or {}).get(ANNOTATION, "")).lower() == "true":
            out.append(entry)
    return out


def build(component: pathlib.Path):
    r = subprocess.run(["kustomize", "build", str(component)], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


for base in ("kube-cluster/apps", "kube-cluster/infra"):
    for component in sorted(p for p in pathlib.Path(base).glob("*") if p.is_dir()):
        marked = marked_generators(component)

        # (2) the option that keeps the name reachable
        for entry in marked:
            if (entry.get("options") or {}).get("disableNameSuffixHash") is not True:
                failures.append(
                    f"  {component}: configMapGenerator {entry['name']!r} is marked "
                    f"{ANNOTATION} but does not set options.disableNameSuffixHash: true, "
                    f"so it applies as {entry['name']}-<hash> and its consumer cannot find it"
                )

        subst = component / "substitution-secrets"
        keys = set()
        if subst.is_dir():
            for f in sorted(subst.glob("*.yaml")):
                keys |= set(KEY_RE.findall(f.read_text(encoding="utf-8")))
        if not keys and not marked:
            continue

        built = build(component)
        if built is None:
            continue  # validate.sh's build loop reports this

        pattern = re.compile(r'\$\{(' + "|".join(sorted(keys)) + r')\}') if keys else None
        for doc in yaml.safe_load_all(built):
            if not doc or doc.get("kind") != "ConfigMap":
                continue
            annotations = (doc.get("metadata") or {}).get("annotations") or {}
            annotated = str(annotations.get(ANNOTATION, "")).lower() == "true"
            name = doc["metadata"]["name"]

            # (1) a placeholder in a ConfigMap that stays a ConfigMap
            if pattern and not annotated:
                for key, value in (doc.get("data") or {}).items():
                    hit = pattern.search(str(value))
                    if hit:
                        failures.append(
                            f"  {component}: ConfigMap/{name} key {key!r} holds "
                            f"${{{hit.group(1)}}} and is not annotated {ANNOTATION}=true, "
                            f"so the substituted credential would be applied in the clear"
                        )

if failures:
    print("== substitution check failed ==", file=sys.stderr)
    print(*failures, sep="\n", file=sys.stderr)
    sys.exit(1)
print("substituted values stay in Secrets, and marked generators keep a reachable name")
