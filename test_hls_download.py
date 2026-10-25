import contextlib
from datetime import date
import http.server
import io
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import hls_download as hls


def manifest(entries, extra=''):
    return '#EXTM3U\n#EXT-X-TARGETDURATION:6\n' + extra + ''.join(
        f'#EXTINF:{duration},\n{url}\n' for duration, url in entries) + '#EXT-X-ENDLIST\n'


class Tests(unittest.TestCase):
    def test_default_output_uses_repo_and_date(self):
        output = hls.default_output('video.mp4', date(2026, 10, 25))
        self.assertEqual(output, Path(hls.__file__).resolve().parent / 'downloads-2026-10-25' / 'video.mp4')

    def test_curl_input(self):
        command = """curl --url 'https://example.test/rendition.m3u8?signature=abc&expires=123' \\
  -H 'accept: */*' \\
  -H 'origin: https://site.test' \\
  -H 'referer: https://site.test/' \\
  -H 'Authorization: Bearer secret' \\
  -H 'Range: bytes=0-10' \\
  -H 'Accept-Encoding: gzip, br' \\
  -b 'session=private' --compressed"""
        url, headers = hls.parse_curl(command)
        self.assertEqual(url, 'https://example.test/rendition.m3u8?signature=abc&expires=123')
        self.assertEqual(headers['cookie'], 'session=private')
        self.assertEqual(headers['authorization'], 'Bearer secret')
        self.assertNotIn('range', headers)
        self.assertNotIn('accept-encoding', headers)
        self.assertEqual(hls.scoped_headers(headers, 'https://example.test/a.ts', url), headers)
        scoped = hls.scoped_headers(headers, 'https://cdn.test/a.ts', url)
        self.assertNotIn('cookie', scoped)
        self.assertNotIn('authorization', scoped)
        self.assertEqual(scoped['referer'], 'https://site.test/')
        markdown, _ = hls.parse_curl("curl --url '[https://example.test/a.m3u8?x=1&y=2](https://example.test/a.m3u8?x=1\\&y=2)'")
        self.assertEqual(markdown, 'https://example.test/a.m3u8?x=1&y=2')
        for command in ["curl https://example.test/a | sh", "curl -X POST https://example.test/a",
                        "curl --data secret https://example.test/a", "curl -b cookies.txt https://example.test/a"]:
            with self.subTest(command=command), self.assertRaises(hls.DownloadError):
                hls.parse_curl(command)

    def test_redirect_credentials(self):
        original = hls.urllib.request.Request('https://example.test/a', headers={
            'Cookie': 'secret=1', 'Authorization': 'Bearer secret', 'Referer': 'https://site.test/'})
        redirected = hls.ScopedRedirect().redirect_request(original, None, 302, '', {}, 'https://cdn.test/a')
        self.assertNotIn('cookie', {k.lower() for k, v in redirected.header_items()})
        self.assertNotIn('authorization', {k.lower() for k, v in redirected.header_items()})
        self.assertEqual(redirected.get_header('Referer'), 'https://site.test/')

    def test_relative_and_signed_paths(self):
        text = manifest([(5, 'a.ts?signature=one'), (4, '../different/b.ts?signature=two')])
        playlist = hls.parse_playlist(text, 'https://example.test/media/rendition.m3u8?token=secret')
        self.assertEqual([s.url for s in playlist.segments], [
            'https://example.test/media/a.ts?signature=one',
            'https://example.test/different/b.ts?signature=two'])
        self.assertEqual(playlist.duration, 9)
        self.assertFalse(playlist.issues)
        self.assertTrue(hls.parse_playlist(text).issues)
        fragmented = hls.parse_playlist(text.replace('#EXTINF:5,',
            '#EXT-X-MAP:URI="init.mp4?signature=init"\n#EXTINF:5,', 1),
            'https://example.test/media/rendition.m3u8?token=secret')
        self.assertEqual(fragmented.init_url, 'https://example.test/media/init.mp4?signature=init')
        self.assertFalse(fragmented.issues)

    def test_unsupported(self):
        for tag in ['#EXT-X-STREAM-INF:BANDWIDTH=100', '#EXT-X-KEY:METHOD=AES-128',
                    '#EXT-X-BYTERANGE:100', '#EXT-X-MAP:URI="init.mp4",BYTERANGE="100@0"',
                    '#EXT-X-DISCONTINUITY']:
            with self.subTest(tag=tag):
                self.assertTrue(hls.parse_playlist(manifest([(5, 'https://example.test/a.ts')], tag + '\n')).issues)
        self.assertTrue(hls.parse_playlist(manifest([(5, 'https://example.test/a.ts')]).replace('#EXT-X-ENDLIST', '')).issues)
        for value in ['nan', '-1', 'garbage']:
            with self.assertRaises(hls.DownloadError):
                hls.parse_playlist(manifest([(value, 'https://example.test/a.ts')]))

    def test_fragmented_mp4_with_separate_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'testsrc=size=160x120:rate=25', '-t', '4', '-an', '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p', '-g', '50', '-hls_segment_type', 'fmp4',
                '-hls_fmp4_init_filename', 'video-init.mp4',
                '-hls_time', '2', '-hls_list_size', '0', str(root / 'video.m3u8')], capture_output=True)
            self.assertEqual(video.returncode, 0, video.stderr)
            audio = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'sine=frequency=440', '-t', '4', '-vn', '-c:a', 'aac',
                '-hls_segment_type', 'fmp4', '-hls_fmp4_init_filename', 'audio-init.mp4',
                '-hls_time', '2', '-hls_list_size', '0',
                str(root / 'audio.m3u8')], capture_output=True)
            self.assertEqual(audio.returncode, 0, audio.stderr)

            class Handler(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=str(root), **kwargs)

                def log_message(self, *args):
                    pass

            with patch('socket.getfqdn', return_value='localhost'):
                server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                audio_curl = root / 'audio-request.txt'
                audio_curl.write_text(f"curl '{base}/audio.m3u8'")
                output = root / 'combined.mp4'
                with contextlib.redirect_stdout(io.StringIO()):
                    code = hls.main(['--playlist-url', base + '/video.m3u8',
                        '--audio-curl-file', str(audio_curl), '--output', str(output)])
                self.assertEqual(code, 0)
                self.assertEqual({s['codec_type'] for s in hls.probe(output)['streams']}, {'audio', 'video'})
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_http_and_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'testsrc=size=160x120:rate=25', '-f', 'lavfi', '-i', 'sine=frequency=440',
                '-t', '6', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-g', '50',
                '-c:a', 'aac', '-f', 'hls', '-hls_time', '2', '-hls_list_size', '0',
                str(root / 'source.m3u8')], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            counts, seen, cookies_seen = {}, [], []

            class Handler(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=str(root), **kwargs)

                def log_message(self, *args):
                    pass

                def do_GET(self):
                    counts[self.path] = counts.get(self.path, 0) + 1
                    seen.append((self.path, self.headers.get('Referer'), self.headers.get('Origin')))
                    cookies_seen.append((self.path, self.headers.get('Cookie')))
                    if self.path.startswith('/expired'):
                        self.send_error(403)
                        return
                    if self.path.startswith('/retry') and counts[self.path] < 3:
                        self.send_error(503)
                        return
                    if self.path.startswith('/broken'):
                        self.send_response(200)
                        self.send_header('Content-Length', '100')
                        self.end_headers()
                        self.wfile.write(b'short')
                        self.close_connection = True
                        return
                    if self.path.startswith('/retry'):
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'complete')
                        return
                    super().do_GET()

            # Avoid an unnecessary reverse DNS lookup when binding a local fixture.
            with patch('socket.getfqdn', return_value='localhost'):
                server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with patch.object(hls.time, 'sleep'), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(hls.fetch(base + '/retry?signature=secret', {})[0], b'complete')
                    self.assertEqual(counts['/retry?signature=secret'], 3)
                    with self.assertRaises(hls.DownloadError) as error:
                        hls.fetch(base + '/expired?signature=secret', {})
                    self.assertIn('fresh playlist', str(error.exception))
                    self.assertNotIn('secret', str(error.exception))
                    self.assertEqual(counts['/expired?signature=secret'], 1)
                    with self.assertRaises(hls.DownloadError):
                        hls.fetch(base + '/broken', {}, root / 'partial.ts')
                    self.assertEqual(counts['/broken'], 3)
                output = root / 'video.mp4'
                stdout = io.StringIO()
                args = ['--playlist-url', base + '/source.m3u8', '--output', str(output),
                        '--referer', 'https://example.test/', '--origin', 'https://example.test']
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(hls.main(args), 0)
                data = hls.probe(output)
                self.assertEqual({s['codec_type'] for s in data['streams']}, {'audio', 'video'})
                self.assertAlmostEqual(float(data['format']['duration']), 6, delta=2)
                self.assertFalse(list(root.glob('.hls-download-*')))
                self.assertTrue(any(p.endswith('.ts') and referer and origin for p, referer, origin in seen))
                curl_file = root / 'request.txt'
                curl_file.write_text(f"curl '{base}/source.m3u8' -H 'Referer: https://example.test/' -b 'session=fixture' --compressed")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(hls.main(['--curl-file', str(curl_file), '--output', str(root / 'curl.mp4')]), 0)
                self.assertEqual({s['codec_type'] for s in hls.probe(root / 'curl.mp4')['streams']}, {'audio', 'video'})
                default = root / 'downloads-2026-10-25' / 'video.mp4'
                with patch.object(hls, 'default_output', return_value=default), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(hls.main(['--playlist-url', base + '/source.m3u8']), 0)
                self.assertTrue(default.is_file())
                self.assertTrue(any(path.endswith('.ts') and cookie == 'session=fixture' for path, cookie in cookies_seen))
                original = output.read_bytes()
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(hls.main(args), 1)
                self.assertEqual(output.read_bytes(), original)
                bad = root / 'bad.m3u8'
                bad.write_text(manifest([(5, base + '/expired?signature=secret')]))
                stderr = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                    self.assertEqual(hls.main(['--playlist-file', str(bad), '--output', str(root / 'bad.mp4')]), 1)
                self.assertFalse((root / 'bad.mp4').exists())
                self.assertTrue(list(root.glob('.hls-download-*')))
                self.assertNotIn('secret', stderr.getvalue())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
