# -*- coding: utf-8 -*-
"""Fast, native direct playback for NewPipe.

The resolver uses the same mobile-client sequence as the working trailer path
and gives one combined audio/video URL straight to Kodi. TV clients are not
probed because anonymous YouTube responses from them are unreliable.
"""

from __future__ import absolute_import

import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from tulip import directory, kodi
from tulip.log import log

from . import ui
from . import localization
from . import storage


# The lightweight YouTube resolver is bundled in resources/lib/ytresolver.
# It replaces the external PluginsGR / ResolveURL add-on chain so installation
# does not depend on any script.module package being present in Kodi.
_BUNDLED_ENGINE_PATH = os.path.dirname(__file__)
# Keep fallbacks mobile-only.  TV clients cause anonymous "Please sign in" and
# "The page needs to be reloaded" responses on this device.
_DIRECT_CLIENT_ATTEMPTS = (
    ('iOS', ('ios',)),
    ('iOS compatibility', ('ios_testsuite_params',)),
)
# Music had been pinned to Android compatibility, whose combined file is often
# 360p only. Try the same iOS mobile clients used by trailers first: they
# already return the working 720p/1080p direct variant on this Kodi install.
# Android remains the final fallback for a video that iOS cannot resolve.
_MUSIC_CLIENT_ATTEMPTS = (
    ('iOS', ('ios',)),
    ('iOS compatibility', ('ios_testsuite_params',)),
    ('Android compatibility fallback', ('android_testsuite_params',)),
)
_SAFE_HEADERS = (
    'User-Agent',
    'Referer',
    'Origin',
    'Accept-Language',
    'X-YouTube-Client-Name',
    'X-YouTube-Client-Version',
    'X-Goog-Visitor-Id',
)
_STREAM_CACHE_TTL = 15 * 60
_STREAM_CACHE_CAP = 30
# Invalidate results from versions that rewrote HLS to a local file URL.
_STREAM_CACHE_VERSION = 12
_RESOLVE_LOCK_TTL = 35
_HLS_STREAM_CACHE_TTL = 60
_HLS_RESOLUTION_RE = re.compile(r'(?:^|,)RESOLUTION=(\d+)x(\d+)(?:,|$)', re.I)
_HLS_URI_RE = re.compile(r'URI=(?:"([^"]+)"|([^,]+))', re.I)
_ORIGINAL_AUDIO_REQUEST_STATE = 'force_original_audio'
_ORIGINAL_AUDIO_REQUEST_TTL = 120


def _error(message):
    """Log errors at Kodi's visible error level; Tulip logging may be debug-only."""

    try:
        import xbmc
        xbmc.log('[NewPipe] {0}'.format(message), xbmc.LOGERROR)
    except Exception:
        log('NewPipe: {0}'.format(message))


def _info(message):
    try:
        import xbmc
        xbmc.log('[NewPipe] {0}'.format(message), xbmc.LOGINFO)
    except Exception:
        log('NewPipe: {0}'.format(message))


def _profile_dir():
    try:
        path = kodi.transPath('special://profile/addon_data/plugin.video.newpipe/')
    except Exception:
        path = ''
    if not path or path.startswith('special://'):
        path = os.path.join(os.getcwd(), 'newpipe-progressive')
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _state_path(name):
    return os.path.join(_profile_dir(), name)


def _read_state(name, default):
    try:
        with open(_state_path(name), encoding='utf-8') as handle:
            value = json.load(handle)
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError, TypeError):
        return default


def _write_state(name, value):
    path = _state_path(name)
    temporary = path + '.tmp'
    try:
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(',', ':'))
        os.replace(temporary, path)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass


def _playback_profile(profile):
    value = str(profile or '').lower()
    if value == 'trailer':
        return 'trailer'
    if value == 'music':
        return 'music'
    if value == 'subscription':
        return 'subscription'
    return 'default'


