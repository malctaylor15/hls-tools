#!/usr/bin/env python3
"""Find media tracks in a link list or a folder of HLS inputs."""
import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

import hls_download as hls
import subtitles_download as subtitles


@dataclass
class Source:
    name: str
    url: str = None
    headers: dict = None
    credential_origin: str = None
    text: str = None
    base_url: str = None
    language: str = None
    height: int = None
    group: str = None


@dataclass
class Track:
    kind: str
    source: Source
    playlist: hls.Playlist
    height: int = None
    language: str = None
    has_audio: bool = False


def source_from_text(name, text, path=None):
    text = text.strip()
    if text.startswith('curl '):
        url, copied = hls.parse_curl(text)
        return Source(name, url, {'user-agent': 'Python-HLS-Downloader/1.0',
            'accept': '*/*', **copied}, url)
    if text.startswith(('http://', 'https://')) and '\n' not in text:
        parsed = urllib.parse.urlsplit(text)
        if parsed.username or parsed.password:
            raise hls.DownloadError('URL contains embedded credentials.')
        return Source(name, text,
            {'user-agent': 'Python-HLS-Downloader/1.0', 'accept': '*/*'}, text)
    if path and path.suffix.lower() == '.m3u8':
        sidecar = path.with_name(path.name + '.base-url.txt')
        base = sidecar.read_text().strip() if sidecar.exists() else None
        return Source(name, text=text, base_url=base)
    raise hls.DownloadError('Expected one playlist URL, cURL command, or .m3u8 file.')


def bundle_entries(path):
    pending, start = '', 0
    for number, raw in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        if not pending:
            start = number
        pending += raw + '\n'
        if raw.rstrip().endswith('\\'):
            continue
        yield start, pending.strip()
        pending = ''
    if pending.strip():
        yield start, pending.strip()


def input_sources(location, keyword=None):
    sources = []
    if location.is_file():
        group = ''
        url_groups = {}
        warned_pairs = set()
        for number, entry in bundle_entries(location):
            if not entry or entry.startswith(('#', ';')):
                continue
            heading = re.fullmatch(r'\[([^]]+)\]', entry)
            if heading:
                group = heading[1].strip()
                continue
            if keyword and keyword.casefold() not in (group + ' ' + entry).casefold():
                continue
            name = f'{group or location.stem} / line {number}'
            try:
                if entry.startswith('@') or entry.lower().endswith('.m3u8') and not entry.startswith(('http://', 'https://')):
                    target = (location.parent / entry.lstrip('@')).resolve()
                    source = source_from_text(name, target.read_text(encoding='utf-8-sig'), target)
                else:
                    source = source_from_text(name, entry)
                source.group = group or location.stem
                if source.url and source.url in url_groups and url_groups[source.url] != source.group:
                    pair = (source.url, source.group)
                    if pair not in warned_pairs:
                        print(f'Warning: the same playlist appears under [{url_groups[source.url]}] '
                            f'and [{source.group}].', file=sys.stderr)
                        warned_pairs.add(pair)
                elif source.url:
                    url_groups[source.url] = source.group
                sources.append(source)
            except (hls.DownloadError, OSError, UnicodeError, ValueError) as error:
                reason = str(error) if isinstance(error, hls.DownloadError) else 'invalid or unreadable input'
                print(f'Skipped {name}: {reason}', file=sys.stderr)
        if not sources:
            raise hls.DownloadError('No matching playlist entries. Check --keyword or add a [Title] heading.')
        return sources
    for path in sorted(location.iterdir()):
        if not path.is_file() or path.name.startswith('.') or path.name.endswith('.base-url.txt'):
            continue
        if path.suffix.lower() not in ('.m3u8', '.txt', '.url'):
            continue
        try:
            text = path.read_text(encoding='utf-8-sig')
            if text.strip():
                source = source_from_text(path.name, text, path)
                source.group = location.name
                sources.append(source)
        except (hls.DownloadError, OSError, UnicodeError, ValueError) as error:
            reason = str(error) if isinstance(error, hls.DownloadError) else 'invalid or unreadable input'
            print(f'Skipped {path.name}: {reason}', file=sys.stderr)
    if not sources:
        raise hls.DownloadError('No .m3u8, .url, or .txt playlist inputs found in the folder.')
    return sources


