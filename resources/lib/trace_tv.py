# -*- coding: utf-8 -*-
"""Trace TV live channels for NewPipe MOD.

Each channel keeps a primary address and optional fallbacks; before playback
the first address that answers is chosen, so a dead CDN does not leave Kodi
with a silent failure while another mirror is still online.
"""
from __future__ import absolute_import

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


_CACHE_PROPERTY = 'newpipe.trace.stream.{0}'
_CACHE_SECONDS = 600
_PROBE_TIMEOUT = 3


def _alive(url, timeout=_PROBE_TIMEOUT):
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


def _window():
    try:
        import xbmcgui
        return xbmcgui.Window(10000)
    except Exception:
        return None


def _cached(channel_id, urls):
    window = _window()
    if window is None:
        return ''
    try:
        stamp, url = window.getProperty(_CACHE_PROPERTY.format(channel_id)).split('|', 1)
        if url in urls and time.time() - float(stamp) < _CACHE_SECONDS:
            return url
    except (ValueError, TypeError):
        pass
    return ''


def _remember(channel_id, url):
    window = _window()
    if window is not None:
        window.setProperty(_CACHE_PROPERTY.format(channel_id), '{0}|{1}'.format(time.time(), url))


def stream_url(channel_id):
    """Return the first working stream address (or the primary one).

    All mirrors are probed at the same time with a short timeout, so a dead
    primary costs at most ~3 s instead of 5 s per address. The chosen address
    is remembered for 10 minutes, so zapping back to a channel starts at once.
    """
    entry = channel(channel_id)
    if not entry:
        return ''
    urls = entry[3]
    if len(urls) == 1:
        return urls[0]
    cached = _cached(channel_id, urls)
    if cached:
        return cached
    results = {}
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        futures = {pool.submit(_alive, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            results[url] = future.result()
            # Stop waiting once the preferred address that is still pending
            # cannot beat one already known to work.
            for candidate in urls:
                if candidate not in results:
                    break
                if results[candidate]:
                    _remember(channel_id, candidate)
                    return candidate
    return urls[0]
