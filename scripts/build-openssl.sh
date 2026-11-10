#!/bin/sh
set -eu

version=3.5.9
sha256=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
prefix=${1:?Usage: build-openssl.sh absolute-install-prefix}
case "$prefix" in /*) ;; *) echo 'Install prefix must be absolute' >&2; exit 1 ;; esac
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
archive="$work/openssl.tar.gz"
curl --fail --location --proto '=https' --tlsv1.2 "https://github.com/openssl/openssl/releases/download/openssl-$version/openssl-$version.tar.gz" -o "$archive"
printf '%s  %s\n' "$sha256" "$archive" | sha256sum -c -
tar -xzf "$archive" -C "$work"
cd "$work/openssl-$version"
./Configure "--prefix=$prefix" --libdir=lib no-shared no-tests no-module no-zlib -fPIC
make -j2
make install_sw
cp LICENSE.txt "$prefix/OPENSSL-LICENSE.txt"
printf '%s\n' "$version" > "$prefix/OPENSSL-VERSION"
