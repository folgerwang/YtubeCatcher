"""
YtubeCatcher - dump the audio from YouTube videos, with an optional BGM filter
that removes background music and keeps only the voice.

  GUI:  python ytubecatcher.py
  CLI:  python ytubecatcher.py URL_OR_FILE [...] [-f mp3] [-o out] [--start 1:00 --end 2:30]
                               [--filter-bgm] [--quality fast|best] [--keep-original]

Built on yt-dlp + ffmpeg. The BGM filter uses Demucs (optional, see install_vocals.bat).
Only download content you own or have permission to use.
"""
from __future__ import annotations

import argparse
import os

try:
    import numpy as np
except ImportError:      # only needed for the BGM filter
    np = None
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# less VRAM fragmentation when torch is used (must be set before torch is imported)
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

APP_NAME = "YtubeCatcher"
FORMATS = ["mp3", "m4a", "wav", "flac", "opus"]
BITRATES = ["320", "256", "192", "128"]
RESOLUTIONS = ["best", "2160", "1440", "1080", "720", "480", "360"]
DEFAULT_OUT = str(Path.home() / "Music" / APP_NAME)
# BGM filter engines. "best" = BS-RoFormer (audio-separator), far less music bleed than Demucs.
QUALITY_MODELS = {"best": "BS-RoFormer", "fast": "Demucs"}
ROFORMER_MODEL = "model_bs_roformer_ep_368_sdr_12.9628.ckpt"   # stem labels verified: (Vocals) == voice
DENOISE_MODEL = "denoise_mel_band_roformer_aufr33_aggr_sdr_27.9768.ckpt"   # 2nd stage: (dry) == clean voice
DEMUCS_MODEL = "htdemucs"
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
# GUI inputs are auto-saved here (next to the app, so they survive ytubecatcher.py updates)
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
# a deno.exe next to the app is found by yt-dlp (YouTube's JS challenges need a JS runtime)
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _APP_DIR + os.pathsep + os.environ.get("PATH", "")


def js_runtime_available() -> bool:
    """yt-dlp solves YouTube's JavaScript challenges with Deno (without it, signed-in / web-client
    requests often come back with no formats: 'Requested format is not available')."""
    return bool(shutil.which("deno"))


def install_deno(log=print) -> str:
    """Download the official Deno build (~40 MB) and put deno(.exe) next to this file."""
    import io
    import platform
    import urllib.request
    import zipfile
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    target = {"nt": f"{arch}-pc-windows-msvc"}.get(os.name) or \
        (f"{arch}-apple-darwin" if sys.platform == "darwin" else f"{arch}-unknown-linux-gnu")
    url = f"https://github.com/denoland/deno/releases/latest/download/deno-{target}.zip"
    log("downloading Deno (JavaScript runtime for yt-dlp, ~40 MB)...")
    req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    name = "deno.exe" if os.name == "nt" else "deno"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        member = next(n for n in z.namelist() if os.path.basename(n).lower() == name)
        dst = os.path.join(_APP_DIR, name)
        with z.open(member) as src, open(dst + ".part", "wb") as out:
            shutil.copyfileobj(src, out)
    os.replace(dst + ".part", dst)
    if os.name != "nt":
        os.chmod(dst, 0o755)
    return "Deno installed - YouTube's JS challenges can be solved now"


def is_no_formats_error(msg: str) -> bool:
    low = (msg or "").lower()
    return ("requested format is not available" in low or "no video formats found" in low
            or "only images are available" in low or "n challenge" in low)


