#!/usr/bin/env bash
# Render docs/architecture/system.svg inside a throwaway container.
#
# Graphviz and diagrams exist only in that container. CI never installs them:
# on 2026-09-09 an apt mirror served a bad hash and failed a pull request that
# had nothing to do with architecture, and a drawing tool must not be able to
# break somebody else's build again.
#
# Run this when the deployment changes, then commit the SVG. The test suite
# checks, without Graphviz, that the SVG still shows every compose service and
# every node declared in docs/architecture/system.py.
#
#   scripts/render_architecture.sh                   # system.svg
#   ARCH_FORMATS=svg,png scripts/render_architecture.sh
#                                                    # also a PNG to look at;
#                                                    # it is ignored by git
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

docker run --rm \
  -v "$ROOT:/repo" -w /repo \
  -e ARCH_FORMATS="${ARCH_FORMATS:-svg}" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  python:3.12-slim bash -c '
    set -euo pipefail
    apt-get update -qq >/dev/null
    apt-get install -y -qq --no-install-recommends graphviz >/dev/null
    pip install -q --root-user-action=ignore diagrams==0.25.1
    python docs/architecture/system.py
    chown "$HOST_UID:$HOST_GID" docs/architecture/system.svg
    if [ -f docs/architecture/system.png ]; then
      chown "$HOST_UID:$HOST_GID" docs/architecture/system.png
    fi
  '
echo "Rendered docs/architecture/system.svg"
