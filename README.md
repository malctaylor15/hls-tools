# HLS Tools

Small, standard-library Python tools for downloading a completed HLS video or subtitle track from a media playlist. The video tool remuxes MPEG-TS or fragmented MP4 segments into MP4 with FFmpeg. The subtitle tool combines WebVTT segments into VTT, SRT, or plain text.

## Requirements

- Python 3. No `pip install` step is needed.
- `ffmpeg` and `ffprobe` on your `PATH` for video downloads. Subtitle downloads do not need them.
- A media playlist (`.m3u8`) or a fresh browser request for a playlist you are authorized to access.

## Quick start

Clone the repository and work from its root:

```bash
git clone https://github.com/malctaylor15/hls-tools.git
cd hls-tools
python3 -m unittest -v
```

## Organize several playlists

Put all playlist inputs for one recording in a folder. The catalog accepts saved `.m3u8` responses, plain URL files, and Chrome cURL request files. It identifies video resolutions, audio tracks, and WebVTT subtitles; a master playlist is expanded into its media tracks.

```bash
mkdir -p 'media/My Recording'
# Add your playlist files or links to that folder, then:
python3 hls_catalog.py list 'media/My Recording'
python3 hls_catalog.py download 'media/My Recording' --kind video
python3 hls_catalog.py download 'media/My Recording' --kind audio
python3 hls_catalog.py download 'media/My Recording' --kind subtitles --format srt
```

Video defaults to the highest resolution and pairs the first separate audio track when needed. Use `--track N` to choose a numbered track from `list`, `--audio-track N` to choose a different audio track, or `--quality lowest`. The catalog saves files in the dated downloads folder. See the [folder template](media/TEMPLATE.md) for file formats and examples.

### Download video

In your browser's Network panel, locate the **media playlist** request (often `rendition.m3u8`), then use **Copy as cURL (bash)**. Save that command in a local file such as `request.txt` and run:

```bash
python3 hls_download.py --curl-file request.txt --inspect
python3 hls_download.py --curl-file request.txt
```

`--inspect` checks the playlist without downloading its segments. The download saves to `downloads-YYYY-MM-DD/video.mp4` inside the repository, using your local date. You can choose another path with `--output video.mp4`. The tool also accepts `--playlist-url URL` or `--playlist-file path/to/playlist.m3u8`. For saved playlists with relative segment URLs, supply `--base-url ORIGINAL_PLAYLIST_URL`.

If the video playlist has no audio, capture its separate audio playlist as another cURL file and pair them:

```bash
python3 hls_download.py --curl-file video-request.txt --audio-curl-file audio-request.txt
```

### Download subtitles

Capture the **subtitle playlist** request separately and save its cURL command as `subtitles-request.txt`:

```bash
python3 subtitles_download.py --curl-file subtitles-request.txt --inspect
python3 subtitles_download.py --curl-file subtitles-request.txt --format srt
```

The default destination is `downloads-YYYY-MM-DD/subtitles.srt` in this example. Without `--format`, it saves `subtitles.vtt`; use `--format txt` for plain text. An explicit `--output subtitles.srt` also selects the format from its extension. The subtitle tool accepts the same playlist input options as the video tool. If you have a saved subtitle playlist, use `--playlist-file path/to/subtitles.m3u8`.

## What the tools support

Both tools accept completed, unencrypted media playlists and preserve the exact segment URLs. Browser cookies and authorization headers from a copied cURL request are scoped to the playlist's origin. Existing output files are never overwritten; if the default filename already exists, choose another name with `--output`. Failed downloads retain a temporary directory beside the requested output for inspection.

Master playlists, live streams, encryption, byte ranges, multiple initialization segments, and discontinuities are outside the supported subset. Video downloads require MPEG-TS or fragmented MP4 segments and codecs compatible with MP4 remuxing. Signed URLs can expire; capture a fresh request if access fails.

## More information

- [Video inputs, header handling, and limits](HLS_README.md)
- [Subtitle timing, formats, and limits](SUBTITLES_README.md)
- [Contributor guidelines](AGENTS.md)

Keep captured request files, signed playlists, and downloaded media out of Git. The test suite uses local HTTP fixtures rather than live requests.
