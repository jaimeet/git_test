#!/bin/sh
# Make /data writable by the app user even if the host folder was created as root (e.g. with sudo mkdir),
# then drop root and run the app as that user.
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p /data/images
    if find /data \( ! -user hisab -o ! -group hisab \) -print -quit | grep -q .; then
        chown -R hisab:hisab /data
    fi
    exec setpriv --reuid=hisab --regid=hisab --init-groups "$@"
fi
exec "$@"
