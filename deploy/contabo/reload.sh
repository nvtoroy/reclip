#!/bin/sh
# certbot --deploy-hook for the d.cegth.cc lineage.
#
# Registered per-lineage via "certbot certonly --deploy-hook", which writes
# renew_hook= into /etc/letsencrypt/renewal/d.cegth.cc.conf. It is deliberately
# NOT dropped into /etc/letsencrypt/renewal-hooks/deploy/: scripts there are
# global and run after EVERY lineage renews, so this one would fire for
# dva.melekh.in, r-service.cegth.cc and the sslip.io certificate too.
#
# The lineage guard below is belt and braces in case it is ever moved there.
# The unconditional exit 0 matters: a hook returning non-zero makes certbot
# report the renewal as failed and turns certbot.service red, which would hide
# a genuine renewal failure on one of the other three certificates.
[ "${RENEWED_LINEAGE:-/etc/letsencrypt/live/d.cegth.cc}" = /etc/letsencrypt/live/d.cegth.cc ] || exit 0

if docker inspect reclip-ingress >/dev/null 2>&1; then
    docker exec reclip-ingress nginx -s reload >/dev/null 2>&1 \
        || docker restart reclip-ingress >/dev/null 2>&1 \
        || true
fi

exit 0
