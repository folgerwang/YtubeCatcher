"""YtubeCatcher - YouTube player + clip extractor (engine: ytubecatcher.py).

No web browser: videos are streamed by yt-dlp and played in an embedded mpv (libmpv).
Search YouTube, watch, mark IN/OUT clips on the seek bar, then Extract them as voice only
(BGM filtered out), audio or video - the cutting/filtering is done by ytubecatcher.py.

Needs:  pip install mpv   +   libmpv-2.dll next to this file (run.bat does both).
Keys:   Space/K play/pause   Left/Right -5/+5 s (paused: one frame)   Shift+Left/Right -1/+1 s   J -10 s   , . frame step
        E frame-by-frame mode   S screenshot   Ctrl+O open a file   I mark IN   O mark OUT   L loop clip   Del delete clip   M mute   F fullscreen
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
CACHE_DIR = os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or HERE, "YtubeCatcher-cache")
# mpv cache profiles. "ahead": keep downloading the whole video while it plays (packets spill to a temp file,
# so a long 1080p video does not sit in RAM) and keep what was already watched, so seeking back is instant.
# "normal": a short read-ahead window - enough for smooth playback without pulling the whole video.
CACHE_PROFILES = {
    True: {"cache": "yes", "cache_secs": 36000, "demuxer_readahead_secs": 36000, "demuxer_max_bytes": "4GiB",
           "demuxer_max_back_bytes": "1GiB", "demuxer_seekable_cache": "yes", "cache_pause_wait": 2},
    False: {"cache": "yes", "cache_secs": 30, "demuxer_readahead_secs": 30, "demuxer_max_bytes": "150MiB",
            "demuxer_max_back_bytes": "50MiB", "demuxer_seekable_cache": "auto", "cache_pause_wait": 1},
}

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


def fmt_precise(secs) -> str:
    """seconds -> 'm:ss.mmm' (trailing zeros dropped): frame-accurate clip times."""
    secs = max(0.0, round(float(secs), 3))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    s_txt = f"{s:06.3f}".rstrip("0").rstrip(".")
    if len(s_txt) < 2 or s_txt[1] == ".":
        s_txt = "0" + s_txt
    return f"{int(h)}:{int(m):02d}:{s_txt}" if h else f"{int(m)}:{s_txt}"


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


# --------------------------------------------------------------------------- suggestions (home feed, up next)
def _walk_key(o, key):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield v
            yield from _walk_key(v, key)
    elif isinstance(o, list):
        for x in o:
            yield from _walk_key(x, key)


def _text(t) -> str:
    """YouTube text object ({content} / {simpleText} / {runs:[...]}) -> str."""
    if isinstance(t, str):
        return t
    if not isinstance(t, dict):
        return ""
    if "content" in t:
        return str(t["content"])
    if "simpleText" in t:
        return str(t["simpleText"])
    return "".join(str(r.get("text", "")) for r in t.get("runs") or [] if isinstance(r, dict))


def parse_video_cards(data) -> list[dict]:
    """ytInitialData of a YouTube page -> [{url, id, title, channel, duration, info}] (videos only:
    no Shorts, playlists, mixes or ads)."""
    out, seen = [], set()

    def add(vid, title, channel, duration, info):
        if vid and title and vid not in seen and len(vid) == 11:
            seen.add(vid)
            out.append({"url": "https://www.youtube.com/watch?v=" + vid, "id": vid, "title": title,
                        "channel": channel, "duration": duration if duration and duration[0].isdigit() else
                        ("LIVE" if duration and "live" in duration.lower() else ""), "info": info})
    for lk in _walk_key(data, "lockupViewModel"):
        if not isinstance(lk, dict) or lk.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
            continue
        md = (lk.get("metadata") or {}).get("lockupMetadataViewModel") or {}
        parts = []                  # home: one row "channel · 5.2K · 1d ago"; watch page: channel row + stats row
        for r in next(_walk_key(md, "metadataRows"), None) or []:
            for p in (r.get("metadataParts") or []) if isinstance(r, dict) else []:
                t = _text(p.get("text")).strip() if isinstance(p, dict) else ""
                if t:
                    if "view" in str(p.get("accessibilityLabel") or "") and "view" not in t:
                        t += " views"
                    parts.append(t)
        badge = next((str(b["text"]) for b in _walk_key(lk.get("contentImage"), "thumbnailBadgeViewModel")
                      if isinstance(b, dict) and b.get("text")), "")
        add(lk.get("contentId"), _text(md.get("title")), parts[0] if parts else "", badge, " • ".join(parts[1:]))
    for key in ("videoRenderer", "compactVideoRenderer", "gridVideoRenderer"):          # older page layouts
        for vr in _walk_key(data, key):
            if not isinstance(vr, dict):
                continue
            info = " • ".join(x for x in (_text(vr.get("shortViewCountText") or vr.get("viewCountText")),
                                          _text(vr.get("publishedTimeText"))) if x)
            add(vr.get("videoId"), _text(vr.get("title")),
                _text(vr.get("longBylineText") or vr.get("ownerText") or vr.get("shortBylineText")),
                _text(vr.get("lengthText")), info)
    return out


def youtube_feed(kind: str, ck: dict, video_id: str | None = None) -> list[dict]:
    """kind 'home': the youtube.com home page (your recommendations when signed in - empty when not);
    kind 'related': the 'Up next' column of a video's watch page."""
    import yt_dlp
    from yt_dlp.networking import Request
    yc = catcher()
    url = "https://www.youtube.com/" if kind == "home" else f"https://www.youtube.com/watch?v={video_id}"
    opts = {"quiet": True, "no_warnings": True, **(yc.cookie_opts(**ck) if yc else {})}
    with yt_dlp.YoutubeDL(opts) as ydl:
        html = ydl.urlopen(Request(url, headers={"Accept-Language": "en-US,en;q=0.9"})).read().decode("utf-8", "replace")
    i = html.find("ytInitialData = ")
    if i < 0:
        i = html.find('ytInitialData"] = ')
    if i < 0:
        raise RuntimeError("YouTube page had no video data")
    data, _ = json.JSONDecoder().raw_decode(html, html.index("{", i))
    items = parse_video_cards(data)
    return [it for it in items if it["id"] != video_id]


