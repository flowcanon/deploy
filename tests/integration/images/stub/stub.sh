#!/bin/sh
# Stand-in service for flow-deploy's integration tests. Behavior comes from env:
#
#   STUB_VERSION         reported in logs; baked per image tag
#   STUB_HEALTH          pass (default) | fail | after:N  (healthy after N seconds)
#   STUB_SIGTERM_DELAY   seconds to keep running after SIGTERM before exiting
#   STUB_IGNORE_SIGTERM  1 to ignore SIGTERM entirely (only SIGKILL stops it)
#   STUB_CRASH           1 to exit 1 immediately (a crash loop under a restart policy)
#
# Health is a file the compose healthcheck tests for: /tmp/healthy.

echo "stub ${STUB_VERSION} starting"
if [ "${STUB_CRASH}" = 1 ]; then
    echo "stub ${STUB_VERSION} crashing"
    exit 1
fi

rm -f /tmp/healthy
case "${STUB_HEALTH:-pass}" in
    pass) touch /tmp/healthy ;;
    fail) ;;
    after:*) (sleep "${STUB_HEALTH#after:}" && touch /tmp/healthy) & ;;
esac

on_term() {
    echo "stub ${STUB_VERSION} got SIGTERM"
    if [ "${STUB_IGNORE_SIGTERM}" = 1 ]; then
        return
    fi
    sleep "${STUB_SIGTERM_DELAY:-0}"
    echo "stub ${STUB_VERSION} exiting"
    exit 0
}
trap on_term TERM

while true; do
    sleep 1 &
    wait $!
done
