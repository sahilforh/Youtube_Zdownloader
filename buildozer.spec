[app]

title = YT Downloader
package.name = ytdownloader
package.domain = org.zakir

source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = bin, .buildozer, .git, .github, libs, tests, tools

version = 1.1.0

# python-for-android installs pip packages with --no-deps,
# so every dependency of requests / yt-dlp is listed here.
# charset-normalizer 2.1.1 = pure Python (newer versions ship x86 binaries that break on Android).
# yt-dlp-ejs = YouTube challenge-solver scripts used together with the JS runtime.
# yt-dlp is NOT pinned on purpose: YouTube changes often and every build should get the newest fixes.
# Names use underscores on purpose: python-for-android only skips its own copy of a dependency when
# the name matches with underscores. With "charset-normalizer" it also pulled PyPI's compiled Android
# wheel and handed it to the PC's pip, which refused it and stopped the build.
requirements = python3,kivy,pyjnius,android,openssl,yt_dlp,yt_dlp_ejs,requests,urllib3,idna,charset_normalizer==2.1.1,certifi

orientation = portrait
fullscreen = 0

icon.filename = %(source.dir)s/assets/icon.png
presplash.filename = %(source.dir)s/assets/presplash.png
android.presplash_color = #121318

# ---------------- Android ----------------
# Storage permission is only needed (and only asked) on Android 9 and older.
android.permissions = INTERNET, ACCESS_NETWORK_STATE, WAKE_LOCK, (name=android.permission.READ_EXTERNAL_STORAGE;maxSdkVersion=32), (name=android.permission.WRITE_EXTERNAL_STORAGE;maxSdkVersion=29)

android.api = 34
android.minapi = 24
# NDK version recommended by python-for-android v2026.05.09 (25b is too old for it)
android.ndk = 28c
android.ndk_api = 24
android.accept_sdk_license = True

# Most phones are 64-bit. Add armeabi-v7a for very old phones (build takes ~2x longer,
# and tools/build_helpers.sh would also need an armeabi-v7a build).
android.archs = arm64-v8a

# Helper programs, built by tools/build_helpers.sh (auto-detected by main.py):
#   libs/arm64-v8a/libffmpeg.so -> FFmpeg executable (720p+ merging, MP3/FLAC/WAV)
#   libs/arm64-v8a/libqjs.so    -> QuickJS executable (JS runtime yt-dlp needs for YouTube)
# They go into the APK's native-lib folder, the only place Android 10+ lets an app run a binary.
android.add_libs_arm64_v8a = libs/arm64-v8a/*.so

# "Share" a link from the YouTube app straight into this app
android.manifest.intent_filters = intent_filters.xml
android.manifest.launch_mode = singleTask

android.allow_backup = True
android.debug_artifact = apk
android.release_artifact = apk

# Pinned release of python-for-android (master moves and can break the build).
p4a.branch = v2026.05.09


[buildozer]
log_level = 2
warn_on_root = 1
