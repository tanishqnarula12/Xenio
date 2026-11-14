#!/bin/bash
# Builds xenio-hands (the agents' private browser, and the cursor layer) from this folder. Needs Apple's command line tools. Output: native/build/xenio-hands, ad-hoc signed.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$here/../build/xenio-hands"
mkdir -p "$here/../build"
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
swiftc -O -swift-version 5 -sdk "$sdk" -target "$(uname -m)-apple-macos13.0" "$here/main.swift" "$here/vault.swift" -framework Cocoa -framework WebKit -framework CryptoKit -framework LocalAuthentication -framework Security -framework ApplicationServices -o "$out.tmp"
codesign --force --sign - "$out.tmp" >/dev/null 2>&1 || true
mv "$out.tmp" "$out"
