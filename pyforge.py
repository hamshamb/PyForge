# SPDX-License-Identifier: GPL-3.0-only
#
# PyForge
# Copyright (C) 2026 hamshamb
#
# Licensed under the GNU General Public License version 3 only.
# See the LICENSE file for the complete license terms.
"""
PyForge
A friendly GUI front-end for PyInstaller.

Pick a .py file, choose how it should behave, press Forge. Every option is
explained in plain English right next to it, and the Environment page keeps
the build toolchain healthy for you.

Runs either as a script (`python pyforge.py`) or as a frozen .exe. Either way
it drives a real Python installation to do the build, because PyInstaller works
by copying that installation's interpreter and standard library into whatever
it produces.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import queue
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tkinter.font as tkfont

# Drag-and-drop is optional; the app works fine without it.
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except Exception:
    _HAS_DND = False


APP_NAME = "PyForge"
IS_FROZEN = getattr(sys, "frozen", False)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
SETTINGS_PATH = Path(os.environ.get("APPDATA") or Path.home()) / "PyForge" / "settings.json"


def resource_path(name):
    """Find a bundled asset, whether running as a script or a frozen exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "assets" / name

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")

# Segoe UI has no gear, hexagon or four-point star — only Segoe UI Symbol
# does, and Tk draws a tofu box instead of falling back. Anywhere a symbol
# shares a widget with ordinary text, the symbol is dropped instead.
SYMBOL_FONT = "Segoe UI Symbol"

# If the script imports any of these it's almost certainly a windowed app.
GUI_IMPORT_HINTS = (
    "tkinter", "Tkinter", "customtkinter", "ttkbootstrap",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "wx", "kivy", "pygame", "arcade", "pyglet", "dearpygui", "flet",
)

# Progress narration, keyed off PyInstaller's own log lines.
STAGE_MARKERS = (
    ("Analyzing",               "Reading your script…",          20),
    ("Processing module hooks", "Working out dependencies…",     40),
    ("Looking for ctypes",      "Collecting libraries…",         55),
    ("Building PYZ",            "Bundling Python modules…",      70),
    ("Building PKG",            "Packing everything together…",  82),
    ("Building EXE",            "Assembling the .exe…",          90),
    ("Building COLLECT",        "Copying support files…",        95),
)

THEMES = {
    "dark": {
        "bg": "#14161b", "panel": "#191d24", "card": "#212630", "card_hi": "#2a3140",
        "border": "#333b4a", "ink": "#e9ecf1", "muted": "#98a2b3", "faint": "#6b7484",
        "accent": "#5b83ff", "accent_hi": "#7699ff", "on_accent": "#ffffff",
        "ok": "#3fcf8e", "warn": "#f2b134", "bad": "#f4595f",
        "log_bg": "#0e1014", "log_fg": "#ccd2dc", "knob": "#ffffff",
    },
    "light": {
        "bg": "#f1f3f7", "panel": "#ffffff", "card": "#ffffff", "card_hi": "#eef1f8",
        "border": "#dde1e9", "ink": "#151920", "muted": "#5f6b7d", "faint": "#8a94a4",
        "accent": "#3059f2", "accent_hi": "#1e45d6", "on_accent": "#ffffff",
        "ok": "#10805c", "warn": "#a96a06", "bad": "#c8353b",
        "log_bg": "#111419", "log_fg": "#d3d9e2", "knob": "#ffffff",
    },
}

