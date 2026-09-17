#!/usr/bin/env python3
"""Check every SOPS-encrypted file for the damage that is invisible without a key.

A file whose MAC no longer validates is accepted by git, by yamllint, by kustomize and
by every check in this repo; only kustomize-controller notices, and it notices by
silently refusing to reconcile the whole Kustomization. The same is true of a file
encrypted to a recipient Flux does not hold, or one where half the values are still
plaintext.

CI holds no age private key, so this asserts only what is checkable from the ciphertext:

  * the `sops:` block exists, with a `mac` and a `lastmodified`
  * every value under the paths `encrypted_regex` covers is an ENC[AES256_GCM,...]
    string -- a half-encrypted file
  * the recorded `encrypted_regex` matches the one .sops.yaml prescribes for that path,
    so a file encrypted outside the repo's rules (`sops -e /dev/stdin`, say, whose name
    matches no creation_rule) is caught
  * the age recipients are exactly the ones .sops.yaml prescribes -- a file encrypted to
    a key the cluster does not have
  * the file still parses as YAML and every document is a Secret

WHAT THIS CANNOT CATCH: a broken MAC. Deciding whether the MAC still validates means
decrypting, which needs the age private key, which is deliberately not in CI. A file can
pass every check here and still be undecryptable by Flux. `scripts/check-sops-mac.sh`,
run on a host that holds the key, is the only check for that.

Encrypted files are found by the ENC[AES256_GCM marker rather than by filename, the same
way scripts/make-public.py does: a glob silently skips whatever it has not heard of.
"""
import pathlib
import re
import sys

import yaml

MARKER = "ENC[AES256_GCM"
ENC_VALUE = re.compile(r"^ENC\[AES256_GCM,data:.*,type:\w+\]$")
ROOT = pathlib.Path(__file__).resolve().parent.parent
failures = []


def creation_rules():
    doc = yaml.safe_load((ROOT / ".sops.yaml").read_text(encoding="utf-8")) or {}
    return doc.get("creation_rules") or []


def rule_for(rel: str, rules):
    """The first rule whose path_regex matches, which is how sops itself picks one.

    sops uses a substring match, not a full match -- so does this.
    """
    for rule in rules:
        pattern = rule.get("path_regex")
        if pattern is None or re.search(pattern, rel):
            return rule
    return None


def recipients(rule):
    return {r.strip() for r in str(rule.get("age") or "").split(",") if r.strip()}


rules = creation_rules()

files = sorted(
    p for p in (ROOT / "kube-cluster").rglob("*")
    if p.is_file() and p.suffix in {".yaml", ".yml"}
    and MARKER in p.read_text(encoding="utf-8", errors="replace")
)

for path in files:
    rel = path.relative_to(ROOT).as_posix()
    rule = rule_for(rel, rules)
    if rule is None:
        failures.append(f"  {rel}: encrypted, but no creation_rule in .sops.yaml matches "
                        f"its path -- re-encrypting it would use no rule at all")
        continue
    want_regex = rule.get("encrypted_regex")
    want_recipients = recipients(rule)

    try:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except yaml.YAMLError as e:
        failures.append(f"  {rel}: not parseable as YAML ({str(e).splitlines()[0]})")
        continue

    key_re = re.compile(want_regex) if want_regex else None

    for i, doc in enumerate(d for d in docs if d is not None):
        where = f"  {rel}" + (f" (document {i + 1})" if len(docs) > 1 else "")

        if not isinstance(doc, dict) or doc.get("kind") != "Secret":
            kind = doc.get("kind") if isinstance(doc, dict) else type(doc).__name__
            failures.append(f"{where}: kind is {kind!r}, not Secret")
            continue

        sops = doc.get("sops")
        if not isinstance(sops, dict):
            failures.append(f"{where}: no `sops:` block -- this document is in the clear "
                            f"inside an otherwise encrypted file")
            continue
        for field in ("mac", "lastmodified"):
            if not sops.get(field):
                failures.append(f"{where}: sops block has no {field}")
        if sops.get("mac") and not ENC_VALUE.match(str(sops["mac"])):
            failures.append(f"{where}: sops.mac is not an ENC[AES256_GCM,...] value")

        have = {a.get("recipient") for a in (sops.get("age") or []) if isinstance(a, dict)}
        if have != want_recipients:
            failures.append(
                f"{where}: encrypted to {sorted(have) or '[]'}, but .sops.yaml says "
                f"{sorted(want_recipients)} -- the cluster's key cannot decrypt this")

        if sops.get("encrypted_regex") != want_regex:
            failures.append(
                f"{where}: sops.encrypted_regex is {sops.get('encrypted_regex')!r}, "
                f"but .sops.yaml prescribes {want_regex!r} for this path -- encrypted "
                f"outside the repo's rules")

        # A half-encrypted file: anything under data/stringData still readable.
        if key_re is None:
            continue
        for key, block in doc.items():
            if not key_re.search(key) or not isinstance(block, dict):
                continue
            for name, value in block.items():
                if not isinstance(value, str) or not ENC_VALUE.match(value):
                    failures.append(
                        f"{where}: {key}.{name} is not encrypted -- the value is in the "
                        f"clear in git")

if not files:
    print("== sops check failed ==", file=sys.stderr)
    print("  no encrypted files found under kube-cluster/ -- the marker search is broken",
          file=sys.stderr)
    sys.exit(1)

if failures:
    print("== sops check failed ==", file=sys.stderr)
    print(*failures, sep="\n", file=sys.stderr)
    sys.exit(1)

print(f"{len(files)} encrypted file(s): structure, recipients and encrypted_regex OK "
      f"(MAC NOT checked -- needs the key, see scripts/check-sops-mac.sh)")
