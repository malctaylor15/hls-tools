#!/usr/bin/env python3
"""Download a completed WebVTT HLS playlist into VTT, SRT, or text."""
import argparse
from dataclasses import dataclass
import html
import math
from pathlib import Path
import re
import shutil
import sys
import tempfile

from hls_download import DownloadError, default_output, fetch, parse_curl, parse_playlist


@dataclass(frozen=True)
class Cue:
    start: int
    end: int
    text: str
    settings: str = ''


def timestamp(value):
    match = re.fullmatch(r'(?:(\d{2,}):)?(\d{2}):(\d{2})\.(\d{3})', value)
    if not match:
        raise DownloadError('Invalid WebVTT timestamp.')
    hours, minutes, seconds, millis = match.groups()
    if int(minutes) > 59 or int(seconds) > 59:
        raise DownloadError('Invalid WebVTT timestamp.')
    return ((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def format_time(value, srt=False):
    hours, remainder = divmod(value, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, millis = divmod(remainder, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}{"," if srt else "."}{millis:03d}'


def parse_vtt(text):
    """Return cues and the MPEGTS-minus-LOCAL mapping, in 90kHz ticks."""
    lines = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if not lines or not re.fullmatch(r'WEBVTT(?:[ \t].*)?', lines[0]):
        raise DownloadError('Subtitle segment is not WebVTT.')
    mapping = None
    cursor = 1
    while cursor < len(lines) and lines[cursor].strip():
        line = lines[cursor].strip()
        if line.startswith('X-TIMESTAMP-MAP='):
            values = dict(part.split(':', 1) for part in line.split('=', 1)[1].split(',') if ':' in part)
            try:
                ticks = int(values['MPEGTS'])
                if not 0 <= ticks < 2 ** 33:
                    raise ValueError()
                mapping = ticks - timestamp(values['LOCAL']) * 90
            except (KeyError, ValueError):
                raise DownloadError('Invalid subtitle timestamp mapping.') from None
        cursor += 1
    cues, styles = [], []
    body = '\n'.join(lines[cursor:]).strip()
    for block in re.split(r'\n[ \t]*\n', body) if body else []:
        rows = block.splitlines()
        if rows[0] == 'NOTE' or rows[0].startswith(('NOTE ', 'NOTE\t')):
            continue
        if rows[0] in ('STYLE', 'REGION'):
            styles.append(block)
            continue
        timing_index = 0 if '-->' in rows[0] else 1
        if timing_index >= len(rows):
            raise DownloadError('Malformed WebVTT cue.')
        match = re.fullmatch(r'(\S+)[ \t]+-->[ \t]+(\S+)(?:[ \t]+(.*))?', rows[timing_index])
        if not match:
            raise DownloadError('Malformed WebVTT cue timing.')
        start, end = timestamp(match[1]), timestamp(match[2])
        if end <= start:
            raise DownloadError('Subtitle cue ends before it starts.')
        payload = '\n'.join(rows[timing_index + 1:])
        if payload:
            cues.append(Cue(start, end, payload, match[3] or ''))
    return cues, mapping, styles


def merge_segments(texts, video_start_seconds=None):
    """Normalize mapping changes; never add the segment number/duration to cues."""
    parsed = [parse_vtt(text) for text in texts]
    mappings = [mapping for _, mapping, _ in parsed]
    if any(m is None for m in mappings) and any(m is not None for m in mappings):
        raise DownloadError('Mixed mapped/unmapped subtitle segments require explicit handling.')
    if video_start_seconds is not None and any(m is None for m in mappings):
        raise DownloadError('--video-start-seconds requires timestamp maps on all segments.')
    baseline = (video_start_seconds * 90000 if video_start_seconds is not None
                else next((m for m in mappings if m is not None), 0))
    unique, styles = set(), []
    for cues, mapping, blocks in parsed:
        delta = (mapping - baseline) if mapping is not None else 0
        # Select the wrap nearest the requested origin (MPEG-TS wraps at 2^33).
        delta -= round(delta / (2 ** 33)) * (2 ** 33)
        offset = round(delta / 90)
        for cue in cues:
            start, end = cue.start + offset, cue.end + offset
            if end <= 0:
                continue
            unique.add(Cue(max(0, start), end, cue.text, cue.settings))
        for block in blocks:
            if block not in styles:
                styles.append(block)
    return sorted(unique, key=lambda c: (c.start, c.end, c.text, c.settings)), styles


def plain_text(text):
    return html.unescape(re.sub(r'<[^>]*>', '', text))


def render(cues, styles, kind):
    if kind == 'txt':
        return '\n'.join(plain_text(c.text) for c in cues) + ('\n' if cues else '')
    blocks = ['WEBVTT'] + styles if kind == 'vtt' else []
    for index, cue in enumerate(cues, 1):
        timing = f'{format_time(cue.start, kind == "srt")} --> {format_time(cue.end, kind == "srt")}'
        if kind == 'vtt':
            if cue.settings:
                timing += ' ' + cue.settings
            blocks.append(timing + '\n' + cue.text)
        else:
            blocks.append(f'{index}\n{timing}\n{plain_text(cue.text)}')
    return '\n\n'.join(blocks) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--playlist-file', type=Path)
    inputs.add_argument('--playlist-url')
    inputs.add_argument('--curl-file', help='Chrome cURL bash command file, or - for stdin')
    parser.add_argument('--base-url')
    parser.add_argument('--referer')
    parser.add_argument('--origin')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--format', choices=['vtt', 'srt', 'txt'], help='Defaults to output filename extension')
    parser.add_argument('--inspect', action='store_true')
    parser.add_argument('--video-start-seconds', type=float,
                        help='Optional video MPEG-TS start clock in seconds; default preserves the first map local clock')
    args = parser.parse_args(argv)
    work = None
    try:
        if args.video_start_seconds is not None and not math.isfinite(args.video_start_seconds):
            raise DownloadError('Video start must be a finite number.')
        kind = args.format or (args.output.suffix.lstrip('.').lower() if args.output else 'vtt')
        output = args.output.expanduser().absolute() if args.output else default_output(f'subtitles.{kind}')
        if not args.inspect:
            if kind not in ('vtt', 'srt', 'txt'):
                raise DownloadError('Choose a .vtt, .srt, or .txt output, or specify --format.')
            if output.exists():
                raise DownloadError('Output already exists; choose another filename.')
            if args.output and not output.parent.is_dir():
                raise DownloadError('Output directory does not exist.')
        headers = {'user-agent': 'Python-HLS-Downloader/1.0', 'accept': '*/*'}
        url = args.playlist_url
        if args.curl_file:
            command = sys.stdin.read() if args.curl_file == '-' else Path(args.curl_file).expanduser().read_text()
            url, copied = parse_curl(command)
            headers.update(copied)
        for name in ('referer', 'origin'):
            if getattr(args, name):
                headers[name] = getattr(args, name)
        if url:
            data, final_url = fetch(url, headers, credential_origin=url)
            text, base = data.decode('utf-8-sig'), args.base_url or final_url
        else:
            text, base = args.playlist_file.expanduser().read_text(encoding='utf-8-sig'), args.base_url
        playlist = parse_playlist(text, base)
        print(f'Segments: {len(playlist.segments)}; duration: {playlist.duration:.5f}s')
        if playlist.init_url:
            playlist.issues.append('initialization segments are not WebVTT subtitles')
        if playlist.issues:
            print('Unsupported features: ' + '; '.join(playlist.issues))
            if not args.inspect:
                raise DownloadError('Playlist is outside the supported subset.')
        if args.inspect:
            print('Subtitle contents and timestamp maps are checked during download.')
            return 0
        if not args.output:
            output.parent.mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix='.hls-subtitles-', dir=output.parent))
        texts = []
        for index, segment in enumerate(playlist.segments):
            path = work / f'{index:06d}.vtt'
            fetch(segment.url, headers, path, credential_origin=url)
            texts.append(path.read_text(encoding='utf-8-sig'))
            parse_vtt(texts[-1])
            print(f'Downloaded {index + 1}/{len(playlist.segments)}', flush=True)
        cues, styles = merge_segments(texts, args.video_start_seconds)
        if not cues:
            raise DownloadError('All subtitle segments are empty; no transcript was saved.')
        data = render(cues, styles, kind)
        created = False
        try:
            with output.open('x', encoding='utf-8', newline='\n') as stream:
                created = True
                stream.write(data)
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise
        shutil.rmtree(work)
        work = None
        print(f'Saved {output} ({len(cues)} unique cues).')
        return 0
    except (DownloadError, OSError, UnicodeError, ValueError, KeyboardInterrupt) as error:
        message = str(error) if isinstance(error, DownloadError) else ('Interrupted.' if isinstance(error, KeyboardInterrupt) else 'Local file, input, or system operation failed.')
        print(f'Error: {message}', file=sys.stderr)
        if work:
            print(f'Temporary files retained: {work}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
