#!/usr/bin/env python3
"""Check the ways a substituted config can go wrong.

1. A substituted credential applied as a ConfigMap.

   A component with a substitution-secrets/ dir has envsubst run over its whole build,
   so a ConfigMap holding one of its ${KEY} placeholders receives the real value at
   apply time. That is only safe for a ConfigMap the generator turns into a Secret,
   which is the flux-component-generator/as-secret annotation. Misspell the annotation and
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

   Every ImageComposition and ImageBuild must sit in an artifacts/ dir and export a ref
   (push.writeRefTo) into its component's control namespace gen-<name>, the only place
   substituteFrom can read it from, which oci-composer's refExport.namespaces must list. Its
   keys count as defined for (2). Everything in artifacts/ must be in a namespace
   the component's namespaces/ declares, or in oci-composer (legacy). Applied by the
   component, or into a namespace the component creates, an artifact deadlocks a fresh
   cluster: see templates/kustomizations.yaml.

6. A Namespace owned twice, or a namespaces/ object outside it.

   namespaces/ holds Namespaces and objects in them. A Namespace also in the component's
   build has two Kustomizations owning it, and dropping it from either prunes it with
   everything inside.

8. An ImageBuild whose build Job cannot reach oci-composer.

   The Job runs in the ImageBuild's namespace. Where an Egress policy there selects every pod,
   the Job reaches only what such policies allow, and the builder (8090) and registry (5000) in
   oci-composer are private. A policy for app.kubernetes.io/managed-by: kube-oci-builder must
   then be in namespaces/: artifacts/ builds, and waits for it, before the component applies.
   Nowhere else: in a namespace without one, that policy would cut the Job off the internet.

7. A substitution source in flux-system, or a control-namespace label set by hand.

   substitution-secrets/ objects belong in gen-<name>. The label
   flux-component-generator/control-namespace gets a namespace the SOPS key (infra/reflector),
   so only the generator may set it.

An object annotated kustomize.toolkit.fluxcd.io/substitute: disabled is skipped by (2)
and (3), as Flux skips it.

Key names are read straight from the SOPS files; only the values are encrypted.
"""
import os, pathlib, re, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import clusters

os.chdir(clusters.ROOT)  # every path below is repo-relative
ANNOTATION = "flux-component-generator/as-secret"
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
CONTROL_LABEL = "flux-component-generator/control-namespace"
failures = []


CLUSTERS = clusters.clusters()
PREFIX = {g["base"]: g.get("prefix") for c in CLUSTERS for g in clusters.values(c).get("groups") or []}


def kustomization_namespace(component: pathlib.Path):
    """(5, 7) Where the component's Kustomizations live, so where substituteFrom reads."""
    prefix = PREFIX.get(component.parent.as_posix())
    name = f"{prefix}-{component.name}" if prefix else component.name
    return f"gen-{name}"


