#!/bin/sh
# Builds libusb for the Macs the plugin is shipped for, from a libusb source
# tarball (brew fetch -s libusb prints where it is).
#
#   sh build.sh ~/Library/Caches/Homebrew/downloads/...--libusb-1.0.30.tar.bz2
#
# macOS 11 is the floor, so the library loads on every Mac Xverb runs on. The
# install name is @loader_path, and the result is signed ad hoc.
set -e
cd "$(dirname "$0")"
source=$1
here=$(pwd)
for target in macos-arm64 macos-x64; do
  case $target in
    macos-arm64) arch=arm64; host="" ;;
    macos-x64) arch=x86_64; host="--host=x86_64-apple-darwin" ;;
  esac
  work=$(mktemp -d)
  tar xf "$source" -C "$work" --strip-components 1
  (cd "$work" && ./configure $host CC="clang -arch $arch -mmacosx-version-min=11.0" \
      --disable-static > /dev/null && make -j8 > /dev/null)
  mkdir -p "$target"
  cp "$work/libusb/.libs/libusb-1.0.0.dylib" "$target/"
  install_name_tool -id @loader_path/libusb-1.0.0.dylib "$target/libusb-1.0.0.dylib"
  strip -x "$target/libusb-1.0.0.dylib"
  codesign -f -s - "$target/libusb-1.0.0.dylib"
  chmod 644 "$target/libusb-1.0.0.dylib"
  rm -rf "$work"
  echo "$target/libusb-1.0.0.dylib $(wc -c < "$target/libusb-1.0.0.dylib") bytes"
done
cd "$here"
