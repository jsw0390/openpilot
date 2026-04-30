#!/usr/bin/env bash
CONNECT="$(cat /data/params/d/EnableConnect 2>/dev/null || echo 0)"

case "$CONNECT" in
  2)
    export API_HOST="https://api.carrotpilot.app"
    export ATHENA_HOST="wss://athena.carrotpilot.app"
    ;;
  3)
    export API_HOST="https://api.konik.ai/"
    export ATHENA_HOST="wss://athena.konik.ai"
    ;;
esac

exec ./launch_chffrplus.sh
