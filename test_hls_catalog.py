import contextlib
import http.server
import io
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import hls_catalog as catalog
import hls_download as hls


class Tests(unittest.TestCase):
    def test_master_expansion(self):
        parent = catalog.Source('master.url', 'https://example.test/master.m3u8', {},
            'https://example.test/master.m3u8', base_url='https://example.test/master.m3u8')
        text = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English",'
                'LANGUAGE="en",URI="audio.m3u8"\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=426x240,AUDIO="a"\n'
                'video.m3u8\n')
        children = catalog.expand_master(parent, text)
        self.assertEqual([child.url for child in children], [
            'https://example.test/audio.m3u8', 'https://example.test/video.m3u8'])
        self.assertEqual(children[0].language, 'en')
        self.assertEqual(children[1].height, 240)

    def test_folder_classifies_and_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'testsrc=size=160x120:rate=25', '-t', '2', '-an', '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p', '-hls_segment_type', 'fmp4',
                '-hls_fmp4_init_filename', 'video-init.mp4', '-hls_list_size', '0',
                str(root / 'video.m3u8')], capture_output=True)
            self.assertEqual(video.returncode, 0, video.stderr)
            audio = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'sine=frequency=440', '-t', '2', '-vn', '-c:a', 'aac',
                '-hls_segment_type', 'fmp4', '-hls_fmp4_init_filename', 'audio-init.mp4',
                '-hls_list_size', '0', str(root / 'audio.m3u8')], capture_output=True)
            self.assertEqual(audio.returncode, 0, audio.stderr)
            (root / 'captions.vtt').write_text('WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n')

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
                inputs = root / 'inputs'
                inputs.mkdir()
                (inputs / 'video.txt').write_text(f"curl '{base}/video.m3u8'")
                (inputs / 'audio.url').write_text(base + '/audio.m3u8\n')
                (inputs / 'subtitles.m3u8').write_text(
                    '#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n'
                    f'{base}/captions.vtt\n#EXT-X-ENDLIST\n')
                (root / 'subtitle-track.m3u8').write_text(
                    '#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n'
                    'captions.vtt\n#EXT-X-ENDLIST\n')
                (root / 'master.m3u8').write_text(
                    '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English",'
                    'LANGUAGE="en",URI="audio.m3u8"\n'
                    '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s",NAME="English",'
                    'LANGUAGE="en",URI="subtitle-track.m3u8"\n'
                    '#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=160x120,AUDIO="a",'
                    'SUBTITLES="s"\nvideo.m3u8\n')
                with contextlib.redirect_stdout(io.StringIO()):
                    tracks = catalog.scan(inputs)
                self.assertEqual({track.kind for track in tracks}, {'video', 'audio', 'subtitles'})
                master_inputs = root / 'master-inputs'
                master_inputs.mkdir()
                (master_inputs / 'master.url').write_text(base + '/master.m3u8')
                with contextlib.redirect_stdout(io.StringIO()):
                    master_tracks = catalog.scan(master_inputs)
                self.assertEqual({track.kind for track in master_tracks}, {'video', 'audio', 'subtitles'})
                video_track = catalog.choose(tracks, 'video')
                audio_track = catalog.choose(tracks, 'audio')
                subtitle_track = catalog.choose(tracks, 'subtitles')
                self.assertEqual(video_track.height, 120)
                combined = root / 'combined.mp4'
                sound = root / 'sound.m4a'
                captions = root / 'captions.srt'
                with contextlib.redirect_stdout(io.StringIO()):
                    catalog.download_video(video_track, audio_track, combined)
                    catalog.download_audio(audio_track, sound)
                    catalog.download_subtitles(subtitle_track, captions, 'srt')
                self.assertEqual({s['codec_type'] for s in hls.probe(combined)['streams']},
                    {'video', 'audio'})
                self.assertEqual({s['codec_type'] for s in hls.probe(sound)['streams']}, {'audio'})
                self.assertIn('hello', captions.read_text())
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(catalog.main(['list', str(inputs)]), 0)
                    self.assertEqual(catalog.main(['download', str(inputs), '--kind', 'audio',
                        '--output', str(root / 'via-cli.m4a')]), 0)
                self.assertTrue((root / 'via-cli.m4a').is_file())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
