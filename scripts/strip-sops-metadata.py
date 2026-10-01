#!/usr/bin/env python3
"""Drop the top-level `sops:` block from every document on stdin, as kustomize-controller does
before applying, so `kubeconform -strict` validates encrypted Secrets as the Secrets they become.

Line-based on purpose: re-serialising the YAML would change scalars (a leading-zero number
string loses its quotes), and the rest of the stream must reach kubeconform byte for byte.
"""
import sys

skipping = False
for line in sys.stdin:
    if line.startswith("sops:"):
        skipping = True
        continue
    if skipping:
        # The block ends at the next top-level key or document separator.
        if line.startswith((" ", "\t")) or line.strip() == "":
            continue
        skipping = False
    sys.stdout.write(line)
