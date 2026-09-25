#!/bin/sh
# Builds the native EXR decoder for the machines that have none of their own,
# from this folder's src/, with zig as the cross compiler (brew install zig).
#
#   sh build.sh              # windows-x64, linux-x64, linux-arm64
#   sh build.sh macos-arm64  # one target, e.g. for testing on a Mac
#
# macOS is not shipped: the plugin asks ImageIO there. Linux targets glibc
# 2.17, so the library loads on anything from CentOS 7 on. The C++ runtime is
# linked in, so each library is one file with nothing beside it to install.
set -e
cd "$(dirname "$0")"
targets=${*:-"windows-x64 linux-x64 linux-arm64"}
for target in $targets; do
  case $target in
    windows-x64) triple=x86_64-windows-gnu; out=xvexr.dll; extra="" ;;
    linux-x64) triple=x86_64-linux-gnu.2.17; out=libxvexr.so; extra="-lpthread" ;;
    linux-arm64) triple=aarch64-linux-gnu.2.17; out=libxvexr.so; extra="-lpthread" ;;
    macos-arm64) triple=aarch64-macos; out=libxvexr.dylib; extra="" ;;
    macos-x64) triple=x86_64-macos; out=libxvexr.dylib; extra="" ;;
    *) echo "unknown target $target"; exit 1 ;;
  esac
  mkdir -p "$target" .build
  zig cc -target $triple -O2 -fPIC -c src/miniz.c -o .build/miniz-$target.o
  zig c++ -target $triple -O2 -fPIC -std=c++11 -fvisibility=hidden -shared -s \
    -Isrc src/xvexr.cc .build/miniz-$target.o $extra -o "$target/$out"
  rm -f "$target"/*.lib "$target"/*.pdb
  echo "$target/$out $(wc -c < "$target/$out") bytes"
done
rm -rf .build
