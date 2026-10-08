#!/usr/bin/env python3
"""Every oci-composer sourceRef must carry a `revision` matching its GitRepository's pin.

Without it a pin bump can build the PREVIOUS revision's content and roll it out; with a WRONG
one it waits for a revision that will never arrive. See kube-cluster/infra/oci-composer/README.md.

  check-source-pins.py          verify (scripts/validate.sh)
  check-source-pins.py --fix    rewrite revisions to match (renovate.json postUpgradeTasks)
"""
import pathlib
import re
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import clusters

ROOT = clusters.ROOT
KINDS = ("ImageComposition", "ImageBuild")


def expected_revision(spec):
    """What source-controller publishes as status.artifact.revision for this pin.

    fluxcd/pkg git.Commit.String(): "<ref>@sha1:<hash>" for a named ref, bare "sha1:<hash>" when
    the checkout names none — which a commit-only pin does. The ref half alone is accepted by the
    controller for the first form, and is what a human can write.
    """
    ref = (spec or {}).get("ref") or {}
    if ref.get("commit"):
        return "sha1:" + ref["commit"]
    if ref.get("tag"):
        return ref["tag"]
    return None  # branch, semver, name: nothing fixed to pin to


def load(path):
    docs = []
    try:
        docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(d, dict)]
    except yaml.YAMLError:
        if re.search(r"kind: (ImageComposition|ImageBuild|GitRepository)", path.read_text(encoding="utf-8")):
            raise
    return docs


def source_refs(doc):
    """(layer name, sourceRef) for every sourceRef an object consumes."""
    spec = doc.get("spec") or {}
    if doc["kind"] == "ImageComposition":
        for layer in spec.get("layers") or []:
            if isinstance(layer, dict) and isinstance(layer.get("sourceRef"), dict):
                yield layer.get("name", "?"), layer["sourceRef"]
    else:
        ref = (spec.get("context") or {}).get("sourceRef")
        if isinstance(ref, dict):
            yield "context", ref


def collect(cluster):
    """One cluster with shared/: a sourceRef only reaches a GitRepository in its own cluster."""
    files = sorted(p for tree in (cluster, "shared") for p in (ROOT / tree).rglob("*.yaml"))
    docs = {f: load(f) for f in files}

    # Indexed by name alone: a sourceRef is same-namespace only, and a duplicated name would make
    # a text-level --fix ambiguous, so it is refused rather than guessed at.
    sources, dupes = {}, set()
    for objs in docs.values():
        for d in objs:
            if d.get("kind") == "GitRepository":
                name = (d.get("metadata") or {}).get("name")
                if name in sources and sources[name] != expected_revision(d.get("spec")):
                    dupes.add(name)
                sources[name] = expected_revision(d.get("spec"))

    wanted, problems = {}, []
    for path, objs in docs.items():
        for d in objs:
            if d.get("kind") not in KINDS:
                continue
            where = f"{path.relative_to(ROOT)}: {d['kind']}/{(d.get('metadata') or {}).get('name')}"
            for layer, ref in source_refs(d):
                name = ref.get("name")
                if name in dupes:
                    problems.append(f"{where} layer {layer}: GitRepository {name} is not unique")
                    continue
                if name not in sources:
                    problems.append(f"{where} layer {layer}: no GitRepository {name} in this repo")
                    continue
                want = sources[name]
                if want is None:
                    problems.append(
                        f"{where} layer {layer}: GitRepository {name} tracks a branch or range, "
                        f"which cannot be pinned — pin it to a tag or a commit")
                    continue
                wanted[name] = want
                if ref.get("revision") != want:
                    problems.append(
                        f"{where} layer {layer}: revision is {ref.get('revision')!r}, "
                        f"GitRepository {name} is pinned at {want!r}")

    return files, wanted, problems


def main():
    wanted, problems = {}, []
    for cluster in clusters.clusters():
        files, want, probs = collect(cluster)
        if "--fix" in sys.argv[1:] and probs:
            for path in files:
                rewrite(path, want)
            _, want, probs = collect(cluster)
        wanted.update({(cluster, k): v for k, v in want.items()})
        problems += probs

    if problems:
        print("\n".join(problems), file=sys.stderr)
        print("\nsee kube-cluster/infra/oci-composer/README.md", file=sys.stderr)
        return 1
    print(f"source pins OK ({len(wanted)} pinned source(s))")
    return 0


def rewrite(path, wanted):
    """Set `revision:` under every sourceRef naming a known source, inserting it if absent."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out, i, changed = [], 0, False
    while i < len(lines):
        out.append(lines[i])
        m = re.match(r"^(\s*)sourceRef:\s*$", lines[i])
        i += 1
        if not m:
            continue
        indent = m.group(1) + "  "
        block = []
        while i < len(lines) and (lines[i].startswith(indent) or not lines[i].strip()):
            block.append(lines[i])
            i += 1
        name = next((re.match(r"\s*name:\s*(\S+)", b).group(1) for b in block
                     if re.match(r"\s*name:\s*(\S+)", b)), None)
        want = wanted.get(name)
        if want is None:
            out.extend(block)
            continue
        line = f"{indent}revision: {want}\n"
        for j, b in enumerate(block):
            if re.match(r"\s*revision:", b):
                changed |= block[j] != line
                block[j] = line
                break
        else:
            at = next(j for j, b in enumerate(block) if re.match(r"\s*name:\s*\S+", b))
            block.insert(at + 1, line)
            changed = True
        out.extend(block)
    if changed:
        path.write_text("".join(out), encoding="utf-8", newline="")


if __name__ == "__main__":
    sys.exit(main())
