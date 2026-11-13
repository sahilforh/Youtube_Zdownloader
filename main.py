"""
YT Downloader - Kivy + yt-dlp app for Android (Buildozer / GitHub Actions).

- Paste (or Share) a link, pick a format, tap Download.
- yt-dlp runs in a background thread, so the UI never freezes.
- Files are downloaded to a private work folder first (fast, no storage
  permission problems), then saved to the phone's public Download/YTDownloader.

Helper programs (both are optional, both are auto-detected):
- FFmpeg  -> needed for 720p and above (YouTube sends video and audio
             separately and they must be merged) and for MP3/AAC conversion.
             APK: ship it as libs/arm64-v8a/libffmpeg.so
             Termux / PC: anything named "ffmpeg" on PATH.
- JS runtime -> yt-dlp needs one for full YouTube support.
             APK: ship QuickJS as libs/arm64-v8a/libqjs.so (the GitHub
             workflow builds it). Termux / PC: deno, node or qjs on PATH.
Without FFmpeg the app still works: video up to the best single-file stream
(usually 360p) and M4A/AAC audio.
"""

import os
import re
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace

from kivy.app import App
from kivy.clock import mainthread
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.properties import BooleanProperty, ListProperty, NumericProperty, StringProperty
from kivy.utils import platform

IS_ANDROID = platform == "android"
APP_FOLDER = "YTDownloader"

VIDEO_CHOICES = {
    "Best Video (MP4)": None,
    "2160p (4K)": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}
AUDIO_CHOICES = {
    "MP3 Audio": "mp3",
    "AAC Audio": "aac",
    "M4A Audio": "m4a",
}
FORMAT_CHOICES = list(VIDEO_CHOICES) + list(AUDIO_CHOICES)
URL_RE = re.compile(r"https?://\S+")


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


def clean_error(err):
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(err)).strip()
    text = re.sub(r"^(ERROR:\s*)+", "", text)
    low = text.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube asked for a bot check. Try again later or switch network (Wi-Fi / mobile data)."
    if "requested format is not available" in low:
        return "This quality is not available for this video. Try another option."
    if "private video" in low:
        return "This video is private."
    if "unsupported url" in low:
        return "This link is not supported."
    if "timed out" in low or "unable to download webpage" in low or "network" in low:
        return "Network problem. Check your internet and try again."
    return text[:300] or "Unknown error"


def finished_files(info, work_dir):
    """Final files produced by yt-dlp (after merging / converting)."""
    paths = []
    for item in (info or {}).get("requested_downloads") or []:
        p = item.get("filepath")
        if p and os.path.isfile(p) and p not in paths:
            paths.append(p)
    if not paths:  # playlists or older yt-dlp: just look in the work folder
        for name in sorted(os.listdir(work_dir)):
            if name.startswith(".") or name.endswith((".part", ".ytdl", ".temp")):
                continue
            p = os.path.join(work_dir, name)
            if os.path.isfile(p):
                paths.append(p)
    return paths