def load_settings() -> dict:
    try:
        import json
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        import json
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        pass
VOICE_TAG = " (voice)"
MEDIA_EXT = {".mp3", ".m4a", ".wav", ".flac", ".opus", ".ogg", ".aac", ".wma",
             ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv"}


# --------------------------------------------------------------------------- helpers
def find_ffmpeg() -> str | None:
    """System ffmpeg if on PATH, else the one bundled by imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def parse_time(s: str | None) -> float | None:
    """'90', '1:30', '01:02:03.5' -> seconds. Empty -> None."""
    if not s or not str(s).strip():
        return None
    parts = [float(p) for p in str(s).strip().split(":")]
    if len(parts) > 3:
        raise ValueError(f"Bad time: {s}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs


def roformer_available() -> bool:
    try:
        import audio_separator  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def demucs_available() -> bool:
    try:
        import demucs  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def bgm_filter_available() -> bool:
    return roformer_available() or demucs_available()


def default_quality() -> str:
    return "best" if roformer_available() else "fast"


def _no_window() -> dict:
    # hide console windows spawned from the GUI on Windows
    if os.name == "nt":
        return {"creationflags": 0x08000000}
    return {}


def _codec_args(fmt: str, bitrate: str) -> list[str]:
    return {"mp3": ["-c:a", "libmp3lame", "-b:a", f"{bitrate}k"],
            "m4a": ["-c:a", "aac", "-b:a", f"{bitrate}k"],
            "opus": ["-c:a", "libopus", "-b:a", f"{bitrate}k"],
            "flac": ["-c:a", "flac"],
            "wav": ["-c:a", "pcm_s16le"]}[fmt]


def _trim_args(t0, t1) -> list[str]:
    return (["-ss", str(t0)] if t0 else []) + (["-to", str(t1)] if t1 is not None else [])


def _meta_args(info: dict | None, fmt: str) -> list[str]:
    meta = []
    if info and fmt != "wav":
        for key, field in (("title", "title"), ("artist", "uploader"), ("comment", "webpage_url")):
            if info.get(field):
                meta += ["-metadata", f"{key}={info[field]}"]
    return meta


def _run_ffmpeg(cmd: list[str], data: bytes | None = None) -> bytes:
    # stdin=DEVNULL: inside a GUI (pythonw / YtubeViewer) an inherited stdin makes ffmpeg misread input
    r = subprocess.run(cmd, input=data, capture_output=True,
                       **({} if data is not None else {"stdin": subprocess.DEVNULL}), **_no_window())
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr.decode(errors="replace")[-500:])
    return r.stdout


def _unique(path: str) -> str:
    """Never overwrite a source file: add ' (2)', ' (3)'... if needed."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


# --------------------------------------------------------------------------- conversion
def convert_audio(src: str, dst: str, fmt: str, bitrate: str, ffmpeg: str,
                  info: dict | None = None, t0=None, t1=None) -> str:
    """Transcode src to dst in the target format (trim + title/artist tags)."""
    _run_ffmpeg([ffmpeg, "-v", "error", "-y", "-i", src, *_trim_args(t0, t1), "-vn",
                 "-map_metadata", "-1", *_meta_args(info, fmt), *_codec_args(fmt, bitrate), dst])
    return dst


# --------------------------------------------------------------------------- BGM filter
_ENGINE_CACHE: dict = {}


class _DemucsEngine:
    """Demucs htdemucs: fast, but leaves audible music bleed."""
    name = "Demucs"

    def __init__(self, log):
        import torch
        from demucs.pretrained import get_model
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = get_model(DEMUCS_MODEL).eval()
        self.sr = self.model.samplerate
        self.voc_idx = self.model.sources.index("vocals")
        self.log = log

    def separate(self, x):
        """x: (2, n) float32 mix -> (2, n) float32 voice."""
        from demucs.apply import apply_model
        torch = self.torch
        wav = torch.from_numpy(x)
        ref = wav.mean(0)
        mean, std = ref.mean(), ref.std() + 1e-8
        mix = ((wav - mean) / std)[None]
        for attempt in range(2):
            try:
                with torch.inference_mode():
                    out = apply_model(self.model, mix, device=self.device, shifts=1, split=True,
                                      overlap=0.25, progress=False)[0, self.voc_idx]
                break
            except torch.cuda.OutOfMemoryError:
                if self.device == "cpu" or attempt:
                    raise
                self.log("   WARN: GPU out of memory - continuing on CPU (slower)")
                torch.cuda.empty_cache()
                self.device = "cpu"
                self.model.to("cpu")
        return (out * std + mean).clamp_(-1, 1).numpy()


class _RoformerEngine:
    """BS-RoFormer via audio-separator: state of the art voice/instrumental split."""
    name = "BS-RoFormer"
    model_file = ROFORMER_MODEL
    pick = "(vocals)"

    def __init__(self, log):
        import tempfile
        import torch
        self.torch = torch
        self.log = log
        self.sr = 44100
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.tmp = tempfile.mkdtemp(prefix="ytubecatcher_")
        self._build()

    def _build(self, small_segments=False):
        from audio_separator.separator import Separator
        params = {"segment_size": 128, "override_model_segment_size": True, "batch_size": 1,
                  "overlap": 8, "pitch_shift": 0} if small_segments else None
        self.sep = Separator(output_dir=self.tmp, model_file_dir=MODEL_DIR, output_format="WAV",
                             sample_rate=self.sr, normalization_threshold=1.0,
                             amplification_threshold=0.0,
                             log_level=40, **({"mdxc_params": params} if params else {}))
        self.sep.load_model(self.model_file)

    def separate(self, x):
        import soundfile as sf
        src = os.path.join(self.tmp, "block.wav")
        sf.write(src, x.T, self.sr, subtype="FLOAT")
        for attempt in range(2):
            try:
                outs = self.sep.separate(src)
                break
            except self.torch.cuda.OutOfMemoryError:
                if attempt:
                    raise
                self.log("   WARN: GPU out of memory - retrying with smaller segments")
                self.torch.cuda.empty_cache()
                self._build(small_segments=True)
        voc = [o for o in outs if self.pick in o.lower()] or outs
        path = os.path.join(self.tmp, voc[0])
        y, _ = sf.read(path, dtype="float32", always_2d=True)
        for o in outs:
            try:
                os.remove(os.path.join(self.tmp, o))
            except OSError:
                pass
        y = np.ascontiguousarray(y.T)
        n = x.shape[1]
        if y.shape[1] < n:                      # pad/trim to the exact block length
            y = np.pad(y, ((0, 0), (0, n - y.shape[1])))
        return np.clip(y[:, :n], -1, 1)


class _DenoiseStage(_RoformerEngine):
    """Mel-RoFormer denoise (aufr33, aggressive): removes ambience / noise left in a voice stem."""
    name = "Denoise"
    model_file = DENOISE_MODEL
    pick = "(dry)"


def _get_denoiser(log):
    if "denoise" not in _ENGINE_CACHE:
        if not roformer_available():
            raise RuntimeError("Extra clean-up needs audio-separator - run install_vocals.bat first.")
        log("   loading Denoise model (first run downloads it)...")
        _ENGINE_CACHE["denoise"] = _DenoiseStage(log)
    return _ENGINE_CACHE["denoise"]


def _get_engine(quality: str, log):
    """quality 'best' -> BS-RoFormer, 'fast' -> Demucs. Never downgrades silently."""
    want = "best" if quality == "best" else "fast"
    if want == "best" and not roformer_available():
        raise RuntimeError("Quality 'best' needs the BS-RoFormer engine (audio-separator), which is not "
                           "installed in this Python. Run install_vocals.bat and start the app with run.bat, "
                           "or choose quality 'fast' (Demucs - leaves more music).")
    if want == "fast" and not demucs_available():
        raise RuntimeError("Quality 'fast' needs Demucs, which is not installed in this Python. "
                           "Run install_vocals.bat and start the app with run.bat.")
    if want not in _ENGINE_CACHE:
        log(f"   loading {QUALITY_MODELS[want]} model (first run downloads it)...")
        _ENGINE_CACHE[want] = _RoformerEngine(log) if want == "best" else _DemucsEngine(log)
    return _ENGINE_CACHE[want]


def _media_duration(ffmpeg: str, src: str) -> float | None:
    """Duration in seconds parsed from ffmpeg's banner (no ffprobe needed)."""
    import re
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", src], capture_output=True, stdin=subprocess.DEVNULL, **_no_window())
    m = re.search(rb"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m:
        return None
    h, mi, se = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(se)


# Streaming block sizes (seconds). RAM use is bounded by BLOCK + 2*MARGIN of audio,
# whatever the length of the input. MARGIN gives the model context across block edges.
BGM_BLOCK_SEC = 60
BGM_MARGIN_SEC = 6


def filter_bgm(src: str, dst: str, fmt: str, bitrate: str, ffmpeg: str, quality: str = "best",
               info: dict | None = None, t0=None, t1=None,
               log=print, progress=lambda p, t: None, denoise: bool = False) -> str:
    """Remove background music (and other non-voice sound) from src; write voice only to dst.

    Uses Demucs source separation and keeps the 'vocals' stem. The audio is STREAMED:
    ffmpeg decodes into a pipe, we separate one ~60 s block at a time (with a few seconds
    of context on each side so block edges are seamless) and pipe the voice straight into
    an ffmpeg encoder. Peak memory therefore does not grow with the length of the input."""
    engine = _get_engine(quality, log)
    device = engine.device
    progress(0, f"Filtering BGM with {engine.name} on {device.upper()}...")
    log(f"   filtering BGM [{engine.name}, {device}]: {os.path.basename(src)}")
    sr = engine.sr
    frame = 2 * 4                                  # stereo float32
    block, margin = BGM_BLOCK_SEC * sr, BGM_MARGIN_SEC * sr

    total = _media_duration(ffmpeg, src)
    if total is not None:
        total = min(total, t1) if t1 is not None else total
        total = max(0.0, total - (t0 or 0))

    dec = subprocess.Popen([ffmpeg, "-v", "error", "-i", src, *_trim_args(t0, t1), "-vn",
                            "-f", "f32le", "-ac", "2", "-ar", str(sr), "-"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **_no_window())
    enc = subprocess.Popen([ffmpeg, "-v", "error", "-y", "-f", "f32le", "-ac", "2", "-ar", str(sr),
                            "-i", "-", *_meta_args(info, fmt), *_codec_args(fmt, bitrate), dst],
                           stdin=subprocess.PIPE, stderr=subprocess.PIPE, **_no_window())
    separate = engine.separate
    if denoise:
        dn = _get_denoiser(log)
        log("   + extra clean-up (denoise) stage")
        separate = lambda x, _e=engine.separate, _d=dn.separate: _d(_e(x))   # noqa: E731

    done_frames = 0
    buf = np.zeros((2, 0), dtype=np.float32)
    lead = 0                                     # context frames at the start of buf (0 for the first block)
    eof = False
    try:
        while not eof:
            need = (lead + block + margin) - buf.shape[1]
            raw = dec.stdout.read(need * frame) if need > 0 else b""
            if need > 0 and len(raw) < need * frame:
                eof = True
            if raw:
                buf = np.concatenate([buf, np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).T], axis=1)
            if buf.shape[1] <= lead:
                break                                # nothing left beyond the context
            if eof:
                voice = separate(np.ascontiguousarray(buf))[:, lead:]
                buf = buf[:, :0]
            else:
                voice = separate(np.ascontiguousarray(buf[:, :lead + block + margin]))[:, lead:lead + block]
                buf = buf[:, lead + block - margin:]  # keep `margin` frames as left context for the next block
                lead = margin
            enc.stdin.write(np.ascontiguousarray(voice.T).tobytes())
            done_frames += voice.shape[1]
            if total:
                pct = min(99.0, done_frames / sr * 100 / total)
                progress(pct, f"Filtering BGM ({engine.name}, {engine.device.upper()})  {pct:4.0f}%  {os.path.basename(src)[:45]}")
        enc.stdin.close()
        dec.stdout.close()
    except BrokenPipeError:
        pass
    finally:
        dec.wait()
        err = enc.stderr.read()
        enc.wait()
        device = engine.device
    if done_frames == 0:
        raise RuntimeError(f"No audio found in {os.path.basename(src)}")
    if enc.returncode != 0:
        raise RuntimeError("ffmpeg encode failed: " + err.decode(errors="replace")[-500:])
    return dst


# --------------------------------------------------------------------------- YouTube account + search
COOKIE_BROWSERS = ["firefox", "chrome", "edge", "brave", "chromium", "opera", "vivaldi"]


_COOKIE_CHECK: dict = {}          # spec -> (time, error or None)
COOKIE_NOTE = {"text": ""}         # last "fell back to signed-out" reason, for the GUIs


def browser_cookies_error(spec: tuple) -> str | None:
    """Try reading the browser's cookies once (cached 2 min). Returns the reason it fails, or None."""
    hit = _COOKIE_CHECK.get(spec)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    err = None
    try:
        from yt_dlp.cookies import extract_cookies_from_browser

        class _Quiet:
            def debug(self, m): pass
            def info(self, m): pass
            def warning(self, m, only_once=False): pass
            def error(self, m): pass
        extract_cookies_from_browser(spec[0], spec[1] if len(spec) > 1 else None, logger=_Quiet())
    except Exception as ex:  # noqa: BLE001
        err = explain_cookie_error(str(ex).replace("ERROR: ", ""))
    _COOKIE_CHECK[spec] = (time.time(), err)
    return err


def cookie_opts(cookies_browser: str | None = None, cookies_file: str | None = None,
                strict: bool = False, log=None) -> dict:
    """yt-dlp options that make requests as YOUR YouTube account (age-restricted, members-only,
    private/unlisted videos you can see, and fewer bot checks).

    cookies_browser -> read the login cookies straight from that browser's profile: 'firefox',
                       'chrome', 'edge', ... or 'chrome:Profile 2' for a non-default profile.
                       Log in to youtube.com in that browser first. Firefox is the most reliable;
                       Chrome/Edge on recent Windows builds encrypt their cookies and may refuse.
    cookies_file    -> a Netscape cookies.txt exported with a browser extension
                       (e.g. 'Get cookies.txt LOCALLY')."""
    if cookies_file:
        if not os.path.isfile(cookies_file):
            raise RuntimeError(f"cookies file not found: {cookies_file}")
        return {"cookiefile": cookies_file}
    if cookies_browser:
        name, _, profile = cookies_browser.partition(":")
        spec: tuple = (name.strip().lower(),)
        if profile.strip():
            spec += (profile.strip(),)
        if not strict:
            err = browser_cookies_error(spec)
            if err:                      # unreadable (Chrome lock/encryption): carry on signed out
                COOKIE_NOTE["text"] = f"not signed in - {spec[0]} cookies unreadable: {err}"
                if log:
                    log("WARN: " + COOKIE_NOTE["text"])
                return {}
        COOKIE_NOTE["text"] = ""
        return {"cookiesfrombrowser": spec}
    COOKIE_NOTE["text"] = ""
    return {}


def youtube_search(query: str, limit: int = 20, cookies_browser=None, cookies_file=None) -> list[dict]:
    """Search YouTube (no API key needed) -> [{url, id, title, channel, duration, views}, ...]."""
    import yt_dlp
    query = query.strip()
    if not query:
        return []
    opts = {"extract_flat": True, "quiet": True, "no_warnings": True, "noprogress": True,
            "skip_download": True, **cookie_opts(cookies_browser, cookies_file)}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{max(1, int(limit))}:{query}", download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        vid = e.get("id") or ""
        out.append({"url": e.get("url") or e.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
                    "id": vid, "title": e.get("title") or "(untitled)",
                    "channel": e.get("channel") or e.get("uploader") or "",
                    "duration": fmt_time(e["duration"]) if e.get("duration") else "",
                    "views": e.get("view_count") or 0})
    return out


def youtube_check_login(cookies_browser=None, cookies_file=None) -> str:
    """Try to read the signed-in account's subscriptions feed. Returns a short status line
    when the cookies sign in, raises with the reason otherwise."""
    import yt_dlp
    opts = {"extract_flat": True, "quiet": True, "no_warnings": True, "noprogress": True,
            "skip_download": True, "playlist_items": "1-3", **cookie_opts(cookies_browser, cookies_file, strict=True)}
    if not opts.get("cookiefile") and not opts.get("cookiesfrombrowser"):
        raise RuntimeError("no sign-in method selected")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://www.youtube.com/feed/subscriptions", download=False)
    entries = [e for e in (info or {}).get("entries") or [] if e]
    if not entries:
        raise RuntimeError("cookies were read, but YouTube did not treat them as signed in "
                           "(log in to youtube.com in that browser, then try again)")
    return f"signed in - subscriptions feed readable (latest: {entries[0].get('title', '')[:60]})"


def explain_cookie_error(msg: str) -> str:
    """Turn yt-dlp's cryptic browser-cookie failures into something actionable."""
    low = (msg or "").lower()
    if "could not copy" in low and "cookie" in low:
        return ("the browser keeps its cookie database locked while it is running (Chrome/Edge on Windows), "
                "and Chrome 127+ also encrypts cookies so other apps cannot read them. Use "
                "'Open sign-in window' on the YouTube tab instead (or Firefox / a cookies.txt file).")
    if "dpapi" in low or "app-bound" in low or ("decrypt" in low and "cookie" in low):
        return ("the browser encrypts its cookies (Chrome 127+ app-bound encryption), so they cannot be "
                "read from outside. Use 'Open sign-in window' on the YouTube tab instead "
                "(or Firefox / a cookies.txt file).")
    return msg


# ---- sign-in through the app's own browser window (no cookie-database reading at all)
# A Chrome/Edge window with its own profile is started with a local DevTools port; once you have
# logged in to YouTube there, the cookies are asked from the running browser itself and written
# to a cookies.txt. This sidesteps both the locked cookie database and app-bound encryption.
SIGNIN_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".yt_signin_profile")
SIGNIN_COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "youtube_cookies.txt")
SIGNIN_URL = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F"
_signin_state: dict = {"port": None, "proc": None}


def find_signin_browser() -> str | None:
    """Path of a Chromium browser to host the sign-in window (Chrome, then Edge, then Brave)."""
    cands = []
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                      os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe")]
    for c in cands:
        if os.path.isfile(c):
            return c
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge", "microsoft-edge"):
        w = shutil.which(name)
        if w:
            return w
    return None


def _devtools_json(port: int, path: str):
    import json
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


def open_signin_window() -> str:
    """Start (or re-focus) the sign-in browser window. Returns a status line."""
    import socket
    port = _signin_state.get("port")
    if port:
        try:
            _devtools_json(port, "/json/version")
            return "sign-in window is already open - log in there, then press 'Save sign-in'"
        except Exception:
            pass
    exe = find_signin_browser()
    if not exe:
        raise RuntimeError("no Chrome / Edge / Brave found to open the sign-in window")
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    os.makedirs(SIGNIN_PROFILE_DIR, exist_ok=True)
    proc = subprocess.Popen([exe, f"--user-data-dir={SIGNIN_PROFILE_DIR}", f"--remote-debugging-port={port}",
                             "--remote-debugging-address=127.0.0.1", "--no-first-run",
                             "--no-default-browser-check", "--new-window", SIGNIN_URL])
    _signin_state.update(port=port, proc=proc)
    for _ in range(40):
        try:
            _devtools_json(port, "/json/version")
            break
        except Exception:
            time.sleep(0.25)
    return (f"opened {os.path.basename(exe)} - log in to YouTube in that window "
            "(stays logged in next time), then press 'Save sign-in'")


