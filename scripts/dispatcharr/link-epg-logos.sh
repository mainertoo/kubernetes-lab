#!/usr/bin/env bash
# Link EPG guide data + logos for every channel in a Dispatcharr profile.
#
# The REST API create does NOT auto-link EPG/logos the way the UI does, so after
# `playlist.py apply ... --commit` run this to:
#   - set each channel's logo from its first stream's logo_url
#   - link each channel to the EPGData (active source, most programmes) matching its tvg_id
#
# Dispatcharr only stores programmes for *mapped* channels at EPG-fetch time, so this
# also kicks a refresh of the active EPG sources afterwards so programmes populate.
#
# Usage:  ./link-epg-logos.sh [ProfileName]   (default: Core)
set -euo pipefail
PROFILE="${1:-Core}"
CTX="${KCTX:-production}"
NS="${KNS:-media}"
POD=$(kubectl --context "$CTX" -n "$NS" get pod -l app.kubernetes.io/name=dispatcharr -o jsonpath='{.items[0].metadata.name}')
echo "Linking EPG+logos for profile '$PROFILE' via pod $POD ..."

kubectl --context "$CTX" -n "$NS" exec "$POD" -- sh -c "cd /app && PROFILE='$PROFILE' python manage.py shell -c '
import os
from django.db.models import Count
from apps.channels.models import Channel, Logo, ChannelProfile
from apps.epg.models import EPGData, EPGSource

prof = ChannelProfile.objects.get(name=os.environ[\"PROFILE\"])
chans = Channel.objects.filter(channelprofilemembership__channel_profile=prof,
                               channelprofilemembership__enabled=True)

def best_epg(tvg):
    if not tvg:
        return None
    qs = list(EPGData.objects.filter(tvg_id=tvg))
    if not qs:
        return None
    def score(e):
        s = e.epg_source
        return (1 if (s and s.is_active) else 0, e.programs.count())
    qs.sort(key=score, reverse=True)
    return qs[0]

fe = fl = 0
for ch in chans:
    chg = False
    ed = best_epg(ch.tvg_id)
    if ed and ch.epg_data_id != ed.id:
        ch.epg_data = ed; fe += 1; chg = True
    if not ch.logo_id:
        st = ch.streams.first()
        url = getattr(st, \"logo_url\", None) if st else None
        if url:
            lg, _ = Logo.objects.get_or_create(url=url, defaults={\"name\": ch.name})
            ch.logo = lg; fl += 1; chg = True
    if chg:
        ch.save()
print(\"epg linked:\", fe, \"| logos set:\", fl)

from apps.epg.tasks import refresh_epg_data
for s in EPGSource.objects.filter(is_active=True):
    refresh_epg_data.delay(s.id)
print(\"queued EPG refresh for active sources\")
'"
echo "Done. Allow a few minutes for the EPG refresh, then reload the guide in Plex/Jellyfin."