# --------------------------------------------------------------------------
# Everything platform specific: folders, helper programs, saving files
# --------------------------------------------------------------------------
class Device:
    def __init__(self, data_dir):
        self.sdk = 0
        self.native_dir = None
        self.public_dir = None
        self.private_ext_dir = None
        self._java = None

        self.work_dir = os.path.join(data_dir, "work")
        self.cache_dir = os.path.join(data_dir, "ytdlp-cache")
        shutil.rmtree(self.work_dir, ignore_errors=True)  # leftovers from a crash
        os.makedirs(self.work_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

        if IS_ANDROID:
            os.environ["TMPDIR"] = self.work_dir  # Android has no /tmp
            tempfile.tempdir = None
            self._init_android()
        else:
            self.public_dir = self._desktop_download_dir()

        self.ffmpeg = self._find_ffmpeg()
        self.js = self._find_js()

    # ---- Android setup (must run on the main thread) ----
    def _init_android(self):
        from jnius import autoclass

        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        env = autoclass("android.os.Environment")
        self.sdk = autoclass("android.os.Build$VERSION").SDK_INT
        self.native_dir = activity.getApplicationInfo().nativeLibraryDir

        pub = env.getExternalStoragePublicDirectory(env.DIRECTORY_DOWNLOADS)
        self.public_dir = os.path.join(pub.getAbsolutePath(), APP_FOLDER)
        ext = activity.getExternalFilesDir(env.DIRECTORY_DOWNLOADS)
        self.private_ext_dir = ext.getAbsolutePath() if ext else None

        # Resolve Java classes now: app classes cannot be found from worker threads.
        self._java = SimpleNamespace(
            context=activity,
            resolver=activity.getContentResolver(),
            ContentValues=autoclass("android.content.ContentValues"),
            Integer=autoclass("java.lang.Integer"),
            MediaColumns=autoclass("android.provider.MediaStore$MediaColumns"),
            Downloads=autoclass("android.provider.MediaStore$Downloads") if self.sdk >= 29 else None,
            Scanner=autoclass("android.media.MediaScannerConnection"),
            downloads_name=env.DIRECTORY_DOWNLOADS,
        )

        if self.sdk < 29:  # Android 9 and older need the storage permission
            try:
                from android.permissions import Permission, request_permissions

                request_permissions([Permission.WRITE_EXTERNAL_STORAGE, Permission.READ_EXTERNAL_STORAGE])
            except Exception:
                pass

    @staticmethod
    def _desktop_download_dir():
        for base in ("/storage/emulated/0/Download", os.path.expanduser("~/storage/downloads")):
            if os.path.isdir(base) and os.access(base, os.W_OK):
                return os.path.join(base, APP_FOLDER)  # Termux with storage access
        return os.path.join(os.path.expanduser("~"), "Downloads", APP_FOLDER)

    # ---- helper programs ----
    def _bundled(self, name):
        if self.native_dir:
            p = os.path.join(self.native_dir, name)
            if os.path.isfile(p):
                return p
        return None

    def _find_ffmpeg(self):
        return self._bundled("libffmpeg.so") or shutil.which("ffmpeg")

    def _find_js(self):
        p = self._bundled("libqjs.so")
        if p:
            return ("quickjs", p)
        for runtime, exe in (("deno", "deno"), ("node", "node"), ("quickjs", "qjs")):
            p = shutil.which(exe)
            if p:
                return (runtime, p)
        return None

    # ---- saving ----
    def save(self, src):
        """Put a finished file into public Downloads. Returns where it went."""
        if IS_ANDROID and self.sdk >= 29:
            try:
                return self._save_mediastore(src)
            except Exception:
                pass  # fall back to plain file paths below

        targets = [self.public_dir]
        if IS_ANDROID and self.private_ext_dir:
            targets.append(self.private_ext_dir)
        last_err = None
        for folder in targets:
            if not folder:
                continue
            try:
                os.makedirs(folder, exist_ok=True)
                dst = unique_path(os.path.join(folder, os.path.basename(src)))
                move_file(src, dst)
                self._scan(dst)
                return dst
            except OSError as e:
                last_err = e
        raise OSError(f"Could not save file: {last_err}")

    def _save_mediastore(self, src):
        """Android 10+: create the file through MediaStore (no permission needed)."""
        import mimetypes

        j = self._java
        name = os.path.basename(src)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        rel = f"{j.downloads_name}/{APP_FOLDER}"

        values = j.ContentValues()
        values.put(j.MediaColumns.DISPLAY_NAME, name)
        values.put(j.MediaColumns.MIME_TYPE, mime)
        values.put(j.MediaColumns.RELATIVE_PATH, rel)
        values.put(j.MediaColumns.IS_PENDING, j.Integer(1))
        uri = j.resolver.insert(j.Downloads.EXTERNAL_CONTENT_URI, values)
        if uri is None:
            raise OSError("MediaStore insert failed")
        try:
            fd = j.resolver.openFileDescriptor(uri, "w").detachFd()
            with open(src, "rb") as fin, os.fdopen(fd, "wb") as fout:
                shutil.copyfileobj(fin, fout, 4 * 1024 * 1024)
            done = j.ContentValues()
            done.put(j.MediaColumns.IS_PENDING, j.Integer(0))
            j.resolver.update(uri, done, None, None)
        except Exception:
            try:
                j.resolver.delete(uri, None, None)
            except Exception:
                pass
            raise
        os.remove(src)
        return f"{rel}/{name}"

    def _scan(self, path):
        """Make the file show up in gallery / music apps (older Android)."""
        if IS_ANDROID and self._java:
            try:
                self._java.Scanner.scanFile(self._java.context, [path], None, None)
            except Exception:
                pass

    def describe(self, ytdlp_version=None):
        where = f"Download/{APP_FOLDER}" if IS_ANDROID else self.public_dir
        parts = [
            f"Saves to: {where}",
            f"FFmpeg: {'yes' if self.ffmpeg else 'no (max ~360p, no MP3)'}",
            f"JS runtime: {self.js[0] if self.js else 'none'}",
        ]
        if ytdlp_version:
            parts.append(f"yt-dlp {ytdlp_version}")
        return "\n".join(parts)


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


def build_ydl_opts(choice, out_dir, dev, progress_hook, pp_hook, logger, parse_options):
    """Returns (yt-dlp options, note for the user)."""
    ffmpeg = dev.ffmpeg
    note = ""
    opts = {
        "outtmpl": os.path.join(out_dir, "%(title).90B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "windowsfilenames": True,  # Android storage rejects : ? " | etc.
        "overwrites": True,
        "cachedir": dev.cache_dir,
        "quiet": True,
        "no_warnings": False,
        "logger": logger,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        # speed + reliability
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10 * 1024 * 1024,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
    }
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    opts.update(js_runtime_opts(dev.js, parse_options))

    if choice in VIDEO_CHOICES:
        h = VIDEO_CHOICES[choice]
        lim = f"[height<={h}]" if h else ""
        if ffmpeg:
            opts["format"] = f"bv*{lim}+ba/b{lim}" + ("/bv*+ba/b" if lim else "")
            # highest resolution first; at equal resolution prefer H.264 + AAC (plays everywhere)
            opts["format_sort"] = ["res", "fps", "vcodec:h264", "acodec:aac"]
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = (f"b{lim}[ext=mp4]/b{lim}/" if lim else "") + "b[ext=mp4]/b"
            note = "FFmpeg not found: saved the best single-file stream (YouTube gives these only up to ~360p)."
    else:
        codec = AUDIO_CHOICES[choice]
        if ffmpeg:
            opts["format"] = "ba[ext=m4a]/ba/b" if codec in ("aac", "m4a") else "ba/b"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": "192"},
            ]
            if codec == "mp3":  # title/artist tags for music players
                opts["postprocessors"].append({"key": "FFmpegMetadata", "add_metadata": True})
        else:
            opts["format"] = "ba[ext=m4a]/ba"
            if codec == "mp3":
                note = "FFmpeg not found: MP3 needs FFmpeg, so the audio was saved as M4A."
            elif codec == "aac":
                note = "Saved as .m4a (this file is AAC audio)."
    return opts, note


class YdlLogger:
    def __init__(self):
        self.warnings = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.warnings.append(msg)

    def error(self, msg):
        pass


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
KV = r"""
#:import dp kivy.metrics.dp

<SpinnerOption>:
    background_normal: ''
    background_color: 0.16, 0.17, 0.21, 1
    color: 0.95, 0.95, 0.97, 1
    font_size: '16sp'
    height: dp(48)

<Caption@Label>:
    size_hint_y: None
    height: dp(20)
    font_size: '13sp'
    color: 0.62, 0.64, 0.70, 1
    halign: 'left'
    valign: 'middle'
    text_size: self.size

<RoundButton@Button>:
    bg: 0.90, 0.16, 0.20, 1
    background_normal: ''
    background_down: ''
    background_disabled_normal: ''
    background_disabled_down: ''
    background_color: 0, 0, 0, 0
    color: 1, 1, 1, 1
    disabled_color: 0.75, 0.75, 0.78, 1
    bold: True
    font_size: '16sp'
    canvas.before:
        Color:
            rgba: (0.28, 0.29, 0.34, 1) if self.disabled else ((self.bg[0] * 0.8, self.bg[1] * 0.8, self.bg[2] * 0.8, 1) if self.state == 'down' else self.bg)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(14)]

<Card@BoxLayout>:
    orientation: 'vertical'
    padding: dp(16)
    spacing: dp(10)
    size_hint_y: None
    height: self.minimum_height
    canvas.before:
        Color:
            rgba: 0.11, 0.12, 0.15, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(18)]

<SlimBar@Widget>:
    value: 0
    size_hint_y: None
    height: dp(10)
    canvas:
        Color:
            rgba: 0.20, 0.21, 0.26, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(5)]
        Color:
            rgba: 0.15, 0.78, 0.40, 1
        RoundedRectangle:
            pos: self.pos
            size: (self.width * max(0, min(self.value, 100)) / 100.0, self.height)
            radius: [dp(5)]

ScrollView:
    do_scroll_x: False
    BoxLayout:
        orientation: 'vertical'
        size_hint_y: None
        height: self.minimum_height
        padding: dp(18), dp(26), dp(18), dp(18)
        spacing: dp(16)

        BoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: dp(66)
            Label:
                text: 'YT Downloader'
                font_size: '28sp'
                bold: True
                color: 1, 1, 1, 1
                halign: 'left'
                valign: 'middle'
                text_size: self.size
            Label:
                text: 'Fast video & audio downloads with yt-dlp'
                font_size: '14sp'
                color: 0.62, 0.64, 0.70, 1
                halign: 'left'
                valign: 'middle'
                text_size: self.size

        Card:
            Caption:
                text: 'Video link'
            BoxLayout:
                size_hint_y: None
                height: dp(50)
                spacing: dp(10)
                TextInput:
                    id: url
                    hint_text: 'Paste YouTube link here'
                    multiline: False
                    write_tab: False
                    font_size: '15sp'
                    padding: dp(12), (self.height - self.line_height) / 2
                    background_normal: ''
                    background_active: ''
                    background_disabled_normal: ''
                    background_color: 0.16, 0.17, 0.21, 1
                    foreground_color: 1, 1, 1, 1
                    disabled_foreground_color: 0.6, 0.6, 0.65, 1
                    hint_text_color: 0.50, 0.52, 0.58, 1
                    cursor_color: 0.90, 0.16, 0.20, 1
                    disabled: app.busy
                    on_text_validate: app.start_download()
                RoundButton:
                    text: 'Paste'
                    size_hint_x: None
                    width: dp(84)
                    bg: 0.25, 0.27, 0.34, 1
                    disabled: app.busy
                    on_release: app.paste_url()

            Caption:
                text: 'Format & quality'
            Spinner:
                id: fmt
                text: app.choices[0]
                values: app.choices
                size_hint_y: None
                height: dp(50)
                font_size: '16sp'
                background_normal: ''
                background_down: ''
                background_disabled_normal: ''
                background_color: 0.16, 0.17, 0.21, 1
                color: 1, 1, 1, 1
                disabled: app.busy
                canvas.after:
                    Color:
                        rgba: 0.70, 0.72, 0.78, 1
                    Triangle:
                        points: [self.right - dp(28), self.center_y + dp(4), self.right - dp(14), self.center_y + dp(4), self.right - dp(21), self.center_y - dp(5)]

        BoxLayout:
            size_hint_y: None
            height: dp(56)
            spacing: dp(10)
            RoundButton:
                text: 'Downloading...' if app.busy else 'Download'
                font_size: '18sp'
                disabled: app.busy
                on_release: app.start_download()
            RoundButton:
                text: 'Cancel'
                size_hint_x: None
                width: dp(100) if app.busy else 0
                opacity: 1 if app.busy else 0
                bg: 0.25, 0.27, 0.34, 1
                disabled: not app.busy
                on_release: app.cancel_download()

        Card:
            BoxLayout:
                size_hint_y: None
                height: dp(22)
                Caption:
                    text: 'Progress'
                Label:
                    text: '%d%%' % app.progress
                    font_size: '14sp'
                    bold: True
                    color: 1, 1, 1, 1
                    halign: 'right'
                    valign: 'middle'
                    text_size: self.size
            SlimBar:
                value: app.progress
            Label:
                text: app.status
                font_size: '14sp'
                color: (1, 0.45, 0.45, 1) if app.status_error else (0.86, 0.88, 0.92, 1)
                halign: 'left'
                size_hint_y: None
                text_size: self.width, None
                height: self.texture_size[1] + dp(4)

        Label:
            text: app.engine
            font_size: '12sp'
            color: 0.48, 0.50, 0.56, 1
            halign: 'left'
            size_hint_y: None
            text_size: self.width, None
            height: self.texture_size[1] + dp(4)
"""


class YTDownloaderApp(App):
    status = StringProperty("Paste a link, choose a format, tap Download.")
    status_error = BooleanProperty(False)
    progress = NumericProperty(0)
    busy = BooleanProperty(False)
    engine = StringProperty("")
    choices = ListProperty(FORMAT_CHOICES)

    def build(self):
        self.title = "YT Downloader"
        Window.clearcolor = (0.07, 0.075, 0.095, 1)
        Window.softinput_mode = "below_target"
        self.dev = Device(self.user_data_dir)
        self.engine = self.dev.describe()
        self._cancel = threading.Event()
        self._last_ui = 0.0
        return Builder.load_string(KV)

    def on_start(self):
        threading.Thread(target=self._preload, daemon=True).start()
        if IS_ANDROID:
            self._listen_for_shares()

    def on_pause(self):
        return True  # keep downloading when the app goes to background

    # ---- thread-safe UI update ----
    @mainthread
    def _ui(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)

    def _preload(self):
        try:
            y = load_ytdlp()
            self._ui(engine=self.dev.describe(y.version))
        except Exception as e:
            self._ui(status=f"yt-dlp could not load: {e}", status_error=True)

    # ---- share from YouTube app -> this app ----
    def _listen_for_shares(self):
        try:
            from android import activity
            from jnius import autoclass

            activity.bind(on_new_intent=self._on_intent)
            self._on_intent(autoclass("org.kivy.android.PythonActivity").mActivity.getIntent())
        except Exception:
            pass

    def _on_intent(self, intent):
        try:
            text = intent.getStringExtra("android.intent.extra.TEXT") if intent else None
        except Exception:
            return
        m = URL_RE.search(text or "")
        if m:
            self._set_url(m.group(0))

    @mainthread
    def _set_url(self, url):
        if not self.busy:
            self.root.ids.url.text = url
            self.status, self.status_error = "Link received. Choose a format and tap Download.", False

    # ---- buttons ----
    def paste_url(self):
        from kivy.core.clipboard import Clipboard

        text = (Clipboard.paste() or "").strip()
        m = URL_RE.search(text)
        self.root.ids.url.text = m.group(0) if m else text

    def start_download(self):
        if self.busy:
            return
        url = self.root.ids.url.text.strip()
        if not URL_RE.match(url):
            self.status, self.status_error = "Please paste a valid link (starting with https://).", True
            return
        choice = self.root.ids.fmt.text
        self._cancel.clear()
        self.busy, self.progress = True, 0
        self.status, self.status_error = "Starting...", False
        threading.Thread(target=self._worker, args=(url, choice), daemon=True).start()

    def cancel_download(self):
        if self.busy:
            self._cancel.set()
            self.status = "Cancelling..."

    # ---- background work ----
    def _worker(self, url, choice):
        y = None
        work = None
        logger = YdlLogger()
        try:
            self._ui(status="Loading engine...")
            y = load_ytdlp()
            self._ui(status="Getting video info...")
            work = tempfile.mkdtemp(prefix="job_", dir=self.dev.work_dir)
            opts, note = build_ydl_opts(
                choice, work, self.dev, self._progress_hook, self._pp_hook, logger, y.parse_options
            )
            with y.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)

            files = finished_files(info, work)
            if not files:
                raise RuntimeError("Download finished but no file was found.")
            self._ui(status="Saving to Downloads...", progress=100)
            total = sum(os.path.getsize(f) for f in files)
            saved = [self.dev.save(f) for f in files]

            msg = "Done! Saved:\n" + "\n".join(saved)
            if total:
                msg += f"\n({fmt_size(total)})"
            if note:
                msg += "\n" + note
            self._ui(status=msg, status_error=False, progress=100)
        except Exception as e:
            if self._cancel.is_set() or (y and isinstance(e, y.Cancelled)):
                self._ui(status="Download cancelled.", status_error=False, progress=0)
            else:
                msg = clean_error(e)
                if not self.dev.js and any("javascript runtime" in w.lower() for w in logger.warnings):
                    msg += "\n(No JS runtime found - some YouTube formats may be missing.)"
                self._ui(status=msg, status_error=True)
        finally:
            if work:
                shutil.rmtree(work, ignore_errors=True)
            self._ui(busy=False)
            if IS_ANDROID:
                try:
                    from jnius import detach

                    detach()  # required before a thread that used Java ends
                except Exception:
                    pass

    def _progress_hook(self, d):
        if self._cancel.is_set():
            raise load_ytdlp().UserCancelled()
        status = d.get("status")
        if status == "downloading":
            now = time.monotonic()
            if now - self._last_ui < 0.25:  # max ~4 UI updates per second
                return
            self._last_ui = now
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            info = d.get("info_dict") or {}
            kind = "audio" if info.get("vcodec") == "none" else "video"
            parts = [f"Downloading {kind}", fmt_size(done) + (f" / {fmt_size(total)}" if total else "")]
            if d.get("speed"):
                parts.append(f"{fmt_size(d['speed'])}/s")
            if d.get("eta") is not None:
                parts.append(f"ETA {fmt_eta(d['eta'])}")
            self._ui(progress=(done * 100.0 / total) if total else 0, status="  |  ".join(parts))
        elif status == "finished":
            self._ui(progress=100, status="Stream downloaded, processing...")

    def _pp_hook(self, d):
        if d.get("status") == "started":
            labels = {
                "Merger": "Merging video + audio...",
                "ExtractAudio": "Converting audio...",
                "Metadata": "Writing tags...",
            }
            self._ui(status=labels.get(d.get("postprocessor"), "Processing..."))


if __name__ == "__main__":
    YTDownloaderApp().run()
