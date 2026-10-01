#!/usr/bin/env python3
"""Encrypt a secret into a new SOPS Secret file, without the value reaching a terminal, a
command line, an environment variable or a plaintext file.

Opens a small dialog with a masked field that follows the clipboard live until you type or
paste into it (Enter = OK, Esc = cancel). The folder button, or dropping a file on the
window, takes a file instead (at most 1 MiB, any bytes). On OK, writes Secret
NAMESPACE/NAME with KEY set to exactly that value -- text under stringData, a file under
data as base64 -- encrypted for the .sops.yaml creation rule matching PATH. Prints nothing of
the value, not even a text value's length. Never prompts on the terminal and never reads
stdin. Stdlib only; the dialog and the clipboard need tkinter (drag and drop on Linux needs
tkinterdnd2).

  secret-from-clipboard.py kube-cluster/apps/foo/secret-bar.yaml bar foo TOKEN
  secret-from-clipboard.py --timeout 2m kube-cluster/apps/foo/tls.yaml foo-tls foo tls.key
  secret-from-clipboard.py --dry-run kube-cluster/apps/foo/secret-bar.yaml bar foo TOKEN

Text: trailing CR/LF is stripped and CRLF becomes LF; everything else, including multi-line
text such as a PEM key, is kept exactly. Control characters other than tab are refused.

One Secret per file: adding a key to an existing encrypted file needs the cluster's private
key, which only the cluster holds.
"""
import argparse
import base64
import json
import math
import os
import pathlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAX_FILE = 1024 * 1024  # a Secret's size limit
MASK = chr(0x2022)
PLACEHOLDER = "x" * 12  # shown masked in place of a followed value, whose length stays hidden
NEWLINE_MARK = chr(0x21b5)  # a revealed line break; display only
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# YAML folds or rejects these raw inside a double-quoted scalar; json.dumps leaves them.
YAML_UNSAFE = re.compile(r"[\x80-\x9f\u2028\u2029\ufeff]")
WINDOWS = sys.platform == "win32"


def fail(msg):
    print(f"secret-from-clipboard: {msg}", file=sys.stderr)
    sys.exit(1)


def parse_timeout(text):
    m = re.fullmatch(r"(\d{1,6})([smh]?)", text)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid timeout {text!r} (seconds, or e.g. 90s, 5m, 1h)")
    seconds = int(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[m[2]]
    if not 1 <= seconds <= 86400:
        raise argparse.ArgumentTypeError("timeout must be between 1s and 24h")
    return seconds


def unquote(v):
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return json.loads(v)
    return v


def find_rule(rel):
    """The first creation_rule whose path_regex matches, by substring search, as sops and
    scripts/check-sops.py pick it. Enough YAML for a flat list of scalar rules; anything
    nested is refused rather than misread.
    """
    rules, cur, in_rules = [], None, False
    for raw in (ROOT / ".sops.yaml").read_text(encoding="utf-8").splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw)
        if not line.strip():
            continue
        if not line[0].isspace():
            in_rules = re.fullmatch(r"creation_rules:\s*", line) is not None
            continue
        if not in_rules:
            continue
        m = re.fullmatch(r"\s*(-\s+)?(\w+):\s*(.*?)\s*", line)
        if not m or (cur is None and not m[1]):
            fail(f".sops.yaml: cannot parse {raw!r}")
        if not m[3]:
            fail(f".sops.yaml: nested {m[2]!r} is not supported")
        if m[1]:
            cur = {}
            rules.append(cur)
        cur[m[2]] = unquote(m[3])
    for rule in rules:
        if "path_regex" not in rule or re.search(rule["path_regex"], rel):
            if set(rule) - {"path_regex", "age", "encrypted_regex"} or not rule.get("age"):
                fail(f"the rule matching {rel} is not age-only; not supported")
            return rule
    fail(f"no creation_rule in .sops.yaml matches {rel}")


# --- clipboard and value checks ------------------------------------------------------------

