"""
Download logic for YT Downloader (no Kivy / Android code in here).

Kept separate from the UI so it can be unit tested on a PC (see tests/).
"""

import os
import re
import shutil
import threading
from types import SimpleNamespace

APP_FOLDER = "YTDownloader"
URL_RE = re.compile(r"https?://\S+")

# --------------------------------------------------------------------------
# Format presets.  ("video", max height or None, container)
#                  ("audio", codec or None, bitrate or None)
# --------------------------------------------------------------------------
PRESETS = {
    "Best quality video (MP4)": ("video", None, "mp4"),
    "2160p 4K video (MP4)": ("video", 2160, "mp4"),
    "1440p 2K video (MP4)": ("video", 1440, "mp4"),
    "1080p Full HD video (MP4)": ("video", 1080, "mp4"),
    "720p HD video (MP4)": ("video", 720, "mp4"),
    "480p video (MP4)": ("video", 480, "mp4"),
    "360p video (MP4)": ("video", 360, "mp4"),
    "240p video (MP4)": ("video", 240, "mp4"),
    "144p video (MP4)": ("video", 144, "mp4"),
    "Best quality video (MKV)": ("video", None, "mkv"),
    "Best quality video (WEBM)": ("video", None, "webm"),
    "MP3 audio 320 kbps": ("audio", "mp3", "320"),
    "MP3 audio 192 kbps": ("audio", "mp3", "192"),
    "MP3 audio 128 kbps": ("audio", "mp3", "128"),
    "M4A audio (AAC)": ("audio", "m4a", None),
    "OPUS audio": ("audio", "opus", None),
    "FLAC audio (lossless)": ("audio", "flac", None),
    "WAV audio": ("audio", "wav", None),
    "Original audio (no conversion)": ("audio", None, None),
}
PRESET_NAMES = list(PRESETS)
FORMATS_HEADER = "----- All formats of this video -----"


# --------------------------------------------------------------------------
# yt-dlp is imported lazily (it is big and slow to import on a phone)
# --------------------------------------------------------------------------
_YTDLP = None
_YTDLP_LOCK = threading.Lock()


def load_ytdlp():
    global _YTDLP
    with _YTDLP_LOCK:
        if _YTDLP is None:
            import yt_dlp
            from yt_dlp.utils import DownloadCancelled, DownloadError

            class UserCancelled(DownloadCancelled):
                msg = "Cancelled by user"

            _YTDLP = SimpleNamespace(
                YoutubeDL=yt_dlp.YoutubeDL,
                DownloadError=DownloadError,
                Cancelled=DownloadCancelled,
                UserCancelled=UserCancelled,
                parse_options=getattr(yt_dlp, "parse_options", None),
                version=yt_dlp.version.__version__,
            )
    return _YTDLP


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def fmt_eta(sec):
    sec = int(sec or 0)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def move_file(src, dst):
    """Move without copying file permissions (chmod fails on Android shared storage)."""
    try:
        os.rename(src, dst)
    except OSError:
        shutil.copyfile(src, dst)
        os.remove(src)


def extract_url(text):
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(").,;'\"") if m else None


def clean_error(err):
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(err)).strip()
    text = re.sub(r"^(ERROR:\s*)+", "", text)
    text = re.sub(r"^\[[\w:]+\]\s*[\w-]+:\s*", "", text)  # "[youtube] abc123: "
    low = text.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube asked for a bot check. Try again later or switch network (Wi-Fi / mobile data)."
    if "confirm your age" in low or "age-restricted" in low or "inappropriate for some users" in low:
        return "This video is age-restricted and needs a signed-in account. It cannot be downloaded here."
    if "requested format is not available" in low:
        return "This quality is not available for this video. Try another option or tap 'All formats'."
    if "private video" in low:
        return "This video is private."
    if "drm" in low:
        return "This video is DRM protected and cannot be downloaded."
    if "live event will begin" in low or "premieres in" in low:
        return "This live stream / premiere has not started yet."
    if "video unavailable" in low or "has been removed" in low:
        return "This video is unavailable (removed, blocked in your country or private)."
    if "unsupported url" in low:
        return "This link is not supported."
    if "http error 403" in low:
        return "The site refused the download (HTTP 403). Try again, or try another format."
    if "http error 429" in low or "too many requests" in low:
        return "Too many requests. Wait a few minutes and try again."
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "This option needs FFmpeg, which is missing. Pick 'Original audio' or a 360p video."
    if "timed out" in low or "unable to download webpage" in low or "network is unreachable" in low \
            or "name resolution" in low or "connection" in low:
        return "Network problem. Check your internet and try again."
    if "no space left" in low:
        return "Your phone storage is full."
    return text[:300] or "Unknown error"


