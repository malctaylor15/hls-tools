import contextlib
import http.server
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import subtitles_download as subtitles
from hls_download import DownloadError


HEADER = 'WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\n\n'
OVERLAP = '1233\n00:56:28.770 --> 00:56:30.809\nnot only for the shoulders and\n'


class Tests(unittest.TestCase):
    def test_empty_duplicates_global_times_and_exports(self):
        first = HEADER + OVERLAP
        second = HEADER + OVERLAP + '\n1234\n00:56:30.810 --> 00:56:32.729 line:90%\n<b>back</b> &amp; shoulders\n'
        cues, styles = subtitles.merge_segments([HEADER, first, second])
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start, 3388770)
        vtt = subtitles.render(cues, styles, 'vtt')
        self.assertIn('00:56:28.770 --> 00:56:30.809', vtt)
        self.assertIn('line:90%', vtt)
        self.assertEqual(len(subtitles.parse_vtt(vtt)[0]), 2)
        self.assertIn('00:56:28,770 --> 00:56:30,809', subtitles.render(cues, styles, 'srt'))
        self.assertEqual(subtitles.render(cues, styles, 'txt'), 'not only for the shoulders and\nback & shoulders\n')

    def test_mapping_changes_and_wrap(self):
        first = HEADER + '00:00:01.000 --> 00:00:02.000\nfirst\n'
        second = first.replace('MPEGTS:900000', 'MPEGTS:3600000').replace('first', 'second')
        cues, _ = subtitles.merge_segments([first, second])
        self.assertEqual([c.start for c in cues], [1000, 31000])
        cues, _ = subtitles.merge_segments([first], video_start_seconds=0)
        self.assertEqual(cues[0].start, 11000)
        near_wrap = first.replace('MPEGTS:900000', f'MPEGTS:{2**33 - 90000}')
        after_wrap = first.replace('MPEGTS:900000', 'MPEGTS:90000').replace('first', 'second')
        cues, _ = subtitles.merge_segments([near_wrap, after_wrap])
        self.assertEqual([c.start for c in cues], [1000, 3000])
        with self.assertRaises(DownloadError):
            subtitles.merge_segments([HEADER, 'WEBVTT\n\n'])

    def test_malformed(self):
        for text in ['not vtt', HEADER + 'bad cue', HEADER + '00:00:02.000 --> 00:00:01.000\ntext',
                     HEADER.replace('900000', 'not-a-number')]:
            with self.subTest(text=text), self.assertRaises(DownloadError):
                subtitles.parse_vtt(text)

    def test_local_download_curl_and_saved_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '0.vtt').write_text(HEADER)
            (root / '1.vtt').write_text(HEADER + OVERLAP)
            (root / '2.vtt').write_text(HEADER + OVERLAP)
            text = '#EXTM3U\n#EXT-X-TARGETDURATION:30\n#EXT-X-PLAYLIST-TYPE:VOD\n'
            text += ''.join(f'#EXTINF:30,\n{i}.vtt?signature=fixture{i}\n' for i in range(3)) + '#EXT-X-ENDLIST\n'
            (root / 'subs.m3u8').write_text(text)
            seen = []

            class Handler(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=str(root), **kwargs)

                def log_message(self, *args):
                    pass

                def do_GET(self):
                    seen.append((self.path, self.headers.get('Cookie')))
                    super().do_GET()

            with patch('socket.getfqdn', return_value='localhost'):
                server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}/subs.m3u8'
            curl = root / 'request.txt'
            curl.write_text(f"curl '{base}' -b 'session=fixture'")
            try:
                output = root / 'captions.srt'
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(subtitles.main(['--curl-file', str(curl), '--output', str(output)]), 0)
                self.assertEqual(output.read_text().count('-->'), 1)
                self.assertEqual(seen[-1], ('/2.vtt?signature=fixture2', 'session=fixture'))
                self.assertFalse(list(root.glob('.hls-subtitles-*')))
                previous = output.read_bytes()
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(subtitles.main(['--curl-file', str(curl), '--output', str(output)]), 1)
                self.assertEqual(output.read_bytes(), previous)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(subtitles.main(['--playlist-file', str(root / 'subs.m3u8'),
                        '--base-url', base, '--output', str(root / 'transcript.txt')]), 0)
                self.assertEqual((root / 'transcript.txt').read_text(), 'not only for the shoulders and\n')
                (root / '1.vtt').write_text(HEADER)
                (root / '2.vtt').write_text(HEADER)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(subtitles.main(['--curl-file', str(curl), '--output', str(root / 'empty.vtt')]), 1)
                self.assertFalse((root / 'empty.vtt').exists())
                self.assertTrue(list(root.glob('.hls-subtitles-*')))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