def _quality_value(profile='default'):
    profile = _playback_profile(profile)
    setting = 'trailer_max_height' if profile == 'trailer' else 'direct_max_height'
    default = '2160' if profile == 'trailer' else '1080'
    value = kodi.setting(setting) or default
    try:
        height = int(value)
    except (TypeError, ValueError):
        height = int(default)
    # Kodi keeps add-on settings across upgrades. Normalize legacy or manually
    # edited values to the nearest supported ceiling, never above the chosen
    # maximum. This keeps 4K, 1440p, 1080p and lower choices deterministic.
    supported = (360, 480, 720, 1080, 1440, 2160)
    height = max((candidate for candidate in supported if candidate <= height),
                 default=360)
    # Music playback shares the user's direct-playback limit. Capping this
    # private profile at 480p made a configured 720p/1080p unreachable.
    return height


def _quality_index(max_height):
    """Map the NewPipe setting to the native resolver's progressive quality."""

    return {360: 1, 480: 2, 720: 3, 1080: 4, 1440: 5, 2160: 6}.get(max_height, 3)


def _fast_start_enabled():
    # Existing installations have no stored value, so the new safe default is on.
    return (kodi.setting('fast_start') or 'true').lower() != 'false'


def _audio_preference(profile='default'):
    """Map the NewPipe content-language setting to an audio-selection policy.

    YouTube can provide translated/dubbed tracks alongside the creator's audio.
    The direct resolver used by this add-on must not inherit the account or UI
    language as an implicit instruction to choose a dub.  Original audio is
    therefore the standard policy. Portuguese dubs remain available only when
    Brazilian Portuguese is explicitly chosen as the NewPipe content language.
    The subscriptions feed always plays the creator's original audio.
    """
    if _playback_profile(profile) == 'subscription':
        return 'original', 'original', False
    configured = localization.content_language().replace('_', '-').casefold()
    if configured == 'pt-br':
        return 'pt-br', 'pt', True
    return 'original', 'original', False


def _cache_key(video_id, audio_only, profile):
    audio_mode, _language, _prefer_default = _audio_preference(profile)
    return 'v{0}:{1}:{2}:{3}:{4}:{5}:{6}'.format(
        _STREAM_CACHE_VERSION,
        video_id,
        int(bool(audio_only)),
        _playback_profile(profile),
        _quality_value(profile),
        int(_fast_start_enabled()),
        audio_mode)


def _stream_expiry(stream, now, cache_ttl=_STREAM_CACHE_TTL):
    """Return a conservative local expiry, respecting YouTube URL expiry if present."""

    expires = now + cache_ttl
    try:
        query = parse_qs(urlparse(stream.split('|', 1)[0]).query)
        remote = int((query.get('expire') or ['0'])[0])
        if remote:
            # Leave two minutes before Google's signature expiry.
            expires = min(expires, remote - 120)
    except (TypeError, ValueError):
        pass
    return expires


def _cached_stream(video_id, audio_only, profile):
    now = int(time.time())
    payload = _read_state('progressive_streams.json', {'items': {}})
    entry = (payload.get('items') or {}).get(_cache_key(video_id, audio_only, profile), {})
    stream = entry.get('stream') if isinstance(entry, dict) else ''
    expires = entry.get('expires', 0) if isinstance(entry, dict) else 0
    if isinstance(stream, str) and stream.startswith(('http://', 'https://')) and expires > now:
        _info('stream direto em cache para {0}'.format(video_id))
        return {'url': stream, 'hls': bool(entry.get('hls', False))}
    return None


