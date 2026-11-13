[app]

title = YT Downloader
package.name = ytdownloader
package.domain = org.zakir

source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = bin, .buildozer, .git, .github, libs

version = 1.0.0

# python-for-android installs pip packages with --no-deps,
# so every dependency of requests / yt-dlp is listed here.
# charset-normalizer 2.1.1 = pure Python (newer versions ship x86 binaries that break on Android).
# yt-dlp-ejs = YouTube challenge-solver scripts used together with the JS runtime.
requirements = python3,kivy,pyjnius,android,openssl,yt-dlp,yt-dlp-ejs,requests,urllib3,idna,charset-normalizer==2.1.1,certifi

orientation = portrait
fullscreen = 0

# icon.filename = %(source.dir)s/icon.png
# presplash.filename = %(source.dir)s/presplash.png

# ---------------- Android ----------------
android.permissions = INTERNET, ACCESS_NETWORK_STATE, WAKE_LOCK, READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE

android.api = 34
android.minapi = 24
android.ndk = 25b
android.ndk_api = 24
android.accept_sdk_license = True

# Most phones are 64-bit. Add armeabi-v7a for very old phones (build takes ~2x longer).
android.archs = arm64-v8a

# Optional helper binaries (auto-detected by main.py):
#   libs/arm64-v8a/libffmpeg.so -> FFmpeg executable (720p+ merging, MP3)
#   libs/arm64-v8a/libqjs.so    -> QuickJS executable (JS runtime for yt-dlp)
# They go into the APK's native-lib folder, the only place Android 10+ lets an app run a binary.
android.add_libs_arm64_v8a = libs/arm64-v8a/*.so

# "Share" a link from the YouTube app straight into this app
android.manifest.intent_filters = intent_filters.xml
android.manifest.launch_mode = singleTask

android.allow_backup = True
android.debug_artifact = apk
android.release_artifact = apk

p4a.branch = master


[buildozer]
log_level = 2
warn_on_root = 1