def open_tk():
    if WINDOWS:  # before Tk exists, or Windows bitmap-scales the whole window
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    try:
        import tkinter
    except ImportError:
        fail("tkinter is missing (Debian/Ubuntu: apt install python3-tk); nothing was written")
    try:
        try:
            from tkinterdnd2 import TkinterDnD
            root = TkinterDnD.Tk()
            root.dnd = True
        except (ImportError, RuntimeError):  # RuntimeError: its tkdnd binary did not load
            root = tkinter.Tk()
            root.dnd = False
    except tkinter.TclError as e:
        fail(f"no display for the clipboard and dialog ({e}); nothing was written")
    root.withdraw()
    return root


def clipboard_types(root):
    return ["UTF8_STRING", "STRING"] if root.tk.call("tk", "windowingsystem") == "x11" else ["STRING"]


def try_clipboard(root, types):
    """The clipboard text, or None if it is empty, not text, or held open by another process."""
    import tkinter
    for t in types:
        try:
            return root.clipboard_get(type=t)
        except tkinter.TclError:
            pass
    return None


def check(raw):
    """(normalized value, None) if usable, else (None, reason); reason is None for empty."""
    value = (raw or "").rstrip("\r\n").replace("\r\n", "\n")
    if not value.strip():
        return None, None
    if CONTROL.search(value):
        return None, "contains control characters"
    return value, None


def read_clipboard(root):
    types = clipboard_types(root)
    # Retried: right after a copy, Windows' clipboard history can hold the clipboard open.
    for _ in range(5):
        raw = try_clipboard(root, types)
        if raw is not None:
            return raw
        time.sleep(0.1)
    return ""


def read_file(path):
    """(bytes, None) or (None, reason). Only a regular file of at most MAX_FILE bytes."""
    name = os.path.basename(path.rstrip("/\\")) or path
    try:
        if os.path.isdir(path):
            return None, f"{name} is a folder, not a file"
        with open(path, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return None, f"{name} is not a regular file"
            data = f.read(MAX_FILE + 1)
    except OSError as e:
        return None, f"cannot read {name}: {e.strerror}"
    if len(data) > MAX_FILE:
        return None, f"{name} is over 1 MiB, the Secret size limit"
    if not data:
        return None, f"{name} is empty"
    return data, None


def human_size(n):
    return f"{n} bytes" if n < 1024 else f"{n / 1024:.1f} KiB"


# --- icons: rendered to PNG at the display's scale, so they stay crisp at any DPI ------------

def _seg(x, y, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - ax - t * dx, y - ay - t * dy)


def _shape(kind):
    s = 0.09  # stroke width, in icon units

    def lens(x, y, r):  # the almond of an eye: two intersecting circles
        return math.hypot(x - .5, y - .777) < r and math.hypot(x - .5, y - .223) < r

    def eye(x, y):
        return (lens(x, y, .537) and not lens(x, y, .537 - s)) or math.hypot(x - .5, y - .5) < .14

    def eye_off(x, y):
        d = _seg(x, y, .14, .14, .86, .86)
        return (eye(x, y) and d > 1.5 * s) or d < s / 2

    def folder(x, y):
        body = .06 <= x <= .94 and .30 <= y <= .86
        tab = .06 <= x <= .44 and .16 <= y <= .32
        inner = .06 + s <= x <= .94 - s and .30 + s <= y <= .86 - s
        return (body or tab) and not inner

    def file(x, y):
        doc = .2 <= x <= .8 and .06 <= y <= .94 and x - .56 <= y - .06
        inner = .2 + s <= x <= .8 - s and .06 + s <= y <= .94 - s and x - .56 <= y - .06 - 1.5 * s
        fold = min(_seg(x, y, .56, .06, .56, .30), _seg(x, y, .56, .30, .8, .30), _seg(x, y, .56, .06, .8, .30))
        return (doc and not inner) or fold < s / 2

    return {"eye": eye, "eye_off": eye_off, "folder": folder, "file": file}[kind]


def icon(master, kind, size, rgb=(0x5f, 0x5f, 0x5f)):
    import tkinter
    inside, ss, rows = _shape(kind), 4, []
    for py in range(size):
        row = bytearray(b"\0")
        for px in range(size):
            cov = sum(inside((px + (sx + .5) / ss) / size, (py + (sy + .5) / ss) / size)
                      for sy in range(ss) for sx in range(ss))
            row += bytes((*rgb, round(255 * cov / ss / ss)))
        rows.append(bytes(row))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))
    return tkinter.PhotoImage(master=master, data=base64.b64encode(png))