def attributes(line):
    return {match[1]: match[3] if match[3] is not None else match[2]
            for match in re.finditer(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', line)}


def child_source(parent, uri, name, language=None, height=None):
    url = urllib.parse.urljoin(parent.base_url, uri) if parent.base_url else uri
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise hls.DownloadError(f'{parent.name}: master playlist child needs an HTTP(S) base URL.')
    return Source(name, url, parent.headers, parent.credential_origin,
        language=language, height=height, group=parent.group)


def expand_master(source, text):
    children = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.startswith('#EXT-X-MEDIA:'):
            attrs = attributes(line.partition(':')[2])
            if attrs.get('TYPE') in ('AUDIO', 'SUBTITLES') and attrs.get('URI'):
                children.append(child_source(source, attrs['URI'],
                    f'{source.name} / {attrs.get("NAME", attrs["TYPE"].lower())}',
                    language=attrs.get('LANGUAGE')))
        elif line.startswith('#EXT-X-STREAM-INF:'):
            attrs = attributes(line.partition(':')[2])
            if index + 1 >= len(lines) or lines[index + 1].startswith('#'):
                raise hls.DownloadError(f'{source.name}: master variant has no URI.')
            resolution = attrs.get('RESOLUTION', '')
            height = int(resolution.split('x')[-1]) if re.fullmatch(r'\d+x\d+', resolution) else None
            children.append(child_source(source, lines[index + 1],
                f'{source.name} / {height or "unknown"}p', height=height))
    return children


def read_source(source):
    if source.url:
        data, final = hls.fetch(source.url, source.headers, credential_origin=source.credential_origin)
        source.base_url = final
        return data.decode('utf-8-sig')
    return source.text


def classify(source, playlist):
    if playlist.issues:
        raise hls.DownloadError(f'{source.name}: unsupported playlist features: {", ".join(playlist.issues)}')
    if not shutil.which('ffprobe'):
        raise hls.DownloadError('FFprobe must be installed and on PATH to identify tracks.')
    with tempfile.TemporaryDirectory(prefix='hls-catalog-') as folder:
        probe_file = Path(folder) / ('init.mp4' if playlist.init_url else 'sample')
        url = playlist.init_url or playlist.segments[0].url
        hls.fetch(url, source.headers or {}, probe_file,
            credential_origin=source.credential_origin)
        with probe_file.open('rb') as stream:
            prefix = stream.read(32)
        if not playlist.init_url and prefix.lstrip(b'\xef\xbb\xbf').startswith(b'WEBVTT'):
            return Track('subtitles', source, playlist, language=source.language)
        info = hls.probe(probe_file)
    streams = info.get('streams', [])
    video = [stream for stream in streams if stream.get('codec_type') == 'video']
    audio = [stream for stream in streams if stream.get('codec_type') == 'audio']
    if video:
        height = source.height or max((stream.get('height', 0) for stream in video), default=0)
        return Track('video', source, playlist, height=height, has_audio=bool(audio))
    if audio:
        return Track('audio', source, playlist, language=source.language)
    raise hls.DownloadError(f'{source.name}: no supported audio, video, or WebVTT track found.')


def scan(location, keyword=None):
    queue = input_sources(location, keyword)
    tracks, seen = [], set()
    while queue:
        source = queue.pop(0)
        identity = (source.group, source.url)
        if source.url and identity in seen:
            continue
        try:
            text = read_source(source)
            if source.url:
                seen.add(identity)
            if '#EXT-X-STREAM-INF:' in text or '#EXT-X-MEDIA:' in text:
                queue.extend(expand_master(source, text))
                continue
            tracks.append(classify(source, hls.parse_playlist(text, source.base_url)))
        except (hls.DownloadError, OSError, UnicodeError, ValueError) as error:
            reason = str(error) if isinstance(error, hls.DownloadError) else 'invalid or unreadable input'
            print(f'Skipped {source.name}: {reason}', file=sys.stderr)
    if not tracks:
        raise hls.DownloadError('No usable tracks found. Replace expired links or inspect the skipped inputs.')
    return tracks


def choose(tracks, kind, number=None, quality='highest'):
    if number is not None:
        if not 1 <= number <= len(tracks) or tracks[number - 1].kind != kind:
            raise hls.DownloadError(f'Track {number} is not a {kind} track. Run list to see track numbers.')
        return tracks[number - 1]
    matches = [track for track in tracks if track.kind == kind]
    if not matches:
        raise hls.DownloadError(f'No {kind} track found.')
    if kind == 'video':
        return (min if quality == 'lowest' else max)(matches, key=lambda track: track.height or 0)
    return matches[0]


def download_audio(track, output):
    with tempfile.TemporaryDirectory(prefix='.hls-audio-', dir=output.parent) as folder:
        work = Path(folder)
        hls.download_segments(track.playlist, work, track.source.headers or {}, track.source.credential_origin)
        manifest = hls.local_manifest(track.playlist, work)
        staged = work / 'audio.m4a'
        result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-i', str(manifest),
            '-map', '0:a', '-c', 'copy', str(staged)], capture_output=True, text=True)
        if result.returncode or not staged.exists():
            raise hls.DownloadError('FFmpeg could not save the audio track.')
        streams = hls.probe(staged).get('streams', [])
        if not any(stream.get('codec_type') == 'audio' for stream in streams):
            raise hls.DownloadError('Output contains no audio.')
        created = False
        try:
            with output.open('xb') as destination, staged.open('rb') as source:
                created = True
                shutil.copyfileobj(source, destination)
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise


