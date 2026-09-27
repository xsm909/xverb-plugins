#!/bin/sh
# Builds libarchive for the machines that have none of their own, with zig as
# the cross compiler (brew install zig). The source is fetched, checked against
# its sha256 and thrown away afterwards; nothing of it is kept in this folder
# but the licence, COPYING.libarchive.
#
#   sh build.sh              # windows-x64, linux-x64, linux-arm64
#   sh build.sh linux-x64    # one target
#
# macOS is not shipped: /usr/lib/libarchive.2.dylib is part of the system.
#
# **No zlib, no iconv, no OpenSSL.** RAR and RAR5 are libarchive's own code
# from end to end, so each library is one file that needs the C runtime and
# nothing else — the Windows one the UCRT every Windows 10 has, the Linux ones
# glibc 2.17, CentOS 7 and on. The configuration is the one libarchive keeps
# for building on a host without autoconf (contrib/android/config), with the
# libraries it would look for switched off.
set -e
cd "$(dirname "$0")"
VERSION=3.8.1
SHA256=19f917d42d530f98815ac824d90c7eaf648e9d9a50e4f309c812457ffa5496b5
targets=${*:-"windows-x64 linux-x64 linux-arm64"}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL -o "$work/src.tar.xz" \
  "https://github.com/libarchive/libarchive/releases/download/v$VERSION/libarchive-$VERSION.tar.xz"
echo "$SHA256  $work/src.tar.xz" | shasum -a 256 -c -
tar -xf "$work/src.tar.xz" -C "$work"
src="$work/libarchive-$VERSION"

cat > "$src/xv_linux.h" <<'EOF'
#include "contrib/android/config/linux_host.h"
#undef HAVE_ZLIB_H
#undef HAVE_ICONV
#undef HAVE_ICONV_H
#define __LIBARCHIVE_CONFIG_H_INCLUDED 1
EOF
{
  echo '#include "contrib/android/config/windows_host.h"'
  echo '#undef HAVE_ZLIB_H'
  echo '#undef HAVE_ICONV'
  echo '#undef HAVE_ICONV_H'
  echo '#define __LIBARCHIVE_CONFIG_H_INCLUDED 1'
  for m in $(grep -o "^#define ARCHIVE_CRYPTO_[A-Z0-9_]*" \
      "$src/contrib/android/config/windows_host.h" | awk '{print $2}'); do
    echo "#undef $m"
  done
  echo '#include <windows.h>'
  echo '#include <wincrypt.h>'
} > "$src/xv_windows.h"

common=$(cd "$src" && ls libarchive/*.c | grep -v '_windows\|_posix\|test')
here=$(pwd)
for target in $targets; do
  case $target in
    windows-x64) triple=x86_64-windows-gnu; out=archive.dll; config=xv_windows.h ;;
    linux-x64) triple=x86_64-linux-gnu.2.17; out=libarchive.so; config=xv_linux.h ;;
    linux-arm64) triple=aarch64-linux-gnu.2.17; out=libarchive.so; config=xv_linux.h ;;
    *) echo "unknown target $target"; exit 1 ;;
  esac
  mkdir -p "$here/$target"
  if [ "${target%%-*}" = windows ]; then
    (cd "$src" && zig cc -target $triple -O2 -shared -s -w \
      -DPLATFORM_CONFIG_H="\"$config\"" -I. -Ilibarchive \
      $common libarchive/*_windows.c -ladvapi32 -o "$here/$target/$out")
  else
    (cd "$src" && zig cc -target $triple -O2 -fPIC -shared -s -w \
      -DPLATFORM_CONFIG_H="\"$config\"" -I. -Icontrib/android/include \
      $common libarchive/*_posix.c -o "$here/$target/$out")
  fi
  rm -f "$here/$target"/*.lib "$here/$target"/*.pdb
  echo "$target/$out $(wc -c < "$here/$target/$out") bytes"
done