# --- Windows drag and drop: WM_DROPFILES on a subclassed toplevel ---------------------------

def windows_drop(hwnd, dropped):
    """Accept dropped files on `hwnd`, appending each drop's paths to `dropped`; returns the
    function that undoes it. The WndProc runs inside Tcl's event loop, where calling back
    into tkinter corrupts its thread state, so the dialog's poll picks the drops up."""
    import ctypes
    from ctypes import wintypes
    user32, shell32 = ctypes.WinDLL("user32"), ctypes.WinDLL("shell32")
    lresult = ctypes.c_ssize_t
    wndproc_t = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    # 64-bit needs the Ptr variant; 32-bit user32 only exports SetWindowLongW.
    setlong = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    setlong.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    setlong.restype = ctypes.c_void_p
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallWindowProcW.restype = lresult
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragFinish.argtypes = [wintypes.HANDLE]
    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    WM_DROPFILES, GWLP_WNDPROC = 0x0233, -4

    def proc(h, msg, wp, lp):
        if msg == WM_DROPFILES:
            try:
                paths = []
                for i in range(shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)):
                    n = shell32.DragQueryFileW(wp, i, None, 0) + 1
                    buf = ctypes.create_unicode_buffer(n)
                    shell32.DragQueryFileW(wp, i, buf, n)
                    paths.append(buf.value)
            finally:
                shell32.DragFinish(wp)
            dropped.append(paths)
            return 0
        return user32.CallWindowProcW(old, h, msg, wp, lp)

    callback = wndproc_t(proc)  # referenced by the closure below, so it outlives the window
    old = setlong(hwnd, GWLP_WNDPROC, ctypes.cast(callback, ctypes.c_void_p))
    shell32.DragAcceptFiles(hwnd, True)

    def undo():
        shell32.DragAcceptFiles(hwnd, False)
        setlong(hwnd, GWLP_WNDPROC, old)
        undo.callback = None

    undo.callback = callback
    return undo


def post_drop(hwnd, paths):
    """Test hook: synthesize the WM_DROPFILES that Explorer sends."""
    import ctypes
    from ctypes import wintypes
    k32, user32 = ctypes.WinDLL("kernel32"), ctypes.WinDLL("user32")
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    # DROPFILES {pFiles, pt.x, pt.y, fNC, fWide}, then NUL-separated paths ending in two NULs.
    buf = struct.pack("<IiiII", 20, 0, 0, 0, 1) + ("\0".join(paths) + "\0\0").encode("utf-16-le")
    h = k32.GlobalAlloc(0x0042, len(buf))  # GMEM_MOVEABLE | GMEM_ZEROINIT; DragFinish frees it
    ctypes.memmove(k32.GlobalLock(h), buf, len(buf))
    k32.GlobalUnlock(h)
    user32.PostMessageW(hwnd, 0x0233, h, 0)


# --- the dialog -------------------------------------------------------------------------------