def _ws_call(ws_url: str, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
    """One request/response over a DevTools websocket (tiny stdlib client, localhost only)."""
    import base64
    import json
    import socket
    import struct
    import urllib.parse
    u = urllib.parse.urlparse(ws_url)
    s = socket.create_connection((u.hostname, u.port), timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c:
                raise RuntimeError("DevTools closed the connection")
            buf += c
        head, buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("DevTools refused the websocket: " + head.split(b"\r\n", 1)[0].decode(errors="replace"))
        payload = json.dumps({"id": 1, "method": method, "params": params or {}}).encode()
        n = len(payload)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else
                               bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                               bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

        def read(k):
            nonlocal buf
            while len(buf) < k:
                c = s.recv(65536)
                if not c:
                    raise RuntimeError("DevTools closed the connection")
                buf += c
            out, buf = buf[:k], buf[k:]
            return out
        msg = b""
        while True:
            b0, b1 = read(2)
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", read(8))[0]
            if b1 & 0x80:
                m = read(4)
                data = bytes(b ^ m[i % 4] for i, b in enumerate(read(ln)))
            else:
                data = read(ln)
            op = b0 & 0x0F
            if op == 8:
                raise RuntimeError("DevTools closed the connection")
            if op in (9, 10):
                continue
            msg += data
            if b0 & 0x80:
                obj = json.loads(msg.decode("utf-8"))
                msg = b""
                if obj.get("id") == 1:
                    if "error" in obj:
                        raise RuntimeError(f"DevTools {method}: {obj['error'].get('message')}")
                    return obj.get("result") or {}
    finally:
        s.close()


def save_signin_cookies(dest: str = SIGNIN_COOKIES_FILE) -> str:
    """Pull the YouTube/Google cookies from the open sign-in window into a Netscape cookies.txt."""
    port = _signin_state.get("port")
    if not port:
        raise RuntimeError("open the sign-in window first")
    try:
        ws = _devtools_json(port, "/json/version")["webSocketDebuggerUrl"]
    except Exception:
        raise RuntimeError("the sign-in window is closed - open it again (you stay logged in)") from None
    cookies = _ws_call(ws, "Storage.getCookies").get("cookies") or []
    keep = [c for c in cookies if c.get("domain", "").lstrip(".").endswith(("youtube.com", "google.com"))]
    yt_login = [c for c in keep if c.get("domain", "").lstrip(".").endswith("youtube.com")
                and c.get("name") in ("SAPISID", "__Secure-3PAPISID", "LOGIN_INFO")]
    if not yt_login:      # Google cookies arrive first; wait until the redirect to youtube.com has set YouTube's
        raise RuntimeError("not logged in yet - finish signing in to YouTube in the sign-in window first")
    lines = ["# Netscape HTTP Cookie File", "# written by YtubeCatcher from its sign-in window", ""]
    for c in keep:
        dom = c["domain"]
        exp = int(c.get("expires") or 0)
        lines.append("\t".join([("#HttpOnly_" if c.get("httpOnly") else "") + dom,
                                "TRUE" if dom.startswith(".") else "FALSE", c.get("path") or "/",
                                "TRUE" if c.get("secure") else "FALSE", str(max(exp, 0)),
                                c["name"], c.get("value", "")]))
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, dest)
    return f"saved {len(keep)} cookies to {os.path.basename(dest)}"


# --------------------------------------------------------------------------- core
_RANGE_RE = None


def parse_input_line(line: str):
    """'URL_or_path [a-b a-b ...]' -> (source, [(start, end), ...]).

    Ranges are read from the END of the line, so paths with spaces still work.
    A range is 'm:ss-m:ss'; either side may be blank ('-2:00' = from start, '1:00-' = to end)."""
    import re
    global _RANGE_RE
    if _RANGE_RE is None:
        _RANGE_RE = re.compile(r"^(\d[\d:.]*)?-(\d[\d:.]*)?$")
    tokens = line.strip().split()
    ranges = []
    while tokens and _RANGE_RE.match(tokens[-1]) and tokens[-1] != "-":
        a, b = tokens.pop().split("-")
        ranges.insert(0, (a or None, b or None))
    for a, b in ranges:                       # validate early
        parse_time(a); parse_time(b)
    return " ".join(tokens).strip('"'), ranges


def fmt_time(secs: float) -> str:
    """seconds -> 'm:ss' / 'h:mm:ss' (with .x only when fractional)."""
    secs = max(0.0, float(secs))
    h, rem = divmod(secs, 3600)
    m, sec = divmod(rem, 60)
    sec_s = f"{sec:05.2f}".rstrip("0").rstrip(".") if sec != int(sec) else f"{int(sec):02d}"
    return f"{int(h)}:{int(m):02d}:{sec_s}" if h else f"{int(m)}:{sec_s}"


def parse_timeline(text: str):
    """Free-form timeline -> [(input_index or None, start, end), ...] with times as strings.

    One or more ranges per line; each range is two times joined by '-', '~', ',', 'to' or
    just a space: '6:57-7:02', '06:57 ~ 07:02', '6:57 to 7:02', '1:02:03 1:02:30'.
    Times: ss, m:ss, h:mm:ss, decimals allowed. A line may start with '#N' to apply only to
    the N-th input line (1-based); otherwise it applies to every input."""
    import re
    time_re = r"\d{1,2}(?::\d{1,2}){0,2}(?:\.\d+)?"
    out = []
    for raw in text.splitlines():
        line = raw.split("//")[0].strip()          # allow '// comment'
        if not line:
            continue
        idx = None
        m = re.match(r"^#(\d+)\s*[:.]?\s*", line)
        if m:
            idx = int(m.group(1)) - 1
            line = line[m.end():]
        times = re.findall(time_re, line)
        if len(times) == 0 or len(times) % 2:
            raise ValueError(f"Timeline line needs start and end times: '{raw.strip()}'")
        for a, b in zip(times[0::2], times[1::2]):
            if parse_time(b) <= parse_time(a):
                raise ValueError(f"End is not after start: '{a}-{b}'")
            out.append((idx, a, b))
    return out


def _range_tag(a, b) -> str:
    return " ({}-{})".format(a or "0", b or "end").replace(":", "_")


def combine_clips(pieces: list[str], dst: str, fmt: str, bitrate: str, ffmpeg: str,
                  gap: float = 0.5, info: dict | None = None) -> str:
    """Concatenate WAV pieces into one file with `gap` seconds of silence between them."""
    import tempfile
    n = len(pieces)
    inputs = []
    for piece in pieces:
        inputs += ["-i", piece]
    # silence source for the gaps (44.1 kHz stereo to match the pieces)
    inputs += ["-f", "lavfi", "-t", f"{max(gap, 0.0):.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
    chain = []
    for i in range(n):
        chain.append(f"[{i}:a]")
        if gap > 0 and i < n - 1:
            chain.append(f"[{n}:a]")
    fc = "".join(chain) + f"concat=n={len(chain)}:v=0:a=1[out]"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(fc)
        script = f.name
    try:
        _run_ffmpeg([ffmpeg, "-v", "error", "-y", *inputs, "-filter_complex_script", script, "-map", "[out]",
                     *_meta_args(info, fmt), *_codec_args(fmt, bitrate), dst])
    finally:
        os.remove(script)
    return dst


def dump_audio(inputs, out_dir, fmt="mp3", bitrate="192", start=None, end=None,
               playlist=False, voice_only=False, quality="best", keep_original=False,
               combine=False, gap=0.5, keep_clips=False, timeline=None, pad=0.0, denoise=False,
               cookies_browser=None, cookies_file=None,
               log=print, progress=lambda pct, text: None) -> list[str]:
    """Process each input line: a YouTube/any yt-dlp URL or a local audio/video file,
    optionally followed by time ranges ('URL 6:57-7:02 12:10-12:30').

    timeline    -> ranges from parse_timeline(); apply to every input, or to input #N when tagged.
                   Inline ranges on a URL line take precedence over the timeline for that line.
    pad         -> seconds added before AND after every range (never cut a word in half).

    cookies_*   -> sign downloads in with your YouTube account, see cookie_opts().
    voice_only  -> save the voice with background music filtered out (else the audio as-is).
    combine     -> join all clips (in order) into ONE file, `gap` seconds of silence between them."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Run run.bat (installs imageio-ffmpeg) or install ffmpeg.")
    if voice_only and not bgm_filter_available():
        raise RuntimeError("BGM filter needs BS-RoFormer or Demucs - run install_vocals.bat first.")

    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    tmp_dir = os.path.join(out_dir, ".ytubecatcher_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    default_ranges = [(start, end)] if (start or end) else [(None, None)]
    timeline = timeline or []
    pad = max(0.0, float(pad or 0))

    def padded(a, b):
        """(start, end) strings -> (tag, t0, t1) with `pad` seconds around the range."""
        if not a and not b:
            return "", None, None
        t0, t1 = parse_time(a), parse_time(b)
        tag = _range_tag(a, b)
        if t0 is not None:
            t0 = max(0.0, t0 - pad)
        if t1 is not None:
            t1 = t1 + pad
        return tag, t0, t1

    produced: list[str] = []
    pieces: list[tuple[str, str]] = []        # (wav path, source stem) for combining
    stems_seen: list[str] = []

    def finish(src: str, stem: str, info: dict | None, is_download: bool, ranges):
        """src -> final file(s) in out_dir (or WAV pieces when combining)."""
        stems_seen.append(stem)
        for a, b in ranges:
            tag, t0, t1 = padded(a, b)
            if combine:
                piece = os.path.join(tmp_dir, f"piece{len(pieces):04d}.wav")
                if voice_only:
                    filter_bgm(src, piece, "wav", "0", ffmpeg, quality, None, t0, t1, log, progress, denoise)
                else:
                    convert_audio(src, piece, "wav", "0", ffmpeg, None, t0, t1)
                pieces.append((piece, stem))
                log(f"   clip ready: {stem}{tag}")
                if keep_clips:
                    dst = _unique(os.path.join(out_dir, f"{stem}{tag}{VOICE_TAG if voice_only else ''}.{fmt}"))
                    convert_audio(piece, dst, fmt, bitrate, ffmpeg, info)
                    produced.append(dst)
                    log(f"   saved: {os.path.basename(dst)}")
                continue
            mix_dst = os.path.join(out_dir, f"{stem}{tag}.{fmt}")
            if voice_only:
                voice_dst = _unique(os.path.join(out_dir, f"{stem}{tag}{VOICE_TAG}.{fmt}"))
                filter_bgm(src, voice_dst, fmt, bitrate, ffmpeg, quality, info, t0, t1, log, progress, denoise)
                produced.append(voice_dst)
                log(f"   saved: {os.path.basename(voice_dst)}")
                if keep_original and is_download:
                    mix_dst = _unique(mix_dst)
                    convert_audio(src, mix_dst, fmt, bitrate, ffmpeg, info, t0, t1)
                    produced.append(mix_dst)
                    log(f"   saved: {os.path.basename(mix_dst)}")
            else:
                mix_dst = _unique(mix_dst)
                progress(100, "Converting audio...")
                convert_audio(src, mix_dst, fmt, bitrate, ffmpeg, info, t0, t1)
                produced.append(mix_dst)
                log(f"   saved: {os.path.basename(mix_dst)}")

    jobs = []                                  # (source, ranges) in input order
    for i, item in enumerate(inputs):
        src, ranges = parse_input_line(item)
        if not src:
            continue
        if not ranges:
            ranges = [(a, b) for idx, a, b in timeline if idx is None or idx == i]
        jobs.append((src, ranges or default_ranges))
    if pad and any(a or b for _, rs in jobs for a, b in rs):
        log(f"   padding every segment by {pad:g} s on each side")

    try:
        ydl = None
        for src, ranges in jobs:
            if os.path.isfile(src):
                log(f"==> {src}")
                finish(src, os.path.splitext(os.path.basename(src))[0], None, False, ranges)
                continue
            if ydl is None:
                import yt_dlp

                class _Logger:
                    def debug(self, msg):
                        if not msg.startswith("[debug]") and not msg.startswith("[download]"):
                            log(msg)
                    info = debug
                    def warning(self, msg): log("WARN: " + msg)
                    def error(self, msg): log("ERROR: " + msg)

                def hook(d):
                    if d["status"] == "downloading":
                        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        done = d.get("downloaded_bytes", 0)
                        pct = done * 100 / total if total else 0
                        speed = d.get("_speed_str", "").strip()
                        name = os.path.basename(d.get("filename", ""))
                        progress(pct, f"Downloading {name[:50]}  {pct:5.1f}%  {speed}")

                # No yt-dlp postprocessors: its ExtractAudio step requires ffprobe, which the
                # bundled ffmpeg lacks. Raw streams go to the temp folder and are converted by us.
                ydl = yt_dlp.YoutubeDL({
                    "format": "bestaudio/best",
                    "outtmpl": os.path.join(tmp_dir, "%(title)s [%(id)s].%(ext)s"),
                    "noplaylist": not playlist,
                    "ignoreerrors": playlist,
                    "windowsfilenames": True,
                    "ffmpeg_location": ffmpeg,
                    "progress_hooks": [hook],
                    "logger": _Logger(),
                    "noprogress": True,
                    **cookie_opts(cookies_browser, cookies_file, log=log),
                })
            log(f"==> {src}")
            info = ydl.extract_info(src, download=True)
            if not info:
                log("   (nothing downloaded)")
                continue
            for e in info.get("entries") or [info]:
                for rd in (e or {}).get("requested_downloads") or []:
                    fp = rd.get("filepath")
                    if fp and os.path.exists(fp):
                        finish(fp, os.path.splitext(os.path.basename(fp))[0], e, True, ranges)
                        os.remove(fp)

        if combine and pieces:
            progress(100, "Combining clips...")
            uniq = list(dict.fromkeys(st for _, st in pieces))
            if len(uniq) == 1:
                name = f"{uniq[0]} ({len(pieces)} clips)"
            else:
                import time
                name = f"combined {time.strftime('%Y%m%d-%H%M%S')} ({len(pieces)} clips, {len(uniq)} sources)"
            dst = _unique(os.path.join(out_dir, f"{name}{VOICE_TAG if voice_only else ''}.{fmt}"))
            combine_clips([pth for pth, _ in pieces], dst, fmt, bitrate, ffmpeg, gap)
            produced.append(dst)
            log(f"   saved: {os.path.basename(dst)}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    progress(100, f"Done - {len(produced)} file(s)")
    return produced



# --------------------------------------------------------------------------- video mode
def _video_size(ffmpeg: str, src: str):
    """(width, height) of the first video stream, parsed from ffmpeg's banner."""
    import re
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", src], capture_output=True, stdin=subprocess.DEVNULL, **_no_window())
    m = re.search(rb"Video:.*?\s(\d{2,5})x(\d{2,5})", r.stderr)
    return (int(m.group(1)), int(m.group(2))) if m else (1920, 1080)


def _ffmpeg_video_out(size=None) -> list[str]:
    args = []
    if size:
        w, h = size
        args += ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"]
    return args + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]


def cut_video(src: str, dst: str, ffmpeg: str, t0=None, t1=None, size=None) -> str:
    """Frame-accurate clip (re-encoded); with no range, a plain remux/re-encode to mp4."""
    trim = (["-ss", str(t0)] if t0 else []) + (["-to", str(t1)] if t1 is not None else [])
    _run_ffmpeg([ffmpeg, "-v", "error", "-y", *trim, "-i", src, *_ffmpeg_video_out(size), dst])
    return dst


def combine_videos(pieces: list[str], dst: str, ffmpeg: str, gap: float = 0.5) -> str:
    """Concatenate mp4 pieces (scaled to the first piece's size) with `gap` seconds of black."""
    import tempfile
    w, h = _video_size(ffmpeg, pieces[0])
    n = len(pieces)
    inputs = []
    for piece in pieces:
        inputs += ["-i", piece]
    inputs += ["-f", "lavfi", "-t", f"{max(gap, 0.0):.3f}", "-i", f"color=c=black:s={w}x{h}:r=30",
               "-f", "lavfi", "-t", f"{max(gap, 0.0):.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    parts, chain = [], []
    for i in range(n):
        parts.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}];"
                     f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        chain.append(f"[v{i}][a{i}]")
        if gap > 0 and i < n - 1:
            chain.append(f"[{n}:v][{n + 1}:a]")
    fc = ";".join(parts) + ";" + "".join(chain) + f"concat=n={len(chain)}:v=1:a=1[v][a]"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(fc)
        script = f.name
    try:
        _run_ffmpeg([ffmpeg, "-v", "error", "-y", *inputs, "-filter_complex_script", script,
                     "-map", "[v]", "-map", "[a]", *_ffmpeg_video_out(), dst])
    finally:
        os.remove(script)
    return dst


