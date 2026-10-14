# -*- coding: utf-8 -*-
"""High-contrast, large QR artwork for the NewPipe MOD login submenu.

The QR remains deliberately free of a logo or text overlay. Short-lived
activation codes must scan reliably from a TV screen, so the professional card
design is kept entirely outside the QR quiet zone.
"""
from __future__ import absolute_import

import hashlib
import os
import struct
import zlib

import xbmcaddon
import xbmcvfs
import qrcode


# Restrained, high-contrast card palette. The red accent remains outside the
# QR quiet zone, so scanners see the standard black modules on white.
_CARD_DARK = (8, 15, 28)
_CARD_ACCENT = (235, 43, 35)
_CARD_WHITE = (255, 255, 255)
_QR_DARK = (5, 10, 18)
_BORDER = 28
_ACCENT = 6
_PADDING = 40


def _profile_file(filename):
    profile = xbmcaddon.Addon('plugin.video.newpipe').getAddonInfo('profile')
    profile = xbmcvfs.translatePath(profile)
    if not xbmcvfs.exists(profile):
        xbmcvfs.mkdirs(profile)
    return os.path.join(profile, filename)


def _qr_file(qr_url):
    """Use a unique name, preventing Kodi from reusing an old QR texture."""
    digest = hashlib.sha256(str(qr_url).encode('utf-8')).hexdigest()[:16]
    return _profile_file('newpipe-youtube-login-qr-{0}.png'.format(digest))


def _chunk(kind, payload):
    return (struct.pack('>I', len(payload)) + kind + payload +
            struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff))


def _write_rgb_png(path, width, height, row_builder):
    """Write a true-colour PNG without platform image dependencies."""
    if width <= 0 or height <= 0:
        raise ValueError('Cannot create an empty PNG')
    raw = bytearray()
    for y in range(height):
        row = row_builder(y)
        if len(row) != width * 3:
            raise ValueError('Invalid RGB row width')
        raw.append(0)  # PNG filter: None
        raw.extend(row)
    data = (b'\x89PNG\r\n\x1a\n' +
            _chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            _chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + _chunk(b'IEND', b''))
    with open(path, 'wb') as handle:
        handle.write(data)


def _write_qr_png(value, path, module_size=16):
    """Write a high-definition QR card with a generous white quiet zone."""
    encoder = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=1,
        border=4,
    )
    encoder.add_data(value)
    encoder.make(fit=True)
    matrix = encoder.get_matrix()
    modules = len(matrix)
    if not modules:
        raise ValueError('Cannot create an empty QR matrix')

    qr_size = modules * module_size
    inner_offset = _BORDER + _ACCENT + _PADDING
    canvas_size = qr_size + 2 * inner_offset
    frame_limit = _BORDER
    accent_limit = _BORDER + _ACCENT
    qr_start = inner_offset
    qr_end = qr_start + qr_size

    def row_builder(y):
        row = bytearray()
        for x in range(canvas_size):
            if x < frame_limit or y < frame_limit or x >= canvas_size - frame_limit or y >= canvas_size - frame_limit:
                color = _CARD_DARK
            elif (x < accent_limit or y < accent_limit or
                  x >= canvas_size - accent_limit or y >= canvas_size - accent_limit):
                color = _CARD_ACCENT
            elif qr_start <= x < qr_end and qr_start <= y < qr_end:
                dark = matrix[(y - qr_start) // module_size][(x - qr_start) // module_size]
                color = _QR_DARK if dark else _CARD_WHITE
            else:
                color = _CARD_WHITE
            row.extend(color)
        return row

    _write_rgb_png(path, canvas_size, canvas_size, row_builder)


def create(qr_url):
    """Create the professional QR card shown by Kodi's native picture viewer."""
    if not qr_url:
        raise ValueError('QR URL is required')
    qr_path = _qr_file(qr_url)
    _write_qr_png(qr_url, qr_path)
    return qr_path
