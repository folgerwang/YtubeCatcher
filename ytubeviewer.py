"""YtubeCatcher - YouTube player + clip extractor (engine: ytubecatcher.py).

No web browser: videos are streamed by yt-dlp and played in an embedded mpv (libmpv).
Search YouTube, watch, mark IN/OUT clips on the seek bar, then Extract them as voice only
(BGM filtered out), audio or video - the cutting/filtering is done by ytubecatcher.py.

Needs:  pip install mpv   +   libmpv-2.dll next to this file (run.bat does both).
Keys:   Space/K play/pause   Left/Right -5/+5 s   Shift+Left/Right -1/+1 s   J -10 s   , . frame step
        I mark IN   O mark OUT   L loop clip   Del delete clip   M mute   F fullscreen
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "YtubeCatcher"
VIEWER_SETTINGS = os.path.join(HERE, "viewer_settings.json")
QUALITIES = ["360", "480", "720", "1080", "1440", "2160", "audio only"]
SPEEDS = ["0.5", "0.75", "1.0", "1.25", "1.5", "2.0"]

# libmpv-2.dll and the venv's yt-dlp.exe must be findable by mpv
os.environ["PATH"] = os.pathsep.join([HERE, os.path.dirname(sys.executable), os.environ.get("PATH", "")])
if hasattr(os, "add_dll_directory"):
    try:
        os.add_dll_directory(HERE)
    except OSError:
        pass


# --------------------------------------------------------------------------- small helpers
def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def fmt_time(secs) -> str:
    """seconds -> 'm:ss.s' / 'h:mm:ss.s' (tenths only when not whole)."""
    if secs is None:
        return "--:--"
    secs = max(0.0, round(float(secs), 1))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    s_txt = f"{s:04.1f}" if abs(s - round(s)) > 1e-6 else f"{int(round(s)):02d}"
    return f"{int(h)}:{int(m):02d}:{s_txt}" if h else f"{int(m)}:{s_txt}"


def _no_window() -> dict:
    return {"creationflags": 0x08000000} if os.name == "nt" else {}


# --------------------------------------------------------------------------- libmpv install
def _find_7z() -> str | None:
    for c in (shutil.which("7z"), shutil.which("7za"), shutil.which("7zr"),
              os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "7-Zip", "7z.exe"),
              os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "7-Zip", "7z.exe")):
        if c and os.path.isfile(c):
            return c
    return None


def _extract_dll(arc: str, out_dir: str, log=print) -> str:
    """Pull libmpv-2.dll out of the .7z. The shinchiro builds use the BCJ2 filter, which py7zr
    cannot decode, so real 7-Zip is used: an installed 7z.exe, else Windows' own tar.exe
    (libarchive), else the official standalone 7zr.exe (~600 KB) downloaded next to this file."""
    import urllib.request

    def found():
        for dp, _, fs in os.walk(out_dir):
            for f in fs:
                if f.lower() == "libmpv-2.dll":
                    return os.path.join(dp, f)
        return None

    errors = []
    tools = []
    sz = _find_7z()
    if sz:
        tools.append(("7-Zip", [sz, "e", arc, f"-o{out_dir}", "libmpv-2.dll", "-r", "-y"]))
    tar = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "tar.exe")
    if os.path.isfile(tar):
        tools.append(("Windows tar", [tar, "-xf", arc, "-C", out_dir, "libmpv-2.dll"]))
    for name, cmd in tools:
        log(f"extracting with {name}...")
        r = subprocess.run(cmd, capture_output=True, text=True, **_no_window())
        if found():
            return found()
        errors.append(f"{name}: {(r.stderr or r.stdout).strip()[-200:]}")
    zr = os.path.join(HERE, "7zr.exe")
    if not os.path.isfile(zr):
        log("downloading 7zr.exe (official standalone 7-Zip, ~600 KB)...")
        req = urllib.request.Request("https://www.7-zip.org/a/7zr.exe", headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=60) as resp, open(zr + ".part", "wb") as f:
            shutil.copyfileobj(resp, f)
        os.replace(zr + ".part", zr)
    log("extracting with 7zr...")
    r = subprocess.run([zr, "e", arc, f"-o{out_dir}", "libmpv-2.dll", "-r", "-y"],
                       capture_output=True, text=True, **_no_window())
    if found():
        return found()
    errors.append(f"7zr: {(r.stderr or r.stdout).strip()[-200:]}")
    raise RuntimeError("could not extract libmpv-2.dll - " + " | ".join(errors))


def install_libmpv(log=print) -> str:
    """Download the latest libmpv (shinchiro Windows build, x86_64) and put libmpv-2.dll next to this file."""
    import re
    import tempfile
    import urllib.request
    api = "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"
    with urllib.request.urlopen(urllib.request.Request(api, headers={"User-Agent": APP_NAME}), timeout=30) as r:
        rel = json.loads(r.read().decode("utf-8"))
    asset = next((a for a in rel.get("assets", [])
                  if re.match(r"^mpv-dev-x86_64-\d{8}-git-[0-9a-f]+\.7z$", a["name"])), None)
    if not asset:
        raise RuntimeError("could not find the mpv-dev-x86_64 package in the latest mpv-winbuild release")
    td = tempfile.mkdtemp(prefix="ytubeviewer_")
    try:
        arc = os.path.join(td, asset["name"])
        log(f"downloading {asset['name']} ({asset['size'] / 1e6:.0f} MB)...")
        req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=120) as resp, open(arc, "wb") as f:
            shutil.copyfileobj(resp, f)
        out = os.path.join(td, "out")
        os.makedirs(out, exist_ok=True)
        dll = _extract_dll(arc, out, log)
        shutil.copy2(dll, os.path.join(HERE, "libmpv-2.dll"))
    finally:
        shutil.rmtree(td, ignore_errors=True)
    return "libmpv-2.dll installed"


def import_mpv():
    """-> (mpv module or None, error text)."""
    try:
        import mpv  # python-mpv
        return mpv, ""
    except ImportError as ex:
        if "mpv" in str(ex) and "dll" not in str(ex).lower() and "library" not in str(ex).lower():
            return None, "python-mpv is not installed (pip install mpv)"
        return None, f"libmpv could not be loaded: {ex}"
    except OSError as ex:
        return None, f"libmpv could not be loaded: {ex}"


# --------------------------------------------------------------------------- YtubeCatcher bits
def catcher():
    """The ytubecatcher module (search + cookie settings), or None."""
    try:
        sys.path.insert(0, HERE)
        import ytubecatcher
        return ytubecatcher
    except Exception:
        return None


def cookie_settings() -> dict:
    """Sign-in to use -> {cookies_browser, cookies_file}. The viewer's own choice (account menu)
    wins; otherwise YtubeCatcher's YouTube-tab setting is used."""
    acct = load_json(VIEWER_SETTINGS, {}).get("account")
    s = acct if isinstance(acct, dict) else load_json(os.path.join(HERE, "settings.json"), {})
    mode = s.get("cookies_mode", "none")
    if mode == "browser" and s.get("cookies_browser"):
        return {"cookies_browser": s["cookies_browser"], "cookies_file": None}
    if mode == "file" and s.get("cookies_file") and os.path.isfile(s["cookies_file"]):
        return {"cookies_browser": None, "cookies_file": s["cookies_file"]}
    return {"cookies_browser": None, "cookies_file": None}


def video_key(url: str) -> str:
    """Same YouTube video -> same key, whatever form the link has (watch?v=, youtu.be/, shorts/, &t=...)."""
    import re
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})", url or "")
    return "yt:" + m.group(1) if m else os.path.normcase(os.path.abspath(url)) if os.path.exists(url or "") else (url or "")


def resolve_stream(url: str, quality: str, ck: dict) -> dict:
    """Ask yt-dlp (same library + sign-in as YtubeCatcher's downloads) for direct stream URLs."""
    import yt_dlp
    yc = catcher()
    if quality == "audio only":
        fmt = "ba[protocol^=http]/ba/b"
    else:
        h = quality
        fmt = (f"bv*[height<=?{h}][protocol^=http]+ba[protocol^=http]/"
               f"bv*[height<=?{h}]+ba/b[height<=?{h}]/b")
    base = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "format": fmt}
    signed = yc.cookie_opts(**ck) if yc else {}
    note = ""
    try:
        with yt_dlp.YoutubeDL({**base, **signed}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as ex:  # noqa: BLE001
        # signed-in requests use YouTube's web player, whose streams need a JS runtime (Deno);
        # the signed-out player does not - so fall back to that before giving up
        if not (signed and yc and yc.is_no_formats_error(str(ex))):
            raise
        with yt_dlp.YoutubeDL(base) as ydl:
            info = ydl.extract_info(url, download=False)
        note = "played signed out (signed-in streams need Deno - it is being set up)"
    if info.get("_type") == "playlist":
        info = next(e for e in info.get("entries") or [] if e)
    rf = info.get("requested_formats") or []
    if rf:
        video = rf[0]["url"]
        audio = rf[1]["url"] if len(rf) > 1 else None
        headers = rf[0].get("http_headers") or info.get("http_headers") or {}
        desc = " + ".join(f.get("format_note") or f.get("format_id") or "?" for f in rf)
    else:
        video, audio = info["url"], None
        headers = info.get("http_headers") or {}
        desc = info.get("format_note") or info.get("format_id") or ""
    return {"video": video, "audio": audio, "headers": headers, "title": info.get("title") or url,
            "duration": info.get("duration"), "desc": desc + (f"   |   {note}" if note else "")}


# --------------------------------------------------------------------------- GUI
# --------------------------------------------------------------------------- GUI theme, fonts, DPI
BG = "#0f0f0f"          # page
SURF = "#212121"        # cards
SURF2 = "#272727"       # chips / hover
LINE = "#303030"
TXT = "#f1f1f1"
SUB = "#aaaaaa"
RED = "#ff0033"
CLIP = "#2ecc71"
AMBER = "#ffb300"
UI = {"font": "Segoe UI" if os.name == "nt" else "DejaVu Sans",      # filled in by init_ui()
      "semi": None, "cjk": None, "scale": 1.0}
_FONT_CACHE: dict = {}


def enable_dpi_awareness() -> None:
    """Without this, Windows bitmap-stretches the whole window on scaled displays -> blurry text."""
    if os.name != "nt":
        return
    import ctypes
    for fn in (lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),     # per-monitor (8.1+)
               lambda: ctypes.windll.user32.SetProcessDPIAware()):          # system (Vista+)
        try:
            fn()
            return
        except Exception:
            pass


def init_ui(root) -> None:
    """Pick clean fonts that exist on this machine and the pixel scale for this display."""
    import tkinter.font as tkfont
    fams = set(tkfont.families(root))

    def first(*names):
        return next((n for n in names if n in fams), None)
    UI["font"] = first("Segoe UI Variable Text", "Segoe UI", "Inter", "Roboto", "Noto Sans", "DejaVu Sans") or "TkDefaultFont"
    UI["semi"] = first("Segoe UI Variable Text Semibold", "Segoe UI Semibold", "Inter SemiBold", "Roboto Medium",
                       "Noto Sans Medium")
    UI["cjk"] = first("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC",
                      "Source Han Sans SC", "PingFang SC")
    UI["scale"] = max(1.0, root.winfo_fpixels("1i") / 96.0)
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(name, root).configure(family=UI["font"], size=10)
        except Exception:
            pass