def dump_video(inputs, out_dir, resolution="best", start=None, end=None, playlist=False,
               combine=False, gap=0.5, keep_clips=False, timeline=None, pad=0.0,
               cookies_browser=None, cookies_file=None,
               log=print, progress=lambda pct, text: None) -> list[str]:
    """Video mode: download as MP4 (or take local files), cut the ranges, optionally combine."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Run run.bat (installs imageio-ffmpeg) or install ffmpeg.")
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    tmp_dir = os.path.join(out_dir, ".ytubecatcher_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    default_ranges = [(start, end)] if (start or end) else [(None, None)]
    timeline = timeline or []
    pad = max(0.0, float(pad or 0))
    height = None if str(resolution) == "best" else int(resolution)

    produced: list[str] = []
    pieces: list[tuple[str, str]] = []
    stems_seen: list[str] = []

    def finish(src: str, stem: str, is_download: bool, ranges):
        stems_seen.append(stem)
        for a, b in ranges:
            tag, t0, t1 = "", None, None
            if a or b:
                t0, t1 = parse_time(a), parse_time(b)
                tag = _range_tag(a, b)
                if t0 is not None:
                    t0 = max(0.0, t0 - pad)
                if t1 is not None:
                    t1 = t1 + pad
            if combine:
                piece = os.path.join(tmp_dir, f"vpiece{len(pieces):04d}.mp4")
                progress(100, f"Cutting {stem}{tag}...")
                cut_video(src, piece, ffmpeg, t0, t1)
                pieces.append((piece, stem))
                log(f"   clip ready: {stem}{tag}")
                if keep_clips:
                    dst = _unique(os.path.join(out_dir, f"{stem}{tag}.mp4"))
                    shutil.copy(piece, dst)
                    produced.append(dst)
                    log(f"   saved: {os.path.basename(dst)}")
                continue
            dst = _unique(os.path.join(out_dir, f"{stem}{tag}.mp4"))
            if not tag and is_download and src.lower().endswith(".mp4"):
                shutil.move(src, dst)                # whole video: keep the original stream
            else:
                progress(100, f"Cutting {stem}{tag}..." if tag else f"Converting {stem} to mp4...")
                cut_video(src, dst, ffmpeg, t0, t1)
            produced.append(dst)
            log(f"   saved: {os.path.basename(dst)}")

    jobs = []
    for i, item in enumerate(inputs):
        src, ranges = parse_input_line(item)
        if not src:
            continue
        if not ranges:
            ranges = [(a, b) for idx, a, b in timeline if idx is None or idx == i]
        jobs.append((src, ranges or default_ranges))
    if pad and any(a or b for _, rs in jobs for a, b in rs):
        log(f"   padding every segment by {pad:g} s on each side")

    try:
        ydl = None
        for src, ranges in jobs:
            if os.path.isfile(src):
                log(f"==> {src}")
                finish(src, os.path.splitext(os.path.basename(src))[0], False, ranges)
                continue
            if ydl is None:
                import yt_dlp

                class _Logger:
                    def debug(self, msg):
                        if not msg.startswith("[debug]") and not msg.startswith("[download]"):
                            log(msg)
                    info = debug
                    def warning(self, msg): log("WARN: " + msg)
                    def error(self, msg): log("ERROR: " + msg)

                def hook(d):
                    if d["status"] == "downloading":
                        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        done = d.get("downloaded_bytes", 0)
                        pct = done * 100 / total if total else 0
                        speed = d.get("_speed_str", "").strip()
                        name = os.path.basename(d.get("filename", ""))
                        progress(pct, f"Downloading {name[:50]}  {pct:5.1f}%  {speed}")

                hsel = f"[height<={height}]" if height else ""
                ydl = yt_dlp.YoutubeDL({
                    "format": f"bestvideo{hsel}[ext=mp4]+bestaudio[ext=m4a]/bestvideo{hsel}+bestaudio/best{hsel}/best",
                    "merge_output_format": "mp4",
                    "outtmpl": os.path.join(tmp_dir, "%(title)s [%(id)s].%(ext)s"),
                    "noplaylist": not playlist,
                    "ignoreerrors": playlist,
                    "windowsfilenames": True,
                    "ffmpeg_location": ffmpeg,
                    "progress_hooks": [hook],
                    "logger": _Logger(),
                    "noprogress": True,
                    **cookie_opts(cookies_browser, cookies_file, log=log),
                })
            log(f"==> {src}")
            info = ydl.extract_info(src, download=True)
            if not info:
                log("   (nothing downloaded)")
                continue
            for e in info.get("entries") or [info]:
                for rd in (e or {}).get("requested_downloads") or []:
                    fp = rd.get("filepath")
                    if fp and os.path.exists(fp):
                        finish(fp, os.path.splitext(os.path.basename(fp))[0], True, ranges)
                        if os.path.exists(fp):
                            os.remove(fp)

        if combine and pieces:
            progress(100, "Combining clips...")
            uniq = list(dict.fromkeys(st for _, st in pieces))
            if len(uniq) == 1:
                name = f"{uniq[0]} ({len(pieces)} clips)"
            else:
                import time
                name = f"combined {time.strftime('%Y%m%d-%H%M%S')} ({len(pieces)} clips, {len(uniq)} sources)"
            dst = _unique(os.path.join(out_dir, f"{name}.mp4"))
            combine_videos([pth for pth, _ in pieces], dst, ffmpeg, gap)
            produced.append(dst)
            log(f"   saved: {os.path.basename(dst)}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    progress(100, f"Done - {len(produced)} file(s)")
    return produced


# --------------------------------------------------------------------------- preview
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".mpg", ".mpeg", ".ts", ".wmv"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".flac", ".opus", ".ogg", ".aac", ".wma", ".aiff", ".aif"}
PREVIEW_W, PREVIEW_H = 640, 200          # canvas size (frames are scaled to fit; waveform uses the full width)
PREVIEW_FPS = 12                         # video preview frame rate
PREVIEW_AUDIO_SEG = 600                  # seconds of audio decoded per play segment (bounds temp WAV size)


def is_video_file(ffmpeg: str, path: str) -> bool:
    """True when the file has a real video stream (mp3 cover art does not count)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return True
    if ext in AUDIO_EXT:
        return False
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True, stdin=subprocess.DEVNULL, **_no_window())
    return b"Video:" in r.stderr and b"attached pic" not in r.stderr