def confirm(root, rel, secret, key, seconds):
    """('ok', 'text', str) | ('ok', 'file', bytes) | ('cancel' | 'timeout', None, None)."""
    import tkinter as tk
    from tkinter import filedialog, font, ttk

    style = ttk.Style(root)
    themes = style.theme_names()
    for theme in ("vista", "winnative", "aqua", "clam"):
        if theme in themes:
            style.theme_use(theme)
            break
    # Explicit masters throughout: a failed TkinterDnD.Tk() leaves a stray default root behind.
    if WINDOWS:
        for name in ("TkDefaultFont", "TkTextFont"):
            font.nametofont(name, root=root).configure(family="Segoe UI", size=9)
    scale = root.winfo_fpixels("1i") / 96

    def px(n):
        return int(round(n * scale))

    base = font.nametofont("TkDefaultFont", root=root).actual()
    small = font.Font(root=root, family=base["family"],
                      size=max(base["size"] - 1, 7) if base["size"] > 0 else base["size"])
    style.configure("Muted.TLabel", foreground="#6b6b6b", font=small)
    style.configure("Error.TLabel", foreground="#b3261e", font=small)
    size = px(16)
    img = {k: icon(root, k, size) for k in ("eye", "eye_off", "folder", "file")}

    types = clipboard_types(root)
    st = {"result": "cancel", "mode": "text", "file": None, "following": True, "raw": None,
          "misses": 0, "left": seconds, "shown": False, "updating": False, "error": None,
          "tick": None, "poll": None, "undrop": None, "dropped": [], "clip": "", "typed": ""}

    top = tk.Toplevel(root)
    top.title("Encrypt secret")
    top.resizable(False, False)
    top.attributes("-topmost", True)
    top.img = img  # PhotoImages vanish when unreferenced
    frame = ttk.Frame(top, padding=px(12))
    frame.grid(sticky="nsew")

    info = ttk.Frame(frame)
    info.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, px(10)))
    for i, (label, text) in enumerate([("File", rel), ("Secret", secret), ("Key", key)]):
        ttk.Label(info, text=label, style="Muted.TLabel").grid(row=i, column=0, sticky="w", padx=(0, px(12)))
        ttk.Label(info, text=text).grid(row=i, column=1, sticky="w")

    var = tk.StringVar(top)
    entry = ttk.Entry(frame, textvariable=var, show=MASK, width=44)
    entry.grid(row=1, column=0, sticky="we", ipady=px(2))
    eye = ttk.Button(frame, image=img["eye"], style="Toolbutton", takefocus=False)
    eye.grid(row=1, column=1, sticky="ns")
    browse = ttk.Button(frame, image=img["folder"], takefocus=False)
    browse.grid(row=1, column=2, sticky="ns", padx=(px(6), 0))

    fileline = ttk.Frame(frame)
    ttk.Label(fileline, image=img["file"]).grid(row=0, column=0, padx=(0, px(4)))
    filename = ttk.Label(fileline)
    filename.grid(row=0, column=1)
    filesize = ttk.Label(fileline, style="Muted.TLabel")
    filesize.grid(row=0, column=2, padx=(px(8), px(4)))
    unfile = ttk.Button(fileline, text=chr(0x2715), style="Toolbutton", width=2, takefocus=False)
    unfile.grid(row=0, column=3)
    error = ttk.Label(frame, style="Error.TLabel", wraplength=px(380))

    bottom = ttk.Frame(frame)
    bottom.grid(row=5, column=0, columnspan=3, sticky="we", pady=(px(12), 0))
    bottom.columnconfigure(0, weight=1)
    countdown = ttk.Label(bottom, style="Muted.TLabel")
    countdown.grid(row=0, column=0, sticky="w")
    ok = ttk.Button(bottom, text="OK", default="active")
    ok.grid(row=0, column=1)
    cancel = ttk.Button(bottom, text="Cancel")
    cancel.grid(row=0, column=2, padx=(px(6), 0))

    def text_value():
        return check(st["clip"] if st["following"] else st["typed"])

    def shown_text(value):
        """What the field displays. Revealed, line breaks become a marker, so it stays one
        line and the window never resizes; the value keeps the real ones."""
        if st["shown"]:
            return value.replace("\n", NEWLINE_MARK)
        return value if not st["following"] else PLACEHOLDER if check(value)[0] else ""

    def refresh():
        """Recompute what depends on the state. Called only when something changed."""
        if st["mode"] == "file":
            value, reason = None, st["error"]
            entry.state(["disabled"])
            fileline.grid(row=2, column=0, columnspan=3, sticky="w", pady=(px(6), 0))
            filename.configure(text=st["file"][0])
            filesize.configure(text=human_size(len(st["file"][1])))
            eye.state(["disabled"])
        else:
            value, reason = text_value()
            reason = reason or st["error"]
            entry.state(["!disabled"])
            fileline.grid_remove()
            eye.state(["!disabled"])
            entry.configure(show="" if st["shown"] else MASK)
            # Masked, a followed value shows a fixed-width placeholder: one bullet per
            # character would give away its length.
            raw = st["clip"] if st["following"] else st["typed"]
            display = shown_text(raw)
            if var.get() != display:
                set_text(display)
            # Editing text with markers in it would be ambiguous.
            entry.state(["readonly"] if st["shown"] and "\n" in raw else ["!readonly"])
        eye.configure(image=img["eye_off" if st["shown"] else "eye"])
        if reason:
            error.configure(text=reason)
            error.grid(row=4, column=0, columnspan=3, sticky="w", pady=(px(4), 0))
        else:
            error.grid_remove()
        usable = st["mode"] == "file" or value is not None
        ok.state(["!disabled"] if usable else ["disabled"])

    def set_text(text):
        st["updating"] = True
        var.set(text)
        st["updating"] = False

    def on_edit(*_):
        if st["updating"]:
            return
        st["following"] = False  # typed or pasted: the field is the value now
        st["typed"] = var.get()  # never shows markers here: that state is read-only
        st["error"] = None
        refresh()

    var.trace_add("write", on_edit)

    def before_edit(pasting):
        """Runs before the Entry's own binding: editing a masked, followed value starts from
        an empty field rather than from the placeholder."""
        def handler(e):
            typing = pasting or e.keysym in ("BackSpace", "Delete") or (
                len(e.char) == 1 and e.char >= " " and not e.state & 0x4)
            if typing and st["mode"] == "text" and st["following"] and not entry.instate(["readonly"]):
                st["following"] = False
                st["typed"] = st["clip"] if st["shown"] else ""
                set_text(st["typed"])
                entry.icursor("end")
        return handler

    entry.bind("<KeyPress>", before_edit(False))
    entry.bind("<<Paste>>", before_edit(True))

    def poll():
        st["poll"] = top.after(200, poll)
        while st["dropped"]:
            on_files(st["dropped"].pop(0))
        if st["mode"] != "text" or not st["following"]:
            return
        raw = try_clipboard(root, types)
        # A miss is also what an open clipboard looks like (Windows' clipboard history holds
        # it after a copy): only a second of misses counts as empty.
        if raw is None:
            st["misses"] += 1
            if st["misses"] < 5 and st["raw"] is not None:
                return
        else:
            st["misses"] = 0
        if raw == st["raw"] and st["raw_seen"]:
            return  # unchanged: no redraw, no flicker
        st["raw"], st["raw_seen"] = raw, True
        st["clip"] = (raw or "").rstrip("\r\n").replace("\r\n", "\n")
        refresh()

    st["raw_seen"] = False

    def take_file(path):
        data, reason = read_file(path)
        if reason:
            st["error"] = reason
        else:
            st.update(mode="file", file=(os.path.basename(path), data), error=None, shown=False)
            set_text("")
        refresh()

    def on_files(paths):
        if len(paths) != 1:
            st["error"] = "drop one file at a time"
            refresh()
        else:
            take_file(paths[0])

    def to_text(*_):
        if st["mode"] != "file":
            return
        st.update(mode="text", file=None, error=None, following=True, raw=None, raw_seen=False, clip="", typed="")
        set_text("")
        refresh()
        entry.focus_set()

    def pick():
        path = filedialog.askopenfilename(parent=top, title="Choose the secret file")
        if path:
            take_file(path)

    def toggle():
        st["shown"] = not st["shown"]
        refresh()

    def close(result):
        if result == "ok":
            if ok.instate(["disabled"]):
                return
            if st["mode"] == "file":
                st["value"] = ("file", st["file"][1])
            else:
                st["value"] = ("text", text_value()[0])
        st["result"] = result
        for pending in (st["tick"], st["poll"]):
            if pending:
                top.after_cancel(pending)
        if st["undrop"]:
            st["undrop"]()
        top.destroy()

    def tick():
        st["tick"] = None
        if st["left"] <= 0:
            close("timeout")
            return
        countdown.configure(text="Cancels in %d:%02d" % divmod(st["left"], 60))
        st["left"] -= 1
        st["tick"] = top.after(1000, tick)

    eye.configure(command=toggle)
    browse.configure(command=pick)
    unfile.configure(command=to_text)
    entry.bind("<Button-1>", to_text, add="+")
    ok.configure(command=lambda: close("ok"))
    cancel.configure(command=lambda: close("cancel"))
    top.bind("<Return>", lambda _e: close("ok"))
    top.bind("<Escape>", lambda _e: close("cancel"))
    top.protocol("WM_DELETE_WINDOW", lambda: close("cancel"))

    refresh()
    poll()
    tick()
    top.update_idletasks()
    x = (top.winfo_screenwidth() - top.winfo_reqwidth()) // 2
    y = (top.winfo_screenheight() - top.winfo_reqheight()) // 3
    top.geometry(f"+{x}+{y}")
    top.update_idletasks()
    hwnd = None
    if WINDOWS:
        hwnd = int(top.wm_frame(), 16)
        st["undrop"] = windows_drop(hwnd, st["dropped"])
    elif root.dnd:  # tkinterdnd2
        top.drop_target_register("DND_Files")
        top.dnd_bind("<<Drop>>", lambda e: on_files(list(top.tk.splitlist(e.data))))
    top.lift()
    top.focus_force()
    entry.focus_set()
    test_actions(top, st, var, entry, eye, unfile, ok, cancel, take_file, on_files, hwnd)
    root.wait_window(top)
    return (st["result"],) + (st["value"] if st["result"] == "ok" else (None, None))


