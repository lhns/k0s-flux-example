#!/usr/bin/env python3
"""Tests scripts/secret-from-clipboard.py against a throwaway age key in a scratch repo.

Uses the real clipboard. Its text is saved first and restored after (on X11 only while
something else owns it, such as a clipboard manager; non-text content is lost). Needs sops,
age-keygen and a display (Xvfb in a container). Never reads the cluster's key or any
existing secret. The dialog cases open real dialogs for a few seconds; SKIP_DIALOG=1 skips them.
"""
import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter

HERE = pathlib.Path(__file__).resolve().parent.parent
passed, failed = 0, 0


def ok(name):
    global passed
    passed += 1
    print(f"  ok    {name}")


def bad(name, why):
    global failed
    failed += 1
    print(f"  FAIL  {name}: {why}", file=sys.stderr)


for tool in ("sops", "age-keygen"):
    if not shutil.which(tool):
        sys.exit(f"missing tool: {tool}")

tk = tkinter.Tk()
tk.withdraw()
try:
    saved = tk.clipboard_get()
except tkinter.TclError:
    saved = None

tmp = pathlib.Path(tempfile.mkdtemp())
repo = tmp / "repo"
(repo / "scripts").mkdir(parents=True)
(repo / "kube-cluster/apps/foo").mkdir(parents=True)
shutil.copy(HERE / "scripts/secret-from-clipboard.py", repo / "scripts")
shutil.copy(HERE / "scripts/check-sops.py", repo / "scripts")
key = tmp / "key.txt"
subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
recipient = re.search(r"age1[0-9a-z]+", key.read_text()).group(0)
# The first rule has no encrypted_regex, so the .cmd sops stand-ins below get no `|` for
# cmd.exe to misread as a pipe.
(repo / ".sops.yaml").write_text(
    "creation_rules:\n"
    "  # comment\n"
    "  - path_regex: kube-cluster/noregex/\n"
    f"    age: \"{recipient}\"\n"
    "  - path_regex: kube-cluster/.*\\.yaml$\n"
    "    encrypted_regex: '^(data|stringData)$'\n"
    f"    age: {recipient}\n", encoding="utf-8")
empty_cfg = tmp / "empty.yaml"
empty_cfg.write_text("")
env = {k: v for k, v in os.environ.items() if not k.startswith("SOPS_")}
env["PYTHONIOENCODING"] = "utf-8"


def set_clipboard(text):
    if os.name == "nt":
        return win_set_clipboard(text)
    tk.clipboard_clear()
    if text is not None:
        tk.clipboard_append(text)
    tk.update()


def win_set_clipboard(text):
    """Through the Win32 API: Tk's delayed rendering loses the text when the test exits,
    which would lose the user's restored clipboard."""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32")
    k32 = ctypes.WinDLL("kernel32")
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    for _ in range(100):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        raise OSError("OpenClipboard failed")
    try:
        u32.EmptyClipboard()
        if text is not None:
            buf = text.encode("utf-16-le", "surrogatepass") + b"\0\0"
            h = k32.GlobalAlloc(0x0002, len(buf))  # GMEM_MOVEABLE
            ctypes.memmove(k32.GlobalLock(h), buf, len(buf))
            k32.GlobalUnlock(h)
            if not u32.SetClipboardData(13, h):  # CF_UNICODETEXT; the system owns h now
                raise OSError("SetClipboardData failed")
    finally:
        u32.CloseClipboard()