def _save_stream(video_id, audio_only, profile, resolved):
    stream = resolved.get('url', '') if isinstance(resolved, dict) else ''
    if not isinstance(stream, str) or not stream.startswith(('http://', 'https://')):
        return
    now = int(time.time())
    # HLS manifests contain short-lived signed chunk URLs.  Do not retain one
    # after a transient 403: a subsequent play should obtain a fresh manifest.
    cache_ttl = _HLS_STREAM_CACHE_TTL if resolved.get('hls') else _STREAM_CACHE_TTL
    expires = _stream_expiry(stream, now, cache_ttl=cache_ttl)
    if expires <= now:
        return
    payload = _read_state('progressive_streams.json', {'items': {}})
    items = payload.get('items') or {}
    items = {
        key: item for key, item in items.items()
        if isinstance(item, dict) and item.get('expires', 0) > now
    }
    items[_cache_key(video_id, audio_only, profile)] = {
        'stream': stream,
        'hls': bool(resolved.get('hls', False)),
        'expires': expires,
        'saved': now,
    }
    if len(items) > _STREAM_CACHE_CAP:
        ordered = sorted(items.items(), key=lambda pair: pair[1].get('saved', 0), reverse=True)
        items = dict(ordered[:_STREAM_CACHE_CAP])
    _write_state('progressive_streams.json', {'items': items})


def _acquire_resolve_slot(video_id):
    """Avoid overlapping Kodi demuxers when the user changes video while loading."""

    now = int(time.time())
    active = _read_state('progressive_resolve_lock.json', {})
    started = active.get('started', 0) if isinstance(active, dict) else 0
    try:
        busy = now - int(started) < _RESOLVE_LOCK_TTL
    except (TypeError, ValueError):
        busy = False
    if busy:
        return False
    _write_state('progressive_resolve_lock.json', {'video_id': video_id, 'started': now})
    return True


def _clear_resolve_slot():
    try:
        os.remove(_state_path('progressive_resolve_lock.json'))
    except OSError:
        pass


def _pluginsgr_path():
    path = _BUNDLED_ENGINE_PATH
    if not os.path.isdir(os.path.join(path, 'ytresolver')):
        raise RuntimeError('O motor interno de reprodução não foi encontrado.')
    if path not in sys.path:
        sys.path.insert(0, path)
    return path


def _engine_context(max_height, audio_language, prefer_default_audio):
    """Create an isolated resolver context with NewPipe's quality policy."""

    _pluginsgr_path()
    from ytresolver.kodion.context.standalone import StandaloneContext

    class NewPipePlaybackContext(StandaloneContext):
        """Standalone engine context with NewPipe's explicit audio policy."""

        def get_player_language(self):
            # The bundled resolver expects ``(language, prefer_default)``.
            # Its stock standalone context returns the bare string ``en`` and
            # thus turns an English account/UI into an unintended dub bias.
            return audio_language, prefer_default_audio

    try:
        temp_dir = kodi.transPath('special://temp/newpipe-progressive/')
        profile_dir = _profile_dir()
    except Exception:
        temp_dir = profile_dir = ''

    if not temp_dir or temp_dir.startswith('special://'):
        temp_dir = os.path.join(os.getcwd(), 'newpipe-progressive')
    if not profile_dir or profile_dir.startswith('special://'):
        profile_dir = temp_dir
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(profile_dir, exist_ok=True)

    context = NewPipePlaybackContext(
        data_dir=temp_dir,
        config_file=os.path.join(profile_dir, 'ytresolver-progressive.json'),
        # H.264/AAC is the safest combined stream for the Android device.
        video_codecs=['avc1'],
    )
    settings = context.get_settings()
    settings.use_mpd_videos(False)
    settings.fixed_video_quality(_quality_index(max_height))
    return context


def _stream_with_headers(url, headers):
    """Encode the YouTube client headers Kodi needs for direct Googlevideo I/O."""

    if (url or '').startswith('file://'):
        return url
    selected = {}
    for name in _SAFE_HEADERS:
        value = (headers or {}).get(name)
        if value:
            selected[name] = str(value)
    return '{0}|{1}'.format(url, urlencode(selected)) if selected else url


def _is_hls_stream(item, url):
    """Identify manifests so Kodi uses inputstream.adaptive instead of file demuxing."""

    container = str((item or {}).get('container') or '').lower()
    base_url = (url or '').split('|', 1)[0].lower()
    return container in ('hls', 'm3u8') or '.m3u8' in base_url or '/manifest/hls' in base_url


