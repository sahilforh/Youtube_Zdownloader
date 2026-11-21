"""
YT Downloader - Kivy + yt-dlp app for Android (built with Buildozer on GitHub Actions).

- Paste (or Share from the YouTube app) a link, pick a format, tap Download.
- Presets for every common format (MP4 4K..144p, MKV, WEBM, MP3, M4A, OPUS,
  FLAC, WAV) or tap "All formats" to list every format the video really has.
- Works with YouTube and the 1000+ other sites yt-dlp supports, playlists too.
- yt-dlp runs in a background thread, so the UI never freezes.
- Files are downloaded to a private work folder first (fast, no storage
  permission problems), then saved to the phone's public Download/YTDownloader.

Helper programs (both are optional, both are auto-detected):
- FFmpeg  -> needed for 720p and above (YouTube sends video and audio
             separately and they must be merged) and for MP3/FLAC/WAV.
             APK: shipped as libs/arm64-v8a/libffmpeg.so (built by the workflow)
             Termux / PC: anything named "ffmpeg" on PATH.
- JS runtime -> yt-dlp needs one for full YouTube support.
             APK: QuickJS shipped as libs/arm64-v8a/libqjs.so (built by the
             workflow). Termux / PC: deno, node or qjs on PATH.
Without FFmpeg the app still works: video up to the best single-file stream
(usually 360p) and M4A/OPUS audio.
"""

import os
import shutil
import tempfile
import threading
import time
import traceback
from types import SimpleNamespace

from kivy.app import App
from kivy.clock import mainthread
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.properties import BooleanProperty, ListProperty, NumericProperty, StringProperty
from kivy.utils import platform

from ytcore import (
    APP_FOLDER,
    FORMATS_HEADER,
    PRESET_NAMES,
    YdlLogger,
    base_opts,
    build_ydl_opts,
    clean_error,
    extract_url,
    finished_files,
    fmt_eta,
    fmt_size,
    format_choices,
    load_ytdlp,
    move_file,
    unique_path,
)

