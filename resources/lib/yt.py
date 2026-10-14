# -*- coding: utf-8 -*-
# NewPipe Addon
# Author Twilight0
# SPDX-License-Identifier: GPL-3.0-only
#
# Manual searches may remain cached briefly, but discovery surfaces (Trending,
# Live, Trailers, Random music and Shorts) intentionally fetch fresh YouTube
# data whenever their folder is opened.
import xbmc

from scrapetube.wrapper import (
    list_search,
    list_channel_videos,
    list_shorts,
    list_playlist_videos,
    list_playlists,
)

from .constants import cache_function, cache_duration
from . import localization


def configure():
    """Set the NewPipe-style YouTube content locale for the current request."""
    return localization.configure()


def _dedupe_video_results(source, limit):
    """Return Kodi video tuples with one card per YouTube video id."""
    seen_video_ids = set()
    results = []
    for item in source or []:
        video_id = (item.get('url') or '')[-11:]
        if not video_id or video_id in seen_video_ids:
            continue
        seen_video_ids.add(video_id)
        results.append((item, '', ''))
        if len(results) >= limit:
            break
    return results


@cache_function(cache_duration(15))
def _search(query, limit=25, sort='relevance', locale='pt:BR'):
    localization.configure()
    results = list_search(query, limit=limit, sleep=0, sort_by=sort) or []
    xbmc.log('[NewPipePagina] cache pesquisa: pedido={0} recebido={1} ordem={2} idioma={3}'.format(
        limit, len(results), sort, locale), xbmc.LOGINFO)
    return results


def search(query, limit=25, sort='relevance'):
    """Cached helper retained for manual searches and pagination."""
    return _search(query, limit=limit, sort=sort, locale=configure())


def _video_results(query, limit, sort, locale):
    return _dedupe_video_results(_search(query, limit=limit, sort=sort, locale=locale), limit)


def _fresh_video_results(query, limit, sort):
    """Load a new public result set; never reuse FunctionCache entries."""
    locale = configure()
    source = list_search(query, limit=limit, sleep=0, sort_by=sort) or []
    results = _dedupe_video_results(source, limit)
    xbmc.log('[NewPipePagina] navegação atualizada: pedido={0} recebido={1} ordem={2} idioma={3}'.format(
        limit, len(results), sort, locale), xbmc.LOGINFO)
    return results


@cache_function(cache_duration(15))
def _search_videos(query, limit=25, sort='relevance', locale='pt:BR'):
    return _video_results(query, limit, sort, locale)


def search_videos(query, limit=25, sort='relevance'):
    return _search_videos(query, limit=limit, sort=sort, locale=configure())


def fresh_search_videos(query, limit=25, sort='relevance'):
    """Uncached video search for rotating discovery screens."""
    return _fresh_video_results(query, limit, sort)


@cache_function(cache_duration(15))
def _search_playlists(query, limit=25, locale='pt:BR'):
    localization.configure()
    return list_search(query, limit=limit, sleep=0, results_type='playlist') or []


def search_playlists(query, limit=25):
    return _search_playlists(query, limit=limit, locale=configure())


@cache_function(cache_duration(15))
def _trending_videos(query, limit=25, locale='pt:BR'):
    return _video_results(localization.regional_category_query(query), limit, 'relevance', locale)


def trending_videos(query, limit=25):
    return _trending_videos(query, limit=limit, locale=configure())


def fresh_trending_videos(query, limit=25):
    return _fresh_video_results(localization.regional_category_query(query), limit, 'relevance')


@cache_function(cache_duration(10))
def _live_videos(query, limit=25, locale='pt:BR'):
    return _video_results(localization.regional_category_query(query), limit, 'relevance', locale)


def live_videos(query, limit=25):
    return _live_videos(query, limit=limit, locale=configure())


def fresh_live_videos(query, limit=25):
    return _fresh_video_results(localization.regional_category_query(query), limit, 'relevance')


@cache_function(cache_duration(5))
def _shorts_videos(limit=25, locale='pt:BR'):
    localization.configure()
    results = list_shorts(limit=limit, sleep=0) or []
    return [(item, '', '') for item in results]


def shorts_videos(limit=25):
    """Cached compatibility helper for callers outside the discovery menu."""
    return _shorts_videos(limit=limit, locale=configure())


def fresh_shorts_videos(limit=25):
    """Get the newest public Shorts reel for each opening of the folder."""
    configure()
    return [(item, '', '') for item in (list_shorts(limit=limit, sleep=0) or [])]


@cache_function(cache_duration(15))
def _trailer_videos(query, limit=25, sort='upload_date', locale='pt:BR'):
    return _video_results(query, limit, sort, locale)


def trailer_videos(query, limit=25):
    return _trailer_videos(query, limit=limit, sort='upload_date', locale=configure())


def fresh_trailer_videos(query, limit=25):
    return _fresh_video_results(query, limit, 'upload_date')


@cache_function(cache_duration(30))
def _channel_videos(url, tab='videos', limit=25, locale='pt:BR'):
    localization.configure()
    return list_channel_videos(channel_url=url, limit=limit, sleep=0, content_type=tab)


def channel_videos(url, tab='videos', limit=25):
    return _channel_videos(url, tab=tab, limit=limit, locale=configure())


@cache_function(cache_duration(60))
def _channel_playlists(url, limit=25, locale='pt:BR'):
    localization.configure()
    return list_playlists(url.rstrip('/') + '/playlists', limit=limit, sleep=0)


def channel_playlists(url, limit=25):
    return _channel_playlists(url, limit=limit, locale=configure())


@cache_function(cache_duration(30))
def _playlist_videos(playlist_id, limit=50, locale='pt:BR'):
    localization.configure()
    return list_playlist_videos(playlist_id, limit=limit, sleep=0)


def playlist_videos(playlist_id, limit=50):
    return _playlist_videos(playlist_id, limit=limit, locale=configure())
