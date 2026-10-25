#!/usr/bin/env python3
"""Download simple, completed MPEG-TS HLS playlists using the standard library."""
import argparse
from dataclasses import dataclass
from datetime import date
import http.client
import json
import math
import re
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


class DownloadError(Exception):
    pass


def default_output(filename, current_date=None):
    day = current_date or date.today()
    return Path(__file__).resolve().parent / f'downloads-{day.isoformat()}' / filename


def clean_copied_url(value):
    """Also accept the Markdown-wrapped URLs used in chat examples."""
    match = re.fullmatch(r'\[([^\]]+)\]\((.+)\)', value)
    if match:
        value = match.group(1)
    return value.replace('\\&', '&')


def parse_curl(command):
    """Parse a Chrome bash cURL GET as data; never invoke a shell."""
    try:
        tokens = shlex.split(command.replace('\\\r\n', '').replace('\\\n', ''))
    except ValueError:
        raise DownloadError('Invalid cURL quoting. Use Chrome Copy as cURL (bash).') from None
    if not tokens or tokens.pop(0) != 'curl':
        raise DownloadError('Input must be a single Chrome cURL command.')
    headers, urls = {}, []
    value_options = {'--url', '-H', '--header', '-b', '--cookie', '-A', '--user-agent',
                     '-e', '--referer', '-X', '--request'}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        option, separator, inline = token.partition('=') if token.startswith('--') else (token, '', '')
        if option in value_options:
            if separator:
                value = inline
            else:
                if index >= len(tokens):
                    raise DownloadError('Missing cURL option value.')
                value = tokens[index]
                index += 1
            if option == '--url':
                urls.append(clean_copied_url(value))
            elif option in ('-X', '--request'):
                if value.upper() != 'GET':
                    raise DownloadError('Only GET playlist requests are supported.')
            elif option in ('-H', '--header'):
                name, colon, content = value.partition(':')
                if not colon or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                    raise DownloadError('Invalid cURL header.')
                headers[name.lower()] = content.strip()
            elif option in ('-b', '--cookie'):
                if '=' not in value or value.startswith('@'):
                    raise DownloadError('Use inline browser cookies, not a cookie file.')
                headers['cookie'] = value
            else:
                name = 'user-agent' if option in ('-A', '--user-agent') else 'referer'
                headers[name] = clean_copied_url(value)
        elif token in ('--compressed', '-L', '--location', '-s', '--silent', '-S', '--show-error', '--globoff'):
            continue
        elif token.startswith(('https://', 'http://', '[https://', '[http://')):
            urls.append(clean_copied_url(token))
        else:
            raise DownloadError('Unsupported cURL syntax/option. Copy a single GET request as cURL (bash); do not include pipes or output options.')
    if len(urls) != 1:
        raise DownloadError('Provide exactly one playlist URL in the cURL command.')
    parsed = urllib.parse.urlsplit(urls[0])
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise DownloadError('cURL playlist URL must be HTTP(S), without embedded credentials.')
    # urllib handles transport framing; request identity/range headers cannot be reused.
    for name in ('host', 'content-length', 'transfer-encoding', 'connection', 'accept-encoding',
                 'range', 'if-range', 'if-none-match', 'if-modified-since'):
        headers.pop(name, None)
    for name, value in headers.items():
        if '\r' in value or '\n' in value:
            raise DownloadError('Multiline header values are not supported.')
        if name in ('origin', 'referer'):
            headers[name] = clean_copied_url(value)
    return urls[0], headers