def _inputstream_quality_properties(max_height):
    """Constrain adaptive selections to the same quality chosen in NewPipe settings.

    InputStream Adaptive otherwise uses the Android screen size (3120x1440 on
    the test device) and can select a representation above the add-on setting.
    The chooser accepts resolution families; 4K and 1440p must therefore be
    passed through instead of being collapsed to the old 1080p ceiling.
    """
    try:
        height = int(max_height)
    except (TypeError, ValueError):
        height = 720
    family = (480 if height <= 480 else
              720 if height <= 720 else
              1080 if height <= 1080 else
              1440 if height <= 1440 else 2160)
    return {
        'inputstream.adaptive.stream_selection_type': 'fixed-res',
        'inputstream.adaptive.chooser_resolution_max': '{0}p'.format(family),
    }


def _inputstream_adaptive_available():
    """Return whether Kodi can actually open an HLS item with Adaptive.

    The addon is deliberately self-contained and therefore cannot require
    InputStream Adaptive as a hard dependency. On Kodi builds without that
    binary addon, HLS must be passed to the native player with its HTTP headers
    intact; otherwise ``directory.resolve`` treats it as adaptive, drops the
    headers from the media URL and playback fails silently.
    """
    try:
        details = kodi.addon_details('inputstream.adaptive') or {}
        return bool(details.get('enabled'))
    except Exception:
        return False


def _audio_track_priority(item, audio_mode):
    """Rank a progressive stream by the requested audio policy.

    ``audioTrack.id`` is provided by YouTube in the form ``language.role``.
    Role 4/-1 identifies original/main audio, while 3 and 10 are respectively
    published and automatically generated dubs.  Unknown single-track formats
    remain a valid fallback but never outrank a confirmed original track.
    """
    track = item.get('audio_track') or {}
    if not isinstance(track, dict):
        track = {}
    track_id = str(track.get('id') or '').casefold().replace('_', '-')
    language, _separator, role = track_id.partition('.')
    name = str(track.get('name') or '').casefold()
    is_default = bool(track.get('is_default'))
    is_original = role in ('4', '-1') or 'original' in name
    is_dub = role in ('3', '10') or 'dub' in name

    if audio_mode == 'pt-br':
        if language == 'pt-br':
            language_rank = 2
        elif language == 'pt' or language.startswith('pt-'):
            language_rank = 1
        else:
            language_rank = 0
        if language_rank:
            return (4, language_rank, int(is_original), int(is_default))
        # An unmarked one-track response is safer than a known non-Portuguese
        # dub when Brazilian Portuguese was explicitly requested.
        return (2, 0, 0, 0) if not track else (0, 0, 0, 0)

    if is_original:
        return (4, int(is_default), 0, 0)
    if not track:
        return (2, 0, 0, 0)
    # A default flag can be useful when no original track is exposed, but a
    # declared translated/dubbed track must be the last fallback.
    return (1 if is_default and not is_dub else 0, 0, 0, 0)


def _prefer_audio_candidates(candidates, audio_mode):
    if not candidates:
        return candidates
    ranked = [(_audio_track_priority(item, audio_mode), item) for item in candidates]
    best = max(rank for rank, _item in ranked)
    return [item for rank, item in ranked if rank == best]


