# -*- coding: utf-8 -*-
"""Trace TV live channels for NewPipe MOD.

Stream addresses come from the public CanaisTVPT list
(https://github.com/thomraider12/canaistvpt, ``pt.m3u`` and ``radios.html``).
Each channel keeps a primary address and optional fallbacks; before playback
the first address that answers is chosen, so a dead CDN does not leave Kodi
with a silent failure while another mirror is still online.
"""
from __future__ import absolute_import

from urllib.request import Request, urlopen

from tulip.log import log

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')

# (id, label, logo file in resources/media, [stream urls in order of preference])
CHANNELS = [
    ('urban', 'Trace Urban', 'traceurban', [
        'https://channels.trace.plus/Traceprod/URBAN_INTER_hd/index.m3u8',
        'https://cdn-uw2-prod.tsv2.amagi.tv/linear/amg00398-zeasneuropebv-traceurban-zeasn/playlist.m3u8',
    ]),
    ('toca', 'Trace Toca', 'tracetoca', [
        'https://channels.trace.plus/Traceprod/TOCA_hd/index.m3u8',
    ]),
    ('latina', 'Trace Latina (FR)', 'tracelatina', [
        'https://channels.trace.plus/Traceprod/LATINA_hd/index.m3u8',
        'https://cdn-uw2-prod.tsv2.amagi.tv/linear/tracetvAA-trace-latina-lg-es/playlist.m3u8',
    ]),
    ('brasil', 'Trace Brasil (BR)', 'tracebrasil', [
        'https://amg01131-tracetv-tracebrazuca-samsungbr-rfenz.amagi.tv/playlist/'
        'amg01131-tracetv-tracebrazuca-samsungbr/playlist.m3u8',
        'https://cdn-uw2-prod.tsv2.amagi.tv/linear/amg01131-tracetv-tracebrazuca-xiaomi/playlist.m3u8',
    ]),
]


def channel(channel_id):
    for entry in CHANNELS:
        if entry[0] == channel_id:
            return entry
    return None


def _alive(url, timeout=5):
    try:
        request = Request(url, headers={'User-Agent': USER_AGENT})
        with urlopen(request, timeout=timeout) as response:
            if response.getcode() != 200:
                return False
            head = response.read(512)
            return b'#EXTM3U' in head
    except Exception as exc:
        log('NewPipe Trace: {0} indisponível: {1}'.format(url, exc))
        return False


def stream_url(channel_id):
    """Return the first working stream address (or the primary one)."""
    entry = channel(channel_id)
    if not entry:
        return ''
    urls = entry[3]
    if len(urls) > 1:
        for url in urls:
            if _alive(url):
                return url
    return urls[0]