def px(v: float) -> int:
    """Design pixels (at 96 dpi) -> device pixels."""
    return int(round(v * UI["scale"]))


def _has_cjk(text: str) -> bool:
    return any("\u2e80" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af" or "\uff00" <= ch <= "\uffef"
               for ch in (text or ""))


def F(size: int, bold: bool = False, text: str | None = None):
    """UI font: Segoe UI (semibold instead of heavy bold); YaHei UI when the text has CJK."""
    import tkinter.font as tkfont
    fam, weight = UI["font"], "normal"
    if text is not None and UI["cjk"] and _has_cjk(text):
        fam, weight = UI["cjk"], ("bold" if bold else "normal")
    elif bold:
        fam, weight = (UI["semi"], "normal") if UI["semi"] else (UI["font"], "bold")
    key = (fam, size, weight)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = tkfont.Font(family=fam, size=size, weight=weight)
    return _FONT_CACHE[key]


# --------------------------------------------------------------------------- anti-aliased drawing
# Tk's canvas draws without anti-aliasing (jagged circles / diagonal edges on Windows). When Pillow
# is available every icon, round button, pill and knob is rendered 4x and downsampled instead.
_AA = 4
_IMG_CACHE: dict = {}


def _pil():
    try:
        from PIL import Image, ImageDraw
        return Image, ImageDraw
    except Exception:
        return None


class _AACanvas:
    """Accepts the tk.Canvas calls draw_icon() makes and paints them with Pillow at 4x."""

    def __init__(self, draw):
        self.d = draw

    @staticmethod
    def _xy(pts):
        flat = []
        for p in pts:
            flat.extend(p if isinstance(p, (list, tuple)) else [p])
        return [v * _AA for v in flat]

    def create_polygon(self, *pts, fill="", outline="", width=1, **kw):
        xy = self._xy(pts)
        pairs = list(zip(xy[0::2], xy[1::2]))
        if fill:
            self.d.polygon(pairs, fill=fill)
        if outline and width:
            self.d.line(pairs + pairs[:1], fill=outline, width=max(1, round(width * _AA)), joint="curve")

    def create_rectangle(self, *c, fill=None, outline=None, width=0, **kw):
        x0, y0, x1, y1 = self._xy(c)
        self.d.rectangle((x0, y0, x1, y1), fill=fill or None,
                         outline=outline or None, width=max(0, round(width * _AA)))

    def create_oval(self, *c, fill=None, outline=None, width=1, **kw):
        x0, y0, x1, y1 = self._xy(c)
        self.d.ellipse((x0, y0, x1, y1), fill=fill or None, outline=outline or None,
                       width=max(1, round(width * _AA)))

    def create_arc(self, *c, start=0, extent=90, outline="black", width=1, **kw):
        x0, y0, x1, y1 = self._xy(c)
        self.d.arc((x0, y0, x1, y1), start=-(start + extent), end=-start, fill=outline,
                   width=max(1, round(width * _AA)))

    def create_line(self, *pts, fill="black", width=1, capstyle=None, arrow=None, arrowshape=None, **kw):
        import math
        xy = self._xy(pts)
        pairs = list(zip(xy[0::2], xy[1::2]))
        wpx = max(1, round(width * _AA))
        if arrow == "last" and len(pairs) >= 2:
            d1, d2, d3 = [v * _AA for v in (arrowshape or (8, 10, 3))]
            (xa, ya), (xb, yb) = pairs[-2], pairs[-1]
            L = math.hypot(xb - xa, yb - ya) or 1
            ux, uy = (xb - xa) / L, (yb - ya) / L
            pairs[-1] = (xb - ux * d1 * 0.6, yb - uy * d1 * 0.6)      # stop the shaft inside the head
            self.d.line(pairs, fill=fill, width=wpx, joint="curve")
            bx, by = xb - ux * d2, yb - uy * d2
            self.d.polygon([(xb, yb), (bx - uy * (d3 + wpx / 2), by + ux * (d3 + wpx / 2)),
                            (xb - ux * d1, yb - uy * d1), (bx + uy * (d3 + wpx / 2), by - ux * (d3 + wpx / 2))],
                           fill=fill)
        else:
            self.d.line(pairs, fill=fill, width=wpx, joint="curve")
        if capstyle in ("round", "projecting"):
            r = wpx / 2
            for x, y in (pairs[0], pairs[-1]):
                self.d.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    def rounded(self, x0, y0, x1, y1, r, fill):
        self.d.rounded_rectangle([v * _AA for v in (x0, y0, x1, y1)], radius=r * _AA, fill=fill)


def aa_image(key, w: int, h: int, paint):
    """Cached anti-aliased RGBA PhotoImage (transparent background); paint(canvas_like) draws in w x h."""
    import base64
    import io
    import tkinter as tk
    if key in _IMG_CACHE:
        return _IMG_CACHE[key]
    mods = _pil()
    if not mods:
        return None
    Image, ImageDraw = mods
    im = Image.new("RGBA", (max(1, w) * _AA, max(1, h) * _AA), (0, 0, 0, 0))
    paint(_AACanvas(ImageDraw.Draw(im)))
    im = im.resize((max(1, w), max(1, h)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    img = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))
    _IMG_CACHE[key] = img
    return img


def _rrect(cv, x0, y0, x1, y1, r, **kw):
    """Rounded rectangle (anti-aliased when drawn through aa_image)."""
    if isinstance(cv, _AACanvas):
        return cv.rounded(x0, y0, x1, y1, r, kw.get("fill"))
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
           x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


def icon_img(name: str, size: int, color: str, circle: str | None = None, icon_frac: float = 0.55):
    """An icon (optionally on a filled circle) as an anti-aliased image, or None without Pillow."""
    def paint(c):
        if circle:
            c.create_oval(1, 1, size - 1, size - 1, fill=circle, outline="", width=0)
        draw_icon(c, name, size / 2, size / 2, size * icon_frac, color)
    return aa_image(("icon", name, size, color, circle, icon_frac), size, size, paint)


def dot_img(d: int, color: str):
    return aa_image(("dot", d, color), d, d, lambda c: c.create_oval(0.5, 0.5, d - 0.5, d - 0.5, fill=color,
                                                                    outline="", width=0))


def icon_label(parent, name: str, size: int, color: str, bg: str):
    """Static icon (anti-aliased when Pillow is present)."""
    import tkinter as tk
    img = icon_img(name, size, color, icon_frac=0.9)
    if img:
        lb = tk.Label(parent, image=img, bg=bg, bd=0)
        lb.image = img
        return lb
    cv = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0)
    draw_icon(cv, name, size / 2, size / 2, size * 0.9, color)
    return cv


def draw_icon(cv, name: str, cx: float, cy: float, size: float, color: str):
    """Vector icons in a 24x24 design box centred on (cx, cy)."""
    k = size / 24.0

    def P(*xy):
        return [cx + (v - 12) * k if i % 2 == 0 else cy + (v - 12) * k for i, v in enumerate(xy)]
    w = max(1.5, 2 * k)
    if name == "play":
        cv.create_polygon(P(7, 4, 20, 12, 7, 20), fill=color, outline=color, joinstyle="round")
    elif name == "pause":
        cv.create_rectangle(P(6, 4, 10, 20), fill=color, width=0)
        cv.create_rectangle(P(14, 4, 18, 20), fill=color, width=0)
    elif name in ("back", "fwd"):
        pts = [(12, 5, 3, 12, 12, 19), (21, 5, 12, 12, 21, 19)] if name == "back" else \
              [(3, 5, 12, 12, 3, 19), (12, 5, 21, 12, 12, 19)]
        for t in pts:
            cv.create_polygon(P(*t), fill=color, outline=color)
    elif name in ("frame_back", "frame_fwd"):
        if name == "frame_back":
            cv.create_rectangle(P(5, 6, 7.5, 18), fill=color, width=0)
            cv.create_polygon(P(19, 6, 9, 12, 19, 18), fill=color, outline=color)
        else:
            cv.create_rectangle(P(16.5, 6, 19, 18), fill=color, width=0)
            cv.create_polygon(P(5, 6, 15, 12, 5, 18), fill=color, outline=color)
    elif name in ("volume", "mute"):
        cv.create_polygon(P(3, 9, 7, 9, 12, 4.5, 12, 19.5, 7, 15, 3, 15), fill=color, outline=color)
        if name == "volume":
            cv.create_arc(P(8, 8, 16, 16), start=-50, extent=100, style="arc", outline=color, width=w)
            cv.create_arc(P(6, 4.5, 20, 19.5), start=-50, extent=100, style="arc", outline=color, width=w)
        else:
            cv.create_line(P(15, 9, 21, 15), fill=color, width=w)
            cv.create_line(P(21, 9, 15, 15), fill=color, width=w)
    elif name in ("fullscreen", "unfullscreen"):
        segs = [(3, 9, 3, 3, 9, 3), (15, 3, 21, 3, 21, 9), (21, 15, 21, 21, 15, 21), (9, 21, 3, 21, 3, 15)]
        if name == "unfullscreen":
            segs = [(3, 9, 9, 9, 9, 3), (15, 3, 15, 9, 21, 9), (21, 15, 15, 15, 15, 21), (9, 21, 9, 15, 3, 15)]
        for sg in segs:
            cv.create_line(P(*sg), fill=color, width=w, capstyle="projecting", joinstyle="miter")
    elif name == "search":
        cv.create_oval(P(3.5, 3.5, 15.5, 15.5), outline=color, width=w)
        cv.create_line(P(14, 14, 20.5, 20.5), fill=color, width=w * 1.3, capstyle="round")
    elif name == "loop":
        cv.create_line(P(5, 13, 5, 7, 18, 7), fill=color, width=w, arrow="last",
                       arrowshape=(6 * k, 7 * k, 3 * k))
        cv.create_line(P(19, 11, 19, 17, 6, 17), fill=color, width=w, arrow="last",
                       arrowshape=(6 * k, 7 * k, 3 * k))
    elif name == "trash":
        cv.create_line(P(4, 6.5, 20, 6.5), fill=color, width=w)
        cv.create_line(P(9.5, 6.5, 9.5, 3.5, 14.5, 3.5, 14.5, 6.5), fill=color, width=w)
        cv.create_polygon(P(6, 8.5, 18, 8.5, 17, 20.5, 7, 20.5), fill="", outline=color, width=w)
    elif name == "send":
        cv.create_polygon(P(3, 3.5, 21.5, 12, 3, 20.5, 5.5, 12), fill=color, outline=color, joinstyle="round")
    elif name == "close":
        cv.create_line(P(5, 5, 19, 19), fill=color, width=w)
        cv.create_line(P(19, 5, 5, 19), fill=color, width=w)
    elif name == "mark_in":
        cv.create_line(P(9, 4, 5, 4, 5, 20, 9, 20), fill=color, width=w * 1.2)
        cv.create_polygon(P(10, 7, 19, 12, 10, 17), fill=color, outline=color)
    elif name == "mark_out":
        cv.create_line(P(15, 4, 19, 4, 19, 20, 15, 20), fill=color, width=w * 1.2)
        cv.create_rectangle(P(6, 7.5, 13, 16.5), fill=color, width=0)
    elif name == "logo":
        _rrect(cv, *P(1, 4.5, 23, 19.5), 5 * k, fill=RED, outline="")
        cv.create_polygon(P(9.5, 8, 16.5, 12, 9.5, 16), fill="white", outline="white")
    elif name == "download":
        cv.create_line(P(12, 3, 12, 15), fill=color, width=w * 1.2)
        cv.create_polygon(P(6.5, 10, 12, 16, 17.5, 10), fill=color, outline=color)
        cv.create_line(P(4, 17, 4, 20.5, 20, 20.5, 20, 17), fill=color, width=w * 1.2)
    elif name == "folder":
        cv.create_polygon(P(3, 6, 9, 6, 11, 8.5, 21, 8.5, 21, 19, 3, 19), fill="", outline=color, width=w,
                          joinstyle="round")
    elif name == "user":
        cv.create_oval(P(8, 3.5, 16, 11.5), outline=color, width=w)
        cv.create_arc(P(4, 13, 20, 29), start=0, extent=180, style="arc", outline=color, width=w)
    elif name == "check":
        cv.create_line(P(4.5, 12.5, 9.5, 17.5, 19.5, 6.5), fill=color, width=w * 1.3, capstyle="round",
                       joinstyle="round")
    elif name == "scissors":
        cv.create_oval(P(3, 14, 9, 20), outline=color, width=w)
        cv.create_oval(P(15, 14, 21, 20), outline=color, width=w)
        cv.create_line(P(8, 15, 19, 3), fill=color, width=w)
        cv.create_line(P(16, 15, 5, 3), fill=color, width=w)


