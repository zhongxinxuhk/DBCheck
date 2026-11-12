#!/bin/sh
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
set -eu
DBPULSE_PORTABLE_HOME=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export DBPULSE_PORTABLE_HOME
case "$(uname -s)" in Linux) dbpulse_os=linux ;; Darwin) dbpulse_os=macos ;; *) echo 'Unsupported operating system'; exit 1 ;; esac
case "$(uname -m)" in x86_64|amd64) dbpulse_arch=x86_64 ;; aarch64|arm64) dbpulse_arch=arm64 ;; *) echo 'Unsupported CPU architecture'; exit 1 ;; esac
dbpulse_platform="$dbpulse_os-$dbpulse_arch"
dbpulse_binary="$DBPULSE_PORTABLE_HOME/bin/$dbpulse_platform/dbcheck"
JAVA_HOME="$DBPULSE_PORTABLE_HOME/runtime/$dbpulse_platform/jre"
export JAVA_HOME
if [ ! -f "$dbpulse_binary" ] || [ ! -f "$JAVA_HOME/bin/java" ]; then
    echo "Incomplete portable package for $dbpulse_platform"
    exit 1
fi
# Windows extraction/copy can drop POSIX execution bits.
chmod u+x "$dbpulse_binary"
find "$JAVA_HOME/bin" -type f -exec chmod u+x {} \;
if [ -f "$JAVA_HOME/lib/jspawnhelper" ]; then chmod u+x "$JAVA_HOME/lib/jspawnhelper"; fi
cd "$DBPULSE_PORTABLE_HOME"
exec "$dbpulse_binary" "$@"