def run(clip, *args, path_prefix=None, later=None, actions=None):
    """Runs the script with `clip` on the clipboard; stdin closed, as under an agent.
    `later` = (seconds, text) replaces the clipboard while the script runs; `actions` drive
    the dialog through its test hook."""
    set_clipboard(clip)
    start = time.monotonic()
    e = dict(env)
    if actions is not None:
        (tmp / "actions.json").write_text(json.dumps(actions), encoding="utf-8")
        e["SFC_TEST_ACTIONS"] = str(tmp / "actions.json")
    if path_prefix:
        e["PATH"] = str(path_prefix) + os.pathsep + e["PATH"]
    p = subprocess.Popen([sys.executable, str(repo / "scripts/secret-from-clipboard.py"), *args],
                         cwd=repo, env=e, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # Keep serving the clipboard: on X11 its owner has to answer the script's request.
    while p.poll() is None:
        if later and time.monotonic() - start >= later[0]:
            set_clipboard(later[1])
            later = None
        tk.update()
        time.sleep(0.02)
    return p.returncode, p.stdout.read().decode("utf-8", "replace")


def decrypt(path, k, section="stringData"):
    out = subprocess.run(["sops", "--config", str(empty_cfg), "decrypt", "--output-type", "json", str(path)],
                         env={**env, "SOPS_AGE_KEY_FILE": str(key)}, capture_output=True, check=True).stdout
    doc = json.loads(out.decode("utf-8"))
    return base64.b64decode(doc["data"][k]) if section == "data" else doc["stringData"][k]


def roundtrip(name, clip, want):
    out = f"kube-cluster/apps/foo/secret-{name}.yaml"
    rc, log = run(clip, "--yes", out, name, "foo", "VALUE")
    if rc != 0:
        return bad(name, log)
    got = decrypt(repo / out, "VALUE")
    if got != want:
        return bad(name, "decrypted value differs")
    text = (repo / out).read_text(encoding="utf-8")
    for line in want.split("\n"):
        if len(line) >= 8 and (line in text or line in log):
            return bad(name, "plaintext in the file or the output")
    if "value: text" not in log or "characters" in log:
        return bad(name, f"unexpected output: {log}")
    ok(name)


def refuses(name, clip, msg, *args, path_prefix=None):
    rc, log = run(clip, *args, path_prefix=path_prefix)
    if rc == 0:
        return bad(name, "exited 0")
    if msg not in log:
        return bad(name, f"no {msg!r} in: {log}")
    if isinstance(clip, str) and len(clip.strip()) >= 8 and clip.strip() in log:
        return bad(name, "value printed")
    ok(name)
    return log


PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n\nBKcwggSjAgEAAoIBAQC7\n-----END PRIVATE KEY-----"
try:
    print("== roundtrips ==")
    roundtrip("plain", "ghp_0123456789abcdefABCDEF", "ghp_0123456789abcdefABCDEF")
    special = "a b  \"dq\" 'sq' $HOME ${X} `id` \\n \\\\ back\\slash \ttab: # {x} [y] &z *w !v %u @t"
    roundtrip("special", special, special)
    roundtrip("trailing-lf", "trailing-newline-value\n", "trailing-newline-value")
    roundtrip("trailing-crlf", "trailing-crlf-value\r\n\r\n", "trailing-crlf-value")
    roundtrip("padding", "  padded value \t \n", "  padded value \t ")
    roundtrip("multiline", PEM + "\n", PEM)
    roundtrip("multiline-crlf", "line-one-crlf\r\nline-two-crlf\r\n", "line-one-crlf\nline-two-crlf")
    roundtrip("unicode", "p\u00e4ssw\u00f6rd \u2713 \u65e5\u672c \u00a0nbsp \u2028ls", "p\u00e4ssw\u00f6rd \u2713 \u65e5\u672c \u00a0nbsp \u2028ls")
    roundtrip("emoji", "key-\U0001F510-\U0001F600-end", "key-\U0001F510-\U0001F600-end")
    roundtrip("yaml-lookalike", "- key: value\n  other: [1, 2]\n---\n...", "- key: value\n  other: [1, 2]\n---\n...")
    roundtrip("short", "ab", "ab")

    print("== output ==")
    rc, log = run("replaced-value-123", "--yes", "--force", "kube-cluster/apps/foo/secret-plain.yaml", "plain", "foo", "VALUE")
    if rc == 0 and decrypt(repo / "kube-cluster/apps/foo/secret-plain.yaml", "VALUE") == "replaced-value-123":
        ok("--force replaces")
    else:
        bad("--force", log)

    rc, log = run("dry-run-value-123", "--yes", "--dry-run", "kube-cluster/apps/foo/secret-dry.yaml", "dry", "foo", "VALUE")
    if rc == 0 and not (repo / "kube-cluster/apps/foo/secret-dry.yaml").exists():
        ok("--dry-run writes nothing")
    else:
        bad("--dry-run", log)

    print("== refusals ==")
    existing = repo / "kube-cluster/apps/foo/secret-short.yaml"
    before = existing.read_bytes()
    refuses("existing", "new-value-xyz", "refusing to overwrite", "--yes", str(existing.relative_to(repo)), "short", "foo", "VALUE")
    (ok if existing.read_bytes() == before else lambda n: bad(n, "file changed"))("existing file untouched")

    refuses("empty", None, "clipboard is empty", "--yes", "kube-cluster/apps/foo/secret-e1.yaml", "e1", "foo", "VALUE")
    refuses("whitespace-only", " \t\r\n \n", "clipboard is empty", "--yes", "kube-cluster/apps/foo/secret-e2.yaml", "e2", "foo", "VALUE")
    refuses("outside-rules", "some-value-123", "no creation_rule in .sops.yaml matches", "--yes", "other/secret.yaml", "x", "foo", "VALUE")
    refuses("not-yaml", "some-value-123", "no creation_rule in .sops.yaml matches", "--yes", "kube-cluster/apps/foo/secret.txt", "x", "foo", "VALUE")
    refuses("outside-repo", "some-value-123", "is outside the repo", "--yes", "../elsewhere.yaml", "x", "foo", "VALUE")
    refuses("control-char", "bell\x07value", "control characters", "--yes", "kube-cluster/apps/foo/secret-e3.yaml", "e3", "foo", "VALUE")
    refuses("bad-key", "some-value-123", "invalid key", "--yes", "kube-cluster/apps/foo/secret-e4.yaml", "e4", "foo", "A B")
    refuses("timeout-zero", "some-value-123", "between 1s and 24h", "--timeout", "0", "kube-cluster/apps/foo/secret-e5.yaml", "e5", "foo", "VALUE")
    refuses("timeout-garbage", "some-value-123", "invalid timeout", "--timeout", "5x", "kube-cluster/apps/foo/secret-e5.yaml", "e5", "foo", "VALUE")

    # sops stand-ins: one that does not encrypt, one that fails quoting its input.
    fake = tmp / "fakebin"
    fake.mkdir()
    if os.name == "nt":
        (fake / "sops.cmd").write_text('@findstr "^"\r\n')
    else:
        (fake / "sops").write_text("#!/bin/sh\ncat\n")
        (fake / "sops").chmod(0o755)
    refuses("unencrypted", "leaky-value-123", "verification failed", "--yes",
            "kube-cluster/noregex/secret-e6.yaml", "e6", "foo", "VALUE", path_prefix=fake)
    if os.name == "nt":
        (fake / "sops.cmd").write_text('@findstr "^" 1>&2\r\n@exit /b 1\r\n')
    else:
        (fake / "sops").write_text("#!/bin/sh\ncat >&2\nexit 1\n")
    log = refuses("sops-error", "leaky-value-123", "sops failed", "--yes",
                  "kube-cluster/noregex/secret-e7.yaml", "e7", "foo", "VALUE", path_prefix=fake)
    if log is not None:
        (ok if "line withheld" in log else lambda n: bad(n, log))("sops error lines withheld")

    if os.environ.get("SKIP_DIALOG") == "1":
        print("  skip  dialog cases (SKIP_DIALOG=1)")
    else:
        start = time.monotonic()
        refuses("dialog-timeout", "timeout-value-123", "timed out after 1s", "--timeout", "1s", "--dry-run",
                "kube-cluster/apps/foo/secret-e8.yaml", "e8", "foo", "VALUE")
        if time.monotonic() - start > 30:
            bad("dialog-timeout", "took over 30s")
        refuses("dialog-opens-empty", None, "timed out after 1s", "--timeout", "1s", "--dry-run",
                "kube-cluster/apps/foo/secret-e9.yaml", "e9", "foo", "VALUE")

        def dialog(name, clip, actions, want=None, section="stringData", fail_msg=None):
            """Drives the dialog through its test hook. `want`: the value OK must encrypt;
            `fail_msg`: the error the run must end with. Returns the log."""
            out = f"kube-cluster/apps/foo/secret-{name}.yaml"
            rc, log = run(clip, "--timeout", "20s", out, name, "foo", "VALUE", actions=actions)
            if fail_msg:
                if rc == 0 or fail_msg not in log or (repo / out).exists():
                    return bad(name, log)
            elif rc != 0:
                return bad(name, log)
            elif decrypt(repo / out, "VALUE", section) != want:
                return bad(name, "decrypted value differs")
            ok(name)
            return log

        def expect(name, log, *fragments):
            if log is not None and all(f in log for f in fragments):
                ok(name)
            else:
                bad(name, log)

        log = dialog("live", None, [{"at": 0.8, "do": "report"},
                                    {"at": 1.2, "do": "clipboard", "text": "later-value-456\r\n"},
                                    {"at": 2.0, "do": "report"}, {"at": 2.2, "do": "ok"}], want="later-value-456")
        expect("OK disabled while empty, enabled once copied", log,
               "test: mode=text following=1 ok=disabled", "test: mode=text following=1 ok=enabled")
        expect("masked field hides the length (fixed placeholder)", log, "ok=enabled placeholder=1")

        # Reveal a multi-line value in place: markers, read-only, same window size.
        log = dialog("reveal-multiline", PEM, [{"at": 0.8, "do": "geometry", "tag": "hidden"},
                                               {"at": 0.9, "do": "reveal"}, {"at": 1.2, "do": "report"},
                                               {"at": 1.3, "do": "geometry", "tag": "shown"},
                                               {"at": 1.4, "do": "keys", "text": "zzz"},
                                               {"at": 1.6, "do": "reveal"}, {"at": 1.8, "do": "report"},
                                               {"at": 2.0, "do": "ok"}], want=PEM)
        expect("revealed multi-line shows markers and is read-only", log,
               "following=1 ok=enabled placeholder=0 shown=1 readonly=1 marker=1",
               "following=1 ok=enabled placeholder=1 shown=0 readonly=0 marker=0")
        sizes = re.findall(r"test: geometry (\w+)=(\d+x\d+)", log or "")
        if len(sizes) == 2 and sizes[0][1] == sizes[1][1]:
            ok("window size unchanged by reveal")
        else:
            bad("window size unchanged by reveal", sizes)
        log = dialog("reveal-single", "single-line-888", [{"at": 0.8, "do": "reveal"}, {"at": 1.0, "do": "report"},
                                                          {"at": 1.2, "do": "ok"}], want="single-line-888")
        expect("revealed single-line stays editable", log, "shown=1 readonly=0 marker=0")

        dialog("typed-keys", "clip-value-777", [{"at": 0.8, "do": "keys", "text": "abc123XYZ"},
                                                {"at": 1.5, "do": "ok"}], want="abc123XYZ")
        if log and "later-value-456" in log:
            bad("live", "value printed")

        log = dialog("typed", "clip-value-111", [{"at": 0.8, "do": "type", "text": "typed-value-222"},
                                                 {"at": 1.2, "do": "clipboard", "text": "clip-value-333"},
                                                 {"at": 2.0, "do": "report"}, {"at": 2.2, "do": "ok"}],
                     want="typed-value-222")
        expect("typing stops following the clipboard", log, "following=0")

        files = tmp / "files"
        files.mkdir()
        text_file = files / "notes.txt"
        text_file.write_bytes(b"first-line-value\r\nsecond line\n")
        dialog("file-text", "clip-value-444", [{"at": 0.6, "do": "file", "path": str(text_file)},
                                               {"at": 1.2, "do": "ok"}],
               want=text_file.read_bytes(), section="data")
        binary = files / "blob.bin"
        binary.write_bytes(b"\x00\xff\x00" + os.urandom(4096) + b"\xff\x00")
        log = dialog("file-binary-drop", "clip-value-555", [{"at": 0.6, "do": "drop", "paths": [str(binary)]},
                                                            {"at": 1.4, "do": "report"}, {"at": 1.6, "do": "ok"}],
                     want=binary.read_bytes(), section="data")
        expect("drop enters file mode" + (" (WM_DROPFILES)" if os.name == "nt" else ""), log, "mode=file")

        log = dialog("file-clear-x", "after-clear-value", [{"at": 0.6, "do": "file", "path": str(binary)},
                                                           {"at": 1.0, "do": "clear_x"}, {"at": 1.8, "do": "report"},
                                                           {"at": 2.0, "do": "ok"}],
                     want="after-clear-value")
        expect("x returns to text mode, following", log, "mode=text following=1 ok=enabled")
        log = dialog("file-click-field", "click-clear-value", [{"at": 0.6, "do": "file", "path": str(binary)},
                                                               {"at": 1.0, "do": "click_field"},
                                                               {"at": 1.8, "do": "report"}, {"at": 2.0, "do": "ok"}],
                     want="click-clear-value")
        expect("clicking the disabled field returns to text mode", log, "mode=text following=1")

        big = files / "big.bin"
        big.write_bytes(b"\x01" * (1024 * 1024 + 1))
        log = dialog("file-too-big", None, [{"at": 0.6, "do": "file", "path": str(big)}, {"at": 1.0, "do": "report"},
                                            {"at": 1.2, "do": "cancel"}], fail_msg="cancelled")
        expect("over 1 MiB refused", log, "mode=text", "ok=disabled", "over 1 MiB")
        log = dialog("file-folder", None, [{"at": 0.6, "do": "file", "path": str(files)}, {"at": 1.0, "do": "report"},
                                           {"at": 1.2, "do": "cancel"}], fail_msg="cancelled")
        expect("folder refused", log, "mode=text", "is a folder")
        log = dialog("drop-two", None, [{"at": 0.6, "do": "drop", "paths": [str(text_file), str(binary)]},
                                        {"at": 1.4, "do": "report"}, {"at": 1.6, "do": "cancel"}], fail_msg="cancelled")
        expect("two dropped files refused", log, "mode=text", "one file at a time")

    try:
        import yaml  # noqa: F401 -- check-sops.py needs PyYAML
        r = subprocess.run([sys.executable, "scripts/check-sops.py"], cwd=repo, capture_output=True, text=True)
        (ok if r.returncode == 0 else lambda n: bad(n, r.stderr))("check-sops.py accepts every file")
    except ImportError:
        print("  skip  check-sops.py (no PyYAML)")

    leftover = [p.name for p in (repo / "kube-cluster/apps/foo").iterdir()
                if re.match(r"secret-e\d|secret-dry|.*\.yaml\.", p.name)]
    (ok if not leftover and not (repo / "other").exists() else lambda n: bad(n, leftover))("refusals left no files")
finally:
    set_clipboard(saved)
    tk.destroy()
    shutil.rmtree(tmp, ignore_errors=True)

print(f"== {passed} passed, {failed} failed ==")
sys.exit(1 if failed else 0)