class Tooltip:
    def __init__(self, widget, text: str):
        self.w, self.text, self.tip, self.job = widget, text, None, None
        widget.bind("<Enter>", self._arm, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _arm(self, _e=None):
        self.job = self.w.after(450, self._show)

    def _show(self):
        import tkinter as tk
        if self.tip or not self.text:
            return
        x = self.w.winfo_rootx() + self.w.winfo_width() // 2
        y = self.w.winfo_rooty() - px(32)
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.attributes("-topmost", True)
        lb = tk.Label(self.tip, text=self.text, bg="#3a3a3a", fg=TXT, font=F(9), padx=px(8), pady=px(4))
        lb.pack()
        self.tip.update_idletasks()
        self.tip.geometry(f"+{x - lb.winfo_reqwidth() // 2}+{max(0, y)}")

    def _hide(self, _e=None):
        if self.job:
            self.w.after_cancel(self.job)
            self.job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


def popup_above(menu, widget) -> None:
    """Open a menu just above the widget that opened it (measured, so it never covers the widget)."""
    menu.update_idletasks()
    y = widget.winfo_rooty() - menu.winfo_reqheight() - px(6)
    if y < 0:                                   # no room above: open below instead
        y = widget.winfo_rooty() + widget.winfo_height() + px(6)
    menu.tk_popup(widget.winfo_rootx(), y)


def icon_button(parent, icon: str, command, size: int = 40, bg: str = BG, fg: str = TXT,
                tip: str = "", hover: str = "#3a3a3a", icon_size: float | None = None):
    """Round, hover-highlighted icon button (like YouTube's player controls). size is in design px."""
    import tkinter as tk
    s = px(size)
    frac = (icon_size / size) if icon_size else 0.55
    cv = tk.Canvas(parent, width=s, height=s, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
    cv.icon = icon

    def draw(hot=False):
        cv.delete("all")
        img = icon_img(cv.icon, s, fg, circle=hover if hot else None, icon_frac=frac)
        if img:
            cv.create_image(s / 2, s / 2, image=img)
            return
        if hot:
            cv.create_oval(2, 2, s - 2, s - 2, fill=hover, outline="")
        draw_icon(cv, cv.icon, s / 2, s / 2, s * frac, fg)
    cv.set_icon = lambda name: (setattr(cv, "icon", name), draw())
    cv.bind("<Enter>", lambda e: draw(True))
    cv.bind("<Leave>", lambda e: draw(False))
    cv.bind("<ButtonRelease-1>", lambda e: command() if 0 <= e.x < s and 0 <= e.y < s else None)
    draw()
    if tip:
        Tooltip(cv, tip)
    return cv


def pill_button(parent, text: str, command, icon: str | None = None, bg: str = SURF2, fg: str = TXT,
                hover: str = "#3f3f3f", parent_bg: str = BG, height: int = 36, tip: str = "",
                font_size: int = 10, bold: bool = True):
    """Rounded 'chip' button with an optional icon (YouTube's Subscribe/Share style). height in design px."""
    import tkinter as tk
    f = F(font_size, bold)
    h = px(height)
    iw = round(h * 0.5) if icon else 0
    gap = px(8)

    def calc_w():
        return int(f.measure(cv.text) + iw + (h * 0.95 if icon else h * 0.8) + (gap if icon and cv.text else 0))
    cv = tk.Canvas(parent, width=10, height=h, bg=parent_bg, highlightthickness=0, bd=0, cursor="hand2")
    cv.text, cv.colors = text, (bg, hover, fg)

    def draw(hot=False):
        b, hv, c = cv.colors
        width = calc_w()
        if int(cv.cget("width")) != width:
            cv.configure(width=width)
        cv.delete("all")
        fill = hv if hot else b
        bgimg = aa_image(("pill", width, h, fill), width, h,
                         lambda k: _rrect(k, 0, 0, width, h, h / 2, fill=fill)) if fill != parent_bg else None
        if bgimg:
            cv.create_image(0, 0, image=bgimg, anchor="nw")
        elif fill != parent_bg:
            _rrect(cv, 0, 0, width - 1, h - 1, h / 2, fill=fill, outline="")
        x = h * 0.45
        if icon:
            img = icon_img(icon, iw, c, icon_frac=1.0)
            if img:
                cv.create_image(x + iw / 2, h / 2, image=img)
            else:
                draw_icon(cv, icon, x + iw / 2, h / 2, iw, c)
            x += iw + gap
        cv.create_text(x if icon else width / 2, h / 2, text=cv.text, fill=c, font=f,
                       anchor="w" if icon else "center")
    cv.redraw = draw
    cv.bind("<Enter>", lambda e: draw(True))
    cv.bind("<Leave>", lambda e: draw(False))
    cv.bind("<ButtonRelease-1>", lambda e: command(e) if 0 <= e.x < calc_w() and 0 <= e.y < h else None)
    draw()
    if tip:
        Tooltip(cv, tip)
    return cv


def fetch_thumb_png(video_id: str, w: int, h: int) -> bytes | None:
    """YouTube thumbnail -> PNG bytes at w x h (Pillow if present, else the bundled ffmpeg)."""
    import urllib.request
    try:
        req = urllib.request.Request(f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            jpg = r.read()
    except Exception:
        return None
    try:
        import io
        from PIL import Image
        from PIL import ImageDraw
        im = Image.open(io.BytesIO(jpg)).convert("RGBA").resize((w, h), Image.LANCZOS)
        mask = Image.new("L", (w * 4, h * 4), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1), radius=max(4, w // 21) * 4, fill=255)
        im.putalpha(mask.resize((w, h), Image.LANCZOS))
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()
    except Exception:
        pass
    yc = catcher()
    ff = yc.find_ffmpeg() if yc else shutil.which("ffmpeg")
    if not ff:
        return None
    try:
        r = subprocess.run([ff, "-loglevel", "error", "-i", "pipe:0", "-vf", f"scale={w}:{h}",
                            "-f", "image2pipe", "-vcodec", "png", "pipe:1"], input=jpg,
                           capture_output=True, timeout=20, **_no_window())
        return r.stdout or None
    except Exception:
        return None


def qual_label(v: str) -> str:
    return "Audio" if v == "audio only" else f"{v}p"


def fmt_views(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            v = n / div
            return f"{v:.1f}".rstrip("0").rstrip(".") + suf + " views"
    return f"{n} views" if n else ""


# --------------------------------------------------------------------------- GUI
def run_gui(initial_url: str | None = None):
    import base64
    import tkinter as tk
    from tkinter import messagebox

    st = load_json(VIEWER_SETTINGS, {})
    enable_dpi_awareness()
    root = tk.Tk()
    init_ui(root)
    root.title(APP_NAME)
    def initial_geometry() -> str:
        """Saved size, converted to this display's scale and clamped to the screen."""
        import re
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        m = re.match(r"(\d+)x(\d+)(?:([+-]-?\d+)([+-]-?\d+))?", st.get("geometry") or "")
        f = UI["scale"] / float(st.get("geometry_scale") or 1.0)
        w, h = (int(int(m.group(1)) * f), int(int(m.group(2)) * f)) if m else (px(1480), px(960))
        w, h = min(w, sw - px(20)), min(h, sh - px(60))
        if m and m.group(3):
            x, y = int(m.group(3)), int(m.group(4))
            if 0 <= x < sw - px(100) and 0 <= y < sh - px(100):
                return f"{w}x{h}{x:+d}{y:+d}"
        return f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}"
    root.geometry(initial_geometry())
    root.minsize(px(1000), px(640))
    root.configure(bg=BG)
    q: queue.Queue = queue.Queue()
    yc = catcher()
    menu_kw = dict(tearoff=0, bg="#282828", fg=TXT, activebackground="#3d3d3d", activeforeground=TXT,
                   bd=0, font=F(10), activeborderwidth=px(4))

    # =========================================================== top bar
    top = tk.Frame(root, bg=BG, height=px(60))
    top.pack(fill="x")
    top.pack_propagate(False)
    logo = icon_label(top, "logo", px(38), RED, BG)
    logo.pack(side="left", padx=(px(18), px(4)))
    tk.Label(top, text=APP_NAME, bg=BG, fg=TXT, font=F(15, True)).pack(side="left")

    right_top = tk.Frame(top, bg=BG)
    right_top.pack(side="right", padx=px(16))
    acct_chip = pill_button(right_top, "not signed in", lambda e: account_menu(e), icon="user", bg=BG,
                            hover=SURF2, fg="#3ea6ff", parent_bg=BG, height=36, tip="YouTube account",
                            font_size=9)
    acct_chip.pack(side="right")

    sbox = tk.Frame(top, bg=BG)
    sbox.pack(side="left", fill="x", expand=True, padx=(px(60), px(60)))
    sb_in = tk.Frame(sbox, bg=LINE)                      # 1px border
    sb_in.pack(side="left", fill="x", expand=True, pady=px(12))
    query_var = tk.StringVar(value=st.get("query", ""))
    q_entry = tk.Entry(sb_in, textvariable=query_var, bg="#121212", fg=TXT, insertbackground=TXT,
                       relief="flat", font=F(12), highlightthickness=0, bd=0)
    q_entry.pack(side="left", fill="both", expand=True, padx=(px(1), 0), pady=1, ipady=px(6), ipadx=px(12))
    s_btn = icon_button(sb_in, "search", lambda: go_search(), size=34, bg=SURF2, hover="#3a3a3a",
                        tip="Search, or paste a video URL (Enter)")
    s_btn.pack(side="left", padx=(px(1), px(1)), pady=1, ipadx=px(10))

    # =========================================================== body: player | results
    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True, padx=(px(24), px(12)), pady=(px(4), px(12)))
    body.columnconfigure(0, weight=1)
    body.columnconfigure(1, weight=0, minsize=px(410))
    body.rowconfigure(0, weight=1)

    col = tk.Frame(body, bg=BG)
    col.grid(row=0, column=0, sticky="nsew")
    side = tk.Frame(body, bg=BG, width=px(410))
    side.grid(row=0, column=1, sticky="nsew", padx=(px(24), 0))
    side.grid_propagate(False)

    # ---------------- player box (video + control bar, black like YouTube's player)
    pbox = tk.Frame(col, bg="black")
    pbox.pack(fill="both", expand=True)
    video = tk.Frame(pbox, bg="black", width=px(640), height=px(300))
    video.pack(fill="both", expand=True)
    msg_lbl = tk.Label(video, bg="black", fg="#c8c8c8", font=F(12), justify="center")
    msg_lbl.place(relx=0.5, rely=0.5, anchor="center")

    seek = tk.Canvas(pbox, height=px(20), bg="black", highlightthickness=0, cursor="hand2")
    seek.pack(fill="x", padx=px(12))
    bar = tk.Frame(pbox, bg="black")
    bar.pack(fill="x", padx=px(8), pady=(0, px(6)))
    CB = dict(bg="black", size=44, hover="#2b2b2b")
    play_btn = icon_button(bar, "play", lambda: toggle(), tip="Play / Pause (Space)", **CB)
    play_btn.pack(side="left")
    icon_button(bar, "back", lambda: rel_seek(-10), tip="Back 10 s (Left = 5 s)", **CB).pack(side="left")
    icon_button(bar, "fwd", lambda: rel_seek(10), tip="Forward 10 s (Right = 5 s)", **CB).pack(side="left")
    icon_button(bar, "frame_back", lambda: cmd("frame_back_step"), tip="Previous frame (,)", **CB).pack(side="left")
    icon_button(bar, "frame_fwd", lambda: cmd("frame_step"), tip="Next frame (.)", **CB).pack(side="left")
    vol_btn = icon_button(bar, "volume", lambda: toggle_mute(), tip="Mute (M)", **CB)
    vol_btn.pack(side="left")
    vol = tk.Canvas(bar, width=px(90), height=px(44), bg="black", highlightthickness=0, cursor="hand2")
    vol.pack(side="left")
    time_var = tk.StringVar(value="0:00 / 0:00")
    tk.Label(bar, textvariable=time_var, bg="black", fg=TXT, font=F(10)).pack(side="left", padx=px(12))
    fs_btn = icon_button(bar, "fullscreen", lambda: toggle_fullscreen(), tip="Full screen (F)", **CB)
    fs_btn.pack(side="right")
    qual_var = tk.StringVar(value=st.get("quality", "1080"))
    speed_var = tk.StringVar(value="1.0")
    qual_chip = pill_button(bar, qual_label(qual_var.get()), lambda e: pop_menu(e, "quality"), bg="#1f1f1f",
                            hover="#333333", parent_bg="black", height=30, tip="Quality", font_size=9)
    qual_chip.pack(side="right", padx=px(4))
    speed_chip = pill_button(bar, "1x", lambda e: pop_menu(e, "speed"), bg="#1f1f1f", hover="#333333",
                             parent_bg="black", height=30, tip="Playback speed", font_size=9)
    speed_chip.pack(side="right", padx=px(4))
    loop_btn = icon_button(bar, "loop", lambda: loop_toggle(), tip="Loop selected clip (L)", **CB)
    loop_btn.pack(side="right")

    # ---------------- title + channel
    meta = tk.Frame(col, bg=BG)
    meta.pack(fill="x", pady=(px(12), 0))
    title_var = tk.StringVar(value="")
    title_lbl = tk.Label(meta, textvariable=title_var, bg=BG, fg=TXT, font=F(15, True), anchor="w",
                         justify="left", wraplength=px(900))
    title_var.trace_add("write", lambda *_: title_lbl.configure(font=F(15, True, title_var.get())))
    title_lbl.pack(fill="x")
    sub_var = tk.StringVar(value="")
    sub_lbl = tk.Label(meta, textvariable=sub_var, bg=BG, fg=SUB, font=F(10), anchor="w")
    sub_lbl.pack(fill="x", pady=(px(2), 0))
    sub_var.trace_add("write", lambda *_: sub_lbl.configure(font=F(10, False, sub_var.get())))
    meta.bind("<Configure>", lambda e: title_lbl.configure(wraplength=max(300, e.width - 10)))

    # ---------------- clips card
    card = tk.Frame(col, bg=SURF)
    card.pack(fill="x", pady=(px(12), 0))
    ch = tk.Frame(card, bg=SURF)
    ch.pack(fill="x", padx=px(14), pady=(px(10), px(6)))
    icon_label(ch, "scissors", px(20), TXT, SURF).pack(side="left")
    tk.Label(ch, text="Clips", bg=SURF, fg=TXT, font=F(11, True)).pack(side="left", padx=(px(6), px(16)))
    PB = dict(parent_bg=SURF, height=32, font_size=9)
    pill_button(ch, "Clear all", lambda e: clear_clips(), bg=SURF, hover=SURF2, fg=SUB, bold=False,
                **PB).pack(side="right", padx=px(6))
    pill_button(ch, "Mark IN", lambda e: mark_in(), icon="mark_in", tip="Mark clip start (I)", **PB).pack(side="left", padx=px(3))
    pill_button(ch, "Mark OUT", lambda e: mark_out(), icon="mark_out", tip="Mark clip end (O)", **PB).pack(side="left", padx=px(3))
    in_var = tk.StringVar(value="")
    tk.Label(ch, textvariable=in_var, bg=SURF, fg=AMBER, font=F(10, True)).pack(side="left", padx=px(10))
    clip_rows = tk.Frame(card, bg=SURF)
    clip_rows.pack(fill="x", padx=px(14), pady=(0, px(6)))

    # ---------------- extract (the YtubeCatcher engine, built in)
    ysaved = yc.load_settings() if yc else {}
    ex_st = st.get("extract") or {}
    ex = {"mode": ex_st.get("mode") or ("voice" if ysaved.get("voice_only") else ysaved.get("mode", "audio")),
          "fmt": ex_st.get("fmt") or ysaved.get("format", "mp3"),
          "bitrate": ex_st.get("bitrate") or ysaved.get("bitrate", "192"),
          "res": ex_st.get("res") or str(ysaved.get("resolution", "best")),
          "quality": ex_st.get("quality") or ysaved.get("quality") or "best",
          "denoise": bool(ex_st.get("denoise", ysaved.get("denoise", False))),
          "pad": str(ex_st.get("pad", ysaved.get("pad", "0.3"))),
          "out_dir": ex_st.get("out_dir") or ysaved.get("out_dir") or (yc.DEFAULT_OUT if yc else HERE)}
    filt = {"ok": bool(yc and yc.bgm_filter_available()), "proc": None}
    ex["whole"] = False                               # True = save the whole video even when clips exist
    if ex["mode"] == "voice" and not filt["ok"]:
        ex["mode"] = "audio"
    tk.Frame(card, bg=LINE, height=1).pack(fill="x", padx=px(14))
    xr = tk.Frame(card, bg=SURF)
    xr.pack(fill="x", padx=px(14), pady=(px(10), px(4)))
    tk.Label(xr, text="Save as", bg=SURF, fg=SUB, font=F(9)).pack(side="left", padx=(px(2), px(10)))
    CH = dict(parent_bg=SURF, height=30, font_size=9, bold=False)
    go_chip = pill_button(xr, "Extract", lambda e: extract(), icon="download", bg=RED, hover="#cc0029",
                          fg="white", parent_bg=SURF, height=34, font_size=10,
                          tip="Cut the clips (or the whole video) and save them")
    go_chip.pack(side="right")
    mode_chips = {}
    for key_, label, tip_ in (("voice", "Voice only", "Voice with the background music removed (BGM filter)"
                                                     + ("" if filt["ok"] else " - click to install it, one time")),
                              ("audio", "Audio", "The sound track as-is"),
                              ("video", "Video", "MP4 video")):
        c = pill_button(xr, label, lambda e, k=key_: set_mode(k), tip=tip_, **CH)
        c.pack(side="left", padx=px(2))
        mode_chips[key_] = c
    res_chip = pill_button(xr, "", lambda e: res_menu(e), tip="Download resolution (max)", **CH)
    opt_chip = pill_button(xr, "", lambda e: extract_menu(e), tip="Format / quality options", **CH)
    opt_chip.pack(side="left", padx=(px(10), px(2)))
    clean_chip = pill_button(xr, "Extra clean-up", lambda e: flip("denoise"),
                             tip="2nd pass that also removes ambience/noise from the voice", **CH)
    clean_chip.pack(side="left", padx=px(2))
    xr2 = tk.Frame(card, bg=SURF)
    xr2.pack(fill="x", padx=px(14), pady=(px(2), px(4)))
    tk.Label(xr2, text="Part", bg=SURF, fg=SUB, font=F(9)).pack(side="left", padx=(px(2), px(10)))
    whole_chip = pill_button(xr2, "Whole video", lambda e: set_scope(True), tip="Save the entire video", **CH)
    whole_chip.pack(side="left", padx=px(2))
    clips_chip = pill_button(xr2, "Clips", lambda e: set_scope(False),
                             tip="Save only the marked clips (several clips are joined into one file)", **CH)
    clips_chip.pack(side="left", padx=px(2))
    folder_chip = pill_button(xr2, "", lambda e: pick_folder(), icon="folder", tip="Save to (click to change)",
                              **CH)
    folder_chip.pack(side="left", padx=(px(16), 0))
    prog = tk.Canvas(xr2, height=px(6), bg=SURF, highlightthickness=0)
    prog.pack(side="left", fill="x", expand=True, padx=px(12))
    prog_var = tk.StringVar(value="")
    prog_lbl = tk.Label(xr2, textvariable=prog_var, bg=SURF, fg=SUB, font=F(9), width=34, anchor="e")
    prog_lbl.pack(side="right")
    outs = tk.Frame(card, bg=SURF)
    outs.pack(fill="x", padx=px(14), pady=(0, px(10)))

    status_var = tk.StringVar(value="")
    tk.Label(col, textvariable=status_var, bg=BG, fg=SUB, font=F(9), anchor="w", justify="left",
             wraplength=px(900)).pack(fill="x", pady=(px(8), 0))

    # ---------------- results sidebar
    side_head = tk.Frame(side, bg=BG)
    side_head.pack(fill="x")
    res_title = tk.StringVar(value="Search results")
    tk.Label(side_head, textvariable=res_title, bg=BG, fg=TXT, font=F(12, True), anchor="w").pack(side="left")
    search_status = tk.StringVar(value="Search above, or paste a YouTube link.")
    tk.Label(side, textvariable=search_status, bg=BG, fg=SUB, font=F(9), anchor="w", justify="left",
             wraplength=px(390)).pack(fill="x", pady=(px(2), px(8)))
    lst = tk.Canvas(side, bg=BG, highlightthickness=0)
    lst.pack(fill="both", expand=True)
    inner = tk.Frame(lst, bg=BG)
    lst_win = lst.create_window(0, 0, window=inner, anchor="nw")
    inner.bind("<Configure>", lambda e: lst.configure(scrollregion=lst.bbox("all")))
    lst.bind("<Configure>", lambda e: lst.itemconfigure(lst_win, width=e.width))

    def wheel(e):
        if e.widget.winfo_toplevel() is root and str(e.widget).startswith(str(lst)):
            lst.yview_scroll(int(-e.delta / 120) if e.delta else (-3 if e.num == 4 else 3), "units")
    root.bind_all("<MouseWheel>", wheel, add="+")
    root.bind_all("<Button-4>", wheel, add="+")
    root.bind_all("<Button-5>", wheel, add="+")

    # =========================================================== state + mpv
    state = {"url": "", "title": "", "in": None, "dur": None, "pos": None, "drag": None,
             "loop": None, "fs": False, "sel": None, "vol_drag": False}
    clips_by_url: dict[str, list] = {}
    for k_, v_ in (st.get("clips") or {}).items():          # older files keyed by raw URL: merge per video
        lst_ = clips_by_url.setdefault(video_key(k_), [])
        lst_.extend(tuple(x) for x in v_ if tuple(x) not in lst_)
    vol_val = [float(st.get("volume", 80))]
    results: list[dict] = []
    thumbs: dict[int, object] = {}
    search_gen = [0]
    load_gen = [0]
    mpvmod, mpv_err = import_mpv()
    player = None

    pending_in: dict[str, float | None] = {}      # an IN mark without its OUT yet, per video

    def clips() -> list:
        """Clips of the video that is loaded now (each video keeps its own list)."""
        return clips_by_url.setdefault(video_key(state["url"]), [])

    def show_msg(text: str):
        msg_lbl.configure(text=text)
        if text:
            msg_lbl.place(relx=0.5, rely=0.5, anchor="center")
        else:
            msg_lbl.place_forget()

    def update_account():
        if signin_watch["on"]:
            return
        ck = cookie_settings()
        if ck["cookies_file"]:
            t = "Signed in"
        elif ck["cookies_browser"] and not (yc and yc.COOKIE_NOTE["text"]):
            t = f"{ck['cookies_browser'].split(':')[0].title()} cookies"
        else:
            t = "Sign in"
        if acct_chip.text != t:
            acct_chip.text = t
            acct_chip.redraw()

    def set_account(mode, browser=None, file=None):
        st["account"] = {"cookies_mode": mode, "cookies_browser": browser, "cookies_file": file}
        persist()
        update_account()

    def account_menu(e):
        m = tk.Menu(root, **menu_kw)
        if yc:
            m.add_command(label="Sign in with a browser window (Chrome/Edge)", command=acct_open)
            m.add_command(label="Save sign-in now (if it wasn't picked up)", command=acct_save)
            m.add_separator()
        m.add_command(label="Use Firefox's cookies (logged in to YouTube there)",
                      command=lambda: set_account("browser", browser="firefox"))
        m.add_command(label="Use a cookies.txt file...", command=acct_file)
        m.add_command(label="Test sign-in", command=acct_test)
        m.add_separator()
        m.add_command(label="Sign out (no cookies)", command=lambda: set_account("none"))
        m.tk_popup(e.widget.winfo_rootx(), e.widget.winfo_rooty() + e.widget.winfo_height())

    def acct_bg(fn, ok_cb=None):
        def work():
            try:
                msg, ok = fn(), True
            except Exception as ex:  # noqa: BLE001
                msg, ok = str(ex).replace("ERROR: ", ""), False
            q.put(("call", lambda: (status_var.set(("" if ok else "Sign-in: ") + msg[-300:]),
                                    ok and ok_cb and ok_cb())))
        threading.Thread(target=work, daemon=True).start()

    signin_watch = {"on": False}

    def acct_open():
        status_var.set("Opening the sign-in window...")
        acct_bg(yc.open_signin_window, start_signin_watch)

    def start_signin_watch():
        """Poll the sign-in window; the moment YouTube's login cookies show up, save them and switch over."""
        if signin_watch["on"]:
            return
        signin_watch["on"] = True
        acct_chip.text = "Waiting for sign-in..."
        acct_chip.redraw()
        status_var.set("Sign in to YouTube in the window that opened - the app picks it up automatically.")

        def work():
            t_end = time.time() + 15 * 60
            msg, ok = "sign-in window timed out - open it again from the Sign in menu", False
            while time.time() < t_end and signin_watch["on"]:
                try:
                    msg, ok = yc.save_signin_cookies(), True
                    break
                except Exception as ex:  # noqa: BLE001
                    msg = str(ex)
                    if "closed" in msg or "open the sign-in window first" in msg:
                        break
                time.sleep(2.5)
            q.put(("call", lambda: signin_done(ok, msg)))
        threading.Thread(target=work, daemon=True).start()

    def signin_done(ok, msg):
        signin_watch["on"] = False
        if ok:
            set_account("file", file=yc.SIGNIN_COOKIES_FILE)
            status_var.set("Signed in - " + msg + ". Checking with YouTube...")
            acct_test()
        else:
            status_var.set("Sign-in: " + msg[-300:])
            update_account()

    def acct_save():
        signin_watch["on"] = False
        acct_bg(yc.save_signin_cookies,
                lambda: (set_account("file", file=yc.SIGNIN_COOKIES_FILE), acct_test()))

    def acct_file():
        from tkinter import filedialog
        p = filedialog.askopenfilename(title="Pick cookies.txt", filetypes=[("cookies.txt", "*.txt"), ("All", "*.*")])
        if p:
            set_account("file", file=p)

    def acct_test():
        if not yc:
            return
        status_var.set("Checking the sign-in...")
        ck = cookie_settings()

        def work():
            try:
                msg, ok = yc.youtube_check_login(**ck), True
            except Exception as ex:  # noqa: BLE001
                msg, ok = str(ex).replace("ERROR: ", ""), False

            def show():
                status_var.set(("YouTube: " + msg) if ok else ("Sign-in check failed: " + msg[-300:]))
                if ok:
                    acct_chip.text = "Signed in"
                    acct_chip.redraw()
            q.put(("call", show))
        threading.Thread(target=work, daemon=True).start()

    def make_player():
        nonlocal player
        root.update_idletasks()
        player = mpvmod.MPV(wid=str(video.winfo_id()), ytdl=False, osc=False, keep_open="yes",
                            input_default_bindings=False, input_vo_keyboard=False, cursor_autohide=1000,
                            hwdec="auto-safe", cache="yes", demuxer_max_bytes="300MiB",
                            volume=vol_val[0], log_handler=mpv_log, loglevel="warn")
        player.register_event_callback(mpv_event)

    def mpv_log(level, component, text):
        if level in ("error", "fatal"):
            q.put(("status", f"[{component}] {text.strip()}"))

    def mpv_event(ev):
        try:
            d = ev.as_dict() if hasattr(ev, "as_dict") else {}
        except Exception:
            d = {}
        name = d.get("event")
        name = name.decode() if isinstance(name, bytes) else name
        reason = d.get("reason")
        reason = reason.decode() if isinstance(reason, bytes) else reason
        if name == "end-file" and reason in ("error", 4):
            err = d.get("file_error") or d.get("error")
            err = err.decode() if isinstance(err, bytes) else ("" if isinstance(err, int) else str(err or ""))
            q.put(("error", err or "the stream could not be opened"))
        elif name == "file-loaded":
            q.put(("loaded", None))

    def getp(name):
        if not player:
            return None
        try:
            return getattr(player, name)
        except Exception:
            return None

    def setp(name, val):
        if player:
            try:
                setattr(player, name, val)
            except Exception:
                pass

    def cmd(name, *a):
        if player:
            try:
                getattr(player, name)(*a)
            except Exception as ex:  # noqa: BLE001
                status_var.set(str(ex))

    # =========================================================== playback
    def play(url: str, start: float | None = None, meta_info: dict | None = None):
        url = (url or "").strip().strip('"')
        if not url:
            return
        if not player:
            messagebox.showerror(APP_NAME, mpv_err or "player not ready")
            return
        if "://" not in url and len(url) == 11 and not os.path.exists(url):
            url = "https://www.youtube.com/watch?v=" + url
        if state["url"]:                              # park the old video's open IN mark with that video
            pending_in[video_key(state["url"])] = state["in"]
        state.update({"url": url, "title": "", "in": pending_in.get(video_key(url)), "dur": None, "pos": None,
                      "loop": None, "sel": None, "ready": False})
        try:
            player.pause = True             # old video frozen; I/O are refused until the new one has loaded
            player["ab-loop-a"] = "no"
            player["ab-loop-b"] = "no"
        except Exception:
            pass
        title_var.set("Loading...")
        in_var.set("IN " + fmt_time(state["in"]) if state["in"] is not None else "")
        if meta_info:
            sub_var.set("  •  ".join(x for x in (meta_info.get("channel"), fmt_views(meta_info.get("views"))) if x))
        else:
            sub_var.set("")
        refresh_clips()
        load_gen[0] += 1
        gen = load_gen[0]
        highlight_result()
        if not url.lower().startswith(("http://", "https://")):          # local file
            load_stream(gen, url, {"video": url, "audio": None, "headers": {}, "title": os.path.basename(url),
                                   "desc": "local file"}, start)
            return
        show_msg("Loading...")
        ck, qv = cookie_settings(), qual_var.get()

        def work():
            try:
                q.put(("resolved", (gen, url, resolve_stream(url, qv, ck), start)))
            except Exception as ex:  # noqa: BLE001
                msg = str(ex).replace("ERROR: ", "")
                if yc:
                    msg = yc.explain_cookie_error(msg)
                q.put(("resolve_err", (gen, msg[-500:])))
        threading.Thread(target=work, daemon=True).start()

    def load_stream(gen, url, r, start):
        if gen != load_gen[0] or url != state["url"]:
            return
        try:
            player["ab-loop-a"] = "no"
            player["ab-loop-b"] = "no"
            player["start"] = f"{start:.2f}" if start else "none"
            player.audio_files = [r["audio"]] if r.get("audio") else []
            player.http_header_fields = [f"{k}: {v}" for k, v in (r.get("headers") or {}).items()
                                         if k.lower() in ("user-agent", "referer", "origin", "accept-language")]
            player.force_media_title = r.get("title") or ""
            player.play(r["video"])
            player.pause = False
            state["title"] = r.get("title") or ""
            title_var.set(state["title"])
            root.title(f"{state['title']} - {APP_NAME}")
            note = yc.COOKIE_NOTE["text"] if yc else ""
            status_var.set((r.get("desc") or "")
                           + ("   |   signed out: browser cookies unreadable - use Sign in > Open sign-in window"
                              if note else ""))
        except Exception as ex:  # noqa: BLE001
            show_msg("Could not start playback:\n" + str(ex))

    def toggle():
        if player:
            setp("pause", not bool(getp("pause")))

    def toggle_mute():
        setp("mute", not bool(getp("mute")))

    def rel_seek(d: float):
        if player and getp("duration"):
            try:
                player.seek(d, reference="relative", precision="exact")
            except Exception:
                pass

    def abs_seek(t: float):
        if player and getp("duration"):
            try:
                player.seek(max(0.0, t), reference="absolute", precision="exact")
            except Exception:
                pass

    def toggle_fullscreen():
        state["fs"] = not state["fs"]
        if state["fs"]:
            top.pack_forget()
            side.grid_remove()
            meta.pack_forget()
            card.pack_forget()
            body.pack_configure(padx=0, pady=0)
            root.attributes("-fullscreen", True)
        else:
            root.attributes("-fullscreen", False)
            body.pack_forget()
            top.pack(fill="x")
            body.pack(fill="both", expand=True, padx=(px(24), px(12)), pady=(px(4), px(12)))
            side.grid()
            meta.pack(fill="x", pady=(px(12), 0))
            card.pack(fill="x", pady=(px(12), 0))
        fs_btn.set_icon("unfullscreen" if state["fs"] else "fullscreen")

    def pop_menu(e, which):
        m = tk.Menu(root, **menu_kw)
        if which == "quality":
            for v in reversed(QUALITIES[:-1]):
                m.add_radiobutton(label=qual_label(v), value=v, variable=qual_var, command=requality)
            m.add_separator()
            m.add_radiobutton(label="Audio only", value="audio only", variable=qual_var, command=requality)
        else:
            for v in SPEEDS:
                m.add_radiobutton(label="Normal" if v == "1.0" else f"{v}x", value=v, variable=speed_var,
                                  command=set_speed)
        popup_above(m, e.widget)

    def set_speed():
        setp("speed", float(speed_var.get()))
        speed_chip.text = "1x" if speed_var.get() == "1.0" else f"{float(speed_var.get()):g}x"
        speed_chip.redraw()

    def requality():
        qual_chip.text = qual_label(qual_var.get())
        qual_chip.redraw()
        if state["url"]:
            play(state["url"], start=getp("time_pos"))
        persist()

    # =========================================================== clips
    def refresh_clips():
        for w in clip_rows.winfo_children():
            w.destroy()
        cl = clips()
        if not cl:
            tk.Label(clip_rows, text="No clips yet - press I at the start and O at the end of a part you want.",
                     bg=SURF, fg=SUB, font=F(9), anchor="w").pack(fill="x", pady=px(4))
        for i, (a, b) in enumerate(cl):
            sel = state["sel"] == i
            rbg = SURF2 if sel else SURF
            row = tk.Frame(clip_rows, bg=rbg, cursor="hand2")
            row.pack(fill="x", pady=1)
            dimg = dot_img(px(9), CLIP)
            dot = tk.Label(row, image=dimg, bg=rbg) if dimg else tk.Label(row, text="●", fg=CLIP, bg=rbg)
            dot.image = dimg
            dot.pack(side="left", padx=(px(10), px(8)), pady=px(7))
            lb = tk.Label(row, text=f"Clip {i + 1}", bg=rbg, fg=TXT, font=F(10, True), anchor="w", width=6)
            lb.pack(side="left")
            tk.Label(row, text=f"{fmt_time(a)}  –  {fmt_time(b)}", bg=rbg, fg=TXT, font=F(10)).pack(side="left",
                                                                                              padx=px(8))
            tk.Label(row, text=f"{b - a:.1f} s", bg=rbg, fg=SUB, font=F(9)).pack(side="left", padx=px(12))
            icon_button(row, "trash", lambda i=i: del_clip(i), size=30, bg=rbg, hover="#444444",
                        tip="Delete clip").pack(side="right", padx=px(4))
            icon_button(row, "loop", lambda i=i: loop_clip(i), size=30, bg=rbg, hover="#444444",
                        tip="Loop this clip").pack(side="right")
            for w in (row, lb, dot):
                w.bind("<Button-1>", lambda e, i=i: select_clip(i))
                w.bind("<Double-Button-1>", lambda e, i=i: loop_clip(i))
        draw_seek()
        update_scope()

    def select_clip(i):
        state["sel"] = i
        refresh_clips()
        if i < len(clips()):
            abs_seek(clips()[i][0])

    def not_ready() -> bool:
        if state.get("ready"):
            return False
        status_var.set("Wait until the video has loaded before marking clips.")
        return True

    def mark_in():
        t = getp("time_pos")
        if t is None or not_ready():
            return
        state["in"] = float(t)
        in_var.set("IN " + fmt_time(t))
        draw_seek()

    def mark_out():
        t = getp("time_pos")
        if t is None or not_ready():
            return
        a = state["in"]
        if a is None:
            status_var.set("Mark IN first (I), then OUT (O).")
            return
        a, b = sorted((a, float(t)))
        if b - a < 0.2:
            status_var.set("Clip too short.")
            return
        clips().append((round(a, 1), round(b, 1)))
        clips().sort()
        state["in"] = None
        state["sel"] = clips().index((round(a, 1), round(b, 1)))
        ex["whole"] = False
        in_var.set("")
        refresh_clips()
        persist()
        status_var.set(f"Clip added: {fmt_time(a)} - {fmt_time(b)}")

    def loop_clip(i=None):
        if i is None:
            i = state["sel"] if state["sel"] is not None else len(clips()) - 1
        if i is None or i < 0 or i >= len(clips()) or not player:
            return
        a, b = clips()[i]
        state["sel"] = i
        setp("ab_loop_a", a)
        setp("ab_loop_b", b)
        state["loop"] = (a, b)
        abs_seek(a)
        setp("pause", False)
        refresh_clips()
        loop_btn.set_icon("loop")
        status_var.set(f"Looping clip {i + 1}: {fmt_time(a)} - {fmt_time(b)}   (L again to stop)")

    def stop_loop():
        if player:
            player["ab-loop-a"] = "no"
            player["ab-loop-b"] = "no"
        state["loop"] = None
        status_var.set("Loop off")

    def loop_toggle():
        stop_loop() if state["loop"] else loop_clip()

    def del_clip(i=None):
        if i is None:
            i = state["sel"]
        if i is not None and 0 <= i < len(clips()):
            del clips()[i]
            state["sel"] = None
            refresh_clips()
            persist()

    def clear_clips():
        if clips() and messagebox.askyesno(APP_NAME, "Remove all clips of this video?"):
            clips().clear()
            state["sel"] = None
            refresh_clips()
            persist()

    # =========================================================== extract
    busy = [False]

    def paint_chip(c, on):
        c.colors = ("#f1f1f1", "#d9d9d9", "#0f0f0f") if on else (SURF2, "#3f3f3f", TXT)
        c.redraw()

    def refresh_extract():
        for k, c in mode_chips.items():
            paint_chip(c, ex["mode"] == k)
        if not filt["ok"]:
            mode_chips["voice"].text = "Voice only  ↓" if not filt["proc"] else "Voice only  (installing...)"
            mode_chips["voice"].redraw()
        if ex["mode"] == "video":
            opt = "MP4"
            res_chip.text = "Resolution  " + ("Best" if ex["res"] == "best" else ex["res"] + "p") + "  ▾"
            paint_chip(res_chip, False)
            res_chip.pack(side="left", padx=(px(10), px(2)), before=opt_chip)
        else:
            res_chip.pack_forget()
            opt = f"{ex['fmt'].upper()} {ex['bitrate']}k" if ex["fmt"] in ("mp3", "m4a", "opus") else ex["fmt"].upper()
            if ex["mode"] == "voice":
                opt += "  ·  " + ("BS-RoFormer" if ex["quality"] == "best" else "Demucs")
        opt += f"  ·  pad {ex['pad']}s"
        opt_chip.text = opt + "  ▾"
        paint_chip(opt_chip, False)
        paint_chip(clean_chip, ex["denoise"])
        if ex["mode"] == "voice" and yc and yc.roformer_available():
            clean_chip.pack(side="left", padx=px(2))
        else:
            clean_chip.pack_forget()
        folder_chip.text = ex["out_dir"] if len(ex["out_dir"]) < 48 else "..." + ex["out_dir"][-45:]
        paint_chip(folder_chip, False)
        update_scope()

    def use_whole() -> bool:
        return ex["whole"] or not clips()

    def update_scope():
        n = len(clips())
        clips_chip.text = ("Clips  (mark with I / O)" if n == 0 else "1 clip" if n == 1
                           else f"{n} clips  →  1 file")
        paint_chip(whole_chip, use_whole())
        paint_chip(clips_chip, not use_whole())
        if n == 0:                                    # nothing to pick yet: looks like a hint, still clickable
            clips_chip.colors = (SURF, SURF2, SUB)
            clips_chip.redraw()

    def set_scope(whole: bool):
        if not whole and not clips():
            status_var.set("Mark clips first: press I at the start and O at the end of each part you want.")
            return
        ex["whole"] = whole
        update_scope()

    def res_menu(e):
        m = tk.Menu(root, **menu_kw)

        def setr(r):
            ex["res"] = r
            refresh_extract()
            persist()
        for r in (yc.RESOLUTIONS if yc else ["best", "1080", "720"]):
            m.add_command(label=("Best available" if r == "best" else r + "p") + ("   ✓" if ex["res"] == r else ""),
                          command=lambda r=r: setr(r))
        popup_above(m, e.widget)

    def set_mode(k):
        if k == "voice" and not filt["ok"]:
            install_filter()
            return
        ex["mode"] = k
        refresh_extract()
        persist()

    def install_filter():
        if filt["proc"] and filt["proc"].poll() is None:
            status_var.set("The BGM filter installer is still running in its own window.")
            return
        bat = os.path.join(HERE, "install_vocals.bat")
        if os.name != "nt" or not os.path.isfile(bat):
            status_var.set("Voice only needs the BGM filter: pip install torch audio-separator demucs")
            return
        if not messagebox.askyesno(APP_NAME, "Voice only removes the background music with an AI model "
                                   "(the BGM filter).\n\nIt needs a one-time install: PyTorch + BS-RoFormer, "
                                   "about 3.5 GB (uses your NVIDIA GPU if present).\n\nInstall it now?"):
            return
        filt["proc"] = subprocess.Popen(["cmd", "/c", bat], cwd=HERE, creationflags=0x00000010)  # new console
        status_var.set("Installing the BGM filter in a separate window - Voice only unlocks when it finishes.")
        refresh_extract()
        root.after(3000, watch_install)

    def watch_install():
        p_ = filt["proc"]
        if p_ and p_.poll() is None:
            root.after(3000, watch_install)
            return

        def check():
            try:            # check in a fresh interpreter: this one may have cached the failed imports
                ok = subprocess.run([sys.executable, "-c", "import torch, audio_separator"],
                                    capture_output=True, stdin=subprocess.DEVNULL, **_no_window()).returncode == 0
            except Exception:
                ok = False
            q.put(("call", lambda: filter_checked(ok)))
        threading.Thread(target=check, daemon=True).start()

    def filter_checked(ok):
        filt["proc"] = None
        if ok:
            filt["ok"] = True
            for m in [m for m in list(sys.modules) if m.startswith(("torch", "audio_separator", "demucs"))]:
                sys.modules.pop(m, None)
            mode_chips["voice"].text = "Voice only"
            ex["mode"] = "voice"
            status_var.set("BGM filter installed - Voice only is ready.")
        else:
            status_var.set("The BGM filter install did not finish - check its window, then click Voice only to retry.")
        refresh_extract()
        persist()

    def flip(k):
        ex[k] = not ex[k]
        refresh_extract()
        persist()

    def extract_menu(e):
        m = tk.Menu(root, **menu_kw)

        def setv(k, v):
            ex[k] = v
            refresh_extract()
            persist()
        if ex["mode"] != "video":
            for f in (yc.FORMATS if yc else ["mp3"]):
                m.add_command(label=f.upper() + ("   ✓" if ex["fmt"] == f else ""), command=lambda f=f: setv("fmt", f))
            m.add_separator()
            for b in (yc.BITRATES if yc else ["192"]):
                m.add_command(label=f"{b} kbps" + ("   ✓" if ex["bitrate"] == b else ""),
                              command=lambda b=b: setv("bitrate", b))
            if ex["mode"] == "voice":
                m.add_separator()
                for k_, lb in (("best", "Filter: BS-RoFormer (best)"), ("fast", "Filter: Demucs (fast)")):
                    m.add_command(label=lb + ("   ✓" if ex["quality"] == k_ else ""), command=lambda k_=k_: setv("quality", k_))
            m.add_separator()
        for p in ("0", "0.2", "0.3", "0.5", "1.0"):
            m.add_command(label=f"Pad clips {p} s" + ("   ✓" if ex["pad"] == p else ""), command=lambda p=p: setv("pad", p))
        popup_above(m, e.widget)

    def pick_folder():
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=ex["out_dir"] if os.path.isdir(ex["out_dir"]) else None)
        if d:
            ex["out_dir"] = os.path.normpath(d)
            refresh_extract()
            persist()

    def draw_prog(frac):
        prog.delete("all")
        w = max(prog.winfo_width(), 20)
        h = px(6)
        prog.create_rectangle(0, px(1), w, h - px(1), fill="#3a3a3a", width=0)
        if frac:
            prog.create_rectangle(0, px(1), w * max(0.0, min(1.0, frac)), h - px(1), fill=RED, width=0)

    def extract():
        if busy[0]:
            status_var.set("An extraction is already running.")
            return
        if not yc:
            messagebox.showerror(APP_NAME, "ytubecatcher.py is missing next to this file")
            return
        if not state["url"]:
            status_var.set("Play a video first, mark clips (I / O), then Extract.")
            return
        cl = [] if use_whole() else list(clips())
        timeline = [(None, fmt_time(a), fmt_time(b)) for a, b in cl] or None
        try:
            pad = float(ex["pad"])
        except ValueError:
            pad = 0.0
        common = dict(out_dir=ex["out_dir"], combine=len(cl) > 1, gap=0.5,
                      timeline=timeline, pad=pad if cl else 0.0, **cookie_settings(),
                      log=lambda m: q.put(("xlog", m)),
                      progress=lambda p, t: q.put(("xprog", (p, t))))
        mode = ex["mode"]
        busy[0] = True
        go_chip.text = "Working..."
        go_chip.colors = ("#5a5a5a", "#5a5a5a", "white")
        go_chip.redraw()
        draw_prog(0.01)
        what = f"{len(cl)} clip{'s' if len(cl) != 1 else ''}" if cl else "the whole video"
        prog_var.set(f"Starting - {what}...")
        url = state["url"]

        def work():
            try:
                os.makedirs(ex["out_dir"], exist_ok=True)
                def run(kw):
                    if mode == "video":
                        return yc.dump_video([url], resolution=ex["res"], **kw)
                    return yc.dump_audio([url], fmt=ex["fmt"], bitrate=ex["bitrate"],
                                         voice_only=(mode == "voice"), quality=ex["quality"],
                                         denoise=ex["denoise"] and mode == "voice" and yc.roformer_available(),
                                         **kw)
                try:
                    files = run(common)
                except Exception as e1:  # noqa: BLE001
                    if not ((common.get("cookies_file") or common.get("cookies_browser"))
                            and yc.is_no_formats_error(str(e1))):
                        raise
                    q.put(("xlog", "signed-in download found no formats (needs Deno) - retrying signed out"))
                    files = run({**common, "cookies_file": None, "cookies_browser": None})
                q.put(("xdone", files))
            except Exception as ex_:  # noqa: BLE001
                q.put(("xerr", yc.explain_cookie_error(str(ex_))))
        threading.Thread(target=work, daemon=True).start()

    def extract_finished(files=None, err=None):
        busy[0] = False
        go_chip.text = "Extract"
        go_chip.colors = (RED, "#cc0029", "white")
        go_chip.redraw()
        if err:
            draw_prog(0)
            prog_var.set("Failed")
            status_var.set("Extract failed: " + err[-400:])
            return
        draw_prog(1.0)
        prog_var.set(f"Done - {len(files)} file{'s' if len(files) != 1 else ''}")
        for w in outs.winfo_children():
            w.destroy()
        for f in files[-6:]:
            r = tk.Frame(outs, bg=SURF)
            r.pack(fill="x", pady=1)
            icon_label(r, "check", px(16), CLIP, SURF).pack(side="left", padx=(px(2), 0))
            lb = tk.Label(r, text=os.path.basename(f), bg=SURF, fg=TXT, font=F(9, False, os.path.basename(f)),
                          anchor="w", cursor="hand2")
            lb.pack(side="left", padx=px(6))
            Tooltip(lb, "Click to preview in the player")
            lb.bind("<Button-1>", lambda e, f=f: play(f))
            icon_button(r, "folder", lambda f=f: show_in_folder(f), size=28, bg=SURF, hover=SURF2,
                        tip="Show in folder").pack(side="left")
        status_var.set("Saved to " + ex["out_dir"])

    def show_in_folder(f):
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(f)])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(f)])
        except Exception as ex_:  # noqa: BLE001
            status_var.set(str(ex_))

    # =========================================================== seek bar + volume
    hover_seek = [False]

    def x_of(t, w):
        d = state["dur"] or 0
        m = px(4)
        return m + (w - 2 * m) * (t / d) if d else m

    def draw_seek():
        seek.delete("all")
        w = max(seek.winfo_width(), 50)
        cy = px(10)
        th = px(5) if hover_seek[0] else px(3)
        y0, y1 = cy - th / 2, cy + th / 2
        seek.create_rectangle(px(4), y0, w - px(4), y1, fill="#4d4d4d", width=0)
        d = state["dur"]
        if not d:
            return
        for i, (a, b) in enumerate(clips()):
            col_ = "#7dffb0" if state["sel"] == i else CLIP
            seek.create_rectangle(x_of(a, w), cy - px(6), max(x_of(b, w), x_of(a, w) + 2), cy + px(6),
                                  fill=col_, width=0)
        p = state["drag"] if state["drag"] is not None else state["pos"]
        if p is not None:
            seek.create_rectangle(px(4), y0, x_of(p, w), y1, fill=RED, width=0)
        if state["in"] is not None:
            x = x_of(state["in"], w)
            seek.create_line(x, px(1), x, px(19), fill=AMBER, width=px(2))
            if p is not None and p > state["in"]:
                seek.create_rectangle(x, cy - px(4), x_of(p, w), cy + px(4), outline=AMBER, width=1)
        if p is not None:
            xp = x_of(p, w)
            r = px(7) if hover_seek[0] or state["drag"] is not None else px(5)
            img = dot_img(2 * r + 1, RED)
            if img:
                seek.create_image(xp, cy, image=img)
            else:
                seek.create_oval(xp - r, cy - r, xp + r, cy + r, fill=RED, outline="")

    def seek_time(event):
        d = state["dur"]
        w = max(seek.winfo_width(), 50)
        if not d:
            return None
        return max(0.0, min(d, (event.x - px(4)) / (w - px(8)) * d))

    def seek_press(e):
        t = seek_time(e)
        if t is not None:
            state["drag"] = t
            draw_seek()

    def seek_release(e):
        t = seek_time(e)
        state["drag"] = None
        if t is not None:
            abs_seek(t)
            state["pos"] = t
            draw_seek()

    def seek_hover(on):
        hover_seek[0] = on
        draw_seek()

    seek.bind("<Button-1>", seek_press)
    seek.bind("<B1-Motion>", seek_press)
    seek.bind("<ButtonRelease-1>", seek_release)
    seek.bind("<Enter>", lambda e: seek_hover(True))
    seek.bind("<Leave>", lambda e: seek_hover(False))
    seek.bind("<Configure>", lambda e: draw_seek())

    def draw_vol():
        vol.delete("all")
        v = 0 if getp("mute") else vol_val[0]
        x0, x1, y = px(7), px(83), px(22)
        xv = x0 + (x1 - x0) * min(v, 100) / 100
        vol.create_rectangle(x0, y - px(1.5), x1, y + px(1.5), fill="#5a5a5a", width=0)
        vol.create_rectangle(x0, y - px(1.5), xv, y + px(1.5), fill="white", width=0)
        img = dot_img(px(12) + 1, "white")
        if img:
            vol.create_image(xv, y, image=img)
        else:
            vol.create_oval(xv - px(6), y - px(6), xv + px(6), y + px(6), fill="white", outline="")

    def vol_set(e):
        vol_val[0] = max(0.0, min(100.0, (e.x - px(7)) / px(76) * 100))
        setp("volume", vol_val[0])
        setp("mute", False)
        draw_vol()
    vol.bind("<Button-1>", vol_set)
    vol.bind("<B1-Motion>", vol_set)
    vol.bind("<ButtonRelease-1>", lambda e: persist())

    # =========================================================== search + results
    def looks_like_url(s: str) -> bool:
        s = s.strip()
        return "://" in s or s.startswith(("www.", "youtu.be/", "youtube.com/")) or \
            (len(s) == 11 and " " not in s and any(c.isdigit() for c in s) and any(c.isupper() for c in s)) or \
            os.path.isfile(s.strip('"'))

    def go_search(*_):
        text = query_var.get().strip()
        if not text:
            return
        if looks_like_url(text):
            u = text if ("://" in text or os.path.isfile(text.strip('"')) or len(text) == 11) else "https://" + text
            play(u)
            return
        if not yc:
            search_status.set("ytubecatcher.py not found next to this file - search unavailable")
            return
        search_gen[0] += 1
        gen = search_gen[0]
        res_title.set("Searching...")
        search_status.set(f"Looking for '{text}' on YouTube...")
        ck = cookie_settings()

        def work():
            try:
                res, err = yc.youtube_search(text, 30, **ck), None
            except Exception as ex:  # noqa: BLE001
                res, err = [], yc.explain_cookie_error(str(ex).replace("ERROR: ", ""))[-300:]
            q.put(("results", (gen, text, res, err)))
        threading.Thread(target=work, daemon=True).start()
        persist()

    def show_results(gen, text, res, err):
        if gen != search_gen[0]:
            return
        results.clear()
        results.extend(res)
        thumbs.clear()
        for w in inner.winfo_children():
            w.destroy()
        rows.clear()
        lst.yview_moveto(0)
        res_title.set(f"Results for '{text}'" if not err else "Search failed")
        note = yc.COOKIE_NOTE["text"] if yc else ""
        search_status.set(err if err else f"{len(res)} videos - click one to play"
                          + ("   (signed out: browser cookies unreadable - use Sign in at the top right)" if note else ""))
        for i, r in enumerate(res):
            build_row(i, r)
        threading.Thread(target=load_thumbs, args=(gen, list(enumerate(res))), daemon=True).start()
        highlight_result()

    TW, TH = px(168), px(94)
    rows: list = []

    def build_row(i, r):
        row = tk.Frame(inner, bg=BG, cursor="hand2")
        row.pack(fill="x", pady=px(4))
        th = tk.Canvas(row, width=TW, height=TH, bg=BG, highlightthickness=0)
        th.pack(side="left")
        ph = aa_image(("thumbph", TW, TH), TW, TH, lambda k: _rrect(k, 0, 0, TW, TH, px(8), fill="#262626"))
        if ph:
            th.create_image(0, 0, image=ph, anchor="nw")
        else:
            th.create_rectangle(0, 0, TW, TH, fill="#262626", width=0)
        if r.get("duration"):
            f = F(8, True)
            tw, bh, m = f.measure(r["duration"]) + px(10), px(17), px(5)
            badge = aa_image(("badge", tw, bh), tw, bh, lambda k: _rrect(k, 0, 0, tw, bh, px(4), fill="#000000"))
            if badge:
                th.create_image(TW - m - tw, TH - m - bh, image=badge, anchor="nw", tags="badge")
            else:
                th.create_rectangle(TW - m - tw, TH - m - bh, TW - m, TH - m, fill="#000000", width=0, tags="badge")
            th.create_text(TW - m - tw / 2, TH - m - bh / 2, text=r["duration"], fill="white", font=f, tags="badge")
        txt = tk.Frame(row, bg=BG)
        txt.pack(side="left", fill="both", expand=True, padx=(px(10), 0))
        t = tk.Label(txt, text=r["title"], bg=BG, fg=TXT, font=F(10, True, r["title"]), anchor="nw",
                     justify="left", wraplength=px(215))
        t.pack(fill="x")
        # wrap titles to the space the sidebar really has (a fixed wraplength cut long titles off)
        txt.bind("<Configure>", lambda e, t=t: t.configure(wraplength=max(px(80), e.width - px(4)))
                 if abs(int(t.cget("wraplength")) - (e.width - px(4))) > 2 else None)
        ch_ = r.get("channel") or ""
        c = tk.Label(txt, text=ch_, bg=BG, fg=SUB, font=F(9, False, ch_), anchor="w")
        c.pack(fill="x", pady=(px(4), 0))
        v = tk.Label(txt, text=fmt_views(r.get("views")), bg=BG, fg=SUB, font=F(9), anchor="w")
        v.pack(fill="x")
        widgets = (row, th, txt, t, c, v)

        def paint(bg):
            for w in (row, txt, t, c, v, th):
                w.configure(bg=bg)
        for w in widgets:
            w.bind("<Enter>", lambda e: paint(SURF2) if state.get("hl") != i else None)
            w.bind("<Leave>", lambda e: paint(BG) if state.get("hl") != i else None)
            w.bind("<Button-1>", lambda e, r=r: play(r["url"], meta_info=r))
        rows.append((i, r, paint, th))

    def highlight_result():
        state["hl"] = None
        for i, r, paint, _ in rows:
            on = bool(state["url"]) and (r["url"] == state["url"] or (r.get("id") and r["id"] in state["url"]))
            paint("#263850" if on else BG)
            if on:
                state["hl"] = i

    def load_thumbs(gen, items):
        from concurrent.futures import ThreadPoolExecutor

        def one(it):
            i, r = it
            if gen != search_gen[0] or not r.get("id"):
                return
            png = fetch_thumb_png(r["id"], TW, TH)
            if png:
                q.put(("thumb", (gen, i, png)))
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(one, items))

    def put_thumb(gen, i, png):
        if gen != search_gen[0]:
            return
        for j, _, _, th in rows:
            if j == i:
                try:
                    img = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
                except tk.TclError:
                    return
                thumbs[i] = img
                th.delete("thumbimg")
                th.create_image(0, 0, image=img, anchor="nw", tags="thumbimg")
                th.tag_raise("badge")
                return

    q_entry.bind("<Return>", go_search)

    # =========================================================== keys (not while typing)
    def key(e):
        if isinstance(e.widget, tk.Entry):
            return
        k, shift = e.keysym.lower(), bool(e.state & 0x1)
        acts = {"space": toggle, "k": toggle, "m": toggle_mute, "comma": lambda: cmd("frame_back_step"),
                "period": lambda: cmd("frame_step"), "i": mark_in, "o": mark_out, "l": loop_toggle,
                "delete": del_clip, "f": toggle_fullscreen,
                "left": lambda: rel_seek(-1 if shift else -5), "right": lambda: rel_seek(1 if shift else 5),
                "j": lambda: rel_seek(-10)}
        if k == "escape" and state["fs"]:
            toggle_fullscreen()
            return "break"
        if k in acts:
            acts[k]()
            return "break"
    root.bind_all("<KeyPress>", key)
    video.bind("<Button-1>", lambda e: root.focus_set())
    video.bind("<Double-Button-1>", lambda e: toggle_fullscreen())

    # =========================================================== loop
    def persist():
        keep = {u: c for u, c in clips_by_url.items() if c}
        save_json(VIEWER_SETTINGS, {"geometry": root.geometry() if not state["fs"] else st.get("geometry"),
                                    "geometry_scale": UI["scale"],
                                    "query": query_var.get(), "quality": qual_var.get(),
                                    "volume": vol_val[0], "clips": keep, "extract": ex,
                                    **({"account": st["account"]} if st.get("account") else {})})

    tick_n = [0]
    last_pause = [None]

    def tick():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "results":
                    show_results(*val)
                elif kind == "xlog":
                    status_var.set(str(val)[-240:])
                elif kind == "xprog":
                    draw_prog((val[0] or 0) / 100.0)
                    t_, room = str(val[1]), max(px(80), prog_lbl.winfo_width() - px(6))
                    while len(t_) > 4 and F(9).measure(t_) > room:       # trim to the pixels there are
                        t_ = t_[:-2].rstrip() + "…" if not t_.endswith("…") else t_[:-3].rstrip() + "…"
                    prog_var.set(t_)
                elif kind == "xdone":
                    extract_finished(files=val)
                elif kind == "xerr":
                    extract_finished(err=val)
                elif kind == "call":
                    val()
                elif kind == "thumb":
                    put_thumb(*val)
                elif kind == "status":
                    status_var.set(val[-240:])
                elif kind == "resolved":
                    load_stream(*val)
                elif kind == "resolve_err":
                    if val[0] == load_gen[0]:
                        show_msg("Could not get the video stream:\n\n" + val[1])
                        title_var.set("")
                        status_var.set(val[1][-240:])
                elif kind == "error":
                    show_msg("Playback failed:\n" + val + "\n\n(the stream link may have expired - play it again)")
                elif kind == "loaded":
                    show_msg("")
                    setp("pause", False)
                    state["ready"] = True
        except queue.Empty:
            pass
        if player:
            state["pos"] = getp("time_pos")
            state["dur"] = getp("duration")
            buf = "   buffering..." if getp("paused_for_cache") else ""
            time_var.set(f"{fmt_time(state['pos'] or 0).split('.')[0]} / "
                         f"{fmt_time(state['dur'] or 0).split('.')[0]}{buf}")
            pz = bool(getp("pause")) or not state["dur"]
            if pz != last_pause[0]:
                play_btn.set_icon("play" if pz else "pause")
                last_pause[0] = pz
            draw_seek()
            if tick_n[0] % 4 == 0:
                vol_btn.set_icon("mute" if getp("mute") or vol_val[0] == 0 else "volume")
                draw_vol()
        tick_n[0] += 1
        if tick_n[0] % 60 == 0:
            update_account()
        root.after(150, tick)

    def on_close():
        persist()
        try:
            if player:
                player.terminate()
        except Exception:
            pass
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)

    # =========================================================== start
    if mpvmod:
        try:
            make_player()
            show_msg("Search above or paste a YouTube link.\n\n"
                     "Space play/pause   ·   ← → seek   ·   , . frame step\n"
                     "I / O mark a clip   ·   L loop it   ·   F full screen")
        except Exception as err_:  # noqa: BLE001
            mpv_err = f"mpv failed to start: {err_}"
            player = None
    if not player:
        show_msg(mpv_err + "\n\nClick 'Install player' (downloads libmpv, ~30 MB), then restart the viewer.")

        def do_install(_e=None):
            def work():
                try:
                    if import_mpv()[0] is None and "python-mpv" in mpv_err:
                        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "mpv"], **_no_window())
                    msg = install_libmpv(log=lambda m: q.put(("status", m)))
                    q.put(("status", msg + " - restart the viewer"))
                except Exception as ex:  # noqa: BLE001
                    q.put(("status", "install failed: " + str(ex)))
            threading.Thread(target=work, daemon=True).start()
        pill_button(video, "Install player", do_install, bg=RED, hover="#cc0029", fg="white",
                    parent_bg="black").place(relx=0.5, rely=0.72, anchor="center")
    def ensure_deno():
        if not yc or yc.js_runtime_available():
            return

        def work():
            try:
                q.put(("status", yc.install_deno(log=lambda m: q.put(("status", m)))))
            except Exception as ex_:  # noqa: BLE001
                q.put(("status", f"Could not set up Deno ({ex_}) - signed-in playback may fail; "
                                 "install it with: winget install DenoLand.Deno"))
        threading.Thread(target=work, daemon=True).start()
    root.after(1500, ensure_deno)
    update_account()
    refresh_extract()
    refresh_clips()
    draw_vol()
    tick()
    if initial_url:
        root.after(300, lambda: play(initial_url))
    elif query_var.get().strip() and not looks_like_url(query_var.get()):
        root.after(300, go_search)
    root.mainloop()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="YtubeCatcher - YouTube player + clip extractor")
    ap.add_argument("url", nargs="?", help="video to open")
    ap.add_argument("--install-libmpv", action="store_true", help="download libmpv-2.dll next to this file")
    ap.add_argument("--install-deno", action="store_true", help="download deno.exe (JS runtime for yt-dlp)")
    a = ap.parse_args()
    if a.install_deno:
        yc = catcher()
        print(yc.install_deno() if yc and not yc.js_runtime_available() else "Deno already available")
        return
    if a.install_libmpv:
        print(install_libmpv())
        return
    run_gui(a.url)


if __name__ == "__main__":
    main()
