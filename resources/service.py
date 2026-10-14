# -*- coding: utf-8 -*-
"""Background helper for progressive playback and account-link completion."""

from __future__ import absolute_import

import json
import os
import sys
import time

import xbmc
import xbmcgui

# Kodi executes a service extension with ``resources/`` as the script path,
# unlike main.py which starts at the add-on root. Add that root explicitly so
# ``resources.lib`` can always be imported on Android as well as desktop Kodi.
_ADDON_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)
_LIBRARY_PATH = os.path.join(_ADDON_ROOT, 'resources', 'lib')
if _LIBRARY_PATH not in sys.path:
    sys.path.insert(0, _LIBRARY_PATH)

from resources.lib import random_music
from resources.lib import storage
from resources.lib import ui
from resources.lib import youtube_sync


_ACCOUNT_MENU_URL = 'plugin://plugin.video.newpipe/?action=subscriptions'
_NEXT_MUSIC_PREVIEW_SECONDS = 15
_NEXT_MUSIC_PREVIEW_DURATION = 5500
_ORIGINAL_AUDIO_REQUEST_STATE = 'force_original_audio'


def _synchronize_authorized_account():
    """Import subscriptions immediately after YouTube approves the QR link.

    The service owns this work so the person never has to return to the
    activation menu or select a manual confirmation action. Any later refresh
    remains possible through reconnecting the account.
    """
    try:
        xbmcgui.Dialog().notification(
            'NewPipe MOD',
            ui.text(30184, 'Synchronizing YouTube subscriptions…'),
            time=8000)
    except Exception:
        pass
    try:
        summary = youtube_sync.sync_library()
    except Exception as exc:
        message = ui.text(30070, 'YouTube sync failed: {0}').format(exc)
        xbmc.log('[NewPipe YouTube] sincronização automática falhou: {0}'.format(exc),
                 xbmc.LOGWARNING)
        try:
            xbmcgui.Dialog().notification('NewPipe MOD', message, time=8000)
        except Exception:
            pass
        return False
    message = ui.text(
        30200, 'YouTube synchronized automatically: {0} channels, {1} videos').format(
            summary.get('channels', 0), summary.get('videos', 0))
    try:
        xbmcgui.Dialog().notification('NewPipe MOD', message, time=8000)
    except Exception:
        pass
    _open_account_menu()
    return True


def _open_account_menu():
    """Replace the QR submenu with the account menu after authorization."""
    try:
        xbmc.executebuiltin('Container.Update({0},replace)'.format(
            _ACCOUNT_MENU_URL))
        xbmc.log('[NewPipe YouTube] menu de sincronização aberto', xbmc.LOGINFO)
    except Exception as exc:
        xbmc.log('[NewPipe YouTube] não foi possível abrir o menu da conta: {0}'.format(exc),
                 xbmc.LOGWARNING)


def _rpc(method, params):
    try:
        result = xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params,
        }))
        return json.loads(result).get('result')
    except Exception:
        return None


def _force_subscription_original_audio():
    """Select Kodi's actual ``original`` stream for one subscription video.

    Googlevideo HLS keeps every language in one master playlist, meaning that
    InputStream Adaptive, not the plugin resolver, selects its initial audio.
    Earlier versions of this service overrode that choice and always chose a
    Portuguese/English dubbed track. Only a short-lived subscription marker is
    now honoured, and only a stream explicitly labelled ``original`` is set.
    """
    state = storage.get_local_state(_ORIGINAL_AUDIO_REQUEST_STATE)
    if not state or state.get('applied'):
        return False
    try:
        if int(state.get('expires', 0)) <= int(time.time()):
            storage.set_local_state(_ORIGINAL_AUDIO_REQUEST_STATE, {})
            return False
    except (TypeError, ValueError):
        storage.set_local_state(_ORIGINAL_AUDIO_REQUEST_STATE, {})
        return False
    try:
        playing_file = xbmc.Player().getPlayingFile() or ''
    except Exception:
        playing_file = ''
    if 'googlevideo' not in playing_file.lower():
        return False

    data = _rpc('Player.GetProperties', {
        'playerid': 1,
        'properties': ['audiostreams', 'currentaudiostream'],
    })
    if not data:
        return False

    tracks = data.get('audiostreams') or []
    if not tracks:
        return False
    current = data.get('currentaudiostream') or {}
    current_index = current.get('index', -1)
    selected = next((track for track in tracks if 'original' in '{0} {1}'.format(
        track.get('language') or '', track.get('name') or '').casefold()), None)
    if not selected:
        # Keep Kodi's own selection when the manifest fails to describe an
        # original stream. Guessing by language could select a dub again.
        return False
    try:
        index = int(selected.get('index'))
    except (TypeError, ValueError):
        return False
    if index != current_index or current_index < 0:
        _rpc('Player.SetAudioStream', {'playerid': 1, 'stream': index})
    state['applied'] = True
    state['applied_at'] = int(time.time())
    state['selected_index'] = index
    storage.set_local_state(_ORIGINAL_AUDIO_REQUEST_STATE, state)
    xbmc.log('[NewPipe Playback] áudio original selecionado automaticamente: faixa {0}'.format(index),
             xbmc.LOGINFO)
    return True