def ref_export_namespaces(cluster):
    """(5) The namespaces the cluster's oci-composer lets an object export into besides its own."""
    rel = pathlib.Path(cluster, "infra/oci-composer/release.yaml")
    if not rel.is_file():
        return set()
    for doc in clusters.load_all(rel.read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "HelmRelease":
            return set((((doc["spec"].get("values") or {}).get("refExport") or {}).get("namespaces")) or [])
    return set()


REF_EXPORT_NAMESPACES = {c: ref_export_namespaces(c) for c in CLUSTERS}


def marked_generators(component: pathlib.Path):
    """configMapGenerator entries in this component that ask to become Secrets."""
    kust = component / "kustomization.yaml"
    if not kust.exists():
        return []
    doc = clusters.load(kust.read_text(encoding="utf-8")) or {}
    out = []
    for entry in doc.get("configMapGenerator") or []:
        options = entry.get("options") or {}
        if str((options.get("annotations") or {}).get(ANNOTATION, "")).lower() == "true":
            out.append(entry)
    return out


def declared_namespaces(component: pathlib.Path):
    """(6) The Namespaces this component's namespaces/ creates."""
    docs = []
    for f in sorted((component / "namespaces").glob("*.yaml")):
        docs += [(f, d) for d in clusters.load_all(f.read_text(encoding="utf-8")) if d]
    names = {d["metadata"]["name"] for _, d in docs if d.get("kind") == "Namespace"}
    for f, d in docs:
        if d.get("kind") != "Namespace" and (d.get("metadata") or {}).get("namespace") not in names:
            failures.append(f"  {f}: {d.get('kind')} is not in a Namespace namespaces/ declares")
    return names


def exported_keys(component: pathlib.Path, namespaces: set):
    """(5) The ConfigMap keys this component's artifacts/ export where it can substitute them."""
    target = kustomization_namespace(component)
    keys = set()
    for f in sorted((component / "artifacts").glob("*.yaml")):
        for doc in clusters.load_all(f.read_text(encoding="utf-8")):
            if not doc:
                continue
            ns = (doc.get("metadata") or {}).get("namespace")
            if ns != "oci-composer" and ns not in namespaces:
                failures.append(
                    f"  {f}: {doc.get('kind')} is in namespace {ns!r}, which "
                    f"{component / 'namespaces'} does not declare, so it deadlocks a fresh cluster"
                )
            if doc.get("kind") not in KINDS:
                continue
            export = ((doc.get("spec") or {}).get("push") or {}).get("writeRefTo") or {}
            exported = export.get("keys") or {}
            if export.get("namespace") != target or not exported.get("ref"):
                failures.append(
                    f"  {f}: {doc['kind']}/{doc['metadata']['name']} does not export "
                    f"push.writeRefTo keys.ref into {target}, so no consumer can use it"
                )
            elif target != ns and target not in REF_EXPORT_NAMESPACES[component.parts[0]]:
                failures.append(
                    f"  {f}: {doc['kind']}/{doc['metadata']['name']} exports into {target}, which "
                    f"refExport.namespaces in {component.parts[0]}/infra/oci-composer/release.yaml "
                    f"does not list, so oci-composer refuses it"
                )
            keys |= set(exported.values())
    return keys


BUILDER_LABEL = ("app.kubernetes.io/managed-by", "kube-oci-builder")


def build_egress(component: pathlib.Path, built: str):
    """(8) Build Jobs in an egress-restricted namespace need their policy in namespaces/."""
    builds = set()
    for f in sorted((component / "artifacts").glob("*.yaml")):
        for doc in clusters.load_all(f.read_text(encoding="utf-8")):
            ns = ((doc or {}).get("metadata") or {}).get("namespace")
            if (doc or {}).get("kind") == "ImageBuild" and ns != "oci-composer":
                builds.add(ns)
    if not builds:
        return
    early = [d for f in sorted((component / "namespaces").glob("*.yaml"))
             for d in clusters.load_all(f.read_text(encoding="utf-8")) if d]
    policies = [d for d in list(clusters.load_all(built)) + early
                if d and d.get("kind") == "NetworkPolicy" and "Egress" in (d["spec"].get("policyTypes") or [])]
    for ns in sorted(builds):
        here = [p for p in policies if p["metadata"].get("namespace") == ns]
        if not any(not (p["spec"].get("podSelector") or {}) for p in here):
            continue
        allowed = any(d.get("kind") == "NetworkPolicy" and d["metadata"].get("namespace") == ns
                      and "Egress" in (d["spec"].get("policyTypes") or [])
                      and ((d["spec"].get("podSelector") or {}).get("matchLabels") or {}).get(BUILDER_LABEL[0])
                      == BUILDER_LABEL[1] for d in early)
        if not allowed:
            failures.append(
                f"  {component}: an Egress policy selects every pod in {ns}, where an ImageBuild runs "
                f"its Job; add one for {BUILDER_LABEL[0]}: {BUILDER_LABEL[1]} to oci-composer "
                f"(8090, 5000) in namespaces/, or the build cannot reach the builder or registry"
            )


def substituted(built: str):
    """The build minus the documents Flux does not substitute."""
    out = []
    for raw in re.split(r'(?m)^---\s*$', built):
        doc = clusters.load(raw)
        meta = (doc.get("metadata") or {}) if isinstance(doc, dict) else {}
        if "disabled" in ((meta.get("annotations") or {}).get(DISABLED),
                          (meta.get("labels") or {}).get(DISABLED)):
            continue
        out.append(raw)
    return "---".join(out)


# validate.sh builds every component once, into one file per component path with / as %.
BUILDS = os.environ.get("KUSTOMIZE_BUILDS")


def build(component: pathlib.Path):
    if BUILDS:
        done = pathlib.Path(BUILDS, component.as_posix().replace("/", "%"))
        if done.is_file() and not done.with_name(done.name + ".failed").exists():
            return done.read_text(encoding="utf-8")
    r = subprocess.run(["kustomize", "build", str(component)], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


examined = 0
for base in PREFIX:
    for component in sorted(p for p in pathlib.Path(base).glob("*") if p.is_dir()):
        examined += 1
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
            target = kustomization_namespace(component)
            for f in sorted(subst.glob("*.yaml")):
                text = f.read_text(encoding="utf-8")
                keys |= set(KEY_RE.findall(text))
                # (7) the template refuses this too
                for doc in clusters.load_all(text):
                    if (isinstance(doc, dict) and doc.get("kind") in ("Secret", "ConfigMap")
                            and (doc.get("metadata") or {}).get("namespace") == "flux-system"):
                        failures.append(
                            f"  {f}: {doc['kind']}/{doc['metadata']['name']} is in flux-system, but "
                            f"its component's Kustomizations are in {target}; move it there"
                        )
        namespaces = declared_namespaces(component)
        refs = exported_keys(component, namespaces)
        if not keys and not refs and not marked and not namespaces:
            continue

        built = build(component)
        if built is None:
            continue  # validate.sh's build loop reports this

        build_egress(component, built)

        # (6) one owner per Namespace
        for doc in clusters.load_all(built):
            if doc and doc.get("kind") == "Namespace" and doc["metadata"]["name"] in namespaces:
                failures.append(
                    f"  {component}: Namespace/{doc['metadata']['name']} is in both the component "
                    f"and namespaces/; removing it from either prunes it"
                )
        if not keys and not refs and not marked:
            continue

        pattern = re.compile(r'\$\{(' + "|".join(sorted(keys)) + r')\}') if keys else None
        for doc in clusters.load_all(built):
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

# (5) nothing outside artifacts/, (7) no hand-set control-namespace label
for f in sorted(f for tree in CLUSTERS + ["shared"] for f in pathlib.Path(tree).rglob("*.yaml")):
    text = f.read_text(encoding="utf-8")
    if CONTROL_LABEL in text:
        for doc in clusters.load_all(text):
            if isinstance(doc, dict) and CONTROL_LABEL in (((doc.get("metadata") or {}).get("labels")) or {}):
                failures.append(f"  {f}: sets label {CONTROL_LABEL}, which only the generator may: it grants sops-age")
    if f.parent.name == "artifacts":
        continue
    if not re.search(r"^kind: (ImageComposition|ImageBuild)\s*$", text, re.M):
        continue
    for doc in clusters.load_all(text):
        if isinstance(doc, dict) and doc.get("kind") in KINDS:
            failures.append(f"  {f}: {doc['kind']}/{doc['metadata']['name']} is not in an artifacts/ dir")

if not examined:
    failures.append("  no component found: the group bases in the root HelmReleases match no directory")

if failures:
    print("== substitution check failed ==", file=sys.stderr)
    print(*failures, sep="\n", file=sys.stderr)
    sys.exit(1)
print(f"{examined} component(s): substituted values stay in Secrets, and marked generators keep a reachable name")
