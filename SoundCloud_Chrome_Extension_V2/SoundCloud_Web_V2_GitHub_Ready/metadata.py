"""Metadata and album-art handling for the exact SoundCloud track.

Only uses fields from the downloaded track's yt-dlp metadata. Does not lookup
other recordings, and never transcodes audio to add tags/artwork.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import pathlib
import re
import subprocess
import urllib.parse
import urllib.request

from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, TCON, TALB, TDRC, TIT2, TPE1, TPE2, TPUB, TPOS, TRCK, TXXX, WOAS
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

SOUNDCLOUD_IMAGE_HOST = re.compile(r'(^|\.)sndcdn\.com$', re.IGNORECASE)
# Keep local filenames compatible with Mac/Windows DJ libraries.
BAD_FILE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_str(value):
    return str(value).strip() if value is not None else ''


def _first(items):
    if isinstance(items, list):
        return _safe_str(next((v for v in items if isinstance(v, str) and v.strip()), ''))
    return _safe_str(items) if isinstance(items, str) else ''


def _date(info):
    # Prefer a real release date to an upload timestamp; do not invent one.
    for key, origin in (('release_date', 'release'), ('upload_date', 'upload')):
        v = _safe_str(info.get(key))
        if re.fullmatch(r'\d{8}', v):
            return f'{v[:4]}-{v[4:6]}-{v[6:]}', origin
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', v):
            return v, origin
    for key, origin in (('release_timestamp', 'release'), ('timestamp', 'upload')):
        if isinstance(info.get(key), (float, int)) and info[key] > 0:
            return datetime.fromtimestamp(info[key], timezone.utc).strftime('%Y-%m-%d'), origin
    return '', ''


def parse_track(info: dict) -> dict:
    raw_title = _safe_str(info.get('track') or info.get('title')) or 'Untitled'
    uploader = _safe_str(info.get('uploader'))
    explicit_artist = _safe_str(info.get('artist')) or _first(info.get('artists'))
    credited_artist, title = '', raw_title
    # SoundCloud label accounts routinely put real artists in the track title.
    # Parse only the FIRST visible " - " separator, keeping remix names intact.
    if ' - ' in raw_title:
        head, rest = raw_title.split(' - ', 1)
        if 1 <= len(head) <= 110 and 1 <= len(rest) <= 250:
            credited_artist, title = head.strip(), rest.strip()
    if credited_artist and (not explicit_artist or explicit_artist.casefold() == uploader.casefold()):
        artist = credited_artist
    elif explicit_artist:
        artist = explicit_artist
        # Only remove the prefix if it is actually the credited artist.
        if credited_artist and explicit_artist.casefold() == credited_artist.casefold():
            title = raw_title.split(' - ', 1)[1]
        elif credited_artist:
            title = raw_title
    else:
        artist = uploader or credited_artist or 'Unknown Artist'
    date, date_source = _date(info)
    genres = info.get('genres')
    genre = ', '.join(str(v) for v in genres if v) if isinstance(genres, list) else _safe_str(info.get('genre'))
    tags = info.get('tags')
    if not isinstance(tags, (str, list)):
        tags = ''
    if isinstance(tags, list):
        tags = ', '.join(str(x) for x in tags if x)
    original_url = _safe_str(info.get('webpage_url'))
    if not original_url or not original_url.startswith('https://soundcloud.com/'):
        original_url = _safe_str(info.get('_source_url'))
    # Never retain a numeric SoundCloud ID at the end of a real title.
    scid = _safe_str(info.get('id'))
    if scid:
        title = re.sub(r'\s*\[' + re.escape(scid) + r'\]$', '', title).strip()
    album = _safe_str(info.get('album'))
    albumartist = _safe_str(info.get('album_artist')) or _first(info.get('album_artists'))
    track_number = info.get('track_number')
    disc_number = info.get('disc_number')
    return {
        'title': title, 'artist': artist, 'uploader': uploader,
        'album': album, 'album_artist': albumartist, 'genre': genre,
        'date': date, 'date_source': date_source,
        'description': _safe_str(info.get('description')),
        'license': _safe_str(info.get('license')),
        'soundcloud_url': original_url,
        'soundcloud_id': scid,
        'tags': tags,
        'track_number': str(track_number) if track_number else '',
        'disc_number': str(disc_number) if disc_number else '',
    }


def filename_for(meta, extension: str):
    base = f"{meta['artist']} - {meta['title']}"
    base = BAD_FILE_CHARS.sub(' ', base)
    base = re.sub(r'\s+', ' ', base).strip(' .')
    # Avoid excessively long names and invisible or reserved characters.
    base = (base[:190].rstrip(' .') or 'SoundCloud Track')
    ext = re.sub(r'[^a-zA-Z0-9]', '', extension.lower()) or 'audio'
    return f'{base}.{ext}'


def artwork_candidates(info):
    thumbs = info.get('thumbnails') or []
    ordering = {'original': 0, 't500x500': 1, 'crop': 2, 't300x300': 3, 'large': 4}
    scored = []
    for thumb in thumbs:
        if not isinstance(thumb, dict) or not thumb.get('url'):
            continue
        rank = ordering.get(_safe_str(thumb.get('id')), 20)
        width = thumb.get('width') or 0
        scored.append((rank, -width if isinstance(width, (float, int)) else 0, thumb['url']))
    scored.sort()
    result = []
    for _, _, url in scored:
        if url not in result:
            result.append(url)
    fallback = info.get('thumbnail')
    if fallback and fallback not in result:
        result.append(fallback)
    return result


def image_safe_url(raw):
    try:
        p = urllib.parse.urlsplit(raw)
        return (p.scheme == 'https' and not p.username and not p.password and
                p.port in (None, 443) and bool(SOUNDCLOUD_IMAGE_HOST.search(p.hostname or '')))
    except (ValueError, TypeError):
        return False


class SafeArtworkRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not image_safe_url(newurl):
            raise ValueError('Artwork redirected outside SoundCloud CDN')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def artwork_bytes(info):
    opener = urllib.request.build_opener(SafeArtworkRedirect())
    for url in artwork_candidates(info):
        if not image_safe_url(url):
            continue
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'image/jpeg,image/png,image/webp'})
            with opener.open(req, timeout=15) as response:
                body = response.read(12 * 1024 * 1024 + 1)
                if len(body) > 12 * 1024 * 1024:
                    continue
                if body.startswith(b'\xff\xd8\xff'):
                    return body, 'image/jpeg', url
                if body.startswith(b'\x89PNG\r\n\x1a\n'):
                    return body, 'image/png', url
                # WebP can be converted to JPEG if ffmpeg is available.
                if body.startswith(b'RIFF') and body[8:12] == b'WEBP':
                    return body, 'image/webp', url
        except (OSError, ValueError, TimeoutError):
            continue
    return None, None, None


def normalize_artwork(content, mime, tmpdir: pathlib.Path, ffmpeg: str | None):
    if content is None:
        return None, None
    if mime in ('image/png', 'image/jpeg'):
        return content, mime
    if mime == 'image/webp' and ffmpeg:
        original = tmpdir / 'cover.webp'
        output = tmpdir / 'cover.jpg'
        original.write_bytes(content)
        proc = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(original), '-frames:v', '1', str(output)],
                              capture_output=True, timeout=30)
        if proc.returncode == 0 and output.is_file():
            return output.read_bytes(), 'image/jpeg'
    return None, None


def _common_map(meta):
    # Only write known values; preserve any existing tags for unknown fields.
    vals = {'TITLE': meta.get('title'), 'ARTIST': meta.get('artist'),
            'ALBUM': meta.get('album'), 'ALBUMARTIST': meta.get('album_artist'),
            'GENRE': meta.get('genre'), 'DATE': meta.get('date'),
            'COMMENT': meta.get('description'), 'PUBLISHER': meta.get('uploader'),
            'SOUNDCLOUD_URL': meta.get('soundcloud_url'),
            'SOUNDCLOUD_ID': meta.get('soundcloud_id'),
            'SOUNDCLOUD_TAGS': meta.get('tags'),
            'SOUNDCLOUD_LICENSE': meta.get('license'),
            'SOUNDCLOUD_DATE_SOURCE': meta.get('date_source'),
            'TRACKNUMBER': meta.get('track_number'), 'DISCNUMBER': meta.get('disc_number')}
    return {key: str(value) for key, value in vals.items() if value}


def _id3_tags(tag, meta, image, mime):
    frames = [('title', TIT2), ('artist', TPE1), ('album', TALB),
              ('album_artist', TPE2), ('genre', TCON), ('date', TDRC),
              ('uploader', TPUB), ('track_number', TRCK), ('disc_number', TPOS)]
    for name, frame in frames:
        if meta.get(name):
            tag.add(frame(encoding=3, text=[meta[name]]))
    if meta.get('description'):
        tag.add(COMM(encoding=3, lang='eng', desc='SoundCloud description', text=[meta['description']]))
    if meta.get('soundcloud_url'):
        tag.add(WOAS(url=meta['soundcloud_url']))
    for key in ('soundcloud_id', 'soundcloud_url', 'license', 'tags', 'date_source'):
        if meta.get(key):
            tag.add(TXXX(encoding=3, desc=key.upper(), text=[meta[key]]))
    if image:
        tag.delall('APIC')
        tag.add(APIC(encoding=3, mime=mime, type=3, desc='Front cover', data=image))


def _mp4_tags(tags, meta, image, mime):
    mappings = {
        'title': '\xa9nam', 'artist': '\xa9ART', 'album': '\xa9alb',
        'album_artist': 'aART', 'date': '\xa9day', 'genre': '\xa9gen',
        'description': '\xa9cmt'
    }
    for key, code in mappings.items():
        if meta.get(key):
            tags[code] = [meta[key]]
    if meta.get('track_number'):
        try:
            tags['trkn'] = [(int(meta['track_number']), 0)]
        except ValueError:
            pass
    if meta.get('disc_number'):
        try:
            tags['disk'] = [(int(meta['disc_number']), 0)]
        except ValueError:
            pass
    for key in ('soundcloud_url', 'soundcloud_id', 'license', 'tags', 'date_source', 'uploader'):
        if meta.get(key):
            tags[f'----:com.apple.iTunes:{key.upper()}'] = [meta[key].encode('utf-8')]
    if image:
        image_type = MP4Cover.FORMAT_PNG if mime == 'image/png' else MP4Cover.FORMAT_JPEG
        tags['covr'] = [MP4Cover(image, imageformat=image_type)]


def embed_tags(file_path: pathlib.Path, meta: dict, image=None, mime=None) -> dict:
    """Write all available metadata; return reporting status. Do not change audio data."""
    suffix = file_path.suffix.lower()
    if suffix in ('.m4a', '.mp4', '.m4b'):
        audio = MP4(file_path)
        if audio.tags is None:
            audio.add_tags()
        _mp4_tags(audio.tags, meta, image, mime)
        audio.save()
    elif suffix in ('.mp3', '.aiff', '.aif', '.wav'):
        cls = {'.mp3': MP3, '.aiff': AIFF, '.aif': AIFF, '.wav': WAVE}[suffix]
        audio = cls(file_path)
        if audio.tags is None:
            audio.add_tags()
        _id3_tags(audio.tags, meta, image, mime)
        audio.save()
    elif suffix in ('.flac', '.opus', '.ogg'):
        cls = {'.flac': FLAC, '.opus': OggOpus, '.ogg': OggVorbis}[suffix]
        audio = cls(file_path)
        for key, value in _common_map(meta).items():
            audio[key] = [value]
        if image:
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.desc = 'Front cover'
            pic.data = image
            if suffix == '.flac':
                audio.clear_pictures()
                audio.add_picture(pic)
            else:
                audio['METADATA_BLOCK_PICTURE'] = [base64.b64encode(pic.write()).decode('ascii')]
        audio.save()
    else:
        return {'embedded': False, 'artwork_embedded': False, 'reason': f'No embedded tagging handler for {suffix or "unknown format"}'}
    return {'embedded': True, 'artwork_embedded': bool(image), 'reason': ''}


def safe_export_metadata(meta):
    """In a separate hidden history dir, store relevant source metadata without signed media URLs."""
    return {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))}