def finished_files(info, work_dir):
    """Final files produced by yt-dlp (after merging / converting)."""
    paths = []

    def collect(item):
        if not isinstance(item, dict):
            return
        for dl in item.get("requested_downloads") or []:
            p = dl.get("filepath")
            if p and os.path.isfile(p) and p not in paths:
                paths.append(p)
        for entry in item.get("entries") or []:
            collect(entry)

    collect(info or {})
    if not paths:  # older yt-dlp or unusual extractors: just look in the work folder
        for name in sorted(os.listdir(work_dir)):
            if name.startswith(".") or name.endswith((".part", ".ytdl", ".temp", ".js")):
                continue
            p = os.path.join(work_dir, name)
            if os.path.isfile(p):
                paths.append(p)
    return paths


# --------------------------------------------------------------------------
# "All formats" list for one video
# --------------------------------------------------------------------------
_VCODECS = {"avc1": "H.264", "avc3": "H.264", "h264": "H.264", "vp09": "VP9", "vp9": "VP9",
            "vp8": "VP8", "av01": "AV1", "hev1": "HEVC", "hvc1": "HEVC", "h265": "HEVC"}
_ACODECS = {"mp4a": "AAC", "aac": "AAC", "opus": "Opus", "vorbis": "Vorbis", "mp3": "MP3",
            "ac-3": "AC3", "ec-3": "EAC3", "flac": "FLAC"}


def _codec_name(codec, table):
    if not codec or codec in ("none", "unknown"):
        return ""
    key = codec.split(".")[0].lower()
    return table.get(key, key.upper())


def format_choices(info):
    """List of (label, format dict) for every downloadable format, best first."""
    videos, audios = [], []
    for f in (info or {}).get("formats") or []:
        fid = f.get("format_id")
        if not fid:
            continue
        note = (f.get("format_note") or "").lower()
        if f.get("ext") == "mhtml" or f.get("protocol") == "mhtml" or "storyboard" in note:
            continue
        vcodec = f.get("vcodec") or "unknown"  # missing info = assume present
        acodec = f.get("acodec") or "unknown"
        has_v = vcodec != "none"
        has_a = acodec != "none"
        if not has_v and not has_a:
            continue
        size = f.get("filesize") or f.get("filesize_approx")
        size_txt = f" - {'~' if not f.get('filesize') else ''}{fmt_size(size)}" if size else ""
        ext = f.get("ext") or "?"
        choice = {"id": fid, "ext": ext, "video": has_v, "audio": has_a}
        if has_v:
            h = f.get("height")
            fps = f.get("fps")
            res = f"{h}p" if h else (f.get("resolution") or f.get("format_note") or "video")
            if h and fps and fps > 30:
                res += f"{int(round(fps))}"
            parts = [res, ext, _codec_name(vcodec, _VCODECS)]
            if not has_a:
                parts.append("(+ best audio)")
            if f.get("dynamic_range") and f["dynamic_range"] != "SDR":
                parts.append(f["dynamic_range"])
            label = " ".join(p for p in parts if p) + size_txt + f" [{fid}]"
            videos.append(((h or 0, fps or 0, f.get("tbr") or 0), label, choice))
        else:
            abr = f.get("abr")
            parts = ["Audio", f"{int(round(abr))}k" if abr else "", ext, _codec_name(acodec, _ACODECS)]
            lang = f.get("language")
            if lang:
                parts.append(lang)
            if "original" in note:
                parts.append("(original)")
            elif "drc" in note or "-drc" in fid:
                parts.append("(DRC)")
            label = " ".join(p for p in parts if p) + size_txt + f" [{fid}]"
            audios.append(((abr or f.get("tbr") or 0,), label, choice))
    videos.sort(key=lambda t: t[0], reverse=True)
    audios.sort(key=lambda t: t[0], reverse=True)
    return [(label, choice) for _, label, choice in videos + audios]


# --------------------------------------------------------------------------
# yt-dlp options
# --------------------------------------------------------------------------
def js_runtime_opts(js, parse_options):
    """Turn ('quickjs', '/path') into yt-dlp options using yt-dlp's own CLI parser."""
    if not js or parse_options is None:
        return {}
    runtime, path = js
    base = ["--no-js-runtimes", "--js-runtimes", f"{runtime}:{path}"]
    # remote component = fetch the solver script from GitHub if yt-dlp-ejs is missing/outdated
    for extra in (["--remote-components", "ejs:github"], []):
        try:
            parsed = parse_options(base + extra).ydl_opts
        except (Exception, SystemExit):  # older yt-dlp without these flags
            continue
        return {k: parsed[k] for k in ("js_runtimes", "remote_components") if parsed.get(k)}
    return {}


