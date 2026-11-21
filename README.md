# YT Downloader (Android)

A useful downloader app for Android: paste or **share** a link, pick a format, tap **Download**.
Built with Python ([Kivy](https://kivy.org) + [yt-dlp](https://github.com/yt-dlp/yt-dlp)), packaged as an APK by GitHub Actions.

## Features

- **Every common format**
  - Video: Best / 4K 2160p / 1440p / 1080p / 720p / 480p / 360p / 240p / 144p as **MP4**, or best quality as **MKV** / **WEBM**
  - Audio: **MP3** (320 / 192 / 128 kbps), **M4A** (AAC), **OPUS**, **FLAC**, **WAV**, or the original audio without conversion
- **All formats** button: lists every format the video really has (resolution, fps, codec, size) so you can pick any one.
  Video-only formats automatically get the best audio merged in.
- **Playlists**: tick *Download whole playlist*.
- **Share to app**: in the YouTube app tap *Share → YT Downloader*.
- Works with YouTube and the [1000+ other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) yt-dlp supports.
- Files are saved to **Download/YTDownloader** (visible in Files, Gallery and music apps). No storage permission needed on Android 10+.
- Keeps downloading with the screen off; the back button won't kill a running download.

The APK bundles **FFmpeg** (merging 720p+ video with audio, MP3/FLAC/WAV conversion) and **QuickJS**
(the JavaScript engine yt-dlp needs to unlock all YouTube formats). Both are built from source by the workflow.

## Get the APK

1. Open the repository's **Actions** tab → **Build Android APK** → the newest green run.
2. Download the **YTDownloader-apk** artifact (a zip) and unzip it.
3. Copy the `.apk` to your phone and open it (allow *Install unknown apps* when asked).

Every push to `main` builds a new APK. You can also start a build by hand with **Run workflow**.
Builds use the same signing key, so a new APK installs over the old one.
If Android ever says *App not installed*, uninstall the old version first.

Needs Android 7.0 or newer on a 64-bit (arm64) phone, which is almost every phone from the last 8 years.

## Project layout

| File | What it is |
| --- | --- |
| `main.py` | The app (Kivy UI, Android storage / sharing) |
| `ytcore.py` | Download logic: format presets, the "All formats" list, yt-dlp options |
| `buildozer.spec` | APK build settings |
| `intent_filters.xml` | Lets other apps share links to this one |
| `tools/build_helpers.sh` | Cross-compiles FFmpeg (+ LAME MP3) and QuickJS for Android |
| `.github/workflows/build-apk.yml` | Runs the tests, builds the helpers and the APK |
| `tests/` | Unit tests (`pip install pytest yt-dlp && python -m pytest tests`) |

## Building locally (Linux)

```bash
pip install buildozer==1.6.0 "cython<3.2"
bash tools/build_helpers.sh          # optional: FFmpeg + QuickJS into libs/arm64-v8a
buildozer android debug              # APK appears in bin/
```

You can also run the app on a PC for testing: `pip install kivy yt-dlp && python main.py`.

## If a download fails

YouTube changes often. A fresh build gets the newest yt-dlp, which usually fixes it:
open **Actions → Build Android APK → Run workflow** and install the new APK.

Only download videos you have the right to save (your own, Creative Commons, or with permission),
and respect the terms of the sites you use.