def download_subtitles(track, output, kind):
    with tempfile.TemporaryDirectory(prefix='.hls-subtitles-', dir=output.parent) as folder:
        texts = []
        for index, segment in enumerate(track.playlist.segments):
            path = Path(folder) / f'{index:06d}.vtt'
            hls.fetch(segment.url, track.source.headers or {}, path,
                credential_origin=track.source.credential_origin)
            text = path.read_text(encoding='utf-8-sig')
            subtitles.parse_vtt(text)
            texts.append(text)
            print(f'Downloaded {index + 1}/{len(track.playlist.segments)}', flush=True)
        cues, styles = subtitles.merge_segments(texts)
        if not cues:
            raise hls.DownloadError('Subtitle track has no cues.')
        created = False
        try:
            with output.open('x', encoding='utf-8', newline='\n') as destination:
                created = True
                destination.write(subtitles.render(cues, styles, kind))
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise


def download_video(track, audio, output):
    with tempfile.TemporaryDirectory(prefix='.hls-download-', dir=output.parent) as folder:
        work = Path(folder)
        hls.download_segments(track.playlist, work, track.source.headers or {}, track.source.credential_origin)
        audio_work = None
        if audio:
            audio_work = work / 'audio'
            audio_work.mkdir()
            hls.download_segments(audio.playlist, audio_work, audio.source.headers or {}, audio.source.credential_origin)
        hls.assemble(track.playlist, work, output, audio.playlist if audio else None, audio_work)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    listing = sub.add_parser('list', help='Identify tracks without downloading the full media')
    listing.add_argument('source', type=Path, help='Link list file or folder of playlist inputs')
    listing.add_argument('--keyword', help='Match a [Title] heading or text in a URL')
    downloading = sub.add_parser('download', help='Download a selected track')
    downloading.add_argument('source', type=Path, help='Link list file or folder of playlist inputs')
    downloading.add_argument('--keyword', help='Select a [Title] group or matching URLs')
    downloading.add_argument('--kind', choices=('video', 'audio', 'subtitles'), required=True)
    downloading.add_argument('--track', type=int, help='Track number shown by list')
    downloading.add_argument('--audio-track', type=int, help='Audio track number to pair with video')
    downloading.add_argument('--quality', choices=('highest', 'lowest'), default='highest')
    downloading.add_argument('--format', choices=('vtt', 'srt', 'txt'), default='srt')
    downloading.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        location = args.source.expanduser().resolve()
        if not location.exists() or not (location.is_file() or location.is_dir()):
            raise hls.DownloadError('Input link list or folder does not exist.')
        if args.action == 'download' and location.is_file() and not args.keyword:
            raise hls.DownloadError('Use --keyword to select one recording from a link list.')
        tracks = scan(location, args.keyword)
        if args.action == 'download' and location.is_file():
            groups = {track.source.group for track in tracks}
            if len(groups) > 1:
                raise hls.DownloadError('--keyword matched multiple titles; use a full [Title] heading.')
        for index, track in enumerate(tracks, 1):
            detail = f'{track.height}p' if track.kind == 'video' and track.height else track.language or ''
            print(f'{index:>2}  {track.kind:<9} {detail:<8} {track.source.name}')
        if args.action == 'list':
            return 0
        track = choose(tracks, args.kind, args.track, args.quality)
        audio = choose(tracks, 'audio', args.audio_track) if args.audio_track else (
            next((item for item in tracks if item.kind == 'audio'), None)
            if args.kind == 'video' and not track.has_audio else None)
        title = args.keyword if location.is_file() else location.name
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-') or 'media'
        suffix = '.mp4' if args.kind == 'video' else '.m4a' if args.kind == 'audio' else f'.{args.format}'
        label = f'-{track.height}p' if args.kind == 'video' and track.height else f'-{args.kind}'
        output = args.output.expanduser().absolute() if args.output else hls.default_output(slug + label + suffix)
        if output.exists():
            raise hls.DownloadError('Output already exists; choose another filename with --output.')
        if args.output and not output.parent.is_dir():
            raise hls.DownloadError('Output directory does not exist.')
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise hls.DownloadError('FFmpeg and FFprobe must be installed and on PATH.')
        if not args.output:
            output.parent.mkdir(exist_ok=True)
        if args.kind == 'video':
            download_video(track, audio, output)
        elif args.kind == 'audio':
            download_audio(track, output)
        else:
            download_subtitles(track, output, args.format)
        print(f'Saved {output}')
        return 0
    except (hls.DownloadError, OSError, UnicodeError, ValueError, KeyboardInterrupt) as error:
        message = str(error) if isinstance(error, hls.DownloadError) else 'Local file, input, or system operation failed.'
        print(f'Error: {message}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