def origin(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


def scoped_headers(headers, url, credential_origin):
    # Public browser context can follow CDN links. Credentials/custom headers cannot.
    public = {'accept', 'accept-language', 'user-agent', 'origin', 'referer', 'priority'}
    same = credential_origin is None or origin(url) == origin(credential_origin)
    return {name: value for name, value in headers.items()
            if same or name.lower() in public or name.lower().startswith(('sec-ch-', 'sec-fetch-'))}


class ScopedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected is not None and origin(request.full_url) != origin(newurl):
            allowed = scoped_headers(dict(redirected.header_items()), newurl, request.full_url)
            redirected.headers = allowed
            redirected.unredirected_hdrs = {}
        return redirected


@dataclass
class Segment:
    url: str
    duration: float


@dataclass
class Playlist:
    segments: list
    target: float
    issues: list

    @property
    def duration(self):
        return math.fsum(s.duration for s in self.segments)


def parse_playlist(text, base_url=None):
    lines = [line.strip() for line in text.lstrip('\ufeff').splitlines() if line.strip()]
    if not lines or lines[0] != '#EXTM3U':
        raise DownloadError('Input is not an HLS playlist (#EXTM3U missing).')
    unsupported = {
        '#EXT-X-STREAM-INF': 'master playlist; supply a media rendition playlist',
        '#EXT-X-I-FRAME-STREAM-INF': 'master/I-frame playlist',
        '#EXT-X-MEDIA': 'separate media tracks',
        '#EXT-X-SESSION-KEY': 'encryption',
        '#EXT-X-KEY': 'encryption/key declarations',
        '#EXT-X-BYTERANGE': 'byte ranges',
        '#EXT-X-MAP': 'initialization segments',
        '#EXT-X-DISCONTINUITY': 'discontinuities',
        '#EXT-X-DISCONTINUITY-SEQUENCE': 'discontinuities',
        '#EXT-X-GAP': 'missing segments',
        '#EXT-X-PART': 'partial segments',
        '#EXT-X-I-FRAMES-ONLY': 'I-frame-only media',
    }
    issues, segments = [], []
    pending, target = None, 0
    for line in lines:
        tag = line.split(':', 1)[0]
        if tag in unsupported:
            issues.append(unsupported[tag])
        if tag == '#EXT-X-TARGETDURATION':
            try:
                target = float(line.split(':', 1)[1])
                if not math.isfinite(target) or target <= 0:
                    raise ValueError()
            except (ValueError, IndexError):
                raise DownloadError('Invalid target duration.') from None
        elif tag == '#EXTINF':
            if pending is not None:
                raise DownloadError('Segment duration has no following URI.')
            try:
                pending = float(line.split(':', 1)[1].split(',')[0])
                if not math.isfinite(pending) or pending <= 0:
                    raise ValueError()
            except (ValueError, IndexError):
                raise DownloadError('Invalid segment duration.') from None
        elif not line.startswith('#') and pending is not None:
            url = urllib.parse.urljoin(base_url, line) if base_url else line
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                issues.append('relative URI requires --base-url, or URI is not HTTP(S)')
            if parsed.username or parsed.password:
                issues.append('credentials embedded in segment URL')
            segments.append(Segment(url, pending))
            pending = None
    if pending is not None:
        raise DownloadError('Final segment duration has no following URI.')
    if '#EXT-X-ENDLIST' not in lines:
        issues.append('live/incomplete playlist (#EXT-X-ENDLIST missing)')
    if target == 0:
        issues.append('target duration missing')
    if not segments:
        issues.append('no media segments')
    return Playlist(segments, target, list(dict.fromkeys(issues)))


def fetch(url, headers, destination=None, credential_origin=None):
    """Fetch fully, retry transient failures, never print signed request URLs."""
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        raise DownloadError('Only HTTP(S) requests are supported.')
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers=scoped_headers(headers, url, credential_origin))
            opener = urllib.request.build_opener(ScopedRedirect())
            with opener.open(request, timeout=30) as response:
                expected = response.headers.get('Content-Length')
                total = 0
                chunks = []
                sink = destination.open('wb') if destination else None
                try:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if sink:
                            sink.write(chunk)
                        else:
                            chunks.append(chunk)
                finally:
                    if sink:
                        sink.close()
                if not total or (expected is not None and total != int(expected)):
                    raise http.client.IncompleteRead(b'')
                return b''.join(chunks), response.geturl()
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status in (401, 403):
                raise DownloadError(f'HTTP {status}: access denied; obtain a fresh playlist URL and check required headers.') from None
            if status not in (408, 429, 500, 502, 503, 504):
                raise DownloadError(f'HTTP {status}: request failed.') from None
            reason = f'HTTP {status}'
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
            reason = 'connection failure or incomplete response'
        if attempt == 2:
            raise DownloadError(f'{reason}; failed after three attempts.') from None
        print(f'{reason}; retrying ({attempt + 2}/3).', file=sys.stderr)
        time.sleep(attempt + 1)


def probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                             '-of', 'json', str(path)], capture_output=True, text=True)
    if result.returncode:
        raise DownloadError('FFprobe could not read the media.')
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise DownloadError('FFprobe returned invalid metadata.') from None