def test_actions(top, st, var, entry, eye, unfile, ok, cancel, take_file, on_files, hwnd):
    """SFC_TEST_ACTIONS names a JSON list of timed UI actions; tests only. `report` prints the
    state, never the value."""
    path = os.environ.get("SFC_TEST_ACTIONS")
    if not path:
        return
    with open(path, encoding="utf-8") as f:
        actions = json.load(f)

    def run(a):
        do = a["do"]
        if do == "type":
            var.set(a["text"])  # through the same trace as a keystroke
        elif do == "keys":  # real key events, letters and digits only
            entry.focus_force()
            for ch in a["text"]:
                entry.event_generate("<KeyPress>", keysym=ch)
        elif do == "file":
            take_file(a["path"])
        elif do == "drop":
            if hwnd:
                post_drop(hwnd, a["paths"])
            else:
                on_files(a["paths"])
        elif do == "clear_x":
            unfile.invoke()
        elif do == "click_field":
            entry.event_generate("<Button-1>", x=5, y=5)
        elif do == "report":
            print(f"test: mode={st['mode']} following={int(st['following'])} "
                  f"ok={'disabled' if ok.instate(['disabled']) else 'enabled'} "
                  f"placeholder={int(var.get() == PLACEHOLDER)} shown={int(st['shown'])} "
                  f"readonly={int(entry.instate(['readonly']))} "
                  f"marker={int(NEWLINE_MARK in var.get())} error={st['error'] or ''}",
                  file=sys.stderr, flush=True)
        elif do == "geometry":
            top.update_idletasks()
            print(f"test: geometry {a['tag']}={top.winfo_width()}x{top.winfo_height()}", file=sys.stderr, flush=True)
        elif do == "reveal":
            eye.invoke()
        elif do == "ok":
            ok.invoke()
        elif do == "clipboard":  # in-process, so it lands at a known point in the dialog's life
            top.clipboard_clear()
            top.clipboard_append(a["text"])
        elif do == "cancel":
            cancel.invoke()

    for a in actions:
        top.after(int(a["at"] * 1000), run, a)


