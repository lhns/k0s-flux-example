#!/usr/bin/env python3
"""Check the ways a substituted config can go wrong.

1. A substituted credential applied as a ConfigMap.

   A component with a substitution-secrets/ dir has envsubst run over its whole build,
   so a ConfigMap holding one of its ${KEY} placeholders receives the real value at
   apply time. That is only safe for a ConfigMap the generator turns into a Secret,
   which is the generators.example.com/as-secret annotation. Misspell the annotation and
   the credential is applied in the clear with nothing to notice it.

2. A ${VAR} with nothing behind it.

   kustomize-controller substitutes strictly, so a variable no source defines fails the
   whole reconcile. Loud, but found at reconcile time rather than here.

3. A ${...} envsubst cannot parse at all, or a $$ it silently rewrites.

   Not only placeholders are read: envsubst reads the whole built file, comments
   included, and a dollar-brace whose contents are not a variable name fails the WHOLE
   Kustomization with "unable to parse variable name". $$ is its escape, so any other
   $$ (a regex backreference, a collector config's own escape) is applied as $.

4. A marked generator that produces an object nothing can reach.

   The as-secret patch matches on kind and annotation, never on name, so a hashed name
   still gets converted. What breaks is the consumer: kustomize rewrites hashed names
   only where it recognises a ConfigMap reference, and the workload refers to this as a
   Secret. So the object lands as <name>-<hash> and the pod cannot mount <name>.
   disableNameSuffixHash cannot be patched in, because it is consumed during the
   component build while the Flux Kustomization's patches run after it. So check it.

5. An artifact whose ref nothing can substitute.

   Every ImageComposition and ImageBuild must sit in an artifacts/ dir and export a ref into
   flux-system (push.writeRefTo), the only place the component's substituteFrom can read it
   from. Its keys count as defined for (2). Everything in artifacts/ must be in namespace
   oci-composer. Applied by the component, or into its namespace, an artifact deadlocks a
   fresh cluster: see templates/kustomizations.yaml.

An object annotated kustomize.toolkit.fluxcd.io/substitute: disabled is skipped by (2)
and (3), as Flux skips it.

Key names are read straight from the SOPS files; only the values are encrypted.
"""
import pathlib, re, subprocess, sys, yaml

ANNOTATION = "generators.example.com/as-secret"
DISABLED = "kustomize.toolkit.fluxcd.io/substitute"
KINDS = ("ImageComposition", "ImageBuild")
KEY_RE = re.compile(r'^\s{2,}([A-Z][A-Z0-9_]*):', re.M)
# A variable reference without a default, which strict substitution needs defined. Any
# case: a shell ${lowercase} fails the reconcile too. (?<!\$) skips the $${VAR} escape.
USE_RE = re.compile(r'(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_]|:?[-=])')
# A dollar-brace that is not a plain NAME or NAME with a default. envsubst rejects it and
# takes the whole Kustomization down with it.
BAD_RE = re.compile(r'(?<!\$)(\$\{(?![A-Za-z_][A-Za-z0-9_]*[:}])[^}]{0,40}\})')
# $$ that is not the $${VAR} escape.
DOUBLE_RE = re.compile(r'\$\$(?!\{)')
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


def exported_keys(component: pathlib.Path):
    """(5) The ConfigMap keys this component's artifacts/ export into flux-system."""
    keys = set()
    for f in sorted((component / "artifacts").glob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if not doc:
                continue
            if (doc.get("metadata") or {}).get("namespace") != "oci-composer":
                failures.append(f"  {f}: {doc.get('kind')} is not in namespace oci-composer")
            if doc.get("kind") not in KINDS:
                continue
            export = ((doc.get("spec") or {}).get("push") or {}).get("writeRefTo") or {}
            exported = export.get("keys") or {}
            if export.get("namespace") != "flux-system" or not exported.get("ref"):
                failures.append(
                    f"  {f}: {doc['kind']}/{doc['metadata']['name']} does not export "
                    f"push.writeRefTo keys.ref into flux-system, so no consumer can use it"
                )
            keys |= set(exported.values())
    return keys


def substituted(built: str):
    """The build minus the documents Flux does not substitute."""
    out = []
    for raw in re.split(r'(?m)^---\s*$', built):
        doc = yaml.safe_load(raw)
        meta = (doc.get("metadata") or {}) if isinstance(doc, dict) else {}
        if "disabled" in ((meta.get("annotations") or {}).get(DISABLED),
                          (meta.get("labels") or {}).get(DISABLED)):
            continue
        out.append(raw)
    return "---".join(out)


def build(component: pathlib.Path):
    r = subprocess.run(["kustomize", "build", str(component)], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


for base in ("kube-cluster/apps", "kube-cluster/infra", "kube-cluster/rbac"):
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
        refs = exported_keys(component)
        if not keys and not refs and not marked:
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

        # (2) every ${VAR} in the build has a definition. Only checked where there is a
        # substitution source, because that is the only case where envsubst runs at all.
        # (3) a dollar-brace envsubst would choke on, wherever it appears -- prose in a
        # comment is substituted like anything else.
        text = substituted(built)
        for bad in sorted(set(BAD_RE.findall(text))):
            failures.append(
                f"  {component}: the build contains {bad!r}, which envsubst cannot parse "
                f"as a variable name; it fails the whole Kustomization at reconcile time"
            )

        if keys or refs:
            if DOUBLE_RE.search(text):
                failures.append(
                    f"  {component}: the build contains $$, which substitution applies as $; "
                    f"annotate the object {DISABLED}: disabled"
                )
            for name in sorted(set(USE_RE.findall(text))):
                if name not in keys | refs:
                    failures.append(
                        f"  {component}: the build uses ${{{name}}}, which neither "
                        f"substitution-secrets/ nor an artifacts/ export defines, so the "
                        f"reconcile fails"
                    )

# (5) nothing outside artifacts/
for f in sorted(pathlib.Path("kube-cluster").rglob("*.yaml")):
    if f.parent.name == "artifacts":
        continue
    text = f.read_text(encoding="utf-8")
    if not re.search(r"^kind: (ImageComposition|ImageBuild)\s*$", text, re.M):
        continue
    for doc in yaml.safe_load_all(text):
        if isinstance(doc, dict) and doc.get("kind") in KINDS:
            failures.append(f"  {f}: {doc['kind']}/{doc['metadata']['name']} is not in an artifacts/ dir")

if failures:
    print("== substitution check failed ==", file=sys.stderr)
    print(*failures, sep="\n", file=sys.stderr)
    sys.exit(1)
print("substituted values stay in Secrets, and marked generators keep a reachable name")