def _video_height(item):
    try:
        return int((item.get('video') or {}).get('height') or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def _candidate_summary(streams, limit=12):
    """Return safe diagnostics without putting signed media URLs in the log."""

    result = []
    for item in list(streams)[:limit]:
        container = str((item or {}).get('container') or '?')
        result.append('{0}:{1}p'.format(container, _video_height(item)))
    return ', '.join(result) or 'nenhum'


def _hls_variant_entries(manifest_url, manifest):
    """Return HLS video variants as ``(height, tag, absolute URI)`` tuples."""

    variants = []
    lines = (manifest or '').splitlines()
    for index, line in enumerate(lines):
        if not line.startswith('#EXT-X-STREAM-INF:'):
            continue
        match = _HLS_RESOLUTION_RE.search(line)
        if not match:
            continue
        try:
            height = int(match.group(2))
        except (TypeError, ValueError):
            continue
        for candidate in lines[index + 1:]:
            candidate = candidate.strip()
            if not candidate:
                continue
            if candidate.startswith('#'):
                break
            variants.append((height, line, urljoin(manifest_url, candidate)))
            break
    return variants


def _hls_variant_from_manifest(manifest_url, manifest, max_height):
    """Return the best media-playlist URL not exceeding ``max_height``.

    Kodi's built-in HLS demuxer on this Android installation ignores adaptive
    chooser properties and selects the 2160p rendition from a master playlist.
    Selecting one media playlist here enforces the NewPipe maximum before the
    player starts downloading chunks.
    """

    try:
        limit = int(max_height)
    except (TypeError, ValueError):
        limit = 720
    variants = _hls_variant_entries(manifest_url, manifest)
    within_limit = [item for item in variants if item[0] <= limit]
    if within_limit:
        height, _tag, variant_url = max(within_limit, key=lambda item: item[0])
        return height, variant_url
    return None, None


def _absolute_hls_uri(line, manifest_url):
    """Convert the URI attribute in an EXT-X-MEDIA tag to an absolute URL."""

    match = _HLS_URI_RE.search(line or '')
    if not match:
        return line
    value = match.group(1) or match.group(2) or ''
    replacement = 'URI="{0}"'.format(urljoin(manifest_url, value))
    return ''.join((line[:match.start()], replacement, line[match.end():]))


def _hls_restricted_master(manifest_url, manifest, max_height):
    """Keep one video rendition while retaining the master playlist's audio.

    YouTube HLS video representations are video-only. Passing the selected
    child playlist directly drops its ``EXT-X-MEDIA`` audio group, which leaves
    Kodi with a picture but no audio. This produces a small valid master
    playlist containing the selected video plus the original audio declarations.
    """

    try:
        limit = int(max_height)
    except (TypeError, ValueError):
        limit = 720
    variants = _hls_variant_entries(manifest_url, manifest)
    within_limit = [item for item in variants if item[0] <= limit]
    if not within_limit:
        return None, None
    height, selected_tag, selected_uri = max(within_limit, key=lambda item: item[0])
    audio_tags = []
    global_tags = []
    for line in (manifest or '').splitlines():
        line = line.strip()
        if not line or line == '#EXTM3U':
            continue
        if line.startswith('#EXT-X-MEDIA:'):
            audio_tags.append(_absolute_hls_uri(line, manifest_url))
        elif line.startswith('#EXT-X-STREAM-INF:') or not line.startswith('#'):
            continue
        elif line.startswith('#EXT-X-I-FRAME-STREAM-INF:'):
            continue
        else:
            global_tags.append(line)
    restricted = '\n'.join(['#EXTM3U'] + global_tags + audio_tags + [
        selected_tag, selected_uri, ''
    ])
    return height, restricted


def _write_hls_master(manifest_url, manifest):
    """Write a temporary local HLS master that Kodi can open as a playlist."""

    try:
        directory_path = kodi.transPath('special://temp/newpipe-progressive/')
    except Exception:
        directory_path = ''
    if not directory_path or directory_path.startswith('special://'):
        directory_path = _profile_dir()
    try:
        os.makedirs(directory_path, exist_ok=True)
        digest = hashlib.sha1(manifest_url.encode('utf-8')).hexdigest()[:20]
        path = os.path.join(directory_path, 'newpipe-hls-{0}.m3u8'.format(digest))
        temporary = path + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as handle:
            handle.write(manifest)
        os.replace(temporary, path)
        return 'file://' + path
    except OSError as exc:
        _info('não foi possível gravar manifesto HLS limitado: {0}'.format(exc))
        return None


def _fixed_hls_variant(url, headers, max_height):
    """Fetch a master HLS playlist and return a limited audio/video master."""

    manifest_url = (url or '').split('|', 1)[0]
    request_headers = {}
    for name in _SAFE_HEADERS:
        value = (headers or {}).get(name)
        if value:
            request_headers[name] = str(value)
    try:
        request = Request(manifest_url, headers=request_headers)
        with urlopen(request, timeout=6) as response:
            manifest = response.read().decode('utf-8', 'replace')
        height, restricted = _hls_restricted_master(
            manifest_url, manifest, max_height)
        if not restricted:
            return None, None
        local_url = _write_hls_master(manifest_url, restricted)
        return height, local_url
    except Exception as exc:
        _info('não foi possível fixar HLS em {0}p: {1}'.format(max_height, exc))
        return None, None


def _pick_progressive(streams, audio_only, fast_start, audio_mode='original',
                      max_height=720):
    if audio_only:
        candidates = [
            item for item in streams
            if item.get('url') and item.get('audio') and not item.get('video')
        ]
    else:
        candidates = [
            item for item in streams
            if item.get('url') and item.get('video') and item.get('audio')
        ]
    if audio_only:
        candidates = _prefer_audio_candidates(candidates, audio_mode)
        key = lambda item: (item.get('audio') or {}).get('bitrate', 0)
    else:
        try:
            requested_height = int(max_height)
        except (TypeError, ValueError):
            requested_height = 720
        # Never select a known variant above the NewPipe setting. The resolver
        # usually filters these already, but enforcing the limit here also
        # protects mixed mobile-client responses.
        within_limit = [item for item in candidates
                        if not _video_height(item)
                        or _video_height(item) <= requested_height]
        if within_limit:
            candidates = within_limit

        hls = [item for item in candidates
               if _is_hls_stream(item, item.get('url', ''))]
        if requested_height > 480:
            # On this Android Kodi build, HLS waits about 23 seconds for the
            # first demuxed stream. Prefer a direct 720p/1080p MP4 when one is
            # supplied; retain HLS only when no direct HD variant exists.
            direct_hd = [item for item in candidates
                         if not _is_hls_stream(item, item.get('url', ''))
                         and _video_height(item) > 480]
            # The resolver reports no height for a master HLS playlist even
            # when its representations include 720p/1080p. Treat height zero
            # as a high-quality fallback instead of silently downgrading it.
            high_hls = [item for item in hls
                        if not _video_height(item) or _video_height(item) > 480]
            if direct_hd:
                candidates = direct_hd
            elif high_hls:
                candidates = high_hls
        elif fast_start:
            # Retain the quick-start behavior only when the selected limit is
            # itself 360p/480p. It must not silently defeat a 720p/1080p choice.
            quick = [item for item in candidates if _video_height(item) <= 480]
            if quick:
                candidates = quick
        key = lambda item: (
            _video_height(item),
            int(not _is_hls_stream(item, item.get('url', ''))),
            _audio_track_priority(item, audio_mode),
            (item.get('audio') or {}).get('bitrate', 0),
        )
    return max(candidates, key=key) if candidates else None


def _pick_trailer_compatibility_stream(streams, audio_only, fast_start,
                                       audio_mode='original', max_height=2160):
    """Use the proven 1.4.34 selection path for Cinebox trailers.

    Kodi 22 RC1 on the affected Samsung device opens the trailer correctly
    with the old mobile response. The 1.4.41 HLS quality request may hand the
    Android MediaCodec renderer a different VP9 HLS ladder, after which the
    player closes at ``ReleaseOutputBuffer``. Keep the current selector for
    ordinary NewPipe playback, but use the exact, known-working selection
    policy when Cinebox sends ``profile=trailer``.
    """
    if audio_only:
        candidates = [
            item for item in streams
            if item.get('url') and item.get('audio') and not item.get('video')
        ]
    else:
        candidates = [
            item for item in streams
            if item.get('url') and item.get('video') and item.get('audio')
        ]
    candidates = _prefer_audio_candidates(candidates, audio_mode)
    if audio_only:
        key = lambda item: (item.get('audio') or {}).get('bitrate', 0)
    else:
        try:
            requested_height = int(max_height)
        except (TypeError, ValueError):
            requested_height = 2160
        # The compatibility resolver can return a complete quality ladder.
        # Enforce the trailer setting here as well as in the resolver context,
        # so a 1080p choice can never silently select a 4K stream.
        within_limit = [item for item in candidates
                        if not _video_height(item)
                        or _video_height(item) <= requested_height]
        if within_limit:
            candidates = within_limit
        # This is intentionally the 1.4.34 order: consume a conventional
        # progressive response when supplied, and otherwise keep YouTube's
        # original HLS manifest untouched for InputStream Adaptive.
        regular = [item for item in candidates
                   if not _is_hls_stream(item, item.get('url', ''))]
        if regular:
            candidates = regular
        if fast_start and regular:
            quick = [item for item in candidates
                     if _video_height(item) <= 480]
            if quick:
                candidates = quick
        key = lambda item: (
            _video_height(item),
            (item.get('audio') or {}).get('bitrate', 0),
        )
    return max(candidates, key=key) if candidates else None


def _client_attempts(profile):
    """Choose the bounded mobile client sequence for the playback profile."""
    return _MUSIC_CLIENT_ATTEMPTS if _playback_profile(profile) == 'music' else _DIRECT_CLIENT_ATTEMPTS


def _resolve_from_mobile_clients(video_id, audio_only, profile):
    """Resolve from one mobile-client group at a time, never probing TV clients."""

    _pluginsgr_path()
    from ytresolver.youtube.client.player_client import YouTubePlayerClient

    max_height = _quality_value(profile)
    fast_start = profile != 'trailer' and _fast_start_enabled()
    audio_mode, audio_language, prefer_default_audio = _audio_preference(profile)
    trailer_compatibility = _playback_profile(profile) == 'trailer'
    failures = []
    for label, client_names in _client_attempts(profile):
        try:
            client = YouTubePlayerClient(
                context=_engine_context(
                    max_height, audio_language, prefer_default_audio),
                clients=client_names)
            # `clients=` only inserts a custom group; without this override the
            # engine still continues into TV/test/VR groups after the iOS call.
            client._client_groups = (('newpipe_direct', client_names),)
            request = {
                'video_id': video_id,
                'use_mpd': False,
                'audio_only': audio_only,
            }
            # This omits ask_for_quality on purpose. It restores the exact
            # request made by v1.4.34 for Cinebox trailers, which the supplied
            # log proves starts successfully on this Kodi 22 installation.
            if not trailer_compatibility:
                request['ask_for_quality'] = not audio_only and max_height > 480
            streams, _item = client.load_stream_info(**request)
            stream_list = list(streams)
            _info('formatos via {0} para {1}: {2}'.format(
                label, video_id, _candidate_summary(stream_list)))
            if trailer_compatibility:
                selected = _pick_trailer_compatibility_stream(
                    stream_list, audio_only, fast_start, audio_mode=audio_mode,
                    max_height=max_height)
            else:
                selected = _pick_progressive(
                    stream_list, audio_only, fast_start, audio_mode=audio_mode,
                    max_height=max_height)
            if not selected:
                failures.append('{0}: sem stream A/V direto'.format(label))
                continue

            url = selected.get('url') or ''
            if not url.startswith(('http://', 'https://')):
                failures.append('{0}: URL inválida'.format(label))
                continue
            if '127.0.0.1' in url or '.mpd' in url:
                failures.append('{0}: retornou DASH/proxy'.format(label))
                continue

            hls = _is_hls_stream(selected, url)
            if hls:
                # Kodi 22's InputStream Adaptive rejects local file:// HLS
                # masters (CURLOpen failed). The 1.4.41 quality limiter wrote
                # one of those temporary files and caused every HLS-only
                # video, music item and normal NewPipe trailer to fail before
                # decoding. Keep YouTube's remote manifest intact: Tulip
                # passes its headers through InputStream Adaptive below.
                _info('HLS remoto preservado para {0}; sem manifesto file:// temporário.'.format(
                    video_id))
            _info('stream direto via {0} para {1}: itag={2}, formato={3}, altura={4}, hls={5}, perfil={6}, limite={7}p, áudio={8}'.format(
                label,
                video_id,
                selected.get('itag', '?'),
                selected.get('container', '?'),
                (selected.get('video') or {}).get('height', '?'),
                hls,
                profile,
                max_height,
                audio_mode))
            return {
                'url': _stream_with_headers(url, selected.get('headers') or {}),
                'hls': hls,
            }
        except Exception as exc:
            failures.append('{0}: {1}'.format(label, exc))

    raise RuntimeError('; '.join(failures) or 'Nenhum cliente móvel retornou vídeo direto.')


def resolve(video_id, audio_only=False, profile='default'):
    """Return a direct mobile-client stream for the selected quality."""

    profile = _playback_profile(profile)
    cached = _cached_stream(video_id, audio_only, profile)
    if cached:
        return cached
    try:
        stream = _resolve_from_mobile_clients(video_id, audio_only, profile)
        _save_stream(video_id, audio_only, profile, stream)
        return stream
    except Exception as exc:
        _error('Falha no stream progressivo para {0}: {1}'.format(video_id, exc))
        return None


def _resolve_failure():
    from tulip.init import syshandle
    kodi.resolve(syshandle, False, kodi.item())


def _request_subscription_original_audio(video_id):
    """Tell the resident service to select the HLS track marked ``original``.

    YouTube's HLS master has all dubbed tracks in one adaptive manifest. Kodi
    makes the final track choice after the plugin has returned the URL, so this
    short-lived private marker lets the resident service select the actual
    ``original`` row from Kodi's Audio stream menu once it becomes available.
    """
    storage.set_local_state(_ORIGINAL_AUDIO_REQUEST_STATE, {
        'video_id': video_id,
        'requested_at': int(time.time()),
        'expires': int(time.time()) + _ORIGINAL_AUDIO_REQUEST_TTL,
        'applied': False,
    })


def play(video_id, title='', image='', profile='default'):
    profile = _playback_profile(profile)
    audio_only = kodi.setting('audio_only') == 'true'
    max_height = _quality_value(profile)
    resolved = resolve(video_id, audio_only=audio_only, profile=profile)
    if not resolved:
        _clear_resolve_slot()
        kodi.infoDialog(ui.text(30179, 'Unable to obtain a playable video.'))
        _resolve_failure()
        return

    if profile == 'subscription':
        _request_subscription_original_audio(video_id)
    else:
        # A marker is intentionally limited to the single subscription item;
        # never allow it to alter an unrelated video opened immediately after.
        storage.set_local_state(_ORIGINAL_AUDIO_REQUEST_STATE, {})

    # A progressive file is given straight to Kodi. HLS is given to
    # InputStream Adaptive only when that binary addon is installed/enabled.
    # Without it, Kodi's native HLS reader receives the original URL *and* the
    # HTTP headers. Passing ``dash=True`` without Adaptive used to discard
    # those headers and leave the player on the previous screen.
    is_hls = bool(resolved.get('hls'))
    use_adaptive = is_hls and _inputstream_adaptive_available()
    log('NewPipe: reprodução direta selecionada ({0})'.format(
        'HLS adaptive' if use_adaptive else 'HLS nativo' if is_hls else 'arquivo A/V'))
    inputstream_properties = _inputstream_quality_properties(max_height) if use_adaptive else None
    if is_hls:
        if use_adaptive:
            _info('HLS entregue ao InputStream com limite {0}'.format(
                inputstream_properties.get('inputstream.adaptive.chooser_resolution_max')))
        else:
            _info('InputStream Adaptive indisponível; HLS entregue ao player nativo com cabeçalhos.')
    directory.resolve(
        resolved.get('url'), meta={'title': title}, icon=image,
        dash=use_adaptive,
        manifest_type='hls' if use_adaptive else None,
        inputstream_type='adaptive',
        mimetype='application/x-mpegURL' if is_hls else None,
        inputstream_properties=inputstream_properties,
    )
