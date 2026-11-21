"""
Tests for ytcore (the download logic). Runs on a PC, no phone or internet needed:
    pip install pytest yt-dlp      (+ ffmpeg on PATH for the end-to-end tests)
    python -m pytest -q tests
"""

import functools
import http.server
import os
import shutil
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yt_dlp  # noqa: E402

import ytcore  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def fake_info(base_url="http://127.0.0.1:1"):
    """A YouTube-like video with the usual mix of formats."""
    def f(fid, ext, vcodec, acodec, height=None, abr=None, tbr=None, fps=None, size=None, note=None):
        d = {"format_id": fid, "ext": ext, "vcodec": vcodec, "acodec": acodec,
             "url": f"{base_url}/{fid}.{ext}", "protocol": "https"}
        if height:
            d.update(height=height, width=height * 16 // 9, fps=fps or 30)
        if abr:
            d["abr"] = abr
        d["tbr"] = tbr or abr or (height or 0) * 5
        if size:
            d["filesize"] = size
        if note:
            d["format_note"] = note
        return d

    return {
        "id": "abc123", "title": "Test: video / with ? odd | chars", "extractor": "youtube",
        "extractor_key": "Youtube", "webpage_url": "https://www.youtube.com/watch?v=abc123",
        "formats": [
            f("sb0", "mhtml", "none", "none", note="storyboard"),
            f("18", "mp4", "avc1.42001E", "mp4a.40.2", height=360, size=5_000_000),
            f("140", "m4a", "none", "mp4a.40.2", abr=129, size=2_000_000),
            f("251", "webm", "none", "opus", abr=135, size=2_100_000),
            f("136", "mp4", "avc1.4d401f", "none", height=720, size=9_000_000),
            f("137", "mp4", "avc1.640028", "none", height=1080, size=20_000_000),
            f("248", "webm", "vp9", "none", height=1080, size=15_000_000),
            f("299", "mp4", "avc1.64002a", "none", height=1080, fps=60, size=30_000_000),
            f("303", "webm", "vp9", "none", height=1080, fps=60, size=25_000_000),
            f("313", "webm", "vp9", "none", height=2160, size=90_000_000),
        ],
    }


def make_ydl(opts):
    return yt_dlp.YoutubeDL({**opts, "quiet": True, "logger": ytcore.YdlLogger()})


def select(choice, ffmpeg=FFMPEG or "/usr/bin/ffmpeg", **kw):
    opts, note = ytcore.build_ydl_opts(choice, "/tmp", cache_dir=False, logger=ytcore.YdlLogger(),
                                       ffmpeg=ffmpeg, **kw)
    ydl = make_ydl(opts)
    res = ydl.process_ie_result(fake_info(), download=False)
    ids = [f["format_id"] for f in res.get("requested_formats") or [res]]
    return ids, res, opts, note


# ---------------------------------------------------------------- presets
@pytest.mark.parametrize("name", ytcore.PRESET_NAMES)
@pytest.mark.parametrize("ffmpeg", [True, False])
def test_every_preset_builds_valid_options(name, ffmpeg):
    opts, note = ytcore.build_ydl_opts(name, "/tmp/out", cache_dir=False, logger=ytcore.YdlLogger(),
                                       ffmpeg="/usr/bin/ffmpeg" if ffmpeg else None)
    assert opts["format"]
    ydl = make_ydl(opts)
    ydl.build_format_selector(opts["format"])  # raises on a bad selector
    assert opts["outtmpl"].startswith("/tmp/out")
    assert opts["noplaylist"] is True


@pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")
def test_best_mp4_prefers_h264_and_aac():
    ids, res, _, _ = select("1080p Full HD video (MP4)")
    assert ids == ["299", "140"]  # 1080p60 H.264 beats 1080p60 VP9 and 1080p30
    assert res["ext"] == "mp4"


@pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")
def test_best_quality_takes_4k():
    ids, res, _, _ = select("Best quality video (MP4)")
    assert ids[0] == "313"
    assert res["ext"] == "mp4"


@pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")
def test_height_limits():
    assert select("720p HD video (MP4)")[0][0] == "136"
    assert select("480p video (MP4)")[0][0] == "18"
    # nothing small enough -> falls back to the smallest, not to 4K
    assert select("144p video (MP4)")[0][0] == "18"
    assert select("144p video (MP4)", ffmpeg=None)[0] == ["18"]


@pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")
def test_webm_and_mkv():
    ids, res, _, _ = select("Best quality video (WEBM)")
    assert ids == ["313", "251"] and res["ext"] == "webm"
    ids, res, _, _ = select("Best quality video (MKV)")
    assert ids[0] == "313" and res["ext"] == "mkv"


def test_without_ffmpeg_only_single_file_streams():
    ids, res, _, note = select("1080p Full HD video (MP4)", ffmpeg=None)
    assert ids == ["18"]
    assert "FFmpeg" in note
    ids, _, _, note = select("MP3 audio 320 kbps", ffmpeg=None)
    assert ids == ["140"] and "M4A" in note
    ids, _, _, _ = select("OPUS audio", ffmpeg=None)
    assert ids == ["251"]


@pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")
def test_audio_presets():
    ids, _, opts, _ = select("MP3 audio 192 kbps")
    pp = opts["postprocessors"][0]
    assert pp["preferredcodec"] == "mp3" and pp["preferredquality"] == "192"
    assert select("M4A audio (AAC)")[0] == ["140"]
    assert select("OPUS audio")[0] == ["251"]
    _, _, opts, _ = select("Original audio (no conversion)")
    assert "postprocessors" not in opts


# ---------------------------------------------------------------- "All formats"
def test_format_choices_lists_and_sorts():
    items = ytcore.format_choices(fake_info())
    labels = [label for label, _ in items]
    ids = [c["id"] for _, c in items]
    assert "sb0" not in ids  # storyboards hidden
    assert ids[0] == "313"  # best video first
    assert ids[-2:] == ["251", "140"]  # audio last, best first
    assert len(set(labels)) == len(labels)
    assert any("1080p60" in label for label in labels)
    assert ids.index("299") < ids.index("137")  # 60 fps before 30 fps
    label_137 = labels[ids.index("137")]
    assert "H.264" in label_137 and "(+ best audio)" in label_137 and "19.1 MB" in label_137


@pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")
def test_exact_video_only_format_gets_audio():
    choice = dict(ytcore.format_choices(fake_info()))
    label = next(lbl for lbl in choice if lbl.endswith("[137]"))
    ids, res, _, _ = select(choice[label])
    assert ids == ["137", "140"] and res["ext"] == "mp4"
    label = next(lbl for lbl in choice if lbl.endswith("[313]"))
    ids, res, _, _ = select(choice[label])
    assert ids == ["313", "251"] and res["ext"] == "webm"


def test_exact_audio_format():
    choice = dict(ytcore.format_choices(fake_info()))
    label = next(lbl for lbl in choice if lbl.endswith("[251]"))
    assert select(choice[label])[0] == ["251"]


# ---------------------------------------------------------------- misc helpers
def test_file_names_are_android_safe():
    opts, _ = ytcore.build_ydl_opts("M4A audio (AAC)", "/tmp/o", cache_dir=False, logger=ytcore.YdlLogger())
    ydl = make_ydl(opts)
    name = os.path.basename(ydl.prepare_filename({**fake_info(), "ext": "m4a"}))
    assert not any(c in name for c in ':?|"*<>')
    assert name.endswith("[abc123].m4a")


def test_playlist_file_names_numbered():
    opts, _ = ytcore.build_ydl_opts("M4A audio (AAC)", "/tmp/o", cache_dir=False,
                                    logger=ytcore.YdlLogger(), playlist=True)
    assert opts["noplaylist"] is False
    ydl = make_ydl(opts)
    name = os.path.basename(ydl.prepare_filename({**fake_info(), "ext": "m4a", "playlist_index": 3}))
    assert name.startswith("3 - ")
    name = os.path.basename(ydl.prepare_filename({**fake_info(), "ext": "m4a"}))
    assert name.startswith("Test")


def test_extract_url():
    assert ytcore.extract_url("Watch this! https://youtu.be/abc123?si=x (cool)") == "https://youtu.be/abc123?si=x"
    assert ytcore.extract_url("no link") is None


def test_clean_error():
    assert "bot check" in ytcore.clean_error("ERROR: [youtube] x: Sign in to confirm you're not a bot")
    assert "private" in ytcore.clean_error("ERROR: Private video")
    assert ytcore.clean_error("ERROR: something odd") == "something odd"


def test_js_runtime_options():
    opts = ytcore.js_runtime_opts(("quickjs", "/data/app/lib/libqjs.so"), yt_dlp.parse_options)
    assert opts["js_runtimes"] == {"quickjs": {"path": "/data/app/lib/libqjs.so"}}
    assert ytcore.js_runtime_opts(None, yt_dlp.parse_options) == {}


def test_fmt_helpers():
    assert ytcore.fmt_size(0) == "0 B"
    assert ytcore.fmt_size(1536) == "2 KB"
    assert ytcore.fmt_size(5 * 1024 * 1024) == "5.0 MB"
    assert ytcore.fmt_eta(3725) == "1:02:05"


# ---------------------------------------------------------------- real download, merge, convert
@pytest.fixture(scope="module")
def media_server(tmp_path_factory):
    """Serves a small video-only MP4 and an AAC M4A over HTTP, like YouTube's DASH streams."""
    root = tmp_path_factory.mktemp("media")
    run = functools.partial(subprocess.run, check=True, capture_output=True)
    run([FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=2",
         "-c:v", "mpeg4", "-an", str(root / "137.mp4")])
    run([FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:a", "aac", "-vn", str(root / "140.m4a")])
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def local_info(base):
    info = fake_info(base)
    info["formats"] = [f for f in info["formats"] if f["format_id"] in ("137", "140")]
    for f in info["formats"]:
        f.pop("filesize", None)
        f["protocol"] = "http"
    return info


def phone_like_ffmpeg(tmp_path):
    """FFmpeg under the name it has inside the APK."""
    lib = tmp_path / "nativelib"
    lib.mkdir()
    os.symlink(FFMPEG, lib / "libffmpeg.so")
    return str(lib / "libffmpeg.so")


@pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")
@pytest.mark.parametrize("choice, ext", [
    ("720p HD video (MP4)", "mp4"),
    ("MP3 audio 192 kbps", "mp3"),
    ("M4A audio (AAC)", "m4a"),
    ("FLAC audio (lossless)", "flac"),
])
def test_real_download(media_server, tmp_path, choice, ext):
    out = tmp_path / "work"
    out.mkdir()
    events = []
    opts, _ = ytcore.build_ydl_opts(
        choice, str(out), cache_dir=False, logger=ytcore.YdlLogger(), ffmpeg=phone_like_ffmpeg(tmp_path),
        progress_hook=lambda d: events.append(d["status"]), pp_hook=lambda d: None)
    opts.pop("http_chunk_size")  # python's test server does not do range requests well
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.process_ie_result(local_info(media_server), download=True)
    files = ytcore.finished_files(info, str(out))
    assert len(files) == 1, os.listdir(out)
    assert files[0].endswith("." + ext)
    assert os.path.getsize(files[0]) > 1000
    assert "finished" in events
    if ext == "mp4":  # merged: must contain both video and audio
        probe = subprocess.run([FFMPEG, "-i", files[0]], capture_output=True, text=True).stderr
        assert "Video:" in probe and "Audio:" in probe