# --- main ---------------------------------------------------------------------------------

def yaml_quoted(value):
    s = json.dumps(value, ensure_ascii=False)
    return YAML_UNSAFE.sub(lambda m: "\\u%04X" % ord(m[0]), s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="output file; must match a .sops.yaml creation_rule")
    ap.add_argument("name", help="Secret name")
    ap.add_argument("namespace")
    ap.add_argument("key", help="key under stringData (text) or data (file)")
    ap.add_argument("--timeout", type=parse_timeout, default="10m",
                    help="cancel the dialog after this: seconds, or 90s, 5m, 1h (default 10m)")
    ap.add_argument("--force", action="store_true", help="replace an existing file")
    ap.add_argument("--dry-run", action="store_true", help="show the dialog, write nothing")
    ap.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)  # clipboard, no dialog; tests
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", args.name) or len(args.name) > 253:
        fail(f"invalid Secret name: {args.name}")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", args.namespace) or len(args.namespace) > 63:
        fail(f"invalid namespace: {args.namespace}")
    if not re.fullmatch(r"[-._a-zA-Z0-9]+", args.key):
        fail(f"invalid key: {args.key}")

    dest = (pathlib.Path.cwd() / args.path).resolve()
    try:
        # Repo-relative with forward slashes: sops matches path_regex against the absolute
        # path, which on Windows has backslashes and never matches.
        rel = dest.relative_to(ROOT).as_posix()
    except ValueError:
        fail(f"{args.path} is outside the repo ({ROOT})")
    if dest.exists() and not args.force:
        fail(f"{rel} exists; refusing to overwrite (--force replaces it; adding a key to it "
             f"needs the cluster's private key)")
    rule = find_rule(rel)
    age = ",".join(a.strip() for a in rule["age"].split(",") if a.strip())
    sops = shutil.which("sops")
    if not sops:
        fail("sops is not on PATH")

    root = open_tk()
    if args.yes:
        value, reason = check(read_clipboard(root))
        if not value:
            fail(f"clipboard {reason or 'is empty (or holds no text)'} - copy the secret first, nothing was written")
        kind = "text"
    else:
        answer, kind, value = confirm(root, rel, f"{args.namespace}/{args.name}", args.key, args.timeout)
        if answer == "timeout":
            fail(f"the dialog timed out after {args.timeout}s; nothing was written")
        if answer != "ok":
            fail("cancelled in the dialog; nothing was written")
    root.destroy()
    print("value: text" if kind == "text" else f"value: file, {len(value)} bytes")
    if args.dry_run:
        print("dry run: confirmed, nothing was written")
        return

    # What must not appear in the output, besides inside ENC[...]: each line of the value, its
    # escaped or base64 form.
    if kind == "text":
        encoded = yaml_quoted(value)
        section, parts = "stringData", value.split("\n") + [encoded[1:-1]]
    else:
        encoded = base64.b64encode(value).decode("ascii")
        section, parts = "data", [encoded]
        try:
            parts += value.decode("utf-8").replace("\r\n", "\n").split("\n")
        except UnicodeDecodeError:
            pass
    parts = [p for p in parts if p]
    doc = (f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: {args.name}\n  namespace: {args.namespace}\n"
           f"type: Opaque\n{section}:\n  {args.key}: {encoded}\n")
    try:
        payload = doc.encode("utf-8")
    except UnicodeEncodeError:
        fail("the value holds invalid Unicode; refusing, nothing was written")
    del value, doc, encoded

    # --config on an empty file: sops must not re-apply .sops.yaml, whose rules it would
    # match against an absolute path (see above).
    cfg = tempfile.NamedTemporaryFile(delete=False, suffix=".yaml")
    cfg.close()
    try:
        cmd = [sops, "--config", cfg.name, "encrypt", "--input-type", "yaml", "--output-type", "yaml",
               "--age", age, "--filename-override", dest.name]
        if rule.get("encrypted_regex"):
            cmd += ["--encrypted-regex", rule["encrypted_regex"]]
        proc = subprocess.run(cmd, input=payload, capture_output=True)
    finally:
        os.unlink(cfg.name)
    del payload

    if proc.returncode != 0:
        # A parse error can quote its input: withhold any line sharing text with the value.
        for line in proc.stderr.decode("utf-8", "replace").splitlines():
            if any(p in line for p in parts):
                line = "(line withheld: contains the value)"
            print(f"  sops: {line}", file=sys.stderr)
        fail(f"sops failed (exit {proc.returncode}); nothing was written")

    # The key must be ENC[...], and no part may appear outside ENC[...] and the age armor.
    # Parts under 8 characters are not searched for: they would match YAML keywords.
    cipher = proc.stdout.decode("utf-8")
    enc = leak = armor = False
    for line in cipher.splitlines():
        if "-----BEGIN AGE ENCRYPTED FILE-----" in line:
            armor = True
        elif "-----END AGE ENCRYPTED FILE-----" in line:
            armor = False
        elif not armor:
            m = re.match(r"\s*([^:]+): ENC\[AES256_GCM,", line)
            enc = enc or bool(m and m[1] == args.key)
            bare = re.sub(r"ENC\[[^\]]*\]", "", line)
            leak = leak or any(len(p) >= 8 and p in bare for p in parts)
    del parts
    if not enc:
        fail(f"verification failed: {args.key} is not encrypted in the output; nothing was written")
    if leak:
        fail("verification failed: plaintext found in the output; nothing was written")
    print("plaintext-absent check: pass")

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".")  # ciphertext only
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(proc.stdout)
        os.replace(tmp, dest)
    except BaseException:
        os.unlink(tmp)
        raise
    print(f"wrote {rel}")


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as e:  # noqa: BLE001 -- a message could quote the value; the type cannot
        fail(f"internal error ({type(e).__name__}); nothing was written")