# --------------------------------------------------------------------------- fast-fetch stream proxy
class StreamProxy:
    """Local HTTP server between mpv and YouTube's CDN. One long request to googlevideo is paced at about
    playback speed (~1x), so mpv could never get far ahead; short ranged requests are served at full line
    speed. The proxy answers mpv's (range) requests by fetching the stream in CHUNK-sized pieces, so the
    cache-ahead fills at download speed. mpv's own reading provides the back-pressure."""
    CHUNK = 8 * 1024 * 1024

    def __init__(self):
        import http.server
        self.streams: dict[str, tuple[str, dict]] = {}
        self.sizes: dict[str, int] = {}
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_HEAD(self):
                proxy.serve(self, body=False)

            def do_GET(self):
                proxy.serve(self, body=True)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wrap(self, url: str, headers: dict) -> str:
        """Remote http(s) media URL -> local proxied URL (HLS/DASH manifests pass through untouched)."""
        if not url.lower().startswith(("http://", "https://")) or any(
                m in url.lower() for m in (".m3u8", "/manifest/", "hls_playlist")):
            return url
        import hashlib
        key = hashlib.sha1(url.encode()).hexdigest()[:16]
        if len(self.streams) > 40:                             # old links expire anyway
            self.streams.clear()
            self.sizes.clear()
        self.streams[key] = (url, dict(headers or {}))
        return f"http://127.0.0.1:{self.port}/s/{key}"

    def _fetch(self, url, headers, a, b):
        import urllib.request
        req = urllib.request.Request(url, headers={**headers, "Range": f"bytes={a}-{b}"})
        return urllib.request.urlopen(req, timeout=20)

    def _size(self, key, url, headers) -> int | None:
        if key not in self.sizes:
            with self._fetch(url, headers, 0, 0) as r:
                cr = r.headers.get("Content-Range") or ""
                total = cr.rsplit("/", 1)[-1] if "/" in cr else r.headers.get("Content-Length")
                self.sizes[key] = int(total) if total and total.isdigit() else None
        return self.sizes[key]

    def serve(self, h, body: bool):
        import re
        key = h.path.rsplit("/", 1)[-1]
        if key not in self.streams:
            h.send_error(404)
            return
        url, headers = self.streams[key]
        try:
            size = self._size(key, url, headers)
        except Exception as ex:  # noqa: BLE001
            h.send_error(502, str(ex)[:200])
            return
        if not size:
            h.send_error(502, "unknown stream size")
            return
        m = re.match(r"bytes=(\d*)-(\d*)", h.headers.get("Range") or "")
        a, b = 0, size - 1
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                a = int(m.group(1))
                b = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:                                              # suffix range: last N bytes
                a = max(0, size - int(m.group(2)))
        if a >= size:
            h.send_response(416)
            h.send_header("Content-Range", f"bytes */{size}")
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        h.send_response(206 if m else 200)
        h.send_header("Content-Type", "application/octet-stream")
        h.send_header("Accept-Ranges", "bytes")
        h.send_header("Content-Length", str(b - a + 1))
        if m:
            h.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        h.end_headers()
        if not body:
            return
        pos = a
        try:
            while pos <= b:
                end = min(b, pos + self.CHUNK - 1)
                for attempt in range(3):
                    try:
                        with self._fetch(url, headers, pos, end) as r:
                            while True:
                                data = r.read(256 * 1024)
                                if not data:
                                    break
                                h.wfile.write(data)
                                pos += len(data)
                        break
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        raise
                    except Exception:  # noqa: BLE001  (upstream hiccup: resume where it stopped)
                        if attempt == 2:
                            raise
                if pos <= end:                                 # upstream ended early
                    return
        except Exception:  # noqa: BLE001  (mpv seeked away / closed the file)
            h.close_connection = True


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
    elif name == "camera":
        cv.create_polygon(P(8, 6, 9.5, 3.5, 14.5, 3.5, 16, 6), fill=color, outline=color, joinstyle="round")
        cv.create_polygon(P(2.5, 6, 21.5, 6, 21.5, 19.5, 2.5, 19.5), fill="", outline=color, width=w,
                          joinstyle="round")
        cv.create_oval(P(8, 8.5, 16, 16.5), outline=color, width=w)
        cv.create_oval(P(17.6, 7.8, 19.4, 9.6), fill=color, outline="")
    elif name == "open":
        cv.create_polygon(P(2.5, 5, 9, 5, 11, 7.5, 21.5, 7.5, 21.5, 19.5, 2.5, 19.5), fill="", outline=color,
                          width=w, joinstyle="round")
        cv.create_line(P(12, 10.5, 12, 17), fill=color, width=w)
        cv.create_line(P(8.7, 13.75, 15.3, 13.75), fill=color, width=w)
    elif name == "film":
        cv.create_rectangle(P(3, 5, 21, 19), fill="", outline=color, width=w)
        for y_ in (6.8, 15.2):
            for x_ in (5.2, 9.4, 13.6, 17.8):
                cv.create_rectangle(P(x_ - 0.9, y_ - 0.9, x_ + 0.9, y_ + 0.9), fill=color, width=0)
        cv.create_line(P(3, 9.5, 21, 9.5), fill=color, width=w * 0.7)
        cv.create_line(P(3, 14.5, 21, 14.5), fill=color, width=w * 0.7)
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
    elif name == "home":
        cv.create_line(P(3, 11.5, 12, 3.5, 21, 11.5), fill=color, width=w, capstyle="round", joinstyle="round")
        cv.create_line(P(5.5, 9.5, 5.5, 20.5, 10, 20.5, 10, 14.5, 14, 14.5, 14, 20.5, 18.5, 20.5, 18.5, 9.5),
                       fill=color, width=w, joinstyle="round")
    elif name == "refresh":
        cv.create_arc(P(4, 4, 20, 20), start=70, extent=290, style="arc", outline=color, width=w)
        cv.create_polygon(P(11.5, 1.5, 17.5, 4.5, 12, 8.5), fill=color, outline=color, joinstyle="round")
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
    cv.icon, cv.fg, cv.ring = icon, fg, None

    def draw(hot=False):
        cv.delete("all")
        circle = hover if hot else cv.ring
        img = icon_img(cv.icon, s, cv.fg, circle=circle, icon_frac=frac)
        if img:
            cv.create_image(s / 2, s / 2, image=img)
            return
        if circle:
            cv.create_oval(2, 2, s - 2, s - 2, fill=circle, outline="")
        draw_icon(cv, cv.icon, s / 2, s / 2, s * frac, cv.fg)
    cv.set_icon = lambda name: (setattr(cv, "icon", name), draw())
    cv.set_active = lambda on, col=RED, ring="#3a1016": (setattr(cv, "fg", col if on else fg),
                                                          setattr(cv, "ring", ring if on else None), draw())
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
    jpg = None
    for name in (("hq720.jpg", "mqdefault.jpg") if w > 320 else ("mqdefault.jpg",)):   # hq720: 1280x720, not on all
        try:
            req = urllib.request.Request(f"https://i.ytimg.com/vi/{video_id}/{name}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                jpg = r.read()
            break
        except Exception:
            continue
    if not jpg:
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


def fmt_ago(t) -> str:
    try:
        d = max(0, time.time() - float(t))
    except (TypeError, ValueError):
        return ""
    for sec, unit in ((86400 * 365, "year"), (86400 * 30, "month"), (86400 * 7, "week"), (86400, "day"),
                      (3600, "hour"), (60, "minute")):
        if d >= sec:
            n = int(d // sec)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def qual_label(v: str) -> str:
    return "Audio" if v == "audio only" else f"{v}p"


def local_thumb_png(path: str, w: int, h: int) -> bytes | None:
    """A frame from a local video (or a waveform-ish placeholder for audio) as a rounded PNG."""
    yc = catcher()
    ff = yc.find_ffmpeg() if yc else shutil.which("ffmpeg")
    if not ff or not os.path.isfile(path):
        return None
    for ss in ("5", "0"):
        try:
            r = subprocess.run([ff, "-loglevel", "error", "-ss", ss, "-i", path, "-frames:v", "1",
                                "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
                                "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                               capture_output=True, stdin=subprocess.DEVNULL, timeout=20, **_no_window())
        except Exception:
            return None
        if r.stdout:
            try:
                import io
                from PIL import Image, ImageDraw
                im = Image.open(io.BytesIO(r.stdout)).convert("RGBA")
                mask = Image.new("L", (w * 4, h * 4), 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1),
                                                       radius=max(4, w // 21) * 4, fill=255)
                im.putalpha(mask.resize((w, h), Image.LANCZOS))
                out = io.BytesIO()
                im.save(out, "PNG")
                return out.getvalue()
            except Exception:
                return r.stdout
    return None


def safe_name(text: str, limit: int = 80) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    t = "".join("_" if c in bad else c for c in (text or "")).strip(" .")
    return (t[:limit].rstrip(" .") or "frame")


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


def clamp_lines(text: str, font, width: int, n: int = 2) -> str:
    """Wrap text to width pixels (word-wise; CJK per character) and keep at most n lines, ending in '…'."""
    import re
    lines, cur = [], ""
    for tok in re.findall(r"[　-鿿가-힯＀-￯]|[^\s　-鿿가-힯＀-￯]+\s*|\s+",
                          text or ""):
        if font.measure(cur + tok) <= width:
            cur += tok
            continue
        if cur.strip():
            lines.append(cur.rstrip())
        cur = ""
        while font.measure(tok.rstrip()) > width and len(tok) > 1:       # a token wider than the line
            k = len(tok)
            while k > 1 and font.measure(tok[:k]) > width:
                k -= 1
            lines.append(tok[:k])
            tok = tok[k:]
        cur = tok.lstrip()
        if len(lines) > n:
            break
    if cur.strip():
        lines.append(cur.rstrip())
    if len(lines) > n:
        last = lines[n - 1]
        while last and font.measure(last + "…") > width:
            last = last[:-1]
        lines = lines[:n - 1] + [last.rstrip() + "…"]
    return "\n".join(lines)


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
    home_btn = icon_button(top, "home", lambda: show_home(), size=40, bg=BG, hover=SURF2,
                           tip="Home: recommended videos")
    home_btn.pack(side="left", padx=(px(12), 0))
    logo = icon_label(top, "logo", px(38), RED, BG)
    logo.pack(side="left", padx=(px(6), px(4)))
    app_lbl = tk.Label(top, text=APP_NAME, bg=BG, fg=TXT, font=F(15, True))
    app_lbl.pack(side="left")
    for w_ in (logo, app_lbl):                                  # like youtube.com: the logo goes home
        w_.configure(cursor="hand2")
        w_.bind("<Button-1>", lambda e: show_home())

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
    open_btn = icon_button(sbox, "open", lambda: open_file(), size=40, bg=BG, hover=SURF2,
                           tip="Play a video or audio file from your computer (Ctrl+O)")
    open_btn.pack(side="left", padx=(px(10), 0))

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
    icon_button(bar, "back", lambda: rel_seek(-10), tip="Back 10 s   (Left: 5 s, or 1 frame when paused)", **CB).pack(side="left")
    icon_button(bar, "fwd", lambda: rel_seek(10), tip="Forward 10 s   (Right: 5 s, or 1 frame when paused)", **CB).pack(side="left")
    icon_button(bar, "frame_back", lambda: cmd("frame_back_step"), tip="Previous frame (,)", **CB).pack(side="left")
    icon_button(bar, "frame_fwd", lambda: cmd("frame_step"), tip="Next frame (.)", **CB).pack(side="left")
    frames_btn = icon_button(bar, "film", lambda: toggle_frames(), tip="Frame-by-frame mode (E)", **CB)
    frames_btn.pack(side="left")
    shot_btn = icon_button(bar, "camera", lambda: take_screenshot(), tip="Screenshot of the current frame (S)", **CB)
    shot_btn.pack(side="left")
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
    cache_chip = pill_button(bar, "Cache", lambda e: toggle_cache(), bg="#1f1f1f", hover="#333333",
                             parent_bg="black", height=30, font_size=9,
                             tip="Cache ahead: keep streaming the video ahead of playback (click to toggle)")
    cache_chip.pack(side="right", padx=px(4))
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
    status_lbl = tk.Label(col, textvariable=status_var, bg=BG, fg=SUB, font=F(9), anchor="w", justify="left",
                          wraplength=px(900))
    status_lbl.pack(fill="x", pady=(px(8), 0))

    # ---------------- home: a youtube.com-style grid of recommended videos (in place of the player)
    home = tk.Frame(col, bg=BG)
    hh = tk.Frame(home, bg=BG)
    hh.pack(fill="x", pady=(px(2), px(12)))
    tk.Label(hh, text="Home", bg=BG, fg=TXT, font=F(16, True)).pack(side="left")
    home_sub = tk.StringVar(value="")
    home_sub_lbl = tk.Label(hh, textvariable=home_sub, bg=BG, fg=SUB, font=F(9), anchor="w")
    home_sub_lbl.pack(side="left", fill="x", expand=True, padx=(px(16), px(8)))
    home_sub.trace_add("write", lambda *_: home_sub_lbl.configure(font=F(9, False, home_sub.get())))
    back_chip = pill_button(hh, "Back to video", lambda e: hide_home(), icon="play", parent_bg=BG, height=32,
                            font_size=9, tip="Return to the video you were watching")
    pill_button(hh, "Refresh", lambda e: load_home(force=True), icon="refresh", parent_bg=BG, height=32,
                font_size=9, bold=False, tip="Get new recommendations").pack(side="right", padx=(px(8), 0))
    hc = tk.Canvas(home, bg=BG, highlightthickness=0)
    hc.pack(fill="both", expand=True)
    hin = tk.Frame(hc, bg=BG)
    hin_win = hc.create_window(0, 0, window=hin, anchor="nw")
    hin.bind("<Configure>", lambda e: hc.configure(scrollregion=hc.bbox("all")))

    # ---------------- results sidebar
    side_head = tk.Frame(side, bg=BG)
    side_head.pack(fill="x")
    res_title = tk.StringVar(value="Search results")       # label of the Results tab
    tabs = {}
    for key_, var_or_text in (("results", res_title), ("upnext", "Up next"), ("recent", "Recent")):
        tf = tk.Frame(side_head, bg=BG, cursor="hand2")
        tf.pack(side="left", padx=(0, px(16)))
        kw_ = {"textvariable": var_or_text} if isinstance(var_or_text, tk.StringVar) else {"text": var_or_text}
        tl = tk.Label(tf, bg=BG, fg=TXT, font=F(12, True), anchor="w", cursor="hand2", **kw_)
        tl.pack(anchor="w")
        bar_ = tk.Frame(tf, bg=BG, height=px(3))
        bar_.pack(fill="x", pady=(px(4), 0))
        for w_ in (tf, tl):
            w_.bind("<Button-1>", lambda e, k=key_: show_tab(k))
        tabs[key_] = (tl, bar_)
    clear_recent_lbl = tk.Label(side_head, text="Clear", bg=BG, fg=SUB, font=F(9), cursor="hand2")
    clear_recent_lbl.bind("<Button-1>", lambda e: clear_recent())
    Tooltip(clear_recent_lbl, "Forget the recently played list")
    tk.Frame(side, bg=LINE, height=1).pack(fill="x")
    search_status = tk.StringVar(value="Search above, or paste a YouTube link.")
    tk.Label(side, textvariable=search_status, bg=BG, fg=SUB, font=F(9), anchor="w", justify="left",
             wraplength=px(390)).pack(fill="x", pady=(px(6), px(8)))
    hist_box = tk.Frame(side, bg=BG)                  # recent searches (Results tab)
    hist_box.pack(fill="x")
    lst = tk.Canvas(side, bg=BG, highlightthickness=0)
    lst.pack(fill="both", expand=True)
    inner = tk.Frame(lst, bg=BG)
    lst_win = lst.create_window(0, 0, window=inner, anchor="nw")
    inner.bind("<Configure>", lambda e: lst.configure(scrollregion=lst.bbox("all")))
    lst.bind("<Configure>", lambda e: lst.itemconfigure(lst_win, width=e.width))

    def wheel(e):
        if e.widget.winfo_toplevel() is not root:
            return
        for cv_ in (lst, hc):
            if str(e.widget) == str(cv_) or str(e.widget).startswith(str(cv_) + "."):
                cv_.yview_scroll(int(-e.delta / 120) if e.delta else (-3 if e.num == 4 else 3), "units")
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
    cache_on = [bool(st.get("cache_ahead", True))]
    proxy: list[StreamProxy | None] = [None]
    buffered: list[tuple[float, float]] = []      # seekable (already downloaded) ranges, for the seek bar

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
                            hwdec="auto-safe", volume=vol_val[0], log_handler=mpv_log, loglevel="warn")
        player.register_event_callback(mpv_event)
        try:                                    # big read-ahead goes to a temp file (deleted by mpv on close)
            os.makedirs(CACHE_DIR, exist_ok=True)
            for opt in ("demuxer-cache-dir", "cache-dir"):          # renamed in newer mpv
                try:
                    player[opt] = CACHE_DIR
                    break
                except Exception:
                    pass
            player["cache-on-disk"] = "yes"
        except Exception:
            pass
        apply_cache()

    def apply_cache():
        """Push the current cache profile to mpv; the demuxer picks the new limits up while playing."""
        for k_, v_ in CACHE_PROFILES[cache_on[0]].items():
            setp(k_, v_)
        cache_chip.colors = ("#263850", "#30486a", "#cfe3ff") if cache_on[0] else ("#1f1f1f", "#333333", SUB)
        update_cache_chip(None, force=True)

    def update_cache_chip(ahead, force=False):
        if not cache_on[0]:
            txt = "Cache off"
        elif ahead is None:
            txt = "Cache ahead"
        elif state["dur"] and (state["pos"] or 0) + ahead >= state["dur"] - 0.5:
            txt = "Cached all"
        else:
            txt = "Cached +" + fmt_time(int(ahead))
        if txt != cache_chip.text or force:
            cache_chip.text = txt
            cache_chip.redraw()

    def toggle_cache():
        cache_on[0] = not cache_on[0]
        apply_cache()
        status_var.set("Cache ahead ON: the video keeps downloading ahead of playback" if cache_on[0]
                       else "Cache ahead OFF: only a short window is buffered")
        persist()

    def cache_ranges():
        """-> (seconds buffered ahead of the play head or None, [(start, end), ...] seekable ranges)."""
        cs = getp("demuxer_cache_state")
        if not isinstance(cs, dict):
            return None, []
        rng = []
        for r_ in cs.get("seekable-ranges") or []:
            try:
                rng.append((float(r_["start"]), float(r_["end"])))
            except Exception:
                pass
        ahead = cs.get("cache-duration")
        return (float(ahead) if isinstance(ahead, (int, float)) else None), rng

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
                      "loop": None, "sel": None, "ready": False, "meta": meta_info or {}, "dur_saved": False})
        buffered.clear()
        try:
            player.pause = True             # old video frozen; I/O are refused until the new one has loaded
            player["ab-loop-a"] = "no"
            player["ab-loop-b"] = "no"
        except Exception:
            pass
        title_var.set("Loading...")
        in_var.set("IN " + fmt_time(state["in"]) if state["in"] is not None else "")
        if meta_info:
            sub_var.set("  •  ".join(x for x in (meta_info.get("channel"),
                                                 fmt_views(meta_info.get("views")) or meta_info.get("info")) if x))
        else:
            sub_var.set("")
        refresh_clips()
        load_gen[0] += 1
        gen = load_gen[0]
        if state.get("home"):
            hide_home()
        highlight_result()
        load_upnext(url)
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
            if cache_on[0] and url.lower().startswith(("http://", "https://")):
                r = dict(r)                     # fetch through the fast chunked proxy (see StreamProxy)
                try:
                    if proxy[0] is None:
                        proxy[0] = StreamProxy()
                    hdrs = r.get("headers") or {}
                    r["video"] = proxy[0].wrap(r["video"], hdrs)
                    if r.get("audio"):
                        r["audio"] = proxy[0].wrap(r["audio"], hdrs)
                except Exception as ex_:  # noqa: BLE001
                    status_var.set(f"cache proxy unavailable ({ex_}) - streaming directly")
            player.audio_files = [r["audio"]] if r.get("audio") else []
            player.http_header_fields = [f"{k}: {v}" for k, v in (r.get("headers") or {}).items()
                                         if k.lower() in ("user-agent", "referer", "origin", "accept-language")]
            player.force_media_title = r.get("title") or ""
            player.play(r["video"])
            player.pause = False
            state["title"] = r.get("title") or ""
            title_var.set(state["title"])
            root.title(f"{state['title']} - {APP_NAME}")
            add_recent(url, state["title"])
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
        if state.get("home") and not state["fs"]:
            return
        state["fs"] = not state["fs"]
        if state["fs"]:
            for w in (top, meta, card, status_lbl):
                w.pack_forget()
            side.grid_remove()
            body.columnconfigure(1, minsize=0)       # the hidden sidebar must not keep its column width
            body.grid_columnconfigure(0, weight=1)
            col.grid_configure(columnspan=2)
            body.pack_configure(padx=0, pady=0)
            root.configure(bg="black")
            body.configure(bg="black")
            col.configure(bg="black")
            root.attributes("-fullscreen", True)
        else:
            root.attributes("-fullscreen", False)
            root.configure(bg=BG)
            body.configure(bg=BG)
            col.configure(bg=BG)
            col.grid_configure(columnspan=1)
            body.columnconfigure(1, minsize=px(410))
            body.pack_forget()
            top.pack(fill="x")
            body.pack(fill="both", expand=True, padx=(px(24), px(12)), pady=(px(4), px(12)))
            side.grid()
            meta.pack(fill="x", pady=(px(12), 0))
            card.pack(fill="x", pady=(px(12), 0))
            status_lbl.pack(fill="x", pady=(px(8), 0))
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
            tk.Label(row, text=f"{fmt_precise(a)}  –  {fmt_precise(b)}", bg=rbg, fg=TXT, font=F(10)).pack(side="left",
                                                                                              padx=px(8))
            tk.Label(row, text=(f"{b - a:.1f} s" if abs((b - a) * 10 - round((b - a) * 10)) < 1e-6
                               else f"{b - a:.3f} s"), bg=rbg, fg=SUB, font=F(9)).pack(side="left", padx=px(12))
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
        in_var.set("IN " + (fmt_precise(t) if state.get("frames") else fmt_time(t)))
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
        if b - a < (0.03 if state.get("frames") else 0.2):
            status_var.set("Clip too short.")
            return
        nd = 3 if state.get("frames") else 1          # frame mode keeps the exact frame time
        a, b = round(a, nd), round(b, nd)
        clips().append((a, b))
        clips().sort()
        state["in"] = None
        state["sel"] = clips().index((a, b))
        ex["whole"] = False
        in_var.set("")
        refresh_clips()
        persist()
        status_var.set(f"Clip added: {fmt_precise(a)} - {fmt_precise(b)}")

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
        timeline = [(None, fmt_precise(a), fmt_precise(b)) for a, b in cl] or None
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
        for f in files[-6:]:
            add_output_row(f)
        status_var.set("Saved to " + ex["out_dir"])

    def add_output_row(f, kind="file"):
        r = tk.Frame(outs, bg=SURF)
        r.pack(fill="x", pady=1)
        icon_label(r, "camera" if kind == "shot" else "check", px(16), CLIP, SURF).pack(side="left",
                                                                                     padx=(px(2), 0))
        name = os.path.basename(f)
        lb = tk.Label(r, text=name, bg=SURF, fg=TXT, font=F(9, False, name), anchor="w", cursor="hand2")
        lb.pack(side="left", padx=px(6))
        if kind == "shot":
            Tooltip(lb, "Click to open the picture")
            lb.bind("<Button-1>", lambda e, f=f: open_path(f))
        else:
            Tooltip(lb, "Click to preview in the player")
            lb.bind("<Button-1>", lambda e, f=f: play(f))
        icon_button(r, "folder", lambda f=f: show_in_folder(f), size=28, bg=SURF, hover=SURF2,
                    tip="Show in folder").pack(side="left")
        kids = outs.winfo_children()
        for w in kids[:-6]:                          # keep the list short
            w.destroy()

    def open_path(f):
        try:
            if os.name == "nt":
                os.startfile(f)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", f])
        except Exception as ex_:  # noqa: BLE001
            status_var.set(str(ex_))

    def take_screenshot():
        """Save the frame on screen at the video's own resolution (PNG), into <folder>/Screenshots."""
        if not player or not state.get("ready") or not getp("duration"):
            status_var.set("Play a video first, then pause on the frame you want and press S.")
            return
        if not getp("pause"):
            setp("pause", True)                      # freeze on this frame, so what you see is what you get
        t = getp("time_pos") or 0.0
        folder = os.path.join(ex["out_dir"], "Screenshots")
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as ex_:
            status_var.set(f"Cannot create {folder}: {ex_}")
            return
        stamp = fmt_precise(t).replace(":", "-")
        name_ = state["title"] or os.path.basename(state["url"])
        if not state["url"].lower().startswith(("http://", "https://")):
            name_ = os.path.splitext(name_)[0]                 # "clip.mp4" -> "clip"
        base = safe_name(name_)
        path = os.path.join(folder, f"{base} [{stamp}].png")
        n = 2
        while os.path.exists(path):
            path = os.path.join(folder, f"{base} [{stamp}] ({n}).png")
            n += 1
        try:
            player.command("screenshot-to-file", path, "video")
        except Exception as ex_:  # noqa: BLE001
            status_var.set("Screenshot failed: " + str(ex_))
            return
        fr = getp("estimated_frame_number")
        w_, h_ = getp("width"), getp("height")
        add_output_row(path, "shot")
        status_var.set(f"Screenshot saved ({w_}x{h_}" + (f", frame {fr}" if fr is not None else "") + f"): {path}")

    def open_file():
        from tkinter import filedialog
        exts = " ".join(f"*{e}" for e in sorted(yc.MEDIA_EXT)) if yc else "*.mp4 *.mkv *.webm *.mov *.mp3 *.m4a *.wav"
        start = st.get("last_dir") or os.path.expanduser("~")
        f = filedialog.askopenfilename(title="Play a file", initialdir=start if os.path.isdir(start) else None,
                                       filetypes=[("Video / audio", exts), ("All files", "*.*")])
        if f:
            st["last_dir"] = os.path.dirname(f)
            query_var.set(f)
            play(os.path.normpath(f))

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
        for a, b in buffered:                                    # already downloaded: lighter grey
            if b > a:
                seek.create_rectangle(x_of(max(0.0, a), w), y0, x_of(min(d, b), w), y1, fill="#8a8a8a", width=0)
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
        view["tab"] = "results"
        add_search(text)
        paint_tabs()
        render_history()
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

    view = {"tab": "results", "search": None}          # search = (text, err) of the last search
    recent: list[dict] = [r_ for r_ in (st.get("recent") or []) if isinstance(r_, dict) and r_.get("url")]
    searches: list[str] = [q_ for q_ in (st.get("searches") or []) if isinstance(q_, str) and q_.strip()]

    def add_search(text):
        searches[:] = [text] + [q_ for q_ in searches if q_.lower() != text.lower()][:14]

    def remove_search(text):
        searches[:] = [q_ for q_ in searches if q_ != text]
        persist()
        render_history()

    def run_search(text):
        query_var.set(text)
        go_search()

    def render_history():
        """Recent searches as chips (click = search again, right-click = remove)."""
        for w in hist_box.winfo_children():
            w.destroy()
        if view["tab"] != "results" or not searches:
            hist_box.pack_forget()                 # an emptied frame would keep its old height
            return
        hist_box.pack(fill="x", pady=(0, px(8)), before=lst)
        head = tk.Frame(hist_box, bg=BG)
        head.pack(fill="x", pady=(0, px(4)))
        tk.Label(head, text="Recent searches", bg=BG, fg=SUB, font=F(9)).pack(side="left")
        cl_ = tk.Label(head, text="Clear", bg=BG, fg=SUB, font=F(9), cursor="hand2")
        cl_.pack(side="right")
        cl_.bind("<Button-1>", lambda e: (searches.clear(), persist(), render_history()))
        width = max(side.winfo_width(), px(380))
        h_ = px(28)
        line, used = None, width
        for q_ in searches[:10]:
            label = q_ if len(q_) <= 28 else q_[:27] + "…"
            w_ = int(F(9).measure(label) + round(h_ * 0.5) + h_ * 0.95 + px(8)) + px(6)   # pill width + gap
            if line is None or used + w_ > width:
                line = tk.Frame(hist_box, bg=BG)
                line.pack(fill="x", pady=px(2))
                used = 0
            chip = pill_button(line, label, lambda e, t=q_: run_search(t), icon="search", parent_bg=BG,
                               height=28, font_size=9, bold=False,
                               tip=f"Search '{q_}' again  (right-click: remove)")
            chip.pack(side="left", padx=(0, px(6)))
            chip.bind("<Button-3>", lambda e, t=q_: remove_search(t))
            used += w_
    thumb_png: dict[str, bytes] = {}                      # video id -> PNG, so switching tabs doesn't refetch

    def show_results(gen, text, res, err):
        if gen != search_gen[0]:
            return
        results.clear()
        results.extend(res)
        view.update(tab="results", search=(text, err))
        res_title.set("Search results")
        render_list()

    def paint_tabs():
        for k_, (tl, bar_) in tabs.items():
            on = view["tab"] == k_
            tl.configure(fg=TXT if on else SUB)
            bar_.configure(bg=TXT if on else BG)
        if view["tab"] == "recent" and recent:
            clear_recent_lbl.pack(side="right", pady=(0, px(6)))
        else:
            clear_recent_lbl.pack_forget()

    def show_tab(k):
        view["tab"] = k
        render_list()

    def render_list():
        """Fill the sidebar with the search results or the recently played videos."""
        search_gen[0] += 1
        gen = search_gen[0]
        thumbs.clear()
        for w in inner.winfo_children():
            w.destroy()
        rows.clear()
        lst.yview_moveto(0)
        paint_tabs()
        render_history()
        if view["tab"] == "results":
            items = list(results)
            if view["search"] is None:
                search_status.set("Search above, or paste a YouTube link.")
            else:
                text, err = view["search"]
                note = yc.COOKIE_NOTE["text"] if yc else ""
                search_status.set(err if err else f"{len(items)} videos for '{text}' - click one to play"
                                  + ("   (signed out: browser cookies unreadable - use Sign in at the top right)"
                                     if note else ""))
        elif view["tab"] == "upnext":
            items = list(upnext["items"])
            search_status.set(upnext["status"])
        else:
            items = list(recent)
            search_status.set(f"{len(items)} recently played - click one to play it again" if items
                              else "Videos you play show up here.")
        for i, r in enumerate(items):
            build_row(i, r, recent_row=view["tab"] == "recent")
        threading.Thread(target=load_thumbs, args=(gen, list(enumerate(items))), daemon=True).start()
        highlight_result()

    def add_recent(url, title):
        """Remember a video that just started playing (newest first, one entry per video)."""
        key = video_key(url)
        old = next((r_ for r_ in recent if video_key(r_["url"]) == key), {})
        meta = state.get("meta") or {}
        vid = key[3:] if key.startswith("yt:") else None
        local = not url.lower().startswith(("http://", "https://"))
        entry = {"url": url, "id": vid, "title": title or old.get("title") or os.path.basename(url),
                 "channel": meta.get("channel") or old.get("channel")
                 or (f"Local file  ·  {os.path.basename(os.path.dirname(url)) or os.path.dirname(url)}" if local else ""),
                 "duration": meta.get("duration") or old.get("duration") or "",
                 "views": meta.get("views") or old.get("views") or 0, "t": time.time()}
        recent[:] = [entry] + [r_ for r_ in recent if video_key(r_["url"]) != key][:59]
        persist()
        if view["tab"] == "recent":
            render_list()

    def recent_set_duration(dur):
        key = video_key(state["url"])
        for r_ in recent:
            if video_key(r_["url"]) == key and not r_.get("duration") and dur:
                r_["duration"] = fmt_time(dur).split(".")[0]
                persist()
                return

    def remove_recent(url):
        recent[:] = [r_ for r_ in recent if video_key(r_["url"]) != video_key(url)]
        persist()
        render_list()

    def clear_recent():
        if recent and messagebox.askyesno(APP_NAME, "Clear the recently played list?\n\n(Your clips are kept.)"):
            recent.clear()
            persist()
            render_list()

    TW, TH = px(168), px(94)
    rows: list = []

    def build_row(i, r, recent_row=False):
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
        txt.pack(side="left", fill="both", expand=True, padx=(px(10), px(26) if recent_row else 0))
        t = tk.Label(txt, text=r["title"], bg=BG, fg=TXT, font=F(10, True, r["title"]), anchor="nw",
                     justify="left", wraplength=px(215))
        t.pack(fill="x")
        # wrap titles to the space the sidebar really has (a fixed wraplength cut long titles off)
        txt.bind("<Configure>", lambda e, t=t: t.configure(wraplength=max(px(80), e.width - px(4)))
                 if abs(int(t.cget("wraplength")) - (e.width - px(4))) > 2 else None)
        ch_ = r.get("channel") or ""
        c = tk.Label(txt, text=ch_, bg=BG, fg=SUB, font=F(9, False, ch_), anchor="w")
        c.pack(fill="x", pady=(px(4), 0))
        if recent_row:
            n_clips = len(clips_by_url.get(video_key(r["url"])) or [])
            bits = [f"{n_clips} clip{'s' if n_clips != 1 else ''}" if n_clips else "", fmt_ago(r.get("t"))]
            info = "  •  ".join(b_ for b_ in bits if b_)
        else:
            info = fmt_views(r.get("views")) or r.get("info") or ""
        v = tk.Label(txt, text=info, bg=BG, fg=CLIP if recent_row and "clip" in info else SUB, font=F(9), anchor="w")
        v.pack(fill="x")
        widgets = (row, th, txt, t, c, v)
        if recent_row:
            rm = icon_button(row, "close", lambda u=r["url"]: remove_recent(u), size=26, bg=BG, fg=SUB,
                             hover="#3a3a3a", tip="Remove from Recent", icon_size=12)
            rm.place(relx=1.0, x=-px(2), y=px(2), anchor="ne")

        def paint(bg):
            for w in (row, txt, t, c, v, th):
                w.configure(bg=bg)
            if recent_row:
                rm.configure(bg=bg)
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
            if gen != search_gen[0]:
                return
            key_ = r.get("id") or r.get("url")
            if not key_:
                return
            png = thumb_png.get(key_)
            if not png:
                png = fetch_thumb_png(r["id"], TW, TH) if r.get("id") else local_thumb_png(r["url"], TW, TH)
            if png:
                thumb_png[key_] = png
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

    # =========================================================== suggestions: Up next + Home
    upnext = {"items": [], "status": "Videos like the one you're watching show up here.", "for": None, "gen": 0}

    def load_upnext(url):
        """Fill the Up next tab with the watch page's related videos (YouTube videos only)."""
        key = video_key(url)
        if not key.startswith("yt:") or key == upnext["for"]:
            return
        upnext.update(items=[], status="Finding videos like this one...", **{"for": key})
        upnext["gen"] += 1
        gen, ck = upnext["gen"], cookie_settings()
        if view["tab"] == "upnext":
            render_list()

        def work():
            try:
                items, err = youtube_feed("related", ck, key[3:]), None
            except Exception as ex:  # noqa: BLE001
                items, err = [], str(ex).replace("ERROR: ", "")[-200:]

            def done():
                if gen != upnext["gen"]:
                    return
                upnext["items"] = items
                upnext["status"] = (f"Could not get suggestions: {err}" if err else
                                    f"{len(items)} videos like this one - click one to play" if items
                                    else "YouTube had no suggestions for this video.")
                if view["tab"] == "upnext":
                    render_list()
            q.put(("call", done))
        threading.Thread(target=work, daemon=True).start()

    GW, GGAP = px(300), px(16)
    GH = GW * 9 // 16
    hv = {"items": [], "cards": [], "cols": 0, "gen": 0, "loaded": False, "imgs": {}}
    home_png: dict[str, bytes] = {}

    def show_home():
        if state["fs"]:
            return
        if not state.get("home"):
            state["home"] = True
            for w in (pbox, meta, card):
                w.pack_forget()
            home.pack(fill="both", expand=True, before=status_lbl)
            if state["url"] and not getp("pause"):
                setp("pause", True)
                state["home_paused"] = True
        if state["url"]:
            back_chip.pack(side="right", padx=(px(8), 0))
        else:
            back_chip.pack_forget()
        root.title(APP_NAME)
        load_home()

    def hide_home():
        if not state.get("home"):
            return
        state["home"] = False
        home.pack_forget()
        pbox.pack(fill="both", expand=True, before=status_lbl)
        meta.pack(fill="x", pady=(px(12), 0), before=status_lbl)
        card.pack(fill="x", pady=(px(12), 0), before=status_lbl)
        if state["title"]:
            root.title(f"{state['title']} - {APP_NAME}")
        if state.pop("home_paused", False):
            setp("pause", False)

    def load_home(force=False):
        if hv["loaded"] and not force:
            return
        hv["loaded"] = True
        hv["gen"] += 1
        gen, ck = hv["gen"], cookie_settings()
        home_sub.set("Loading your recommendations...")
        last = next((r_ for r_ in recent if video_key(r_["url"]).startswith("yt:")), None)

        def work():
            note, err, items = "", None, []
            try:
                items = youtube_feed("home", ck)
                if len(items) < 4 and last:           # signed out: YouTube's home page is empty
                    items = youtube_feed("related", ck, video_key(last["url"])[3:])
                    note = f"Videos like “{last.get('title') or 'your last video'}”"
                elif len(items) >= 4:
                    note = "Recommended for you" if any(ck.values()) else "Recommended"
            except Exception as ex:  # noqa: BLE001
                err = str(ex).replace("ERROR: ", "")[-200:]

            def done():
                if gen != hv["gen"]:
                    return
                if err:
                    hv["loaded"] = False                  # try again next time
                    home_sub.set("Could not load recommendations: " + err)
                elif not items:
                    home_sub.set("Sign in (top right) to see your YouTube recommendations - or search above.")
                else:
                    home_sub.set(note + ("   ·   sign in (top right) for your own recommendations"
                                         if not any(ck.values()) else ""))
                render_home(items)
            q.put(("call", done))
        threading.Thread(target=work, daemon=True).start()

    def render_home(items):
        for w in hin.winfo_children():
            w.destroy()
        hv["items"], hv["cards"], hv["cols"], hv["imgs"] = list(items), [], 0, {}
        hc.yview_moveto(0)
        for r in items:
            hv["cards"].append(build_card(r))
        layout_home()
        gen = hv["gen"]
        threading.Thread(target=load_home_thumbs, args=(gen, list(enumerate(items))), daemon=True).start()

    def build_card(r):
        cd = tk.Frame(hin, bg=BG, cursor="hand2")
        th = tk.Canvas(cd, width=GW, height=GH, bg=BG, highlightthickness=0)
        th.pack(anchor="w")
        ph = aa_image(("thumbph", GW, GH), GW, GH, lambda k: _rrect(k, 0, 0, GW, GH, px(12), fill="#262626"))
        if ph:
            th.create_image(0, 0, image=ph, anchor="nw")
        else:
            th.create_rectangle(0, 0, GW, GH, fill="#262626", width=0)
        if r.get("duration"):
            f = F(9, True)
            tw, bh, m = f.measure(r["duration"]) + px(10), px(19), px(7)
            bcol = "#cc0000" if r["duration"] == "LIVE" else "#000000"
            badge = aa_image(("badge", tw, bh, bcol), tw, bh, lambda k: _rrect(k, 0, 0, tw, bh, px(4), fill=bcol))
            if badge:
                th.create_image(GW - m - tw, GH - m - bh, image=badge, anchor="nw", tags="badge")
            else:
                th.create_rectangle(GW - m - tw, GH - m - bh, GW - m, GH - m, fill=bcol, width=0, tags="badge")
            th.create_text(GW - m - tw / 2, GH - m - bh / 2, text=r["duration"], fill="white", font=f, tags="badge")
        tf_ = F(11, True, r["title"])
        t = tk.Label(cd, text=clamp_lines(r["title"], tf_, GW - px(8)), bg=BG, fg=TXT, font=tf_,
                     anchor="nw", justify="left")
        t.pack(fill="x", pady=(px(10), 0))
        ch_ = r.get("channel") or ""
        c = tk.Label(cd, text=ch_, bg=BG, fg=SUB, font=F(10, False, ch_), anchor="w")
        c.pack(fill="x", pady=(px(4), 0))
        v = tk.Label(cd, text=r.get("info") or "", bg=BG, fg=SUB, font=F(10), anchor="w")
        v.pack(fill="x")
        ws = (cd, th, t, c, v)

        def paint(bg):
            for w in ws:
                w.configure(bg=bg)
        for w in ws:
            w.bind("<Enter>", lambda e: paint(SURF))
            w.bind("<Leave>", lambda e: paint(BG))
            w.bind("<Button-1>", lambda e, r=r: open_suggested(r))
        Tooltip(t, r["title"])
        cd.thumb = th
        return cd

    def layout_home(width=None):
        width = width or hc.winfo_width()
        cols = max(1, (width + GGAP) // (GW + GGAP))
        if cols == hv["cols"] and hv["cards"] and hv["cards"][0].winfo_manager():
            return
        hv["cols"] = cols
        for i, cd in enumerate(hv["cards"]):
            cd.grid(row=i // cols, column=i % cols, sticky="nw", padx=(0, GGAP if i % cols < cols - 1 else 0),
                    pady=(0, px(22)))

    def hc_configure(e):
        used = min(e.width, max(1, (e.width + GGAP) // (GW + GGAP)) * (GW + GGAP) - GGAP)
        hc.coords(hin_win, max(0, (e.width - used) // 2), 0)       # centre the grid
        layout_home(e.width)
    hc.bind("<Configure>", hc_configure)

    def open_suggested(r):
        view["tab"] = "upnext"                       # like YouTube: the watch page lists what's next
        state.pop("home_paused", None)
        play(r["url"], meta_info=r)
        render_list()

    def load_home_thumbs(gen, items):
        from concurrent.futures import ThreadPoolExecutor

        def one(it):
            i, r = it
            if gen != hv["gen"]:
                return
            png = home_png.get(r["id"]) or fetch_thumb_png(r["id"], GW, GH)
            if png:
                home_png[r["id"]] = png
                q.put(("call", lambda: put_home_thumb(gen, i, png)))
        with ThreadPoolExecutor(6) as ex_:
            list(ex_.map(one, items))

    def put_home_thumb(gen, i, png):
        if gen != hv["gen"] or i >= len(hv["cards"]):
            return
        th = hv["cards"][i].thumb
        try:
            img = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
        except tk.TclError:
            return
        hv["imgs"][i] = img
        th.delete("thumbimg")
        th.create_image(0, 0, image=img, anchor="nw", tags="thumbimg")
        th.tag_raise("badge")

    q_entry.bind("<Return>", go_search)

    # =========================================================== keys (not while typing)
    def arrow(direction: int, shift: bool):
        """Frame mode: one frame (Shift: 10 frames). Otherwise: paused -> one frame, playing -> 5 s
        (Shift: 1 s)."""
        if not (player and getp("duration")):
            return
        if state.get("frames"):
            if not getp("pause"):
                setp("pause", True)
            if shift:
                fps = getp("container_fps") or getp("estimated_vf_fps") or 30.0
                try:
                    player.seek(direction * 10.0 / float(fps), reference="relative", precision="exact")
                except Exception:
                    pass
            else:
                cmd("frame_step" if direction > 0 else "frame_back_step")
        elif not shift and getp("pause"):
            cmd("frame_step" if direction > 0 else "frame_back_step")
        else:
            rel_seek(direction * (1 if shift else 5))

    def toggle_frames():
        state["frames"] = not state.get("frames")
        frames_btn.set_active(state["frames"])
        if state["frames"]:
            setp("pause", True)
            status_var.set("Frame-by-frame mode: ← → one frame, Shift+← → 10 frames, I / O mark on the exact frame"
                           "  (E to leave)")
        else:
            status_var.set("Frame-by-frame mode off")
        refresh_clips()

    def key(e):
        if isinstance(e.widget, tk.Entry):
            return
        k, shift = e.keysym.lower(), bool(e.state & 0x1)
        acts = {"space": toggle, "k": toggle, "m": toggle_mute, "comma": lambda: cmd("frame_back_step"),
                "period": lambda: cmd("frame_step"), "i": mark_in, "o": mark_out, "l": loop_toggle,
                "delete": del_clip, "f": toggle_fullscreen, "e": toggle_frames, "s": take_screenshot,
                "left": lambda: arrow(-1, shift), "right": lambda: arrow(1, shift),
                "j": lambda: rel_seek(-10)}
        if k == "escape" and state["fs"]:
            toggle_fullscreen()
            return "break"
        if k in acts:
            acts[k]()
            return "break"
    root.bind_all("<KeyPress>", key)
    root.bind_all("<Control-o>", lambda e: (open_file(), "break")[1])
    root.bind_all("<Control-O>", lambda e: (open_file(), "break")[1])
    video.bind("<Button-1>", lambda e: root.focus_set())
    video.bind("<Double-Button-1>", lambda e: toggle_fullscreen())

    # =========================================================== loop
    def persist():
        keep = {u: c for u, c in clips_by_url.items() if c}
        save_json(VIEWER_SETTINGS, {"geometry": root.geometry() if not state["fs"] else st.get("geometry"),
                                    "geometry_scale": UI["scale"],
                                    "query": query_var.get(), "quality": qual_var.get(),
                                    "cache_ahead": cache_on[0],
                                    "volume": vol_val[0], "clips": keep, "extract": ex, "recent": recent, "searches": searches,
                                    "last_dir": st.get("last_dir"),
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
            if state["dur"] and state.get("ready") and not state.get("dur_saved"):
                state["dur_saved"] = True
                recent_set_duration(state["dur"])
            buf = "   buffering..." if getp("paused_for_cache") else ""
            if tick_n[0] % 2 == 0:
                ahead, buffered[:] = cache_ranges()
                update_cache_chip(ahead if state.get("ready") else None)
            if state.get("frames"):
                fr = getp("estimated_frame_number")
                time_var.set(f"{fmt_precise(state['pos'] or 0)} / {fmt_time(state['dur'] or 0).split('.')[0]}"
                             + (f"   ·   frame {fr}" if fr is not None else "") + buf)
            else:
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
        apply_cache()                           # chip look only
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
    render_list()
    update_account()
    refresh_extract()
    refresh_clips()
    draw_vol()
    tick()
    if not initial_url:
        show_home()
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
