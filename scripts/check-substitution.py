#!/usr/bin/env python3
"""Check the four ways a substituted config can silently go wrong.

1. A substituted credential applied as a ConfigMap.

   A component with a substitution-secrets/ dir has envsubst run over its whole build,
   so a ConfigMap holding one of its ${KEY} placeholders receives the real value at
   apply time. That is only safe for a ConfigMap the generator turns into a Secret,
   which is the generators.example.com/as-secret annotation. Misspell the annotation and
   the credential is applied in the clear with nothing to notice it.

2. A ${VAR} with nothing behind it.

   Flux resolves substituteFrom with optional:false, but a variable no source defines is
   not an error there -- envsubst just writes an empty string. A typo in a key name is
   therefore a password that applies as "" and a service that starts and fails to
   authenticate, which looks nothing like a deploy failure.

3. A ${...} envsubst cannot parse at all.

   Not only placeholders are read: envsubst reads the whole built file, comments
   included, and a dollar-brace whose contents are not a variable name fails the WHOLE
   Kustomization with "unable to parse variable name". Nothing is applied, so it is loud
   rather than silent -- but it is found at reconcile time, not here.

4. A marked generator that produces an object nothing can reach.

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
# A ${VAR} as Flux's envsubst sees it. Upper-case only, which is the convention every
# substitution key here follows and keeps shell ${lowercase} in scripts out of it.
# (?<!\$) skips $${VAR}, the escape a manifest uses to keep a variable for a shell at
# runtime -- matching inside it would report the one thing that is already handled.
USE_RE = re.compile(r'(?<!\$)\$\{([A-Z][A-Z0-9_]*)\}')
# A dollar-brace that is not a plain NAME or NAME with a default. envsubst rejects it and
# takes the whole Kustomization down with it.
BAD_RE = re.compile(r'(?<!\$)(\$\{(?![A-Za-z_][A-Za-z0-9_]*[:}])[^}]{0,40}\})')
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

        # (4) the option that keeps the name reachable
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

            # (1a) a placeholder in a ConfigMap that stays a ConfigMap
            if pattern and not annotated:
                for key, value in (doc.get("data") or {}).items():
                    hit = pattern.search(str(value))
                    if hit:
                        failures.append(
                            f"  {component}: ConfigMap/{name} key {key!r} holds "
                            f"${{{hit.group(1)}}} and is not annotated {ANNOTATION}=true, "
                            f"so the substituted credential would be applied in the clear"
                        )

        # (2) every ${VAR} in the build has a definition. Only checked where a
        # substitution-secrets/ dir exists, because that is the only case where
        # envsubst runs over the build at all.
        # (3) a dollar-brace envsubst would choke on, wherever it appears -- prose in a
        # comment is substituted like anything else.
        for bad in sorted(set(BAD_RE.findall(built))):
            failures.append(
                f"  {component}: the build contains {bad!r}, which envsubst cannot parse "
                f"as a variable name; it fails the whole Kustomization at reconcile time"
            )

        if keys:
            for name in sorted(set(USE_RE.findall(built))):
                if name not in keys:
                    failures.append(
                        f"  {component}: the build uses ${{{name}}}, which no Secret or "
                        f"ConfigMap in substitution-secrets/ defines, so it substitutes "
                        f"to an empty string"
                    )

if failures:
    print("== substitution check failed ==", file=sys.stderr)
    print(*failures, sep="\n", file=sys.stderr)
    sys.exit(1)
print("substituted values stay in Secrets, and marked generators keep a reachable name")
