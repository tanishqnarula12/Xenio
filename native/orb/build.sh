#!/bin/bash
# Builds "XENIO Orb.app" (the floating orb + summon shortcut + location helper) from this folder's Swift sources.
# Needs Apple's command line tools (xcode-select --install). Output: native/build/XENIO Orb.app, ad-hoc signed.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$here/../build/XENIO Orb.app"
rm -rf "$out.tmp"
mkdir -p "$out.tmp/Contents/MacOS"
cp "$here/Info.plist" "$out.tmp/Contents/Info.plist"
# the newest macOS SDK this compiler can read (Command Line Tools can ship an SDK newer than their swiftc)
compiler="$(swiftc --version 2>&1 | sed -n 's/.*Apple Swift version \([0-9.]*\).*/\1/p' | head -1)"
sdk=""
for candidate in $(ls -d /Library/Developer/CommandLineTools/SDKs/MacOSX[0-9]*.sdk /Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX*.sdk 2>/dev/null | sort -V); do
  [ -L "$candidate" ] && continue
  built="$(sed -n 's/.*swift-compiler-version: Apple Swift version \([0-9.]*\).*/\1/p' "$candidate/usr/lib/swift/Swift.swiftmodule/$(uname -m)e-apple-macos.swiftinterface" 2>/dev/null | head -1)"
  [ -z "$built" ] && built="$(sed -n 's/.*swift-compiler-version: Apple Swift version \([0-9.]*\).*/\1/p' "$candidate/usr/lib/swift/Swift.swiftmodule/$(uname -m)-apple-macos.swiftinterface" 2>/dev/null | head -1)"
  [ -n "$built" ] && [ "$(printf '%s\n%s\n' "$built" "$compiler" | sort -V | tail -1)" = "$compiler" ] && sdk="$candidate"
done
[ -n "$sdk" ] || { echo "No macOS SDK matches swiftc $compiler; run: softwareupdate --install -a (or xcode-select --install)" >&2; exit 4; }
swiftc -O -swift-version 5 -sdk "$sdk" -target "$(uname -m)-apple-macos13.0" \
  "$here/main.swift" "$here/TripleSpace.swift" \
  -framework Cocoa -framework WebKit -framework Carbon -framework CoreLocation \
  -o "$out.tmp/Contents/MacOS/XenioOrb"
# the app icon (Dock and Command-Tab while Mission Control is open), drawn from code: no image in the repository
mkdir -p "$out.tmp/Contents/Resources"
icon_tmp="$(mktemp -d)"
if swiftc -O -swift-version 5 -sdk "$sdk" -target "$(uname -m)-apple-macos13.0" "$here/make_icon.swift" -o "$icon_tmp/make_icon" 2>/dev/null \
   && "$icon_tmp/make_icon" "$icon_tmp/icon.png"; then
  mkdir -p "$icon_tmp/XENIO.iconset"
  for px in 16 32 128 256 512; do
    sips -z $px $px "$icon_tmp/icon.png" --out "$icon_tmp/XENIO.iconset/icon_${px}x${px}.png" >/dev/null
    sips -z $((px * 2)) $((px * 2)) "$icon_tmp/icon.png" --out "$icon_tmp/XENIO.iconset/icon_${px}x${px}@2x.png" >/dev/null
  done
  iconutil -c icns "$icon_tmp/XENIO.iconset" -o "$out.tmp/Contents/Resources/XENIO.icns" || echo "(no app icon: iconutil failed)" >&2
else
  echo "(no app icon: it could not be drawn)" >&2
fi
rm -rf "$icon_tmp"
codesign --force --sign - "$out.tmp" >/dev/null 2>&1
rm -rf "$out"
mv "$out.tmp" "$out"
echo "$out"