def base_opts(cache_dir, logger, ffmpeg=None, js=None, parse_options=None):
    """Options shared by 'get formats' and 'download'."""
    opts = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": logger,
        "cachedir": cache_dir,
        "noplaylist": True,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
    }
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    opts.update(js_runtime_opts(js, parse_options))
    return opts


def build_ydl_opts(choice, out_dir, *, cache_dir, logger, ffmpeg=None, js=None, parse_options=None,
                   playlist=False, progress_hook=None, pp_hook=None):
    """
    choice: a key of PRESETS, or a format dict from format_choices().
    Returns (yt-dlp options, note for the user).
    """
    note = ""
    opts = base_opts(cache_dir, logger, ffmpeg, js, parse_options)
    name = "%(title).90B [%(id)s].%(ext)s"
    if playlist:
        name = "%(playlist_index&{} - |)s" + name
        opts["ignoreerrors"] = "only_download"  # skip broken items, keep going
    opts.update({
        "outtmpl": os.path.join(out_dir, name),
        "noplaylist": not playlist,
        "windowsfilenames": True,  # Android storage rejects : ? " | etc.
        "overwrites": True,
        "progress_hooks": [progress_hook] if progress_hook else [],
        "postprocessor_hooks": [pp_hook] if pp_hook else [],
        # speed + reliability
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10 * 1024 * 1024,
    })

    if isinstance(choice, dict):  # one exact format picked from "All formats"
        fid = choice["id"]
        if choice.get("video") and not choice.get("audio"):
            if ffmpeg:
                pref_audio = "m4a" if choice.get("ext") == "mp4" else "webm"
                opts["format"] = f"{fid}+ba[ext={pref_audio}]/{fid}+ba/{fid}"
                opts["merge_output_format"] = "mp4/mkv" if choice.get("ext") == "mp4" else "webm/mkv"
            else:
                opts["format"] = fid
                note = "FFmpeg not found: this format has no sound (video and audio can't be merged)."
        else:
            opts["format"] = fid
        return opts, note

    if choice not in PRESETS:
        raise ValueError(f"Unknown format option: {choice}")
    kind, a, b = PRESETS[choice]

    if kind == "video":
        lim = f"[height<={a}]" if a else ""
        container = b
        if ffmpeg:
            if container == "mp4":
                # nothing small enough -> take the smallest there is (not the biggest)
                opts["format"] = f"bv*{lim}+ba/b{lim}" + ("/wv*+ba/w" if lim else "")
                # highest resolution first; at equal resolution prefer H.264 + AAC (plays everywhere)
                opts["format_sort"] = ["res", "fps", "vcodec:h264", "acodec:aac"]
                opts["merge_output_format"] = "mp4"
            elif container == "webm":
                opts["format"] = f"bv*{lim}[ext=webm]+ba[ext=webm]/bv*{lim}+ba/b{lim}"
                opts["merge_output_format"] = "webm/mkv"
            else:  # mkv: best quality in any codec (VP9 / AV1 / Opus)
                opts["format"] = f"bv*{lim}+ba/b{lim}"
                opts["merge_output_format"] = "mkv"
        else:
            if container == "mp4":
                opts["format"] = (f"b{lim}[ext=mp4]/b{lim}/w[ext=mp4]/w" if lim else "b[ext=mp4]/b")
            else:
                opts["format"] = f"b{lim}/w" if lim else "b"
            note = "FFmpeg not found: saved the best single-file stream (YouTube gives these only up to ~360p)."
        return opts, note

    codec, quality = a, b
    if codec is None:  # original audio, no conversion
        opts["format"] = "ba/b"
        return opts, note
    if ffmpeg:
        source = {"m4a": "ba[ext=m4a]/ba/b", "opus": "ba[acodec^=opus]/ba/b"}.get(codec, "ba/b")
        opts["format"] = source
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
        if quality:
            pp["preferredquality"] = quality
        opts["postprocessors"] = [pp]
        if codec in ("mp3", "m4a", "flac", "opus"):  # title/artist tags for music players
            opts["postprocessors"].append({"key": "FFmpegMetadata", "add_metadata": True})
    else:
        if codec == "opus":
            opts["format"] = "ba[acodec^=opus]/ba"
        else:
            opts["format"] = "ba[ext=m4a]/ba"
            if codec != "m4a":
                note = f"FFmpeg not found: {codec.upper()} needs FFmpeg, so the audio was saved as M4A."
    return opts, note


class YdlLogger:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.warnings.append(msg)

    def error(self, msg):
        self.errors.append(msg)