def audio_peaks(ffmpeg: str, src: str, columns: int, rate: int = 8000):
    """Waveform data: one (min, max) pair in -1..1 per column, plus the duration in seconds."""
    raw = _run_ffmpeg([ffmpeg, "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"])
    n = len(raw) // 2
    if n == 0:
        return [], 0.0
    columns = max(1, int(columns))
    step = max(1, n // columns)
    if np is not None:
        x = np.frombuffer(raw, dtype="<i2", count=n)
        m = (n // step) * step
        blocks = x[:m].reshape(-1, step)
        lo, hi = blocks.min(axis=1) / 32768.0, blocks.max(axis=1) / 32768.0
        peaks = list(zip(lo.tolist(), hi.tolist()))
    else:
        import array
        a = array.array("h")
        a.frombytes(raw[:n * 2])
        if sys.byteorder != "little":
            a.byteswap()
        peaks = [(min(a[i:i + step]) / 32768.0, max(a[i:i + step]) / 32768.0)
                 for i in range(0, n - step + 1, step)]
    return peaks[:columns], n / rate


def _scale_filter(w: int, h: int) -> str:
    return f"scale={w}:{h}:force_original_aspect_ratio=decrease"


def video_frame(ffmpeg: str, src: str, t: float, w: int = PREVIEW_W, h: int = PREVIEW_H) -> bytes:
    """One frame at t seconds, scaled to fit w x h, as PPM bytes (tk.PhotoImage reads PPM natively)."""
    return _run_ffmpeg([ffmpeg, "-v", "error", "-ss", str(max(0.0, t)), "-i", src, "-frames:v", "1",
                        "-vf", _scale_filter(w, h), "-f", "image2pipe", "-vcodec", "ppm", "-"])


def _read_ppm(stream) -> bytes | None:
    """Read one binary PPM image from a stream (as ffmpeg's image2pipe writes them); None at EOF."""
    magic = stream.readline()
    if not magic or magic.strip() != b"P6":
        return None
    dims = stream.readline()
    while dims.startswith(b"#"):
        dims = stream.readline()
    maxval = stream.readline()
    w, h = map(int, dims.split())
    need = w * h * 3
    buf = bytearray()
    while len(buf) < need:
        chunk = stream.read(need - len(buf))
        if not chunk:
            return None
        buf += chunk
    return magic + dims + maxval + bytes(buf)


def decode_wav(ffmpeg: str, src: str, dst: str, t0: float = 0.0, length: float | None = None) -> str:
    """Decode `length` seconds from t0 of src into a stereo 44.1 kHz WAV (for playback)."""
    _run_ffmpeg([ffmpeg, "-v", "error", "-y", "-ss", str(max(0.0, t0)), "-i", src,
                 *(["-t", str(length)] if length else []),
                 "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", dst])
    return dst


class _Preview:
    """GUI preview panel: waveform for audio files, video frames for videos, with playback.

    Audio plays through winsound on Windows (afplay / aplay elsewhere) from a temp WAV that
    ffmpeg decodes on demand, so any format ffmpeg reads can be previewed. Video frames are
    streamed from ffmpeg as PPM images and shown in step with the audio clock."""

    def __init__(self, parent, ffmpeg: str, post):
        import tempfile
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ffmpeg, self.post = tk, ffmpeg, post      # post(fn): run fn on the Tk thread
        self.tmp = tempfile.mkdtemp(prefix="ytubecatcher_preview_")
        self.path = None
        self.is_video = False
        self.dur = 0.0
        self.peaks: list = []
        self.frame_img = None
        self.frame_item = None
        self.pos = 0.0                # current position (s)
        self.t0 = 0.0                 # position where the running play segment started
        self.t_start = None           # monotonic clock when the play segment started
        self.playing = False
        self.gen = 0                  # bumped on every load/play/stop; worker threads check it
        self.proc = None              # ffmpeg frame streamer (video)
        self.audio_proc = None        # afplay/aplay on non-Windows
        self.frames: queue.Queue = queue.Queue()

        self.canvas = tk.Canvas(parent, width=PREVIEW_W, height=PREVIEW_H, bg="#111",
                                highlightthickness=0, cursor="hand2")
        self.canvas.pack(fill="x")
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        ctl = ttk.Frame(parent)
        ctl.pack(fill="x", pady=(4, 0))
        self.play_btn = ttk.Button(ctl, text="Play", width=7, command=self.toggle)
        self.play_btn.pack(side="left")
        self.play_btn.state(["disabled"])
        ttk.Button(ctl, text="Stop", width=6, command=self.stop).pack(side="left", padx=4)
        self.time_var = tk.StringVar(value="")
        ttk.Label(ctl, textvariable=self.time_var, font=("Consolas", 10)).pack(side="left", padx=8)
        self.name_var = tk.StringVar(value="Output files appear in the list after a run - click one to preview it")
        ttk.Label(ctl, textvariable=self.name_var, foreground="gray").pack(side="left", padx=8)
        self._tick()

    # ---- geometry helpers
    def _w(self) -> int:
        w = self.canvas.winfo_width()
        return w if w > 50 else PREVIEW_W

    def _x(self, t: float) -> float:
        return (t / self.dur * self._w()) if self.dur else 0.0

    # ---- loading
    def load(self, path: str):
        """Analyse `path` in the background, then show its waveform / first frame."""
        self.stop()
        self.gen += 1
        gen = self.gen
        self.path, self.peaks, self.frame_img, self.dur, self.pos = path, [], None, 0.0, 0.0
        self.is_video = False
        self.name_var.set(os.path.basename(path))
        self.time_var.set("loading...")
        self.play_btn.state(["disabled"])
        self.canvas.delete("all")
        W = self._w()

        def work():
            try:
                if is_video_file(self.ffmpeg, path):
                    dur = _media_duration(self.ffmpeg, path) or 0.0
                    res = ("video", dur, video_frame(self.ffmpeg, path, 0, W, PREVIEW_H))
                else:
                    peaks, dur = audio_peaks(self.ffmpeg, path, W)
                    res = ("audio", dur, peaks)
            except Exception as ex:  # noqa: BLE001
                res = ("err", 0.0, str(ex))
            self.post(lambda: self._loaded(gen, *res))
        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, gen, kind, dur, data):
        if gen != self.gen:
            return
        self.dur = dur
        if kind == "err":
            self.time_var.set("preview failed")
            self.canvas.delete("all")
            self.canvas.create_text(8, 8, anchor="nw", fill="#f66", text=data[-300:], width=self._w() - 16)
            return
        self.is_video = kind == "video"
        if self.is_video:
            self.frame_img = self.tk.PhotoImage(data=data)
        else:
            self.peaks = data
        self.play_btn.state(["!disabled"])
        self._redraw()
        self._update_time()

    # ---- drawing
    def _redraw(self):
        c = self.canvas
        c.delete("all")
        self.frame_item = None
        W, H = self._w(), PREVIEW_H
        if self.is_video and self.frame_img is not None:
            self.frame_item = c.create_image(W // 2, H // 2, image=self.frame_img, anchor="center")
        elif self.peaks:
            mid, amp = H / 2, H / 2 - 8
            sx = W / len(self.peaks)
            c.create_line(0, mid, W, mid, fill="#334")
            for i, (lo, hi) in enumerate(self.peaks):
                x = i * sx
                c.create_line(x, mid - hi * amp, x, mid - lo * amp + 1, fill="#4fc3f7")
        if self.path:
            x = self._x(self.pos)
            c.create_rectangle(0, H - 6, W, H, fill="#222", outline="")
            c.create_rectangle(0, H - 6, x, H, fill="#ff9800", outline="", tags="bar")
            if not self.is_video:                  # playhead over the waveform; video keeps the bar only
                c.create_line(x, 0, x, H, fill="#ff9800", tags="cursor")

    def _cursor(self):
        x, H = self._x(self.pos), PREVIEW_H
        self.canvas.coords("cursor", x, 0, x, H)
        self.canvas.coords("bar", 0, H - 6, x, H)

    def _show_frame(self, ppm: bytes):
        self.frame_img = self.tk.PhotoImage(data=ppm)
        if self.frame_item is None:
            self._redraw()
        else:
            self.canvas.itemconfigure(self.frame_item, image=self.frame_img)

    def _update_time(self):
        self.time_var.set(f"{fmt_time(self.pos)} / {fmt_time(self.dur)}")

    # ---- playback
    def toggle(self):
        if self.playing:
            self.stop(keep_pos=True)
        else:
            self.play(self.pos)

    def play(self, t0: float):
        if not self.path or self.dur <= 0:
            return
        if t0 >= self.dur - 0.05:
            t0 = 0.0
        self.stop(keep_pos=True)
        self.gen += 1
        gen = self.gen
        self.pos = self.t0 = t0
        self.playing = True
        self.play_btn.configure(text="Pause")
        self.time_var.set("starting...")
        self._cursor()
        W, path = self._w(), self.path

        def work():
            wav = os.path.join(self.tmp, f"play{gen}.wav")
            try:
                decode_wav(self.ffmpeg, path, wav, t0, PREVIEW_AUDIO_SEG)
            except Exception as ex:  # noqa: BLE001
                err = str(ex)
                self.post(lambda: self._fail(gen, err))
                return
            proc = None
            if self.is_video:
                proc = subprocess.Popen([self.ffmpeg, "-v", "error", "-ss", str(t0), "-i", path, "-an",
                                         "-t", str(PREVIEW_AUDIO_SEG), "-r", str(PREVIEW_FPS),
                                         "-vf", _scale_filter(W, PREVIEW_H), "-f", "image2pipe", "-vcodec", "ppm", "-"],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        **_no_window())
                first = _read_ppm(proc.stdout)          # have a frame ready before the clock starts
                if first is not None:
                    self.frames.put((gen, t0, first))
            self.post(lambda: self._started(gen, wav, proc))
            if proc is None:
                return
            i = 1
            while gen == self.gen:
                ppm = _read_ppm(proc.stdout)
                if ppm is None:
                    break
                self.frames.put((gen, t0 + i / PREVIEW_FPS, ppm))
                i += 1
                while self.frames.qsize() > PREVIEW_FPS * 2 and gen == self.gen:
                    time.sleep(0.05)
            try:
                proc.kill()
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _fail(self, gen, msg):
        if gen != self.gen:
            return
        self.stop(keep_pos=True)
        self.time_var.set("playback failed")
        self.name_var.set(msg[-120:])

    def _started(self, gen, wav, proc):
        if gen != self.gen:                       # stopped while decoding
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            return
        self.proc = proc
        self._audio_start(wav)
        self.t_start = time.monotonic()

    def _audio_start(self, wav):
        if os.name == "nt":
            import winsound
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        else:
            player = shutil.which("afplay") or shutil.which("aplay") or shutil.which("paplay")
            if player:
                self.audio_proc = subprocess.Popen([player, wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _audio_stop(self):
        if os.name == "nt":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        if self.audio_proc:
            try:
                self.audio_proc.kill()
            except Exception:
                pass
            self.audio_proc = None

    def stop(self, keep_pos: bool = False):
        self.gen += 1
        self.playing = False
        self.t_start = None
        self._audio_stop()
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break
        self.play_btn.configure(text="Play")
        if not keep_pos:
            self.pos = 0.0
        if self.path:
            self._cursor()
            self._update_time()

    def _tick(self):
        """40 ms clock: advance the cursor, show due video frames, chain the next audio segment."""
        try:
            if self.playing and self.t_start is not None:
                self.pos = self.t0 + (time.monotonic() - self.t_start)
                if self.pos >= self.dur:
                    self.stop()
                    self.pos = self.dur
                    self._cursor()
                    self._update_time()
                elif self.pos >= self.t0 + PREVIEW_AUDIO_SEG - 0.05:
                    self.play(self.pos)                     # next segment of a long file
                else:
                    latest = None
                    while not self.frames.empty():
                        gen, t, ppm = self.frames.queue[0]
                        if gen != self.gen:
                            self.frames.get_nowait()
                            continue
                        if t > self.pos:
                            break
                        latest = self.frames.get_nowait()[2]
                    if latest is not None:
                        self._show_frame(latest)
                    self._cursor()
                    self._update_time()
        except Exception:
            pass
        self.canvas.after(40, self._tick)

    def _click(self, event):
        if not self.path or self.dur <= 0:
            return
        t = max(0.0, min(self.dur, event.x / self._w() * self.dur))
        if self.playing:
            self.play(t)
            return
        self.pos = t
        self._cursor()
        self._update_time()
        if self.is_video:
            self.gen += 1
            gen, W, path = self.gen, self._w(), self.path

            def work():
                try:
                    ppm = video_frame(self.ffmpeg, path, t, W, PREVIEW_H)
                except Exception:
                    return
                self.post(lambda: gen == self.gen and self._show_frame(ppm))
            threading.Thread(target=work, daemon=True).start()

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------- GUI
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"{APP_NAME} - YouTube audio dumper")
    root.geometry("980x900")
    root.minsize(800, 720)
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except Exception:
        pass

    q: queue.Queue = queue.Queue()
    pad = {"padx": 8, "pady": 4}
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    frm = ttk.Frame(nb, padding=10)
    nb.add(frm, text="   Download   ")
    yt_tab = ttk.Frame(nb, padding=10)
    nb.add(yt_tab, text="   YouTube search / account   ")
    frm.columnconfigure(1, weight=1)

    ttk.Label(frm, text="YouTube URLs or\nlocal files\n(one per line)").grid(row=0, column=0, sticky="nw", **pad)
    urls_txt = tk.Text(frm, height=5, wrap="none", font=("Consolas", 10))
    urls_txt.grid(row=0, column=1, columnspan=2, sticky="nsew", **pad)
    tl_frame = ttk.Frame(frm)
    tl_frame.grid(row=0, column=3, sticky="nsew", **pad)
    ttk.Label(tl_frame, text="Timeline (one range per line, e.g. 6:57-7:02)").pack(anchor="w")
    timeline_txt = tk.Text(tl_frame, height=5, width=30, wrap="none", font=("Consolas", 10))
    timeline_txt.pack(fill="both", expand=True)
    frm.columnconfigure(3, weight=1)

    def add_lines(lines):
        cur = urls_txt.get("1.0", "end").strip()
        urls_txt.insert("end", ("\n" if cur else "") + "\n".join(lines))

    def paste():
        try:
            clip = root.clipboard_get().strip()
            if clip:
                add_lines([clip])
        except tk.TclError:
            pass

    def add_files():
        exts = " ".join(f"*{e}" for e in sorted(MEDIA_EXT))
        paths = filedialog.askopenfilenames(title="Pick audio/video files",
                                            filetypes=[("Audio/Video", exts), ("All files", "*.*")])
        if paths:
            add_lines(paths)

    row1 = ttk.Frame(frm)
    row1.grid(row=1, column=1, columnspan=3, sticky="e", **pad)
    ttk.Button(row1, text="Add local files...", command=add_files).pack(side="left", padx=4)
    ttk.Button(row1, text="Paste", command=paste).pack(side="left", padx=4)
    ttk.Button(row1, text="Clear", command=lambda: urls_txt.delete("1.0", "end")).pack(side="left", padx=4)

    ttk.Label(frm, text="Save to").grid(row=2, column=0, sticky="w", **pad)
    saved = load_settings()
    if saved.get("inputs"):
        urls_txt.insert("1.0", saved["inputs"])
    if saved.get("timeline"):
        timeline_txt.insert("1.0", saved["timeline"])
    out_var = tk.StringVar(value=saved.get("out_dir") or DEFAULT_OUT)
    ttk.Entry(frm, textvariable=out_var).grid(row=2, column=1, columnspan=2, sticky="ew", **pad)
    ttk.Button(frm, text="Browse...",
               command=lambda: out_var.set(filedialog.askdirectory(initialdir=out_var.get()) or out_var.get())
               ).grid(row=2, column=3, sticky="e", **pad)

    opt = ttk.Frame(frm)
    opt.grid(row=3, column=0, columnspan=4, sticky="ew", **pad)
    mode_var = tk.StringVar(value=saved.get("mode", "audio"))
    fmt_var, br_var = tk.StringVar(value=saved.get("format", "mp3")), tk.StringVar(value=saved.get("bitrate", "192"))
    res_var = tk.StringVar(value=str(saved.get("resolution", "best")))
    start_var, end_var = tk.StringVar(value=saved.get("start", "")), tk.StringVar(value=saved.get("end", ""))
    ttk.Label(opt, text="Mode").pack(side="left")
    ttk.Radiobutton(opt, text="Audio", variable=mode_var, value="audio").pack(side="left", padx=(4, 0))
    ttk.Radiobutton(opt, text="Video (mp4)", variable=mode_var, value="video").pack(side="left", padx=(4, 14))
    fmt_lbl = ttk.Label(opt, text="Format")
    fmt_lbl.pack(side="left")
    fmt_cb = ttk.Combobox(opt, textvariable=fmt_var, values=FORMATS, width=6, state="readonly")
    fmt_cb.pack(side="left", padx=(4, 14))
    br_lbl = ttk.Label(opt, text="Bitrate (kbps)")
    br_lbl.pack(side="left")
    br_cb = ttk.Combobox(opt, textvariable=br_var, values=BITRATES, width=5, state="readonly")
    br_cb.pack(side="left", padx=(4, 14))
    res_lbl = ttk.Label(opt, text="Max resolution")
    res_cb = ttk.Combobox(opt, textvariable=res_var, values=RESOLUTIONS, width=6, state="readonly")
    clip_lbl = ttk.Label(opt, text="Clip from")
    clip_lbl.pack(side="left")
    ttk.Entry(opt, textvariable=start_var, width=9).pack(side="left", padx=4)
    ttk.Label(opt, text="to").pack(side="left")
    ttk.Entry(opt, textvariable=end_var, width=9).pack(side="left", padx=4)
    ttk.Label(opt, text="(m:ss, optional)", foreground="gray").pack(side="left", padx=4)
    ttk.Label(opt, text="Pad (s)").pack(side="left", padx=(14, 4))
    padsec_var = tk.StringVar(value=str(saved.get("pad", "0.3")))
    ttk.Entry(opt, textvariable=padsec_var, width=5).pack(side="left")

    opt2 = ttk.Frame(frm)
    opt2.grid(row=4, column=0, columnspan=4, sticky="ew", **pad)
    pl_var = tk.BooleanVar(value=bool(saved.get("playlist", False)))
    ttk.Checkbutton(opt2, text="Whole playlist", variable=pl_var).pack(side="left", padx=(0, 14))

    # ---- BGM filter
    bgm = ttk.LabelFrame(frm, text="BGM filter", padding=(8, 4))
    bgm.grid(row=5, column=0, columnspan=4, sticky="ew", **pad)
    has_filter = bgm_filter_available()
    voice_var = tk.BooleanVar(value=bool(saved.get("voice_only", has_filter)) and has_filter)
    qual_var = tk.StringVar(value=saved.get("quality") or default_quality())
    keep_var = tk.BooleanVar(value=bool(saved.get("keep_original", False)))
    denoise_var = tk.BooleanVar(value=bool(saved.get("denoise", False)))

    # YouTube account (cookies) - widgets live on the YouTube tab, built further down
    cookies_mode_var = tk.StringVar(value=saved.get("cookies_mode", "none"))
    cookies_browser_var = tk.StringVar(value=saved.get("cookies_browser") or "firefox")
    cookies_file_var = tk.StringVar(value=saved.get("cookies_file") or "")

    def cookie_kw() -> dict:
        m = cookies_mode_var.get()
        return dict(cookies_browser=cookies_browser_var.get().strip() or None if m == "browser" else None,
                    cookies_file=cookies_file_var.get().strip() or None if m == "file" else None)

    # ---- auto-save every input (debounced), plus on Start and on close
    def snapshot():
        d = dict(inputs=urls_txt.get("1.0", "end").rstrip("\n"),
                 timeline=timeline_txt.get("1.0", "end").rstrip("\n"), pad=padsec_var.get(), out_dir=out_var.get(),
                 format=fmt_var.get(), bitrate=br_var.get(), start=start_var.get(), end=end_var.get(),
                 playlist=pl_var.get(), voice_only=voice_var.get(), quality=qual_var.get(),
                 keep_original=keep_var.get(), denoise=denoise_var.get(),
                 mode=mode_var.get(), resolution=res_var.get(),
                 cookies_mode=cookies_mode_var.get(), cookies_browser=cookies_browser_var.get(),
                 cookies_file=cookies_file_var.get())
        try:
            d.update(query=query_var.get(), search_limit=limit_var.get())
        except NameError:
            pass
        try:                                    # combine widgets are created a little later
            d.update(combine=combine_var.get(), gap=gap_var.get(), keep_clips=keepclips_var.get())
        except NameError:
            pass
        return d
    _save_job = [None]
    def autosave(*_):
        if _save_job[0]:
            root.after_cancel(_save_job[0])
        _save_job[0] = root.after(800, lambda: save_settings(snapshot()))
    for v in (out_var, fmt_var, br_var, start_var, end_var, pl_var, voice_var, qual_var, keep_var, padsec_var, denoise_var, mode_var, res_var,
              cookies_mode_var, cookies_browser_var, cookies_file_var):
        v.trace_add("write", autosave)
    def on_text_modified(e):
        e.widget.edit_modified(False)
        autosave()
    urls_txt.bind("<<Modified>>", on_text_modified)
    timeline_txt.bind("<<Modified>>", on_text_modified)
    def on_close():
        save_settings(snapshot())
        try:
            preview.cleanup()
        except NameError:
            pass
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    voice_cb = ttk.Checkbutton(bgm, text="Filter out BGM - keep voice only", variable=voice_var)
    voice_cb.pack(side="left")
    ttk.Label(bgm, text="Quality").pack(side="left", padx=(16, 4))
    qual_cb = ttk.Combobox(bgm, textvariable=qual_var, values=list(QUALITY_MODELS), width=6, state="readonly")
    qual_cb.pack(side="left")
    keep_cb = ttk.Checkbutton(bgm, text="also save original mix", variable=keep_var)
    keep_cb.pack(side="left", padx=(16, 0))
    denoise_cb = ttk.Checkbutton(bgm, text="extra clean-up (ambience/noise)", variable=denoise_var)
    denoise_cb.pack(side="left", padx=(16, 0))

    def sync_bgm(*_):
        st = ["!disabled"] if (has_filter and voice_var.get()) else ["disabled"]
        qual_cb.state(st + (["readonly"] if st == ["!disabled"] else []))
        keep_cb.state(st)
        denoise_cb.state(st if roformer_available() else ["disabled"])
    voice_var.trace_add("write", sync_bgm)

    def sync_mode(*_):
        video = mode_var.get() == "video"
        # re-pack the format/resolution widgets in a stable order
        for w in (res_lbl, res_cb, fmt_lbl, fmt_cb, br_lbl, br_cb):
            w.pack_forget()
        if video:
            res_lbl.pack(side="left", before=clip_lbl)
            res_cb.pack(side="left", padx=(4, 14), before=clip_lbl)
        else:
            fmt_lbl.pack(side="left", before=clip_lbl)
            fmt_cb.pack(side="left", padx=(4, 14), before=clip_lbl)
            br_lbl.pack(side="left", before=clip_lbl)
            br_cb.pack(side="left", padx=(4, 14), before=clip_lbl)
        for child in bgm.winfo_children():
            try:
                child.state(["disabled"] if video else ["!disabled"])
            except Exception:
                pass
        if not video:
            sync_bgm()
        go_btn.configure(text="Download video" if video else "Start")
    mode_var.trace_add("write", sync_mode)
    if not has_filter:
        voice_cb.state(["disabled"])
        ttk.Label(bgm, text="  NOT INSTALLED - output will keep the background!", foreground="#c00000").pack(side="left")

        def run_installer():
            bat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install_vocals.bat")
            if os.name == "nt" and os.path.exists(bat):
                os.startfile(bat)
                messagebox.showinfo(APP_NAME, "The installer opened in a console window (~3.5 GB download).\n"
                                    "When it says Done, close and restart YtubeCatcher with run.bat.")
            else:
                messagebox.showinfo(APP_NAME, "Run install_vocals.bat in the app folder, then restart with run.bat.")
        ttk.Button(bgm, text="Install BGM filter...", command=run_installer).pack(side="left", padx=8)
    sync_bgm()

    # ---- combine clips
    comb = ttk.LabelFrame(frm, text="Combine", padding=(8, 4))
    comb.grid(row=6, column=0, columnspan=4, sticky="ew", **pad)
    combine_var = tk.BooleanVar(value=bool(saved.get("combine", False)))
    gap_var = tk.StringVar(value=str(saved.get("gap", "0.5")))
    keepclips_var = tk.BooleanVar(value=bool(saved.get("keep_clips", False)))
    ttk.Checkbutton(comb, text="Combine all clips into ONE file (in order)", variable=combine_var).pack(side="left")
    ttk.Label(comb, text="gap (s)").pack(side="left", padx=(16, 4))
    ttk.Entry(comb, textvariable=gap_var, width=5).pack(side="left")
    ttk.Checkbutton(comb, text="also keep the individual clips", variable=keepclips_var).pack(side="left", padx=(16, 0))
    for v in (combine_var, gap_var, keepclips_var):
        v.trace_add("write", autosave)

    status_var = tk.StringVar(value="Ready")
    bar = ttk.Progressbar(frm, maximum=100)
    bar.grid(row=7, column=0, columnspan=4, sticky="ew", **pad)
    ttk.Label(frm, textvariable=status_var).grid(row=8, column=0, columnspan=4, sticky="w", **pad)

    # ---- preview: waveform for audio, frames for video, with playback
    prev = ttk.LabelFrame(frm, text="Preview", padding=(8, 4))
    prev.grid(row=9, column=0, columnspan=4, sticky="ew", **pad)
    prev.columnconfigure(1, weight=1)
    lb_frame = ttk.Frame(prev)
    lb_frame.grid(row=0, column=0, sticky="nsew")
    ttk.Label(lb_frame, text="Files (click = preview, double-click = open)").pack(anchor="w")
    files_lb = tk.Listbox(lb_frame, width=42, height=11, font=("Consolas", 9), activestyle="none",
                          exportselection=False)
    files_sb = ttk.Scrollbar(lb_frame, command=files_lb.yview)
    files_lb.configure(yscrollcommand=files_sb.set)
    files_sb.pack(side="right", fill="y")
    files_lb.pack(side="left", fill="both", expand=True)
    pv_frame = ttk.Frame(prev)
    pv_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
    preview = _Preview(pv_frame, find_ffmpeg() or "ffmpeg", lambda fn: q.put(("call", fn)))
    out_files: list[str] = []

    def add_outputs(files):
        for f in files:
            out_files.append(f)
            files_lb.insert("end", os.path.basename(f))
        if files:
            files_lb.selection_clear(0, "end")
            files_lb.selection_set("end")
            files_lb.see("end")
            preview.load(files[-1])

    def on_select(_e=None):
        sel = files_lb.curselection()
        if sel:
            preview.load(out_files[sel[0]])
    files_lb.bind("<<ListboxSelect>>", on_select)

    def open_selected(_e=None):
        sel = files_lb.curselection()
        if not sel:
            return
        p = out_files[sel[0]]
        if os.name == "nt":
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    files_lb.bind("<Double-Button-1>", open_selected)

    def pick_preview():
        exts = " ".join(f"*{e}" for e in sorted(MEDIA_EXT))
        p = filedialog.askopenfilename(title="Pick a file to preview", initialdir=out_var.get(),
                                       filetypes=[("Audio/Video", exts), ("All files", "*.*")])
        if p:
            add_outputs([p])

    log_txt = tk.Text(frm, height=7, state="disabled", font=("Consolas", 9), bg="#111", fg="#ddd")
    log_txt.grid(row=10, column=0, columnspan=4, sticky="nsew", **pad)
    frm.rowconfigure(10, weight=1)

    btns = ttk.Frame(frm)
    btns.grid(row=11, column=0, columnspan=4, sticky="e", **pad)
    ttk.Button(btns, text="Preview file...", command=pick_preview).pack(side="left", padx=4)

    def open_folder():
        p = out_var.get()
        os.makedirs(p, exist_ok=True)
        if os.name == "nt":
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])

    ttk.Button(btns, text="Open folder", command=open_folder).pack(side="left", padx=4)
    go_btn = ttk.Button(btns, text="Start")
    go_btn.pack(side="left", padx=4)

    def worker(items, kw, video_mode=False):
        try:
            fn = dump_video if video_mode else dump_audio
            files = fn(items, log=lambda m: q.put(("log", m)),
                       progress=lambda p, t: q.put(("prog", (p, t))), **kw)
            q.put(("done", files))
        except Exception as ex:  # noqa: BLE001
            q.put(("err", explain_cookie_error(str(ex))))

    def start():
        items = [ln.strip() for ln in urls_txt.get("1.0", "end").splitlines() if ln.strip()]
        if not items:
            messagebox.showwarning(APP_NAME, "Paste a YouTube URL or add a local file.")
            return
        try:
            parse_time(start_var.get()); parse_time(end_var.get())
            for ln in items:
                parse_input_line(ln)
            timeline = parse_timeline(timeline_txt.get("1.0", "end"))
            padsec = float(padsec_var.get() or 0)
        except ValueError as ex:
            messagebox.showwarning(APP_NAME, f"{ex}\n\nTimes look like 90, 1:30 or 1:02:03; ranges like 6:57-7:02.")
            return
        try:
            gap = float(gap_var.get() or 0)
        except ValueError:
            messagebox.showwarning(APP_NAME, "Gap must be a number of seconds, e.g. 0.5")
            return
        video_mode = mode_var.get() == "video"
        if not video_mode and not has_filter and not messagebox.askyesno(
                APP_NAME, "The BGM filter is not installed, so the output will contain the background "
                          "music/sound.\n\nContinue anyway?"):
            return
        save_settings(snapshot())
        go_btn.state(["disabled"])
        bar["value"] = 0
        if video_mode:
            kw = dict(out_dir=out_var.get(), resolution=res_var.get(),
                      start=start_var.get() or None, end=end_var.get() or None, playlist=pl_var.get(),
                      combine=combine_var.get(), gap=gap, keep_clips=keepclips_var.get(),
                      timeline=timeline, pad=padsec, **cookie_kw())
        else:
            kw = dict(out_dir=out_var.get(), fmt=fmt_var.get(), bitrate=br_var.get(),
                      start=start_var.get() or None, end=end_var.get() or None, playlist=pl_var.get(),
                      voice_only=has_filter and voice_var.get(), quality=qual_var.get(),
                      keep_original=keep_var.get(), combine=combine_var.get(), gap=gap,
                      keep_clips=keepclips_var.get(), timeline=timeline, pad=padsec,
                      denoise=denoise_var.get() and roformer_available(), **cookie_kw())
        threading.Thread(target=worker, args=(items, kw, video_mode), daemon=True).start()

    go_btn.configure(command=start)
    sync_mode()

    # ================================================================== YouTube tab
    acct = ttk.LabelFrame(yt_tab, text="YouTube account  (used for searching AND downloading)", padding=(8, 6))
    acct.pack(fill="x")
    acct.columnconfigure(2, weight=1)
    ttk.Radiobutton(acct, text="Not signed in", variable=cookies_mode_var, value="none").grid(row=0, column=0, sticky="w")
    ttk.Radiobutton(acct, text="Sign in with the cookies of my browser:", variable=cookies_mode_var,
                    value="browser").grid(row=1, column=0, sticky="w")
    ttk.Combobox(acct, textvariable=cookies_browser_var, values=COOKIE_BROWSERS, width=18).grid(row=1, column=1, sticky="w", padx=4)
    ttk.Label(acct, foreground="gray", text="(logged in to youtube.com there; ':Profile 2' = other profile)").grid(row=1, column=2, columnspan=2, sticky="w")
    ttk.Label(acct, foreground="gray", text="Chrome/Edge on Windows lock and encrypt their cookies, so reading them usually fails "
              "('Could not copy Chrome cookie database'). Firefox works; for Chrome use the sign-in window "
              "below, or export a cookies.txt with a browser extension (e.g. 'Get cookies.txt LOCALLY'):", wraplength=900, justify="left").grid(row=2, column=0, columnspan=4, sticky="w", pady=(2, 4))
    ttk.Radiobutton(acct, text="Sign in with an exported cookies.txt file:", variable=cookies_mode_var,
                    value="file").grid(row=3, column=0, sticky="w")
    ttk.Entry(acct, textvariable=cookies_file_var).grid(row=3, column=1, columnspan=2, sticky="ew", padx=4)

    def pick_cookies():
        p = filedialog.askopenfilename(title="Pick cookies.txt", filetypes=[("cookies.txt", "*.txt"), ("All files", "*.*")])
        if p:
            cookies_file_var.set(p)
            cookies_mode_var.set("file")
    ttk.Button(acct, text="Browse...", command=pick_cookies).grid(row=3, column=3, sticky="e")
    login_var = tk.StringVar(value="")
    login_btn = ttk.Button(acct, text="Test sign-in")
    login_btn.grid(row=5, column=0, sticky="w", pady=(6, 0))
    ttk.Label(acct, textvariable=login_var, wraplength=900, justify="left").grid(row=5, column=1, columnspan=3, sticky="w", pady=(6, 0))

    # sign-in window: log in once in an app-owned Chrome/Edge window, then pull its cookies live
    sw = ttk.Frame(acct)
    sw.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
    ttk.Label(sw, text="Or sign in inside a dedicated browser window (works with Chrome/Edge):").pack(side="left")
    open_sw_btn = ttk.Button(sw, text="1. Open sign-in window")
    open_sw_btn.pack(side="left", padx=(8, 4))
    save_sw_btn = ttk.Button(sw, text="2. Save sign-in")
    save_sw_btn.pack(side="left", padx=4)

    def open_signin():
        def work():
            try:
                msg = open_signin_window()
            except Exception as ex:  # noqa: BLE001
                msg = "FAILED: " + str(ex)
            q.put(("call", lambda: (login_var.set(msg), log("Sign-in window: " + msg))))
        threading.Thread(target=work, daemon=True).start()

    def save_signin():
        def work():
            try:
                msg = save_signin_cookies()
                ok = True
            except Exception as ex:  # noqa: BLE001
                msg, ok = str(ex), False
            def show():
                login_var.set(("" if ok else "FAILED: ") + msg)
                log("Sign-in window: " + msg)
                if ok:
                    cookies_file_var.set(SIGNIN_COOKIES_FILE)
                    cookies_mode_var.set("file")
                    test_login()
            q.put(("call", show))
        threading.Thread(target=work, daemon=True).start()
    open_sw_btn.configure(command=open_signin)
    save_sw_btn.configure(command=save_signin)

    def test_login():
        if cookies_mode_var.get() == "none":
            login_var.set("pick a sign-in method first")
            return
        login_btn.state(["disabled"])
        login_var.set("checking...")
        kw = cookie_kw()

        def work():
            try:
                msg = youtube_check_login(**kw)
                ok = True
            except Exception as ex:  # noqa: BLE001
                msg, ok = explain_cookie_error(str(ex).replace("ERROR: ", ""))[-300:], False
            def show():
                login_btn.state(["!disabled"])
                login_var.set(("OK: " if ok else "FAILED: ") + msg)
                log(("YouTube sign-in OK: " if ok else "YouTube sign-in failed: ") + msg)
            q.put(("call", show))
        threading.Thread(target=work, daemon=True).start()
    login_btn.configure(command=test_login)

    srch = ttk.LabelFrame(yt_tab, text="Search YouTube", padding=(8, 6))
    srch.pack(fill="both", expand=True, pady=(8, 0))
    srow = ttk.Frame(srch)
    srow.pack(fill="x")
    query_var = tk.StringVar(value=saved.get("query", ""))
    q_entry = ttk.Entry(srow, textvariable=query_var)
    q_entry.pack(side="left", fill="x", expand=True)
    ttk.Label(srow, text="results").pack(side="left", padx=(10, 4))
    limit_var = tk.StringVar(value=str(saved.get("search_limit", "20")))
    ttk.Combobox(srow, textvariable=limit_var, values=["10", "20", "30", "50"], width=4, state="readonly").pack(side="left")
    search_btn = ttk.Button(srow, text="Search")
    search_btn.pack(side="left", padx=(10, 0))
    cols = ("title", "channel", "duration", "views")
    tree_frame = ttk.Frame(srch)
    tree_frame.pack(fill="both", expand=True, pady=6)
    tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="extended")
    for c, txt, w, anchor in (("title", "Title", 520, "w"), ("channel", "Channel", 200, "w"),
                              ("duration", "Length", 80, "e"), ("views", "Views", 110, "e")):
        tree.heading(c, text=txt)
        tree.column(c, width=w, anchor=anchor, stretch=(c == "title"))
    tsb = ttk.Scrollbar(tree_frame, command=tree.yview)
    tree.configure(yscrollcommand=tsb.set)
    tsb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)
    results: list[dict] = []
    search_status = tk.StringVar(value="Type what you are looking for and press Enter. Select rows, then add them to the download list.")
    brow = ttk.Frame(srch)
    brow.pack(fill="x")

    def do_search(*_):
        query = query_var.get().strip()
        if not query:
            return
        search_btn.state(["disabled"])
        search_status.set(f"Searching YouTube for '{query}'...")
        kw = cookie_kw()
        try:
            limit = int(limit_var.get())
        except ValueError:
            limit = 20

        def work():
            try:
                res = youtube_search(query, limit, **kw)
                err = None
            except Exception as ex:  # noqa: BLE001
                res, err = [], explain_cookie_error(str(ex).replace("ERROR: ", ""))[-400:]
            def show():
                search_btn.state(["!disabled"])
                results.clear()
                results.extend(res)
                tree.delete(*tree.get_children())
                for r in res:
                    tree.insert("", "end", values=(r["title"], r["channel"], r["duration"],
                                                   f"{r['views']:,}" if r["views"] else ""))
                if err:
                    search_status.set("Search failed: " + err)
                    log("YouTube search failed: " + err)
                else:
                    search_status.set(f"{len(res)} result(s) for '{query}'. Select rows and add them, or double-click one."
                                      + (f"  [{COOKIE_NOTE['text'][:90]}...]" if COOKIE_NOTE["text"] else ""))
            q.put(("call", show))
        threading.Thread(target=work, daemon=True).start()
    search_btn.configure(command=do_search)
    q_entry.bind("<Return>", do_search)

    def selected_results():
        return [results[tree.index(i)] for i in tree.selection() if tree.index(i) < len(results)]

    def add_selected(*_):
        sel = selected_results()
        if not sel:
            search_status.set("Select one or more results first.")
            return
        add_lines([r["url"] for r in sel])
        search_status.set(f"Added {len(sel)} video(s) to the download list.")
        nb.select(frm)

    def open_selected_web():
        import webbrowser
        for r in selected_results()[:5]:
            webbrowser.open(r["url"])
    ttk.Button(brow, text="Open in browser", command=open_selected_web).pack(side="right", padx=4)
    ttk.Button(brow, text="Add selected to download list  >>", command=add_selected).pack(side="right", padx=4)
    ttk.Label(brow, textvariable=search_status, foreground="gray").pack(side="left", fill="x", expand=True)
    tree.bind("<Double-Button-1>", add_selected)
    for v in (query_var, limit_var):
        v.trace_add("write", autosave)

    def log(msg):
        log_txt.configure(state="normal")
        log_txt.insert("end", msg + "\n")
        log_txt.see("end")
        log_txt.configure(state="disabled")

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    log(val)
                elif kind == "call":
                    val()
                elif kind == "prog":
                    bar["value"] = val[0]
                    status_var.set(val[1])
                elif kind == "done":
                    go_btn.state(["!disabled"])
                    status_var.set(f"Done - {len(val)} file(s) saved")
                    add_outputs(val)
                elif kind == "err":
                    go_btn.state(["!disabled"])
                    status_var.set("Failed")
                    log("ERROR: " + val)
                    messagebox.showerror(APP_NAME, val[:800])
        except queue.Empty:
            pass
        root.after(100, poll)

    poll()
    if not has_filter:
        status_var.set("BGM filter NOT installed - click 'Install BGM filter...' to enable voice-only output")
    log(f"ffmpeg: {find_ffmpeg() or 'NOT FOUND'}")
    if cookies_mode_var.get() == "browser":
        log(f"YouTube account: cookies from {cookies_browser_var.get()} (YouTube tab)")
    elif cookies_mode_var.get() == "file":
        log(f"YouTube account: cookies file {cookies_file_var.get()} (YouTube tab)")
    log("BGM filter: " + (", ".join(n for n, ok in (("BS-RoFormer", roformer_available()), ("Demucs", demucs_available())) if ok)
                          or "not installed (run install_vocals.bat)"))
    root.mainloop()


