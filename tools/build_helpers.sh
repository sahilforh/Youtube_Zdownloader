#!/usr/bin/env bash
# Builds the helper programs the app runs on the phone (Android arm64-v8a):
#
#   libffmpeg.so - FFmpeg with the LAME MP3 encoder. Merges YouTube's separate
#                  video + audio streams (720p and up) and converts to MP3/FLAC/WAV/M4A.
#   libqjs.so    - QuickJS-ng JavaScript engine. yt-dlp needs a JS runtime to
#                  solve YouTube's signature challenges (otherwise formats go missing).
#
# They are ordinary executables. The lib*.so name makes Android install them into
# the app's native library folder, the only place an app may run programs from.
#
# Usage: tools/build_helpers.sh [output dir]      (default: libs/arm64-v8a)
# Needs an Android NDK: $ANDROID_NDK_LATEST_HOME / $ANDROID_NDK_HOME (GitHub runners
# have one), otherwise NDK r28c is downloaded.
set -euo pipefail

FFMPEG_VERSION="${FFMPEG_VERSION:-n7.1.5}"
LAME_VERSION="${LAME_VERSION:-3.100}"
QJS_VERSION="${QJS_VERSION:-v0.18.0}"
API=24
TRIPLE=aarch64-linux-android

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mkdir -p "${1:-$ROOT/libs/arm64-v8a}" && cd "${1:-$ROOT/libs/arm64-v8a}" && pwd)"
WORK="${WORK_DIR:-$ROOT/.helpers-build}"
PREFIX="$WORK/prefix"
JOBS="$(nproc 2>/dev/null || echo 2)"
mkdir -p "$WORK" "$PREFIX"

log() { printf '\n==== %s ====\n' "$*"; }

fetch() {  # fetch <output file> <url>...
    local out="$1"; shift
    [ -s "$out" ] && return 0
    for url in "$@"; do
        if curl -fL --retry 5 --retry-delay 3 --connect-timeout 30 -o "$out.tmp" "$url"; then
            mv "$out.tmp" "$out"
            return 0
        fi
        echo "Download failed: $url"
    done
    echo "ERROR: could not download $out" >&2
    return 1
}

# ---------------------------------------------------------------- NDK
NDK="${ANDROID_NDK_LATEST_HOME:-${ANDROID_NDK_HOME:-${ANDROID_NDK_ROOT:-}}}"
if [ -z "$NDK" ] || [ ! -d "$NDK/toolchains/llvm/prebuilt/linux-x86_64" ]; then
    log "Downloading Android NDK r28c"
    fetch "$WORK/ndk.zip" "https://dl.google.com/android/repository/android-ndk-r28c-linux.zip"
    rm -rf "$WORK/android-ndk-r28c"
    unzip -q "$WORK/ndk.zip" -d "$WORK"
    NDK="$WORK/android-ndk-r28c"
fi
TC="$NDK/toolchains/llvm/prebuilt/linux-x86_64"
export CC="$TC/bin/${TRIPLE}${API}-clang"
export CXX="$TC/bin/${TRIPLE}${API}-clang++"
export AR="$TC/bin/llvm-ar"
export RANLIB="$TC/bin/llvm-ranlib"
export NM="$TC/bin/llvm-nm"
export STRIP="$TC/bin/llvm-strip"
echo "Using NDK: $NDK"
"$CC" --version | head -1

# ---------------------------------------------------------------- LAME (MP3 encoder)
log "LAME $LAME_VERSION"
fetch "$WORK/lame.tar.gz" \
    "https://downloads.sourceforge.net/project/lame/lame/$LAME_VERSION/lame-$LAME_VERSION.tar.gz" \
    "https://deb.debian.org/debian/pool/main/l/lame/lame_$LAME_VERSION.orig.tar.gz"
rm -rf "$WORK/lame-src" && mkdir -p "$WORK/lame-src"
tar xzf "$WORK/lame.tar.gz" -C "$WORK/lame-src" --strip-components=1
(
    cd "$WORK/lame-src"
    # lame's config.sub/config.guess are from 2017: use the system's newer copies if present
    for f in config.sub config.guess; do
        for d in /usr/share/misc /usr/share/automake-*; do
            [ -f "$d/$f" ] && cp "$d/$f" . && break
        done
    done
    CFLAGS="-O2 -fPIC" ./configure --host="$TRIPLE" --prefix="$PREFIX" \
        --enable-static --disable-shared --disable-frontend --disable-decoder \
        --disable-gtktest --disable-dependency-tracking
    make -j"$JOBS"
    make install
)

# ---------------------------------------------------------------- FFmpeg
log "FFmpeg $FFMPEG_VERSION"
if [ ! -d "$WORK/ffmpeg-src/.git" ]; then
    rm -rf "$WORK/ffmpeg-src"
    git clone --depth 1 --branch "$FFMPEG_VERSION" https://github.com/FFmpeg/FFmpeg.git "$WORK/ffmpeg-src"
fi
(
    cd "$WORK/ffmpeg-src"
    make distclean >/dev/null 2>&1 || true
    ./configure \
        --prefix="$PREFIX" \
        --target-os=android --arch=aarch64 --cpu=armv8-a --enable-cross-compile \
        --cc="$CC" --cxx="$CXX" --ar="$AR" --ranlib="$RANLIB" --nm="$NM" --strip="$STRIP" \
        --pkg-config=false \
        --extra-cflags="-O2 -I$PREFIX/include" \
        --extra-ldflags="-L$PREFIX/lib" \
        --extra-libs="-lm" \
        --enable-static --disable-shared --enable-pic \
        --disable-autodetect --enable-zlib --enable-libmp3lame \
        --disable-doc --disable-debug --disable-ffplay --disable-ffprobe \
        --disable-avdevice --disable-network \
        --disable-encoders \
        --enable-encoder=aac,libmp3lame,flac,alac,pcm_s16le,pcm_s24le,mjpeg,png
    make -j"$JOBS"
    cp ffmpeg "$OUT/libffmpeg.so"
)
"$STRIP" "$OUT/libffmpeg.so"

# ---------------------------------------------------------------- QuickJS-ng
log "QuickJS-ng $QJS_VERSION"
if [ ! -d "$WORK/qjs-src/.git" ]; then
    rm -rf "$WORK/qjs-src"
    git clone --depth 1 --branch "$QJS_VERSION" https://github.com/quickjs-ng/quickjs.git "$WORK/qjs-src"
fi
rm -rf "$WORK/qjs-build"
cmake -S "$WORK/qjs-src" -B "$WORK/qjs-build" \
    -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \
    -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM="android-$API" \
    -DCMAKE_BUILD_TYPE=Release
cmake --build "$WORK/qjs-build" --target qjs_exe -j"$JOBS"
cp "$WORK/qjs-build/qjs" "$OUT/libqjs.so"
"$STRIP" "$OUT/libqjs.so"

# ---------------------------------------------------------------- check
log "Result"
chmod 755 "$OUT"/*.so
ls -l "$OUT"
for f in "$OUT/libffmpeg.so" "$OUT/libqjs.so"; do
    file "$f" 2>/dev/null || true
    # must only need Android system libraries
    "$TC/bin/llvm-readelf" -d "$f" | grep NEEDED || true
    if "$TC/bin/llvm-readelf" -d "$f" | grep NEEDED | grep -qvE '\[(libc|libm|libdl|libz|liblog)\.so\]'; then
        echo "ERROR: $f depends on a library that is not part of Android" >&2
        exit 1
    fi
done
echo "Helpers built OK"