VERSION_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0,
    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{company}'),
      StringStruct('FileDescription', '{description}'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{product}'),
      StringStruct('LegalCopyright', '{copyright}'),
      StringStruct('OriginalFilename', '{filename}'),
      StringStruct('ProductName', '{product}'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""

ICON_CONVERT_CODE = (
    "import sys;from PIL import Image;"
    "im=Image.open(sys.argv[1]).convert('RGBA');"
    "im.save(sys.argv[2], sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
)


# ------------------------------------------------------------------ helpers ---

def run_quiet(cmd, timeout=120):
    """Run a command with no console flash, never raising."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def discover_pythons():
    """Find usable python.exe installations, newest first."""
    candidates = []
    if not IS_FROZEN:
        candidates.append(sys.executable)

    listing = run_quiet(["py", "-0p"], timeout=25)
    if listing.returncode == 0:
        for line in listing.stdout.splitlines():
            hit = re.search(r"([A-Za-z]:\\[^\r\n]*?python(?:w)?\.exe)", line, re.I)
            if hit:
                candidates.append(hit.group(1))

    where = run_quiet(["where", "python"], timeout=25)
    if where.returncode == 0:
        candidates += [l.strip() for l in where.stdout.splitlines() if l.strip()]

    local = os.environ.get("LOCALAPPDATA", "")
    patterns = []
    if local:
        patterns.append(Path(local, "Programs", "Python").glob("Python3*/python.exe"))
    patterns.append(Path("C:/").glob("Python3*/python.exe"))
    for pattern in patterns:
        try:
            candidates += [str(p) for p in pattern]
        except OSError:
            pass

    found, seen = [], set()
    for raw in candidates:
        # The Microsoft Store stub isn't a real interpreter; running it opens
        # the Store, so never touch it.
        if "\\WindowsApps\\" in raw:
            continue
        try:
            path = str(Path(raw).resolve())
        except OSError:
            continue
        key = path.lower()
        if key in seen or not Path(path).is_file():
            continue
        seen.add(key)
        probe = run_quiet(
            [path, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
            timeout=25)
        if probe.returncode == 0 and probe.stdout.strip():
            found.append((probe.stdout.strip(), path))

    def sort_key(entry):
        try:
            return tuple(int(n) for n in entry[0].split("."))
        except ValueError:
            return (0,)
    found.sort(key=sort_key, reverse=True)
    return found


def looks_like_gui_app(path: Path) -> bool:
    """Cheap heuristic: does the source mention a GUI toolkit?"""
    try:
        src = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(f"import {n}" in src or f"from {n}" in src for n in GUI_IMPORT_HINTS)


def split_names(text):
    return [t for t in re.split(r"[,\s]+", (text or "").strip()) if t]


def safe_split(text):
    """Split a free-text flag string without mangling Windows paths."""
    try:
        return [t.strip('"') for t in shlex.split(text, posix=False) if t.strip()]
    except ValueError:
        return text.split()


def clean_name(raw, fallback="app"):
    name = (raw or "").strip() or fallback
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name or fallback


def version_tuple(text):
    parts = re.findall(r"\d+", text or "")[:4]
    nums = [int(p) for p in parts] + [0] * (4 - len(parts))
    return tuple(nums[:4])


def human_size(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


def build_command(cfg, work, resolved_mode, icon=None, version_file=None):
    """Assemble the PyInstaller command line. Pure function, used for the
    live preview as well as the real build."""
    cmd = [cfg["python"] or "python", "-m", "PyInstaller", "--noconfirm"]
    if cfg["clean"]:
        cmd.append("--clean")
    cmd += ["--name", clean_name(cfg["name"])]
    cmd += ["--distpath", str(Path(work, "dist")),
            "--workpath", str(Path(work, "build")),
            "--specpath", str(work)]
    if cfg["script"]:
        cmd += ["--paths", str(Path(cfg["script"]).parent)]
    cmd.append("--onefile" if cfg["onefile"] else "--onedir")
    cmd.append("--windowed" if resolved_mode == "windowed" else "--console")
    if icon:
        cmd += ["--icon", str(icon)]
    if version_file:
        cmd += ["--version-file", str(version_file)]
    if cfg["uac"]:
        cmd.append("--uac-admin")
    if not cfg["upx"]:
        cmd.append("--noupx")
    if cfg["splash"]:
        cmd += ["--splash", cfg["splash"]]
    if cfg["debug"]:
        cmd += ["--debug", "all"]
    for item in cfg["adddata"]:
        src = Path(item)
        dest = "." if src.is_file() else src.name
        cmd += ["--add-data", f"{src};{dest}"]
    for mod in split_names(cfg["hidden"]):
        cmd += ["--hidden-import", mod]
    for mod in split_names(cfg["excludes"]):
        cmd += ["--exclude-module", mod]
    cmd += safe_split(cfg["extra"])
    cmd.append(cfg["script"] or "your_script.py")
    return cmd


def write_version_file(work, cfg, exe_name):
    v = cfg["ver"]
    if not any(v.values()):
        return None

    def esc(text):
        return (text or "").replace("\\", "\\\\").replace("'", "\\'")

    version = v["version"].strip() or "1.0.0.0"
    body = VERSION_TEMPLATE.format(
        nums=version_tuple(version),
        company=esc(v["company"]),
        description=esc(v["description"]),
        version=esc(version),
        product=esc(v["product"] or exe_name),
        copyright=esc(v["copyright"]),
        filename=esc(f"{exe_name}.exe"),
    )
    path = Path(work, "version_info.txt")
    path.write_text(body, encoding="utf-8")
    return path


# ------------------------------------------------------------ mini widgets ---

class Tooltip:
    """Hover help. Explanations live inline too; this is the extra detail."""

    def __init__(self, widget, text, app):
        self.widget, self.text, self.app = widget, text, app
        self.tip = None
        self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _=None):
        self._cancel()
        self.job = self.widget.after(500, self._show)

    def _cancel(self):
        if self.job:
            try:
                self.widget.after_cancel(self.job)
            except Exception:
                pass
            self.job = None

    def _show(self):
        if self.tip or not self.text:
            return
        c = self.app.C
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(
            f"+{self.widget.winfo_rootx() + 16}"
            f"+{self.widget.winfo_rooty() + self.widget.winfo_height() + 6}")
        shell = tk.Frame(self.tip, bg=c["border"])
        shell.pack()
        tk.Label(shell, text=self.text, bg=c["card"], fg=c["ink"], justify="left",
                 font=("Segoe UI", 9), padx=11, pady=8, wraplength=330).pack(padx=1, pady=1)

    def hide(self, _=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


def round_points(x1, y1, x2, y2, r):
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


class RoundedButton(tk.Canvas):
    """Flat rounded button, because ttk buttons can't be themed this far."""

    def __init__(self, parent, app, text, command, kind="primary",
                 padx=20, pady=10, font=("Segoe UI Semibold", 10)):
        self.app, self.kind, self.label, self.command = app, kind, text, command
        self.enabled = True
        self.hover = False
        self.font = tkfont.Font(font=font)
        w = self.font.measure(text) + padx * 2
        h = self.font.metrics("linespace") + pady * 2
        super().__init__(parent, width=w, height=h, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        app.repaintable(self)
        self.refresh_theme()

    def _set_hover(self, value):
        self.hover = value
        self.refresh_theme()

    def _click(self, _):
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, value):
        self.enabled = bool(value)
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self.refresh_theme()

    def set_text(self, text):
        self.label = text
        self.refresh_theme()

    def refresh_theme(self):
        c = self.app.C
        self.delete("all")
        self.configure(bg=self.master.cget("bg"))
        if not self.enabled:
            fill, fg = c["card_hi"], c["faint"]
        elif self.kind == "primary":
            fill = c["accent_hi"] if self.hover else c["accent"]
            fg = c["on_accent"]
        elif self.kind == "danger":
            fill, fg = (c["card_hi"], c["bad"])
        else:
            fill = c["border"] if self.hover else c["card_hi"]
            fg = c["ink"]
        w, h = int(self["width"]), int(self["height"])
        self.create_polygon(round_points(1, 1, w - 1, h - 1, min(10, h // 2)),
                            fill=fill, outline=fill, smooth=True)
        self.create_text(w / 2, h / 2, text=self.label, fill=fg, font=self.font)


class ToggleSwitch(tk.Canvas):
    """iOS-style on/off switch bound to a BooleanVar."""
    W, H = 42, 23

    def __init__(self, parent, app, variable, command=None):
        super().__init__(parent, width=self.W, height=self.H,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.app, self.var, self.command = app, variable, command
        self.bind("<Button-1>", self._toggle)
        variable.trace_add("write", lambda *a: self.refresh_theme())
        app.repaintable(self)
        self.refresh_theme()

    def _toggle(self, _):
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def refresh_theme(self):
        c = self.app.C
        self.configure(bg=self.master.cget("bg"))
        self.delete("all")
        on = bool(self.var.get())
        track = c["accent"] if on else c["border"]
        h, w = self.H, self.W
        self.create_oval(0, 0, h, h, fill=track, outline=track)
        self.create_oval(w - h, 0, w, h, fill=track, outline=track)
        self.create_rectangle(h / 2, 0, w - h / 2, h, fill=track, outline=track)
        kx = w - h + 3 if on else 3
        self.create_oval(kx, 3, kx + h - 6, h - 3, fill=c["knob"], outline="")


class Pill(tk.Canvas):
    """Small status badge: OK / Missing / Checking."""

    def __init__(self, parent, app, text="checking…", tone="muted", width=104):
        super().__init__(parent, width=width, height=24, highlightthickness=0, bd=0)
        self.app, self.text, self.tone = app, text, tone
        self.font = tkfont.Font(font=("Segoe UI Semibold", 8))
        app.repaintable(self)
        self.refresh_theme()

    def set(self, text, tone):
        self.text, self.tone = text, tone
        self.refresh_theme()

    def refresh_theme(self):
        c = self.app.C
        self.configure(bg=self.master.cget("bg"))
        self.delete("all")
        colour = c.get(self.tone, c["muted"])
        w, h = int(self["width"]), int(self["height"])
        self.create_polygon(round_points(1, 1, w - 1, h - 1, 11),
                            fill=c["card_hi"], outline=colour, smooth=True)
        self.create_text(w / 2, h / 2, text=self.text, fill=colour, font=self.font)


class OptionCard(tk.Frame):
    """A big clickable choice with a title and an explanation."""

    def __init__(self, parent, app, variable, value, title, desc, width=228):
        super().__init__(parent, highlightthickness=1, bd=0)
        self.app, self.var, self.value = app, variable, value
        self.configure(width=width)

        self.dot = tk.Canvas(self, width=16, height=16, highlightthickness=0, bd=0)
        self.dot.grid(row=0, column=0, sticky="nw", padx=(12, 8), pady=(12, 0))
        self.title = tk.Label(self, text=title, font=("Segoe UI Semibold", 10),
                              anchor="w", justify="left")
        self.title.grid(row=0, column=1, sticky="w", pady=(10, 0), padx=(0, 12))
        self.desc = tk.Label(self, text=desc, font=("Segoe UI", 8), anchor="w",
                             justify="left", wraplength=width - 48)
        self.desc.grid(row=1, column=1, sticky="w", pady=(2, 12), padx=(0, 12))
        self.columnconfigure(1, weight=1)

        for widget in (self, self.dot, self.title, self.desc):
            widget.bind("<Button-1>", self._select)
            widget.configure(cursor="hand2")
        variable.trace_add("write", lambda *a: self.refresh_theme())
        app.repaintable(self)
        self.refresh_theme()

    def _select(self, _):
        self.var.set(self.value)

    def refresh_theme(self):
        c = self.app.C
        chosen = self.var.get() == self.value
        bg = c["card_hi"] if chosen else c["card"]
        edge = c["accent"] if chosen else c["border"]
        self.configure(bg=bg, highlightbackground=edge, highlightcolor=edge)
        self.dot.configure(bg=bg)
        self.title.configure(bg=bg, fg=c["ink"])
        self.desc.configure(bg=bg, fg=c["muted"])
        self.dot.delete("all")
        self.dot.create_oval(1, 1, 15, 15, outline=edge, width=2, fill=bg)
        if chosen:
            self.dot.create_oval(5, 5, 11, 11, fill=c["accent"], outline="")


class ScrollFrame(tk.Frame):
    """Vertically scrollable page body."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.inner = tk.Frame(self.canvas)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self.window, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all(
            "<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        app.register(self, bg="bg")
        app.register(self.canvas, bg="bg")
        app.register(self.inner, bg="bg")

    def _wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


# -------------------------------------------------------------------- app ---

class PyForgeApp:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.cancelled = False
        self.busy = False
        self.msgq = queue.Queue()
        self.last_output = None
        self.pythons = []
        self.data_items = []
        self.env = {}
        self._autofixed = False
        self._syncing = False
        self._painted = []
        self._repaint = []
        self._pages = {}
        self._nav_items = {}
        self.current_page = "build"

        self.settings = self._load_settings()
        self.theme_name = self.settings.get("theme", "dark")
        self.C = THEMES[self.theme_name]

        root.title(APP_NAME)
        root.geometry("1040x760")
        root.minsize(940, 680)
        self._load_branding()

        self._make_vars()
        self._build_chrome()
        self._build_pages()
        self._enable_dnd()
        self.apply_theme()
        self.show_page("build")
        self._apply_settings()

        self.root.after(80, self._drain_queue)
        self.root.after(150, self.scan_environment)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_branding(self):
        """Window icon and header mark. Missing assets are not fatal."""
        self.logo_small = None
        try:
            ico = resource_path("icon.ico")
            if ico.is_file():
                self.root.iconbitmap(default=str(ico))
        except Exception:
            pass
        try:
            png = resource_path("logo_40.png")
            if png.is_file():
                self.logo_small = tk.PhotoImage(file=str(png))
        except Exception:
            self.logo_small = None

    # --------------------------------------------------------------- vars ---

    def _make_vars(self):
        self.script_var = tk.StringVar()
        self.name_var = tk.StringVar()
        self.outdir_var = tk.StringVar()
        self.icon_var = tk.StringVar()
        self.python_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="auto")
        self.hideterm_var = tk.BooleanVar(value=True)
        self.pack_var = tk.StringVar(value="onefile")
        self.uac_var = tk.BooleanVar(value=False)
        self.upx_var = tk.BooleanVar(value=False)
        self.clean_var = tk.BooleanVar(value=True)
        self.debug_var = tk.BooleanVar(value=False)
        self.splash_on_var = tk.BooleanVar(value=False)
        self.splash_var = tk.StringVar()
        self.openwhendone_var = tk.BooleanVar(value=True)
        self.autoinstall_var = tk.BooleanVar(value=True)
        self.hidden_var = tk.StringVar()
        self.excl_var = tk.StringVar()
        self.extra_var = tk.StringVar()
        self.product_var = tk.StringVar()
        self.company_var = tk.StringVar()
        self.version_var = tk.StringVar()
        self.copyright_var = tk.StringVar()
        self.desc_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Choose a Python file to get started.")
        self.detect_var = tk.StringVar(value="")

        for var in (self.script_var, self.name_var, self.outdir_var, self.icon_var,
                    self.mode_var, self.pack_var, self.uac_var, self.upx_var,
                    self.clean_var, self.debug_var, self.splash_on_var,
                    self.splash_var, self.hidden_var, self.excl_var, self.extra_var,
                    self.product_var, self.company_var, self.version_var,
                    self.copyright_var, self.desc_var, self.python_var):
            var.trace_add("write", lambda *a: self._refresh_preview())
        self.mode_var.trace_add("write", lambda *a: self._sync_terminal_toggle())

    # ------------------------------------------------------------- theming ---

    def register(self, widget, **roles):
        """Remember which palette entry drives which widget option."""
        self._painted.append((widget, roles))
        self._paint_one(widget, roles)

    def repaintable(self, obj):
        self._repaint.append(obj)

    def _paint_one(self, widget, roles):
        try:
            widget.configure(**{opt: self.C[key] for opt, key in roles.items()})
        except tk.TclError:
            pass

    def apply_theme(self):
        self.C = THEMES[self.theme_name]
        c = self.C
        self.root.configure(bg=c["bg"])
        for widget, roles in self._painted:
            self._paint_one(widget, roles)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TScrollbar", background=c["card_hi"], troughcolor=c["bg"],
                        bordercolor=c["bg"], arrowcolor=c["muted"], relief="flat")
        style.map("TScrollbar", background=[("active", c["border"])])
        style.configure("Bar.Horizontal.TProgressbar", troughcolor=c["card_hi"],
                        background=c["accent"], bordercolor=c["card_hi"],
                        lightcolor=c["accent"], darkcolor=c["accent"], thickness=6)
        style.configure("Combo.TCombobox", fieldbackground=c["card_hi"],
                        background=c["card_hi"], foreground=c["ink"],
                        arrowcolor=c["muted"], bordercolor=c["border"],
                        selectbackground=c["card_hi"], selectforeground=c["ink"])
        self.root.option_add("*TCombobox*Listbox.background", c["card"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["ink"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", c["on_accent"])

        for obj in self._repaint:
            try:
                obj.refresh_theme()
            except tk.TclError:
                pass
        self._paint_nav()

    def _theme_label(self):
        return "Light theme" if self.theme_name == "dark" else "Dark theme"

    def toggle_theme(self):
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.theme_btn.set_text(self._theme_label())
        self.apply_theme()

    # ------------------------------------------------------ layout helpers ---

    def card(self, parent, pady=(0, 14)):
        shell = tk.Frame(parent, highlightthickness=1, bd=0)
        shell.pack(fill="x", pady=pady)
        self.register(shell, bg="card", highlightbackground="border",
                      highlightcolor="border")
        return shell

    def section(self, parent, title, subtitle=None):
        head = tk.Frame(parent)
        head.pack(fill="x", pady=(4, 8))
        self.register(head, bg="bg")
        lbl = tk.Label(head, text=title, font=("Segoe UI Semibold", 12), anchor="w")
        lbl.pack(anchor="w")
        self.register(lbl, bg="bg", fg="ink")
        if subtitle:
            sub = tk.Label(head, text=subtitle, font=("Segoe UI", 9), anchor="w",
                           justify="left")
            sub.pack(anchor="w", pady=(1, 0))
            self.register(sub, bg="bg", fg="muted")
        return head

    def row(self, parent, title, desc, first=False):
        """A titled + explained settings row. Returns the right-hand slot."""
        wrap = tk.Frame(parent)
        wrap.pack(fill="x", padx=16, pady=(14 if first else 10, 10))
        self.register(wrap, bg="card")

        left = tk.Frame(wrap)
        left.pack(side="left", fill="x", expand=True)
        self.register(left, bg="card")
        t = tk.Label(left, text=title, font=("Segoe UI Semibold", 10), anchor="w")
        t.pack(anchor="w")
        self.register(t, bg="card", fg="ink")
        d = tk.Label(left, text=desc, font=("Segoe UI", 8), anchor="w",
                     justify="left", wraplength=520)
        d.pack(anchor="w", pady=(2, 0))
        self.register(d, bg="card", fg="muted")

        right = tk.Frame(wrap)
        right.pack(side="right", padx=(16, 0))
        self.register(right, bg="card")
        Tooltip(t, desc, self)
        return right

    def divider(self, parent):
        line = tk.Frame(parent, height=1)
        line.pack(fill="x", padx=16)
        self.register(line, bg="border")

    def entry(self, parent, textvariable, width=34):
        shell = tk.Frame(parent, highlightthickness=1, bd=0)
        self.register(shell, bg="card_hi", highlightbackground="border",
                      highlightcolor="accent")
        box = tk.Entry(shell, textvariable=textvariable, relief="flat", bd=0,
                       width=width, font=("Segoe UI", 10), highlightthickness=0)
        box.pack(fill="x", padx=9, pady=7)
        self.register(box, bg="card_hi", fg="ink", insertbackground="ink",
                      disabledbackground="card_hi")
        return shell, box

    def toggle_row(self, parent, title, desc, var, first=False, command=None):
        slot = self.row(parent, title, desc, first=first)
        sw = ToggleSwitch(slot, self, var, command=command)
        sw.pack()
        return sw

    def entry_row(self, parent, title, desc, var, width=30, first=False):
        slot = self.row(parent, title, desc, first=first)
        shell, box = self.entry(slot, var, width=width)
        shell.pack()
        return box

    def picker_row(self, parent, title, desc, var, command, button="Browse…",
                   width=34, first=False):
        slot = self.row(parent, title, desc, first=first)
        shell, box = self.entry(slot, var, width=width)
        shell.pack(side="left")
        btn = RoundedButton(slot, self, button, command, kind="ghost",
                            padx=14, pady=7, font=("Segoe UI", 9))
        btn.pack(side="left", padx=(8, 0))
        return box

    # ------------------------------------------------------------- chrome ---

    def _build_chrome(self):
        root = self.root

        header = tk.Frame(root, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        self.register(header, bg="panel")

        bar = tk.Frame(header)
        bar.pack(fill="both", expand=True, padx=22)
        self.register(bar, bg="panel")

        if self.logo_small:
            badge = tk.Label(bar, image=self.logo_small)
            badge.pack(side="left", padx=(0, 12), pady=(12, 0))
            self.register(badge, bg="panel")

        title = tk.Label(bar, text="PyForge",
                         font=("Segoe UI Semibold", 16), anchor="w")
        title.pack(side="left", pady=(12, 0))
        self.register(title, bg="panel", fg="ink")
        tag = tk.Label(bar, text="forge a Python script into a Windows app",
                       font=("Segoe UI", 9))
        tag.pack(side="left", padx=(12, 0), pady=(17, 0))
        self.register(tag, bg="panel", fg="muted")

        self.theme_btn = RoundedButton(
            bar, self, self._theme_label(), self.toggle_theme, kind="ghost",
            padx=14, pady=7, font=("Segoe UI", 9))
        self.theme_btn.pack(side="right", pady=(14, 0))

        edge = tk.Frame(root, height=1)
        edge.pack(fill="x")
        self.register(edge, bg="border")

        body = tk.Frame(root)
        body.pack(fill="both", expand=True)
        self.register(body, bg="bg")

        self.sidebar = tk.Frame(body, width=190)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.register(self.sidebar, bg="panel")

        for key, glyph, label in (("build", "▣", "Build"),
                                  ("options", "⚙", "Options"),
                                  ("advanced", "✦", "Advanced"),
                                  ("environment", "⬢", "Environment"),
                                  ("log", "≡", "Build log")):
            self._nav_button(key, glyph, label)

        hint = tk.Label(self.sidebar, font=("Segoe UI", 8), justify="left",
                        wraplength=160,
                        text=("Tip: drop a .py file anywhere on this window."
                              if _HAS_DND else "Tip: everything has an explanation "
                                               "next to it."))
        hint.pack(side="bottom", anchor="w", padx=16, pady=16)
        self.register(hint, bg="panel", fg="faint")

        self.content = tk.Frame(body)
        self.content.pack(side="left", fill="both", expand=True)
        self.register(self.content, bg="bg")

        self._build_action_bar(root)

    def _nav_button(self, key, glyph, label):
        item = tk.Frame(self.sidebar, height=42)
        item.pack(fill="x")
        item.pack_propagate(False)
        strip = tk.Frame(item, width=3)
        strip.pack(side="left", fill="y")
        # The glyph gets its own label so it can use Segoe UI Symbol. Segoe UI
        # proper has no gear/hexagon/etc, and Tk renders a tofu box rather
        # than falling back to a font that does.
        icon = tk.Label(item, text=glyph, font=(SYMBOL_FONT, 11), width=2,
                        anchor="center")
        icon.pack(side="left", padx=(10, 0))
        text = tk.Label(item, text=label, font=("Segoe UI", 10), anchor="w")
        text.pack(side="left", fill="both", expand=True, padx=(6, 0))
        for widget in (item, text, strip, icon):
            widget.bind("<Button-1>", lambda e, k=key: self.show_page(k))
            widget.configure(cursor="hand2")
        self._nav_items[key] = (item, strip, text, icon)

    def _paint_nav(self):
        c = self.C
        for key, (item, strip, text, icon) in self._nav_items.items():
            on = key == self.current_page
            bg = c["card_hi"] if on else c["panel"]
            item.configure(bg=bg)
            text.configure(bg=bg, fg=c["ink"] if on else c["muted"],
                           font=("Segoe UI Semibold" if on else "Segoe UI", 10))
            icon.configure(bg=bg, fg=c["accent"] if on else c["faint"])
            strip.configure(bg=c["accent"] if on else bg)

    def show_page(self, key):
        self.current_page = key
        for name, page in self._pages.items():
            page.pack_forget()
        self._pages[key].pack(fill="both", expand=True)
        self._paint_nav()

    def _build_action_bar(self, root):
        edge = tk.Frame(root, height=1)
        edge.pack(fill="x", side="bottom")
        self.register(edge, bg="border")

        bar = tk.Frame(root, height=76)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.register(bar, bg="panel")

        inner = tk.Frame(bar)
        inner.pack(fill="both", expand=True, padx=22, pady=12)
        self.register(inner, bg="panel")

        right = tk.Frame(inner)
        right.pack(side="right")
        self.register(right, bg="panel")

        self.build_btn = RoundedButton(right, self, "Forge .exe", self.start_build,
                                       kind="primary", padx=26, pady=11,
                                       font=("Segoe UI Semibold", 11))
        self.build_btn.pack(side="left")
        self.cancel_btn = RoundedButton(right, self, "Stop", self.cancel_build,
                                        kind="danger", padx=16, pady=11,
                                        font=("Segoe UI", 10))
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.cancel_btn.set_enabled(False)
        self.open_btn = RoundedButton(right, self, "Open folder", self.open_output,
                                      kind="ghost", padx=16, pady=11,
                                      font=("Segoe UI", 10))
        self.open_btn.pack(side="left", padx=(8, 0))
        self.open_btn.set_enabled(False)

        left = tk.Frame(inner)
        left.pack(side="left", fill="x", expand=True)
        self.register(left, bg="panel")
        self.status_lbl = tk.Label(left, textvariable=self.status_var, anchor="w",
                                   font=("Segoe UI", 9), justify="left")
        self.status_lbl.pack(anchor="w")
        self.register(self.status_lbl, bg="panel", fg="muted")
        self.progress = ttk.Progressbar(left, style="Bar.Horizontal.TProgressbar",
                                        mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(8, 0), padx=(0, 24))

    # -------------------------------------------------------------- pages ---

    def _new_page(self):
        page = ScrollFrame(self.content, self)
        body = tk.Frame(page.inner)
        body.pack(fill="both", expand=True, padx=26, pady=22)
        self.register(body, bg="bg")
        return page, body

    def _build_pages(self):
        self._pages["build"] = self._page_build()
        self._pages["options"] = self._page_options()
        self._pages["advanced"] = self._page_advanced()
        self._pages["environment"] = self._page_environment()
        self._pages["log"] = self._page_log()

    # -- Build ---------------------------------------------------------------

    def _page_build(self):
        page, body = self._new_page()

        self.drop = tk.Frame(body, height=112, highlightthickness=2, bd=0,
                             cursor="hand2")
        self.drop.pack(fill="x", pady=(0, 18))
        self.drop.pack_propagate(False)
        self.register(self.drop, bg="card", highlightbackground="border",
                      highlightcolor="border")
        self.drop_title = tk.Label(self.drop, font=("Segoe UI Semibold", 12),
                                   text="Drop your Python file here"
                                   if _HAS_DND else "Choose your Python file")
        self.drop_title.pack(pady=(28, 2))
        self.register(self.drop_title, bg="card", fg="ink")
        self.drop_sub = tk.Label(self.drop, font=("Segoe UI", 9),
                                 text="…or click to browse   ·   .py and .pyw")
        self.drop_sub.pack()
        self.register(self.drop_sub, bg="card", fg="muted")
        for widget in (self.drop, self.drop_title, self.drop_sub):
            widget.bind("<Button-1>", lambda e: self.pick_script())

        card = self.card(body)
        self.name_box = self.entry_row(
            card, "App name",
            "The finished file will be called this, plus .exe.",
            self.name_var, width=28, first=True)
        self.divider(card)
        self.picker_row(card, "Save it to",
                        "Folder the finished app is copied into when the build "
                        "succeeds.", self.outdir_var, self.pick_outdir, width=38)
        self.divider(card)
        self.picker_row(card, "App icon",
                        "Optional. A .ico works directly; PNG or JPG is converted "
                        "for you (needs Pillow — see the Environment page).",
                        self.icon_var, self.pick_icon, button="Choose…", width=38)

        self.section(body, "How should it run?",
                     "This is the difference between a clean-looking app and one "
                     "with a black terminal window behind it.")
        modes = tk.Frame(body)
        modes.pack(fill="x", pady=(0, 6))
        self.register(modes, bg="bg")
        for value, title, desc in (
            ("auto", "Auto-detect  ·  recommended",
             "Looks at your imports and picks for you. Right almost every time."),
            ("windowed", "Windowed App",
             "No console window at all. For GUI apps built with tkinter, PyQt, "
             "pygame and friends."),
            ("console", "Console App",
             "Keeps the terminal window. For scripts that print output or ask "
             "for input — without it you'd see nothing."),
        ):
            OptionCard(modes, self, self.mode_var, value, title, desc)\
                .pack(side="left", fill="both", expand=True, padx=(0, 10))

        detected = tk.Label(body, textvariable=self.detect_var,
                            font=("Segoe UI", 8), anchor="w")
        detected.pack(anchor="w", pady=(2, 10))
        self.register(detected, bg="bg", fg="accent")

        # The same choice as the cards above, said the way people actually
        # ask for it. Kept in sync both ways.
        term = self.card(body, pady=(0, 18))
        self.toggle_row(
            term, "Hide the black terminal window",
            "On: your app opens with no console behind it — this is what you "
            "want for anything with a window.\nOff: the terminal stays, which "
            "you need for scripts that print text or ask for input.",
            self.hideterm_var, first=True, command=self._terminal_toggled)

        self.section(body, "How should it be packaged?")
        packs = tk.Frame(body)
        packs.pack(fill="x")
        self.register(packs, bg="bg")
        for value, title, desc in (
            ("onefile", "Single file  ·  recommended",
             "One portable .exe you can email or copy to a USB stick. Takes a "
             "few seconds longer to start, since it unpacks itself first."),
            ("onedir", "Folder",
             "A folder holding the .exe plus its libraries. Starts instantly, "
             "but everything must stay together."),
        ):
            OptionCard(packs, self, self.pack_var, value, title, desc, width=340)\
                .pack(side="left", fill="both", expand=True, padx=(0, 10))
        return page

    # -- Options -------------------------------------------------------------

    def _page_options(self):
        page, body = self._new_page()

        self.section(body, "Behaviour",
                     "Extra switches that change how the finished app behaves.")
        card = self.card(body)
        self.toggle_row(card, "Ask for administrator rights",
                        "Windows shows the blue UAC prompt when the app starts. "
                        "Only tick this if your script edits protected folders, "
                        "services or the registry.", self.uac_var, first=True)
        self.divider(card)
        self.toggle_row(card, "Show a splash screen while it loads",
                        "Displays an image immediately on launch, which hides the "
                        "unpacking delay of a single-file build.",
                        self.splash_on_var, command=self._sync_splash)
        self.splash_entry = self.picker_row(
            card, "Splash image", "A PNG shown while the app starts up.",
            self.splash_var, self.pick_splash, button="Choose…", width=38)
        self.divider(card)
        self.toggle_row(card, "Compress the .exe with UPX",
                        "Makes the file noticeably smaller. Needs UPX installed, "
                        "slows startup slightly, and some antivirus tools are "
                        "suspicious of packed files.", self.upx_var)

        self.section(body, "Version details",
                     "What Windows shows on the Details tab of the file's "
                     "properties. Leave blank to skip it entirely.")
        info = self.card(body)
        self.entry_row(info, "Product name", "Shown as the app's display name.",
                       self.product_var, first=True)
        self.divider(info)
        self.entry_row(info, "Company", "Publisher shown in file properties.",
                       self.company_var)
        self.divider(info)
        self.entry_row(info, "Version", "Four numbers, e.g. 1.0.0.0.",
                       self.version_var, width=16)
        self.divider(info)
        self.entry_row(info, "Description", "One line describing what it does.",
                       self.desc_var)
        self.divider(info)
        self.entry_row(info, "Copyright", "e.g. © 2026 Your Name.",
                       self.copyright_var)

        self.section(body, "This app")
        mine = self.card(body)
        self.toggle_row(mine, "Open the output folder when a build finishes",
                        "Pops up Explorer with your new .exe selected.",
                        self.openwhendone_var, first=True)
        self.divider(mine)
        self.toggle_row(mine, "Clear the build cache before each build",
                        "Slower, but avoids stale leftovers when you're changing "
                        "dependencies. Leave on unless builds feel sluggish.",
                        self.clean_var)
        return page

    # -- Advanced ------------------------------------------------------------

    def _page_advanced(self):
        page, body = self._new_page()

        self.section(body, "Extra files",
                     "PyInstaller only packs Python code it can see. Images, "
                     "sounds, .json configs and data folders must be listed here "
                     "or your app will crash looking for them.")
        card = self.card(body)
        listwrap = tk.Frame(card, highlightthickness=1, bd=0)
        listwrap.pack(fill="x", padx=16, pady=(14, 8))
        self.register(listwrap, bg="card_hi", highlightbackground="border",
                      highlightcolor="border")
        self.data_list = tk.Listbox(listwrap, height=5, relief="flat", bd=0,
                                    font=("Consolas", 9), highlightthickness=0,
                                    activestyle="none")
        self.data_list.pack(fill="x", padx=2, pady=2)
        self.register(self.data_list, bg="card_hi", fg="ink",
                      selectbackground="accent", selectforeground="on_accent")

        btns = tk.Frame(card)
        btns.pack(fill="x", padx=16, pady=(0, 14))
        self.register(btns, bg="card")
        for label, cmd in (("Add file…", self.add_data_file),
                           ("Add folder…", self.add_data_folder),
                           ("Remove", self.remove_data)):
            RoundedButton(btns, self, label, cmd, kind="ghost", padx=13, pady=7,
                          font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))

        self.section(body, "Modules",
                     "For the rare cases where PyInstaller guesses wrong.")
        mods = self.card(body)
        self.entry_row(mods, "Also include these modules",
                       "Comma-separated. Use this when the finished .exe dies with "
                       "\"No module named …\" even though it runs fine as a script.",
                       self.hidden_var, width=30, first=True)
        self.divider(mods)
        self.entry_row(mods, "Leave these modules out",
                       "Comma-separated. Drops packages you don't use to shrink "
                       "the .exe.", self.excl_var, width=30)
        self.divider(mods)
        self.entry_row(mods, "Extra PyInstaller flags",
                       "Passed through untouched, for anything not covered above.",
                       self.extra_var, width=30)
        self.divider(mods)
        self.toggle_row(mods, "Verbose debug build",
                        "Makes the finished .exe print what it's doing as it "
                        "starts. Useful when an app works as a script but not as "
                        "an .exe — turn it off for the version you share.",
                        self.debug_var)

        self.section(body, "Command preview",
                     "Exactly what will run when you press Build.")
        prev = self.card(body)
        self.preview = tk.Text(prev, height=7, relief="flat", bd=0, wrap="word",
                               font=("Consolas", 9), highlightthickness=0,
                               padx=12, pady=10)
        self.preview.pack(fill="x", padx=2, pady=2)
        self.register(self.preview, bg="log_bg", fg="log_fg",
                      insertbackground="log_fg")
        self.preview.configure(state="disabled")
        return page

    # -- Environment ---------------------------------------------------------

    def _page_environment(self):
        page, body = self._new_page()

        self.section(body, "Build toolchain",
                     "Checked automatically each time the app starts. PyInstaller "
                     "builds an .exe by copying a real Python interpreter into it, "
                     "so a working Python is genuinely required — there's nothing "
                     "to copy without one.")

        card = self.card(body)
        self.env_rows = {}
        specs = [
            ("python", "Python", "Required",
             "The interpreter that gets bundled into your .exe."),
            ("pip", "pip", "Required",
             "Python's package installer. Ships with Python."),
            ("pyinstaller", "PyInstaller", "Required",
             "Does the actual .py → .exe conversion."),
            ("pillow", "Pillow", "Optional",
             "Lets you use PNG or JPG images as your app icon."),
            ("upx", "UPX", "Optional",
             "Shrinks the finished .exe. A separate download, not a Python package."),
            ("dnd", "Drag & drop", "Optional",
             "Lets you drop a .py file onto this window."),
        ]
        for i, (key, title, need, desc) in enumerate(specs):
            slot = self.row(card, f"{title}   ·   {need}", desc, first=(i == 0))
            pill = Pill(slot, self, "checking…", "muted")
            pill.pack(side="left")
            btn = RoundedButton(slot, self, "Install", lambda k=key: self.fix_env(k),
                                kind="ghost", padx=13, pady=6,
                                font=("Segoe UI", 9))
            btn.pack(side="left", padx=(8, 0))
            btn.set_enabled(False)
            self.env_rows[key] = (pill, btn)
            if i < len(specs) - 1:
                self.divider(card)

        actions = tk.Frame(body)
        actions.pack(fill="x", pady=(2, 16))
        self.register(actions, bg="bg")
        RoundedButton(actions, self, "Check again", self.scan_environment,
                      kind="ghost", padx=15, pady=8,
                      font=("Segoe UI", 9)).pack(side="left")
        RoundedButton(actions, self, "Install everything missing",
                      self.fix_all, kind="primary", padx=15, pady=8,
                      font=("Segoe UI Semibold", 9)).pack(side="left", padx=8)

        self.section(body, "Which Python to build with",
                     "Found automatically, newest first. The version you pick here "
                     "is the version baked into your .exe.")
        pycard = self.card(body)
        slot = self.row(pycard, "Interpreter",
                        "Only matters if you have more than one Python installed.",
                        first=True)
        self.python_box = ttk.Combobox(slot, textvariable=self.python_var,
                                       state="readonly", width=44,
                                       style="Combo.TCombobox",
                                       font=("Segoe UI", 9))
        self.python_box.pack(side="left")
        RoundedButton(slot, self, "Find…", self.pick_python, kind="ghost",
                      padx=13, pady=6, font=("Segoe UI", 9))\
            .pack(side="left", padx=(8, 0))

        self.divider(pycard)
        self.toggle_row(pycard, "Fix missing pieces automatically",
                        "On startup, quietly pip-installs any required build tool "
                        "that's missing and keeps PyInstaller up to date. Python "
                        "itself is never installed without asking you first.",
                        self.autoinstall_var)
        return page

    # -- Log -----------------------------------------------------------------

    def _page_log(self):
        page = tk.Frame(self.content)
        self.register(page, bg="bg")
        body = tk.Frame(page)
        body.pack(fill="both", expand=True, padx=26, pady=22)
        self.register(body, bg="bg")

        head = tk.Frame(body)
        head.pack(fill="x", pady=(0, 10))
        self.register(head, bg="bg")
        title = tk.Label(head, text="Build log", font=("Segoe UI Semibold", 12))
        title.pack(side="left")
        self.register(title, bg="bg", fg="ink")
        for label, cmd in (("Save…", self.save_log), ("Copy", self.copy_log),
                           ("Clear", self.clear_log)):
            RoundedButton(head, self, label, cmd, kind="ghost", padx=13, pady=6,
                          font=("Segoe UI", 9)).pack(side="right", padx=(8, 0))

        shell = tk.Frame(body, highlightthickness=1, bd=0)
        shell.pack(fill="both", expand=True)
        self.register(shell, bg="log_bg", highlightbackground="border",
                      highlightcolor="border")
        self.log = tk.Text(shell, relief="flat", bd=0, wrap="none",
                           font=("Consolas", 9), highlightthickness=0,
                           padx=12, pady=10)
        self.log.pack(side="left", fill="both", expand=True)
        self.register(self.log, bg="log_bg", fg="log_fg", insertbackground="log_fg")
        sb = ttk.Scrollbar(shell, orient="vertical", command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        self.log.tag_configure("ok", foreground="#4ade80")
        self.log.tag_configure("bad", foreground="#f87171")
        self.log.tag_configure("info", foreground="#7aa2ff")
        return page

    # ---------------------------------------------------------------- dnd ---

    def _enable_dnd(self):
        if not _HAS_DND:
            return
        try:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event):
        raw = event.data.strip()
        if raw.startswith("{") and raw.endswith("}"):
            raw = raw[1:-1]
        path = Path(raw.split("} {")[0])
        if path.suffix.lower() in (".py", ".pyw"):
            self._set_script(path)
        elif path.suffix.lower() == ".ico" or path.suffix.lower() in IMAGE_SUFFIXES:
            self.icon_var.set(str(path))
            self._log(f"Icon set to {path.name}\n", "info")
        else:
            messagebox.showwarning("Not a Python file",
                                   "Drop a .py or .pyw file, or an image to use "
                                   "as the icon.")

    # ------------------------------------------------------------ pickers ---

    def pick_script(self):
        path = filedialog.askopenfilename(
            title="Choose a Python file",
            filetypes=[("Python files", "*.py *.pyw"), ("All files", "*.*")])
        if path:
            self._set_script(Path(path))

    def _set_script(self, path: Path):
        self.script_var.set(str(path))
        self.name_var.set(path.stem)
        if not self.outdir_var.get():
            self.outdir_var.set(str(path.parent))

        gui = looks_like_gui_app(path)
        self.detect_var.set(
            f"Found a GUI toolkit in {path.name} → this will be built as a "
            "Windowed App, with no terminal window." if gui else
            f"No GUI toolkit found in {path.name} → this will be built as a "
            "Console App. Flip the switch below if you don't want the terminal.")
        self._sync_terminal_toggle()
        try:
            size = human_size(path.stat().st_size)
        except OSError:
            size = "?"
        self.drop_title.configure(text=path.name)
        self.drop_sub.configure(text=f"{path.parent}   ·   {size}   ·   click to "
                                     "choose a different file")
        self.status_var.set(f"Ready to build {path.name}")
        self._paint_status("muted")
        self.show_page("build")

    def pick_outdir(self):
        path = filedialog.askdirectory(
            title="Where should the .exe go?",
            initialdir=self.outdir_var.get() or str(Path.home()))
        if path:
            self.outdir_var.set(path)

    def pick_icon(self):
        path = filedialog.askopenfilename(
            title="Choose an icon",
            filetypes=[("Icon or image", "*.ico *.png *.jpg *.jpeg *.bmp *.gif"),
                       ("All files", "*.*")])
        if path:
            self.icon_var.set(path)

    def pick_splash(self):
        path = filedialog.askopenfilename(
            title="Choose a splash image",
            filetypes=[("Images", "*.png *.gif *.bmp"), ("All files", "*.*")])
        if path:
            self.splash_var.set(path)
            self.splash_on_var.set(True)

    def _sync_splash(self):
        if self.splash_on_var.get() and not self.splash_var.get():
            self.pick_splash()

    def add_data_file(self):
        for path in filedialog.askopenfilenames(title="Add files to bundle"):
            self._add_data(path)

    def add_data_folder(self):
        path = filedialog.askdirectory(title="Add a folder to bundle")
        if path:
            self._add_data(path)

    def _add_data(self, path):
        if path and path not in self.data_items:
            self.data_items.append(path)
            self.data_list.insert("end", path)
            self._refresh_preview()

    def remove_data(self):
        for index in reversed(self.data_list.curselection()):
            self.data_list.delete(index)
            del self.data_items[index]
        self._refresh_preview()

    def open_output(self):
        target = self.last_output or Path(self.outdir_var.get() or ".")
        folder = target if target.is_dir() else target.parent
        if folder.exists():
            os.startfile(str(folder))

    # -------------------------------------------------------- environment ---

    def scan_environment(self):
        for pill, btn in self.env_rows.values():
            pill.set("checking…", "muted")
            btn.set_enabled(False)
        self.status_var.set("Checking the build toolchain…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        result = {"pythons": discover_pythons()}
        python = result["pythons"][0][1] if result["pythons"] else None
        result["python"] = python

        if python:
            pip = run_quiet([python, "-m", "pip", "--version"], timeout=90)
            parts = pip.stdout.split()
            result["pip"] = parts[1] if pip.returncode == 0 and len(parts) > 1 else None
            pyi = run_quiet([python, "-m", "PyInstaller", "--version"], timeout=120)
            result["pyinstaller"] = pyi.stdout.strip() if pyi.returncode == 0 else None
            pil = run_quiet([python, "-c", "import PIL;print(PIL.__version__)"],
                            timeout=60)
            result["pillow"] = pil.stdout.strip() if pil.returncode == 0 else None
        else:
            result["pip"] = result["pyinstaller"] = result["pillow"] = None

        upx = shutil.which("upx")
        result["upx"] = upx
        self.msgq.put(("env", result))

    def _apply_env(self, result):
        self.env = result
        self.pythons = result["pythons"]
        labels = [f"Python {v}   —   {p}" for v, p in self.pythons]
        self.python_box.configure(values=labels)
        wanted = self.settings.get("python")
        if labels and not self.python_var.get():
            match = next((l for l in labels if wanted and l.endswith(wanted)), labels[0])
            self.python_var.set(match)

        def show(key, value, ok_text, missing_text, tone_missing="warn",
                 action=None):
            pill, btn = self.env_rows[key]
            if value:
                pill.set(ok_text, "ok")
                btn.set_enabled(False)
                btn.set_text("Installed")
            else:
                pill.set(missing_text, tone_missing)
                btn.set_enabled(action is not None)
                btn.set_text(action or "—")

        py = result["python"]
        show("python", py, f"v{self.pythons[0][0]}" if self.pythons else "",
             "not found", "bad", "Install")
        show("pip", result["pip"], f"v{result['pip']}" if result["pip"] else "",
             "missing", "bad", "Repair")
        show("pyinstaller", result["pyinstaller"],
             f"v{result['pyinstaller']}" if result["pyinstaller"] else "",
             "missing", "bad", "Install")
        show("pillow", result["pillow"],
             f"v{result['pillow']}" if result["pillow"] else "",
             "not installed", "warn", "Install")
        show("upx", result["upx"], "found", "not installed", "muted", "Get UPX")
        show("dnd", _HAS_DND, "built in", "unavailable", "muted", None)

        missing = self._missing_pip_packages()
        # Only ever auto-fix once per session, so a package that refuses to
        # install can't put us in an install/rescan loop.
        if py and missing and self.autoinstall_var.get() and not self._autofixed:
            self._autofixed = True
            self._log(f"Auto-installing: {', '.join(missing)}\n", "info")
            self.install_packages(missing, upgrade=True)
            return

        if not py:
            self.status_var.set("No Python found — open the Environment page.")
            self._paint_status("bad")
            self.show_page("environment")
        elif result["pyinstaller"]:
            self.status_var.set(self.status_var.get()
                                if self.script_var.get()
                                else "Ready. Choose a Python file to get started.")
            self._paint_status("muted")
        else:
            self.status_var.set("PyInstaller is missing — open the Environment page.")
            self._paint_status("warn")
        self._refresh_preview()

    def _missing_pip_packages(self):
        missing = []
        if not self.env.get("pyinstaller"):
            missing.append("pyinstaller")
        return missing

    def fix_env(self, key):
        if key == "python":
            self.install_python()
        elif key == "upx":
            self._log("UPX is a separate download: https://upx.github.io\n"
                      "Unzip it and put upx.exe somewhere on your PATH, then press "
                      "Check again.\n", "info")
            self.show_page("log")
            if messagebox.askyesno(
                    "Install UPX",
                    "UPX isn't a Python package, so pip can't fetch it.\n\n"
                    "Try installing it with winget?"):
                self.run_stream(["winget", "install", "-e", "--id", "UPX.UPX",
                                 "--accept-package-agreements",
                                 "--accept-source-agreements"], "install")
        elif key == "pip":
            python = self.selected_python()
            if python:
                self.run_stream([python, "-m", "ensurepip", "--upgrade"], "install")
        else:
            self.install_packages([{"pillow": "pillow"}.get(key, key)], upgrade=True)

    def fix_all(self):
        if not self.env.get("python"):
            self.install_python()
            return
        wanted = []
        if not self.env.get("pyinstaller"):
            wanted.append("pyinstaller")
        if not self.env.get("pillow"):
            wanted.append("pillow")
        if not wanted:
            self._log("Everything required is already installed.\n", "ok")
            self.show_page("log")
            return
        self.install_packages(wanted, upgrade=True)

    def install_packages(self, packages, upgrade=False):
        python = self.selected_python()
        if not python:
            messagebox.showerror("No Python", "Install Python first.")
            return
        cmd = [python, "-m", "pip", "install"]
        if upgrade:
            cmd.append("--upgrade")
        cmd += packages
        self.status_var.set(f"Installing {', '.join(packages)}…")
        self._paint_status("muted")
        self.show_page("log")
        self.run_stream(cmd, "install")

    def install_python(self):
        if not shutil.which("winget"):
            messagebox.showinfo(
                "Install Python",
                "Python isn't installed, and winget isn't available to do it "
                "automatically.\n\nDownload it from python.org/downloads and tick "
                "\"Add python.exe to PATH\" during setup, then press Check again.")
            return
        if not messagebox.askyesno(
                "Install Python",
                "This will install the latest Python using winget:\n\n"
                "    winget install Python.Python.3.13\n\n"
                "Windows may ask you to approve it. Continue?"):
            return
        self.status_var.set("Installing Python…")
        self.show_page("log")
        self.run_stream(["winget", "install", "-e", "--id", "Python.Python.3.13",
                         "--accept-package-agreements",
                         "--accept-source-agreements"], "install")

    def selected_python(self):
        label = self.python_var.get()
        for _version, path in self.pythons:
            if label.endswith(path):
                return path
        return self.pythons[0][1] if self.pythons else None

    def pick_python(self):
        path = filedialog.askopenfilename(
            title="Locate python.exe",
            filetypes=[("Python interpreter", "python.exe python*.exe"),
                       ("All files", "*.*")])
        if not path:
            return
        probe = run_quiet([path, "-c",
                           "import sys;print('%d.%d.%d' % sys.version_info[:3])"])
        if probe.returncode != 0 or not probe.stdout.strip():
            messagebox.showerror("Not a Python interpreter", f"Couldn't run:\n{path}")
            return
        entry = (probe.stdout.strip(), str(Path(path).resolve()))
        self.pythons = [entry] + [e for e in self.pythons if e[1] != entry[1]]
        labels = [f"Python {v}   —   {p}" for v, p in self.pythons]
        self.python_box.configure(values=labels)
        self.python_var.set(labels[0])
        self.scan_environment()

    # -------------------------------------------------------------- build ---

    def collect_config(self):
        """Read every Tk variable — on the main thread, always."""
        return {
            "python": self.selected_python(),
            "script": self.script_var.get().strip().strip('"'),
            "name": clean_name(self.name_var.get()),
            "outdir": self.outdir_var.get().strip(),
            "icon": self.icon_var.get().strip().strip('"'),
            "mode": self.mode_var.get(),
            "onefile": self.pack_var.get() == "onefile",
            "uac": self.uac_var.get(),
            "upx": self.upx_var.get(),
            "clean": self.clean_var.get(),
            "debug": self.debug_var.get(),
            "splash": self.splash_var.get().strip() if self.splash_on_var.get() else "",
            "hidden": self.hidden_var.get(),
            "excludes": self.excl_var.get(),
            "extra": self.extra_var.get(),
            "adddata": list(self.data_items),
            "openwhendone": self.openwhendone_var.get(),
            "ver": {
                "product": self.product_var.get().strip(),
                "company": self.company_var.get().strip(),
                "version": self.version_var.get().strip(),
                "copyright": self.copyright_var.get().strip(),
                "description": self.desc_var.get().strip(),
            },
        }

    def _terminal_toggled(self):
        """User flipped the plain-English switch — commit to that choice."""
        if self._syncing:
            return
        self._syncing = True
        self.mode_var.set("windowed" if self.hideterm_var.get() else "console")
        self._syncing = False

    def _sync_terminal_toggle(self):
        """Mode changed elsewhere — show what it actually resolves to."""
        if self._syncing:
            return
        self._syncing = True
        resolved = self.resolved_mode({"mode": self.mode_var.get(),
                                       "script": self.script_var.get()})
        self.hideterm_var.set(resolved == "windowed")
        self._syncing = False

    def resolved_mode(self, cfg):
        if cfg["mode"] != "auto":
            return cfg["mode"]
        script = Path(cfg["script"]) if cfg["script"] else None
        if script and script.is_file():
            return "windowed" if looks_like_gui_app(script) else "console"
        return "console"

    def _refresh_preview(self):
        if not hasattr(self, "preview"):
            return
        cfg = self.collect_config()
        icon = cfg["icon"]
        if icon and Path(icon).suffix.lower() in IMAGE_SUFFIXES:
            icon = "<converted-icon>.ico"
        vfile = "<version-info>.txt" if any(cfg["ver"].values()) else None
        cmd = build_command(cfg, "<temp build folder>", self.resolved_mode(cfg),
                            icon or None, vfile)
        text = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _validate(self, cfg):
        if not cfg["python"]:
            messagebox.showerror(
                "No Python found",
                "A Python installation is needed to build with.\n\n"
                "Open the Environment page to install or locate one.")
            self.show_page("environment")
            return None
        if not cfg["script"]:
            messagebox.showwarning("No file", "Choose a .py file first.")
            self.show_page("build")
            return None
        script = Path(cfg["script"])
        if not script.is_file():
            messagebox.showerror("Missing file", f"Can't find:\n{script}")
            return None
        if script.suffix.lower() not in (".py", ".pyw"):
            messagebox.showerror("Not a Python file",
                                 "That file isn't a .py or .pyw script.")
            return None

        outdir = Path(cfg["outdir"] or script.parent)
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Bad output folder", str(exc))
            return None
        cfg["outdir"] = str(outdir)

        for label, value in (("icon", cfg["icon"]), ("splash image", cfg["splash"])):
            if value and not Path(value).is_file():
                messagebox.showerror(f"Missing {label}", f"Can't find:\n{value}")
                return None
        if cfg["icon"] and Path(cfg["icon"]).suffix.lower() in IMAGE_SUFFIXES \
                and not self.env.get("pillow"):
            messagebox.showerror(
                "Pillow needed",
                "Converting a PNG or JPG into an icon needs Pillow.\n\n"
                "Install it from the Environment page, or pick a .ico file.")
            self.show_page("environment")
            return None
        if cfg["upx"] and not self.env.get("upx"):
            messagebox.showwarning(
                "UPX not found",
                "UPX compression is switched on but UPX isn't installed.\n"
                "The build will continue without compression.")

        dest = Path(outdir, f"{cfg['name']}.exe" if cfg["onefile"] else cfg["name"])
        if dest.exists():
            what = "file" if dest.is_file() else "folder"
            if not messagebox.askyesno(
                    "Already there",
                    f"This {what} already exists and will be replaced:\n\n{dest}\n\n"
                    "Overwrite it?"):
                return None
        return dest

    def start_build(self):
        if self.busy:
            return
        cfg = self.collect_config()
        dest = self._validate(cfg)
        if dest is None:
            return
        if not self.env.get("pyinstaller"):
            if not messagebox.askyesno(
                    "PyInstaller needed",
                    "PyInstaller isn't installed for the selected Python.\n\n"
                    "Install it now? (needs internet, one time only)"):
                return
            self.install_packages(["pyinstaller"], upgrade=True)
            return

        self._set_busy(True)
        self.cancelled = False
        self.last_output = None
        self.progress.configure(value=5)
        self.status_var.set("Starting PyInstaller…")
        self._paint_status("muted")
        self.clear_log()
        self.show_page("log")

        work = Path(tempfile.mkdtemp(prefix="pyforge_"))
        threading.Thread(target=self._run_build,
                         args=(cfg, work, dest, self.resolved_mode(cfg)),
                         daemon=True).start()

    def _run_build(self, cfg, work, dest, mode):
        """Worker thread. Talks to the UI only through self.msgq."""
        try:
            icon = cfg["icon"]
            if icon and Path(icon).suffix.lower() in IMAGE_SUFFIXES:
                self.msgq.put(("log", f"Converting {Path(icon).name} to .ico…\n"))
                ico = Path(work, "icon.ico")
                made = run_quiet([cfg["python"], "-c", ICON_CONVERT_CODE,
                                  icon, str(ico)], timeout=120)
                if made.returncode != 0 or not ico.exists():
                    self.msgq.put(("fail", "Couldn't turn that image into an icon. "
                                           "Try a .ico file instead."))
                    return
                icon = str(ico)

            version_file = write_version_file(work, cfg, cfg["name"])
            cmd = build_command(cfg, work, mode, icon or None, version_file)
            self.msgq.put(("log", f"Building a {mode} app "
                                  f"({'single file' if cfg['onefile'] else 'folder'})"
                                  f" with {cfg['python']}\n\n", "info"))
            self.msgq.put(("log", "$ " + " ".join(cmd) + "\n\n", "info"))

            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1, cwd=str(work),
                env=env, creationflags=CREATE_NO_WINDOW)
            for line in self.proc.stdout:
                self.msgq.put(("log", line))
            code = self.proc.wait()

            if self.cancelled:
                self.msgq.put(("cancelled", None))
                return
            if code != 0:
                self.msgq.put(("fail", f"PyInstaller exited with code {code}. "
                                       "Scroll up for the reason."))
                return

            built = Path(work, "dist", f"{cfg['name']}.exe") if cfg["onefile"] \
                else Path(work, "dist", cfg["name"])
            if not built.exists():
                self.msgq.put(("fail", "Build finished but produced no .exe."))
                return

            self.msgq.put(("log", f"\nCopying result to {cfg['outdir']}…\n"))
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            if built.is_dir():
                shutil.copytree(built, dest)
                final = Path(dest, f"{cfg['name']}.exe")
            else:
                shutil.copy2(built, dest)
                final = dest
            self.msgq.put(("done", (final, cfg["openwhendone"])))

        except FileNotFoundError:
            self.msgq.put(("fail", "Couldn't launch the selected Python."))
        except Exception as exc:
            self.msgq.put(("fail", f"{type(exc).__name__}: {exc}"))
        finally:
            self.proc = None
            shutil.rmtree(work, ignore_errors=True)

    def run_stream(self, cmd, kind):
        """Stream any long-running command into the log (installs, winget)."""
        if self.busy:
            return
        self._set_busy(True)
        self.progress.configure(value=0)
        self._log("$ " + " ".join(cmd) + "\n", "info")

        def worker():
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=CREATE_NO_WINDOW)
                for line in self.proc.stdout:
                    self.msgq.put(("log", line))
                code = self.proc.wait()
            except FileNotFoundError:
                self.msgq.put(("log", "Command not found.\n", "bad"))
                code = 1
            except Exception as exc:
                self.msgq.put(("log", f"{exc}\n", "bad"))
                code = 1
            finally:
                self.proc = None
            self.msgq.put((kind, code))

        threading.Thread(target=worker, daemon=True).start()

    def cancel_build(self):
        if self.proc is None:
            return
        self.cancelled = True
        self.status_var.set("Stopping…")
        try:
            # PyInstaller spawns children, so kill the whole tree.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------ UI queue pump ---

    def _drain_queue(self):
        try:
            while True:
                item = self.msgq.get_nowait()
                kind, payload = item[0], item[1]
                tag = item[2] if len(item) > 2 else None
                if kind == "log":
                    self._log(payload, tag)
                    self._update_stage(payload)
                elif kind == "env":
                    self._apply_env(payload)
                elif kind == "install":
                    self._finish_install(payload)
                elif kind == "done":
                    self._finish_ok(*payload)
                elif kind == "fail":
                    self._finish_bad(payload)
                elif kind == "cancelled":
                    self._finish_cancelled()
        except queue.Empty:
            pass
        self.root.after(80, self._drain_queue)

    def _update_stage(self, line):
        for marker, text, pct in STAGE_MARKERS:
            if marker in line:
                self.status_var.set(text)
                if pct > self.progress["value"]:
                    self.progress.configure(value=pct)
                break

    def _set_busy(self, value):
        self.busy = value
        self.build_btn.set_enabled(not value)
        self.cancel_btn.set_enabled(value)

    def _paint_status(self, key):
        self.status_lbl.configure(fg=self.C[key])

    def _finish_install(self, code):
        self._set_busy(False)
        if code == 0:
            self._log("\nInstalled successfully.\n", "ok")
            self.status_var.set("Installed. Re-checking…")
            self._paint_status("ok")
        else:
            self._log(f"\nInstall failed (exit code {code}).\n", "bad")
            self.status_var.set("Install failed — see the log.")
            self._paint_status("bad")
        self.scan_environment()

    def _finish_ok(self, final: Path, open_folder):
        self.last_output = final
        self._set_busy(False)
        self.progress.configure(value=100)
        self.status_var.set(f"Done  ·  {final}")
        self._paint_status("ok")
        self._log(f"\nSUCCESS: {final}\n", "ok")
        self.open_btn.set_enabled(True)
        if open_folder:
            try:
                subprocess.Popen(["explorer", "/select,", str(final)])
            except Exception:
                pass

    def _finish_bad(self, msg):
        self._set_busy(False)
        self.progress.configure(value=0)
        self.status_var.set("Build failed — see the build log.")
        self._paint_status("bad")
        self._log(f"\nFAILED: {msg}\n", "bad")
        self.show_page("log")

    def _finish_cancelled(self):
        self._set_busy(False)
        self.progress.configure(value=0)
        self.status_var.set("Stopped.")
        self._paint_status("muted")
        self._log("\nStopped.\n", "bad")

    # ----------------------------------------------------------------- log ---

    def _log(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end"))
        self.status_var.set("Build log copied to the clipboard.")
        self._paint_status("muted")

    def save_log(self):
        path = filedialog.asksaveasfilename(
            title="Save build log", defaultextension=".txt",
            filetypes=[("Text file", "*.txt")])
        if path:
            Path(path).write_text(self.log.get("1.0", "end"), encoding="utf-8")
            self.status_var.set(f"Log saved to {path}")

    # ------------------------------------------------------------ settings ---

    def _load_settings(self):
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _apply_settings(self):
        s = self.settings
        self.outdir_var.set(s.get("outdir", ""))
        self.mode_var.set(s.get("mode", "auto"))
        self.pack_var.set(s.get("pack", "onefile"))
        self.uac_var.set(s.get("uac", False))
        self.upx_var.set(s.get("upx", False))
        self.clean_var.set(s.get("clean", True))
        self.openwhendone_var.set(s.get("openwhendone", True))
        self.autoinstall_var.set(s.get("autoinstall", True))
        self.company_var.set(s.get("company", ""))
        self.copyright_var.set(s.get("copyright", ""))

    def _save_settings(self):
        data = {
            "theme": self.theme_name,
            "outdir": self.outdir_var.get(),
            "mode": self.mode_var.get(),
            "pack": self.pack_var.get(),
            "uac": self.uac_var.get(),
            "upx": self.upx_var.get(),
            "clean": self.clean_var.get(),
            "openwhendone": self.openwhendone_var.get(),
            "autoinstall": self.autoinstall_var.get(),
            "company": self.company_var.get(),
            "copyright": self.copyright_var.get(),
            "python": self.selected_python() or "",
        }
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self):
        if self.busy:
            if not messagebox.askyesno("Still working",
                                       "Something is still running. Quit anyway?"):
                return
            self.cancel_build()
        self._save_settings()
        self.root.destroy()


def main():
    root = TkinterDnD.Tk() if _HAS_DND else tk.Tk()
    PyForgeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()


