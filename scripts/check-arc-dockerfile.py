#!/usr/bin/env python3
"""The ARC runner images are stages of one Dockerfile, kept as an identical inline copy in each
ImageBuild: oci-composer refuses a configMapRef Dockerfile under --require-pinned-sources and
cannot build one ImageBuild FROM another (apps/github-runner/README.md, "Runner images")."""
import pathlib, sys
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
D = ROOT / "kube-cluster/apps/github-runner/artifacts"
files = ["arc-runner-base.yaml", "arc-runner-k0s-flux.yaml"]
texts = [yaml.safe_load((D / f).read_text(encoding="utf-8"))["spec"]["dockerfile"]["inline"] for f in files]
if texts[0] != texts[1]:
    sys.exit(f"{files[0]} and {files[1]} must hold the same Dockerfile; edit one, copy it to the other")
print("ARC runner Dockerfiles identical")