IS_ANDROID = platform == "android"


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
        self._wakelock = None

        self.log_file = os.path.join(data_dir, "last_error.txt")
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

        # Keep the CPU awake while downloading (screen may turn off).
        try:
            Context = autoclass("android.content.Context")
            PowerManager = autoclass("android.os.PowerManager")
            pm = activity.getSystemService(Context.POWER_SERVICE)
            self._wakelock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "YTDownloader:download")
            self._wakelock.setReferenceCounted(False)
        except Exception:
            self._wakelock = None

        if self.sdk < 29:  # Android 9 and older need the storage permission
            try:
                from android.permissions import Permission, request_permissions

                request_permissions([Permission.WRITE_EXTERNAL_STORAGE, Permission.READ_EXTERNAL_STORAGE])
            except Exception:
                pass

    def keep_awake(self, on):
        if not self._wakelock:
            return
        try:
            if on:
                self._wakelock.acquire(3 * 60 * 60 * 1000)  # auto-release after 3 hours
            elif self._wakelock.isHeld():
                self._wakelock.release()
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
            if os.path.isfile(p) and os.access(p, os.X_OK):
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
        mime = mimetypes.guess_type(name)[0] or {
            ".mkv": "video/x-matroska",
            ".webm": "video/webm",
            ".opus": "audio/ogg",
            ".m4a": "audio/mp4",
            ".flac": "audio/flac",
        }.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
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
            pfd = j.resolver.openFileDescriptor(uri, "w")
            if pfd is None:
                raise OSError("MediaStore could not open the file")
            fd = pfd.detachFd()
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

    def log_error(self, text):
        try:
            with open(self.log_file, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    def describe(self, ytdlp_version=None):
        where = f"Download/{APP_FOLDER}" if IS_ANDROID else self.public_dir
        parts = [
            f"Saves to: {where}",
            f"FFmpeg: {'yes' if self.ffmpeg else 'no (max ~360p, no MP3)'}",
            f"JS runtime: {self.js[0] if self.js else 'none (some YouTube formats may be missing)'}",
        ]
        if ytdlp_version:
            parts.append(f"yt-dlp {ytdlp_version}")
        return "\n".join(parts)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
KV = r"""
#:import dp kivy.metrics.dp

<SpinnerOption>:
    background_normal: ''
    background_color: 0.16, 0.17, 0.21, 1
    color: (0.55, 0.57, 0.63, 1) if self.text.startswith('-----') else (0.95, 0.95, 0.97, 1)
    font_size: '15sp'
    height: dp(48)
    shorten: True
    shorten_from: 'right'
    text_size: self.width - dp(20), None
    halign: 'left'

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
                text: 'Video & audio in every format - YouTube and 1000+ sites'
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
                    on_text: app.url_changed(self.text)
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
            BoxLayout:
                size_hint_y: None
                height: dp(50)
                spacing: dp(10)
                Spinner:
                    id: fmt
                    text: app.choices[0] if app.choices else ''
                    values: app.choices
                    font_size: '15sp'
                    shorten: True
                    shorten_from: 'right'
                    text_size: self.width - dp(46), None
                    halign: 'left'
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
                RoundButton:
                    text: 'All\nformats'
                    font_size: '13sp'
                    halign: 'center'
                    size_hint_x: None
                    width: dp(84)
                    bg: 0.25, 0.27, 0.34, 1
                    disabled: app.busy
                    on_release: app.fetch_formats()

            BoxLayout:
                size_hint_y: None
                height: dp(36)
                spacing: dp(6)
                CheckBox:
                    id: playlist
                    size_hint_x: None
                    width: dp(36)
                    disabled: app.busy
                    color: 0.90, 0.16, 0.20, 1
                Label:
                    text: 'Download whole playlist (if the link has one)'
                    font_size: '14sp'
                    color: 0.86, 0.88, 0.92, 1
                    halign: 'left'
                    valign: 'middle'
                    text_size: self.size

            Label:
                text: app.video_title
                font_size: '14sp'
                bold: True
                color: 0.95, 0.95, 0.97, 1
                halign: 'left'
                size_hint_y: None
                text_size: self.width, None
                height: (self.texture_size[1] + dp(4)) if self.text else 0
                opacity: 1 if self.text else 0

        BoxLayout:
            size_hint_y: None
            height: dp(56)
            spacing: dp(10)
            RoundButton:
                text: app.busy_text if app.busy else 'Download'
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
                on_release: app.cancel()

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
    busy_text = StringProperty("Downloading...")
    engine = StringProperty("")
    video_title = StringProperty("")
    choices = ListProperty(PRESET_NAMES)

    def build(self):
        self.title = "YT Downloader"
        Window.clearcolor = (0.07, 0.075, 0.095, 1)
        Window.softinput_mode = "below_target"
        Window.bind(on_keyboard=self._on_key)
        self.dev = Device(self.user_data_dir)
        self.engine = self.dev.describe()
        self._cancel = threading.Event()
        self._job = 0  # increases on every start / cancel; old threads see they are stale
        self._last_ui = 0.0
        self._format_map = {}  # label -> format dict (from "All formats")
        self._formats_url = None
        return Builder.load_string(KV)

    def on_start(self):
        threading.Thread(target=self._preload, daemon=True).start()
        if IS_ANDROID:
            self._listen_for_shares()

    def on_pause(self):
        return True  # keep downloading when the app goes to background

    def on_resume(self):
        pass

    def _on_key(self, window, key, *args):
        # Android back button: never kill a running download, just hide the app
        if key == 27 and self.busy and IS_ANDROID:
            try:
                from jnius import autoclass

                autoclass("org.kivy.android.PythonActivity").mActivity.moveTaskToBack(True)
            except Exception:
                pass
            return True
        return False

    # ---- thread-safe UI update ----
    @mainthread
    def _ui(self, job=None, **kw):
        if job is not None and job != self._job:
            return  # result of a cancelled job
        for key, value in kw.items():
            setattr(self, key, value)

    def _preload(self):
        try:
            y = load_ytdlp()
            self._ui(engine=self.dev.describe(y.version))
        except Exception as e:
            self.dev.log_error(traceback.format_exc())
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
            if not text and intent is not None and intent.getDataString():
                text = intent.getDataString()
        except Exception:
            return
        url = extract_url(text)
        if url:
            self._set_url(url)

    @mainthread
    def _set_url(self, url):
        if not self.busy:
            self.root.ids.url.text = url
            self.status, self.status_error = "Link received. Choose a format and tap Download.", False

    # ---- format list ----
    def url_changed(self, text):
        if self._format_map and text.strip() != self._formats_url:
            self._reset_formats()

    def _reset_formats(self):
        self._format_map = {}
        self._formats_url = None
        self.video_title = ""
        current = self.root.ids.fmt.text if self.root else ""
        self.choices = PRESET_NAMES
        if self.root:
            self.root.ids.fmt.text = current if current in PRESET_NAMES else PRESET_NAMES[0]

    @mainthread
    def _show_formats(self, job, url, title, items):
        if job != self._job:
            return
        self._format_map = dict(items)
        self._formats_url = url
        self.video_title = title
        self.choices = PRESET_NAMES + [FORMATS_HEADER] + [label for label, _ in items]
        self.root.ids.fmt.text = items[0][0]
        self.busy, self.progress = False, 0
        self.status = (f"Found {len(items)} formats. Pick one from the list (video-only "
                       f"formats get the best audio added automatically).")
        self.status_error = False
        self.root.ids.fmt.is_open = True

    # ---- buttons ----
    def paste_url(self):
        from kivy.core.clipboard import Clipboard

        text = (Clipboard.paste() or "").strip()
        self.root.ids.url.text = extract_url(text) or text

    def _get_url(self):
        url = extract_url(self.root.ids.url.text.strip())
        if not url:
            self.status, self.status_error = "Please paste a valid link (starting with https://).", True
        return url

    def fetch_formats(self):
        if self.busy:
            return
        url = self._get_url()
        if not url:
            return
        self._job += 1
        self.busy, self.busy_text, self.progress = True, "Loading formats...", 0
        self.status, self.status_error = "Getting the list of formats...", False
        threading.Thread(target=self._fetch_worker, args=(self._job, url), daemon=True).start()

    def start_download(self):
        if self.busy:
            return
        url = self._get_url()
        if not url:
            return
        label = self.root.ids.fmt.text
        if label == FORMATS_HEADER or (label not in PRESET_NAMES and label not in self._format_map):
            self.status, self.status_error = "Please choose a format from the list.", True
            return
        choice = self._format_map.get(label, label)
        playlist = self.root.ids.playlist.active
        self._cancel.clear()
        self._job += 1
        self.busy, self.busy_text, self.progress = True, "Downloading...", 0
        self.status, self.status_error = "Starting...", False
        self.dev.keep_awake(True)
        threading.Thread(
            target=self._download_worker, args=(self._job, url, choice, playlist), daemon=True
        ).start()

    def cancel(self):
        if not self.busy:
            return
        self._cancel.set()
        if self.busy_text == "Loading formats...":
            self._job += 1  # forget the running format lookup
            self.busy = False
            self.status, self.status_error = "Cancelled.", False
        else:
            self.status = "Cancelling..."

    # ---- background work ----
    def _detach(self):
        if IS_ANDROID:
            try:
                from jnius import detach

                detach()  # required before a thread that used Java ends
            except Exception:
                pass

    def _fetch_worker(self, job, url):
        logger = YdlLogger()
        try:
            y = load_ytdlp()
            opts = base_opts(self.dev.cache_dir, logger, self.dev.ffmpeg, self.dev.js, y.parse_options)
            with y.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info and not info.get("formats") and info.get("entries"):
                raise RuntimeError("This link is a playlist. 'All formats' works for single videos - "
                                   "use a preset and tick 'Download whole playlist'.")
            items = format_choices(info)
            if not items:
                raise RuntimeError("No downloadable formats found for this link.")
            self._show_formats(job, url, (info or {}).get("title") or "", items)
        except Exception as e:
            self.dev.log_error(traceback.format_exc())
            msg = self._explain(e, logger)
            self._ui(job=job, status=msg, status_error=True, busy=False)
        finally:
            self._detach()

    def _download_worker(self, job, url, choice, playlist):
        y = None
        work = None
        logger = YdlLogger()
        try:
            self._ui(job=job, status="Loading engine...")
            y = load_ytdlp()
            self._ui(job=job, status="Getting video info...")
            work = tempfile.mkdtemp(prefix="job_", dir=self.dev.work_dir)
            opts, note = build_ydl_opts(
                choice, work,
                cache_dir=self.dev.cache_dir, logger=logger, ffmpeg=self.dev.ffmpeg, js=self.dev.js,
                parse_options=y.parse_options, playlist=playlist,
                progress_hook=self._progress_hook, pp_hook=self._pp_hook,
            )
            with y.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            if self._cancel.is_set():
                raise y.UserCancelled()

            files = finished_files(info, work)
            if not files:
                if logger.errors:
                    raise RuntimeError(logger.errors[-1])
                raise RuntimeError("Download finished but no file was found.")
            self._ui(job=job, status="Saving to Downloads...", progress=100)
            total = sum(os.path.getsize(f) for f in files)
            saved = [self.dev.save(f) for f in files]

            shown = saved if len(saved) <= 5 else saved[:5] + [f"... and {len(saved) - 5} more"]
            msg = f"Done! Saved {len(saved)} file{'s' if len(saved) > 1 else ''}:\n" + "\n".join(shown)
            if total:
                msg += f"\n({fmt_size(total)})"
            if playlist and logger.errors:
                msg += f"\n{len(logger.errors)} item(s) could not be downloaded."
            if note:
                msg += "\n" + note
            self._ui(job=job, status=msg, status_error=False, progress=100)
        except Exception as e:
            if self._cancel.is_set() or (y and isinstance(e, y.Cancelled)):
                self._ui(job=job, status="Download cancelled.", status_error=False, progress=0)
            else:
                self.dev.log_error(traceback.format_exc())
                self._ui(job=job, status=self._explain(e, logger), status_error=True)
        finally:
            if work:
                shutil.rmtree(work, ignore_errors=True)
            self._finish_download(job)
            self._detach()

    @mainthread
    def _finish_download(self, job):
        self.dev.keep_awake(False)
        if job == self._job:
            self.busy = False

    def _explain(self, err, logger):
        msg = clean_error(err)
        if not self.dev.js and any("javascript runtime" in w.lower() or "js runtime" in w.lower()
                                   for w in logger.warnings):
            msg += "\n(No JS runtime found - some YouTube formats may be missing.)"
        return msg

    def _progress_hook(self, d):
        if self._cancel.is_set():
            raise load_ytdlp().UserCancelled()
        status = d.get("status")
        info = d.get("info_dict") or {}
        item = ""
        if info.get("playlist_index") and info.get("n_entries"):
            item = f"Item {info['playlist_index']}/{info['n_entries']}  |  "
        if status == "downloading":
            now = time.monotonic()
            if now - self._last_ui < 0.25:  # max ~4 UI updates per second
                return
            self._last_ui = now
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            kind = "audio" if info.get("vcodec") == "none" else "video"
            parts = [f"{item}Downloading {kind}", fmt_size(done) + (f" / {fmt_size(total)}" if total else "")]
            if d.get("speed"):
                parts.append(f"{fmt_size(d['speed'])}/s")
            if d.get("eta") is not None:
                parts.append(f"ETA {fmt_eta(d['eta'])}")
            title = info.get("title")
            self._ui(progress=(done * 100.0 / total) if total else 0,
                     status=(f"{title}\n" if title else "") + "  |  ".join(parts))
        elif status == "finished":
            self._ui(progress=100, status=f"{item}Stream downloaded, processing...")

    def _pp_hook(self, d):
        if d.get("status") == "started":
            labels = {
                "Merger": "Merging video + audio...",
                "ExtractAudio": "Converting audio...",
                "Metadata": "Writing tags...",
                "FixupM4a": "Fixing file...",
                "FixupM3u8": "Fixing file...",
            }
            self._ui(status=labels.get(d.get("postprocessor"), "Processing..."))


if __name__ == "__main__":
    YTDownloaderApp().run()