def _remaining_seconds(player):
    """Return current remaining time, including a label fallback for HLS."""
    try:
        total = float(player.getTotalTime())
        elapsed = float(player.getTime())
        if total > elapsed >= 0:
            return total - elapsed
    except Exception:
        pass
    try:
        value = xbmc.getInfoLabel('Player.TimeRemaining').strip().lstrip('-')
        parts = [int(part) for part in value.split(':')]
        if parts and all(part >= 0 for part in parts):
            seconds = 0
            for part in parts:
                seconds = seconds * 60 + part
            return float(seconds)
    except Exception:
        pass
    return None


def _show_music_up_next(player, shown_key=''):
    """Show Kodi's native mini notification shortly before a queued song ends."""
    tracks = random_music.queue_tracks()
    if len(tracks) < 2:
        return ''
    try:
        if not player.isPlayingVideo():
            return shown_key
    except Exception:
        return shown_key
    position = random_music.queue_position()
    if position < 0:
        return shown_key
    upcoming = random_music.next_after(position)
    if not upcoming:
        return shown_key

    key = '{0}:{1}'.format(position, upcoming.get('video_id', ''))
    if key == shown_key:
        return shown_key
    remaining = _remaining_seconds(player)
    if remaining is None or remaining <= 0 or remaining > _NEXT_MUSIC_PREVIEW_SECONDS:
        return shown_key

    title = upcoming.get('title') or upcoming.get('video_id') or ''
    message = ui.text(30196, 'Up next: {0}').format(title)
    try:
        xbmcgui.Dialog().notification(
            'NewPipe MOD', message, upcoming.get('image') or '',
            _NEXT_MUSIC_PREVIEW_DURATION)
        xbmc.log('[NewPipe Random music] próxima faixa mostrada: {0}'.format(
            upcoming.get('video_id', '')), xbmc.LOGINFO)
    except Exception as exc:
        xbmc.log('[NewPipe Random music] prévia não exibida: {0}'.format(exc),
                 xbmc.LOGWARNING)
    return key


def _start_pending_music_playlist():
    """Start one complete native Kodi playlist after the source card exits."""
    tracks = random_music.consume_playlist_start()
    if not tracks:
        return False
    try:
        playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
        playlist.clear()
        for track in tracks:
            item = xbmcgui.ListItem(label=track.get('title') or track.get('video_id') or '')
            image = track.get('image') or ''
            if image:
                try:
                    item.setArt({'thumb': image, 'icon': image, 'fanart': image})
                except Exception:
                    pass
            try:
                item.setProperty('IsPlayable', 'true')
            except Exception:
                pass
            playlist.add(random_music.plugin_url(track), item)
        xbmc.Player().play(playlist)
        xbmc.log('[NewPipe Random music] playlist Kodi iniciada: {0} faixas'.format(
            len(tracks)), xbmc.LOGINFO)
        return True
    except Exception as exc:
        random_music.clear_queue()
        xbmc.log('[NewPipe Random music] playlist não iniciou: {0}'.format(exc),
                 xbmc.LOGWARNING)
        return False


class _PlaybackObserver(xbmc.Player):

    def __init__(self):
        super(_PlaybackObserver, self).__init__()

    def onAVStarted(self):
        self._configure_audio()

    def onPlayBackStarted(self):
        self._configure_audio()

    def onPlayBackStopped(self):
        """Treat an explicit Kodi Stop as cancellation of Random music."""
        if random_music.is_playlist_active() and random_music.cancel():
            xbmc.log('[NewPipe Random music] fila cancelada pelo botão Stop', xbmc.LOGINFO)

    def _configure_audio(self):
        try:
            current = self.getPlayingFile()
        except Exception:
            current = ''
        if 'googlevideo' not in (current or '').lower():
            return
        xbmc.sleep(1200)
        _force_subscription_original_audio()


def run():
    monitor = xbmc.Monitor()
    observer = _PlaybackObserver()
    last_login_error = ''
    last_poll = 0
    last_original_audio_try = 0
    last_preview = ''
    xbmc.log('[NewPipe Playback] helper progressivo iniciado', xbmc.LOGINFO)
    while not monitor.abortRequested():
        _start_pending_music_playlist()
        last_preview = _show_music_up_next(observer, last_preview)
        # Audio stream metadata can arrive shortly after onAVStarted. Retry
        # only while the one-shot marker is pending; after a successful switch
        # it is marked applied and manual selections remain untouched.
        if time.time() - last_original_audio_try >= 1:
            last_original_audio_try = time.time()
            _force_subscription_original_audio()
        if time.time() - last_poll >= 5:
            last_poll = time.time()
            try:
                result = youtube_sync.poll_pending_once()
                last_login_error = ''
                if result.get('state') == 'authorized':
                    _synchronize_authorized_account()
                elif result.get('state') == 'expired':
                    xbmcgui.Dialog().notification(
                        'NewPipe', 'O código de ligação do YouTube expirou', time=5000)
                elif result.get('state') == 'failed':
                    message = result.get('message', 'erro desconhecido')
                    if message != last_login_error:
                        xbmc.log('[NewPipe YouTube] ligação falhou: {0}'.format(message), xbmc.LOGWARNING)
                        last_login_error = message
            except Exception as exc:
                message = str(exc)
                if message != last_login_error:
                    xbmc.log('[NewPipe YouTube] serviço de ligação: {0}'.format(message), xbmc.LOGWARNING)
                    last_login_error = message
        if monitor.waitForAbort(0.25):
            break
    del observer


if __name__ == '__main__':
    run()
