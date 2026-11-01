#!/usr/bin/env bash
# Builds the phone app as a debug-signed APK with the Android SDK's command-line tools.
#   android/build.sh        build/tprint.apk
#   android/build.sh deps   only fetch the Java libraries (also used by the desktop tests)
set -euo pipefail
cd "$(dirname "$0")"

SDK=${ANDROID_HOME:-$HOME/Android}
TOOLS=$SDK/build-tools/34.0.0
PLATFORM=$SDK/platforms/android-33/android.jar
OUT=build
DEPS=$OUT/deps
MAVEN=https://repo1.maven.org/maven2

fetch() { # file, maven path, sha256
    if [ -f "$DEPS/$1" ] && echo "$3  $DEPS/$1" | sha256sum -c --status; then
        return
    fi
    mkdir -p "$DEPS"
    curl -sSfL -o "$DEPS/$1.part" "$MAVEN/$2"
    echo "$3  $DEPS/$1.part" | sha256sum -c --quiet
    mv "$DEPS/$1.part" "$DEPS/$1"
}

# xz 1.9, not 1.10: some 1.10 classes carry nameless MethodParameters entries, which crash d8 8.2.
fetch xz.jar org/tukaani/xz/1.9/xz-1.9.jar \
    211b306cfc44f8f96df3a0a3ddaf75ba8c5289eed77d60d72f889bb855f535e5
fetch qrcodegen.jar io/nayuki/qrcodegen/1.8.0/qrcodegen-1.8.0.jar \
    1d1aef3caea19387e7dd8034835054bab83db57a02b69cfc6dbc849746f0be21
[ "${1:-}" = deps ] && exit 0

for f in "$TOOLS/aapt2" "$TOOLS/d8" "$PLATFORM"; do
    [ -e "$f" ] || { echo "missing $f (set ANDROID_HOME; needs build-tools 34.0.0, android-33)" >&2; exit 1; }
done

rm -rf "$OUT/assets" "$OUT/classes" "$OUT/dex"
mkdir -p "$OUT/assets/web" "$OUT/classes" "$OUT/dex"

# The designer's own files, plus the Unicode name index the desktop server builds at runtime.
cp -r ../web/static/. "$OUT/assets/web/"
uv run -q --no-project --python-preference only-system --with pillow --with segno python -c '
import json, sys, unicodedata
sys.path.insert(0, "../web")
import server
json.dump({"version": unicodedata.unidata_version, "chars": server.unicode_index()},
          open(sys.argv[1], "w"), separators=(",", ":"))' "$OUT/assets/unicode.json"

"$TOOLS/aapt2" compile --dir res -o "$OUT/res.zip"
"$TOOLS/aapt2" link -I "$PLATFORM" --manifest AndroidManifest.xml -A "$OUT/assets" \
    --min-sdk-version 26 --target-sdk-version 33 --version-code 1 --version-name 0.1 \
    --debug-mode -o "$OUT/base.apk" "$OUT/res.zip"

javac --release 8 -Xlint:-options -encoding UTF-8 -d "$OUT/classes" \
    -cp "$PLATFORM:$DEPS/xz.jar:$DEPS/qrcodegen.jar" $(find src -name '*.java')
"$TOOLS/d8" --min-api 26 --lib "$PLATFORM" --output "$OUT/dex" \
    $(find "$OUT/classes" -name '*.class') "$DEPS/xz.jar" "$DEPS/qrcodegen.jar"

cp "$OUT/base.apk" "$OUT/unaligned.apk"
zip -qj "$OUT/unaligned.apk" "$OUT/dex/classes.dex"
"$TOOLS/zipalign" -f 4 "$OUT/unaligned.apk" "$OUT/aligned.apk"

KS=$HOME/.android/debug.keystore
if [ ! -f "$KS" ]; then
    mkdir -p "$(dirname "$KS")"
    keytool -genkeypair -keystore "$KS" -alias androiddebugkey -storepass android \
        -keypass android -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Android Debug,O=Android,C=US"
fi
"$TOOLS/apksigner" sign --ks "$KS" --ks-pass pass:android --ks-key-alias androiddebugkey \
    --key-pass pass:android --out "$OUT/tprint.apk" "$OUT/aligned.apk"
rm -f "$OUT/unaligned.apk" "$OUT/aligned.apk" "$OUT/tprint.apk.idsig"
echo "built android/$OUT/tprint.apk ($(du -k "$OUT/tprint.apk" | cut -f1) KB)"