def assemble(playlist, work, output):
    manifest = work / 'local.m3u8'
    lines = ['#EXTM3U', '#EXT-X-VERSION:3',
             f'#EXT-X-TARGETDURATION:{math.ceil(max(playlist.target, max(s.duration for s in playlist.segments)))}',
             '#EXT-X-PLAYLIST-TYPE:VOD']
    for index, segment in enumerate(playlist.segments):
        lines.extend([f'#EXTINF:{segment.duration:.9f},', f'{index:06d}.ts'])
    lines.append('#EXT-X-ENDLIST')
    manifest.write_text('\n'.join(lines) + '\n')
    source = probe(manifest)
    source_audio = sum(s['codec_type'] == 'audio' for s in source['streams'])
    staged = work / 'result.mp4'
    result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-i', str(manifest),
                             '-map', '0:v', '-map', '0:a?', '-c', 'copy',
                             '-movflags', '+faststart', str(staged)], capture_output=True, text=True)
    if result.returncode:
        raise DownloadError('FFmpeg could not remux these tracks into MP4; downloaded files were retained.')
    metadata = probe(staged)
    streams = metadata['streams']
    if not any(s['codec_type'] == 'video' for s in streams):
        raise DownloadError('Output contains no video.')
    if sum(s['codec_type'] == 'audio' for s in streams) != source_audio:
        raise DownloadError('Output did not preserve all source audio tracks.')
    try:
        duration = float(metadata['format']['duration'])
    except (KeyError, ValueError):
        raise DownloadError('Output duration could not be verified.') from None
    if not math.isfinite(duration) or abs(duration - playlist.duration) > playlist.target:
        raise DownloadError('Output duration differs from the playlist by more than one target segment.')
    # Exclusive creation prevents overwriting even if another process creates output meanwhile.
    created = False
    try:
        with output.open('xb') as sink:
            created = True
            with staged.open('rb') as source_file:
                shutil.copyfileobj(source_file, sink)
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    return duration, source_audio


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--playlist-url')
    inputs.add_argument('--playlist-file', type=Path)
    inputs.add_argument('--curl-file', help='File containing Chrome Copy as cURL (bash); use - for stdin')
    parser.add_argument('--base-url', help='Original playlist URL for resolving relative segment URIs')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--referer')
    parser.add_argument('--origin')
    parser.add_argument('--inspect', action='store_true')
    args = parser.parse_args(argv)
    work = None
    try:
        output = args.output.expanduser().absolute() if args.output else default_output('video.mp4')
        if not args.inspect:
            if output.exists():
                raise DownloadError('Output already exists; choose another filename.')
            if args.output and not output.parent.is_dir():
                raise DownloadError('Output directory does not exist.')
            if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
                raise DownloadError('FFmpeg and FFprobe must be installed and on PATH.')
        headers = {'user-agent': 'Python-HLS-Downloader/1.0', 'accept': '*/*'}
        playlist_url = args.playlist_url
        if args.curl_file:
            command = sys.stdin.read() if args.curl_file == '-' else Path(args.curl_file).expanduser().read_text()
            playlist_url, copied_headers = parse_curl(command)
            headers.update(copied_headers)
        for name, value in [('Referer', args.referer), ('Origin', args.origin)]:
            if value:
                headers[name.lower()] = value
        if playlist_url:
            data, final_url = fetch(playlist_url, headers, credential_origin=playlist_url)
            text = data.decode('utf-8-sig')
            base = args.base_url or final_url
        else:
            text = args.playlist_file.expanduser().read_text(encoding='utf-8-sig')
            base = args.base_url
        playlist = parse_playlist(text, base)
        print(f'Segments: {len(playlist.segments)}; duration: {playlist.duration:.5f}s; target: {playlist.target:g}s')
        if playlist.issues:
            print('Unsupported features: ' + '; '.join(playlist.issues))
            if args.inspect:
                return 0
            raise DownloadError('This playlist is outside the supported subset.')
        if args.inspect:
            print('Supported: completed, unencrypted media playlist. Media format is checked during download.')
            return 0
        if not args.output:
            output.parent.mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix='.hls-download-', dir=output.parent))
        for index, segment in enumerate(playlist.segments):
            destination = work / f'{index:06d}.ts'
            fetch(segment.url, headers, destination, credential_origin=playlist_url)
            with destination.open('rb') as stream:
                prefix = stream.read(377)
            if len(prefix) < 377 or any(prefix[pos] != 0x47 for pos in (0, 188, 376)):
                raise DownloadError(f'Segment {index + 1} is not standard MPEG-TS media.')
            print(f'Downloaded {index + 1}/{len(playlist.segments)}', flush=True)
        duration, audio = assemble(playlist, work, output)
        shutil.rmtree(work)
        work = None
        print(f'Saved {output} ({duration:.3f}s; {audio} audio track(s)).')
        return 0
    except (DownloadError, OSError, UnicodeError, ValueError, KeyboardInterrupt) as error:
        # Do not expose arbitrary exception strings, which may include signed URLs.
        message = str(error) if isinstance(error, DownloadError) else ('Interrupted.' if isinstance(error, KeyboardInterrupt) else 'Local file, input, or system operation failed.')
        print(f'Error: {message}', file=sys.stderr)
        if work:
            print(f'Temporary files retained: {work}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
