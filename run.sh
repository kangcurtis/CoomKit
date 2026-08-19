#!/usr/bin/env bash
# CoomKit launcher — stdlib Python only, nothing to install.
set -euo pipefail
cd "$(dirname "$0")"
# Fail with a sentence, not a TypeError traceback. The annotations use PEP 604
# unions (`str | None`), which are evaluated at import time and are a hard 3.10
# requirement — on 3.9 the first import dies with "unsupported operand type(s)
# for |", which reads like a bug in CoomKit rather than an old interpreter.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "CoomKit needs Python 3.10 or newer. You have $(python3 -V 2>&1)." >&2
  echo "Nothing to install — just point python3 at a newer interpreter." >&2
  exit 1
fi

exec python3 server.py "$@"