# --------------------------------------------------------------------------- CLI
def main():
    if len(sys.argv) == 1:
        run_gui()
        return
    ap = argparse.ArgumentParser(prog=APP_NAME,
                                 description="Dump audio from YouTube videos; optionally filter out BGM (voice only).")
    ap.add_argument("inputs", nargs="*",
                    help="YouTube URLs and/or local files, each optionally followed by time ranges in the same "
                         "argument, e.g. \"URL 6:57-7:02 12:10-12:30\"")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT)
    ap.add_argument("--video", action="store_true", help="video mode: download/cut MP4 instead of audio")
    ap.add_argument("--res", default="best", choices=RESOLUTIONS, help="video mode: max height (default best)")
    ap.add_argument("-f", "--format", default="mp3", choices=FORMATS)
    ap.add_argument("-b", "--bitrate", default="192")
    ap.add_argument("--start", help="clip start, e.g. 1:30")
    ap.add_argument("--end", help="clip end, e.g. 2:45")
    ap.add_argument("--playlist", action="store_true", help="download every video in a playlist URL")
    ap.add_argument("--filter-bgm", "--voice-only", "--vocals-only", dest="voice", action="store_true",
                    help="remove background music, keep voice only (needs Demucs)")
    ap.add_argument("--quality", default=default_quality(), choices=list(QUALITY_MODELS),
                    help="BGM filter engine: best=BS-RoFormer (default, cleanest), fast=Demucs (more bleed)")
    ap.add_argument("--keep-original", action="store_true", help="with --filter-bgm, also save the original mix")
    ap.add_argument("--denoise", action="store_true", help="extra clean-up stage after the BGM filter (removes ambience/noise)")
    ap.add_argument("--timeline", help="ranges applied to every input, e.g. \"6:57-7:02, 12:10-12:30\" "
                                       "(or @file.txt with one range per line; '#2 1:00-1:30' targets input 2)")
    ap.add_argument("--pad", type=float, default=0.3, help="seconds added before and after each range (default 0.3)")
    ap.add_argument("--combine", action="store_true", help="join all clips into ONE file, in input order")
    ap.add_argument("--gap", type=float, default=0.5, help="seconds of silence between combined clips (default 0.5)")
    ap.add_argument("--keep-clips", action="store_true", help="with --combine, also save each clip separately")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER[:PROFILE]",
                    help="sign in with your YouTube account via that browser's cookies (firefox, chrome, edge, ...)")
    ap.add_argument("--cookies", metavar="FILE", help="sign in with an exported cookies.txt file")
    ap.add_argument("--search", metavar="QUERY", help="search YouTube and print the results instead of downloading")
    ap.add_argument("--search-limit", type=int, default=20)
    a = ap.parse_args()
    if not a.inputs and not a.search:
        ap.error("give at least one URL/file, or --search QUERY")
    cookies = dict(cookies_browser=a.cookies_from_browser, cookies_file=a.cookies)

    if a.search:
        for i, r in enumerate(youtube_search(a.search, a.search_limit, **cookies), 1):
            print(f"{i:3d}. {r['title']}  [{r['channel']}, {r['duration']}, {r['views']:,} views]\n     {r['url']}")
        return

    last = [-1]
    def prog(p, t):
        if int(p) // 10 != last[0] or p >= 100:
            last[0] = int(p) // 10
            print("  " + t, flush=True)

    tl_text = a.timeline or ""
    if tl_text.startswith("@"):
        with open(tl_text[1:], encoding="utf-8") as f:
            tl_text = f.read()
    timeline = parse_timeline(tl_text.replace(",", "\n") if "\n" not in tl_text else tl_text)

    if a.video:
        files = dump_video(a.inputs, a.out, a.res, a.start, a.end, a.playlist, combine=a.combine, gap=a.gap,
                           keep_clips=a.keep_clips, timeline=timeline, pad=a.pad, progress=prog, **cookies)
        print("\nSaved:")
        for f in files:
            print("  " + f)
        return
    files = dump_audio(a.inputs, a.out, a.format, a.bitrate, a.start, a.end, a.playlist,
                       voice_only=a.voice, quality=a.quality, keep_original=a.keep_original,
                       combine=a.combine, gap=a.gap, keep_clips=a.keep_clips,
                       timeline=timeline, pad=a.pad, denoise=a.denoise, progress=prog, **cookies)
    print("\nSaved:")
    for f in files:
        print("  " + f)


if __name__ == "__main__":
    main()
