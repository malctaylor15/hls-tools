# Python HLS downloader

This educational downloader handles completed, unencrypted MPEG-TS media playlists.
Python downloads exact segment URLs in order; FFmpeg packages the local media into MP4
without re-encoding. No Python packages are needed. Install FFmpeg/FFprobe on PATH.

## Inspect a saved playlist

```bash
python3 hls_download.py --playlist-file '/path/to/playlist.m3u8' --inspect
```

Inspection reads the playlist only. URL input fetches that playlist, but no media.
It reports unsupported features without attempting to download them.

## Download

Chrome input: right-click the **media playlist** request in Network, choose
**Copy → Copy as cURL (bash)**, and paste the whole command into a local `request.txt`.
Then run:

```bash
python3 hls_download.py --curl-file request.txt --output ~/Downloads/class-video.mp4
```

On macOS you can also pass the clipboard directly:

```bash
pbpaste | python3 hls_download.py --curl-file - --output ~/Downloads/class-video.mp4
```

The command is parsed as text, never executed. GET requests, `-H` headers, inline
`-b` cookies, user agent and Referer are supported. Transport, compression, range,
and cache-validation headers are omitted so full segments are requested. Browser
identity/context headers are reused; `--referer` and `--origin` override copied values.
Cookies, authorization, and custom headers are sent only to the original playlist
origin (scheme, hostname, port), and stripped on redirects to another origin.
Signed query parameters are kept exactly on their respective URLs; a playlist
signature is never copied to segment URLs. Expired signatures require a fresh
capture. Cookie files, shell pipelines, POST requests, and PowerShell/cmd syntax
are not supported. `--inspect` works with cURL input too.

An HLS playlist describes the segments of **one video**, not a collection of
classes/videos. Downloading a website's collection requires a separate input flow.

```bash
python3 hls_download.py \
  --playlist-url 'FRESH_SIGNED_M3U8_URL' \
  --referer 'https://example.com/' \
  --origin 'https://example.com' \
  --output ~/Downloads/class-video.mp4
```

Use the plain URL copied from Chrome, including its query parameters, inside quotes.
For a saved playlist, replace `--playlist-url` with `--playlist-file`. If it contains
relative links, also supply `--base-url 'ORIGINAL_PLAYLIST_URL'`. Absolute segment
URLs need no base URL. Playlist text files may use any filename extension.

Downloads use 30-second socket timeouts and three total attempts for transient
failures. Access errors stop immediately: obtain a fresh playlist and check headers.
Existing outputs are never overwritten. Verification checks video, source audio
track count, and duration. Failed jobs retain a `.hls-download-*` directory beside
the requested output for examination. This version does not resume retained jobs.

Master playlists, separate audio, live streams, encryption, byte ranges, initialization
segments, and discontinuities require a broader downloader. This program does not
extract browser cookies automatically or change signed URLs. An MP4 remux can also fail when the
source codecs are incompatible with that container.

## Tests

```bash
python3 -m unittest -v test_hls_download.py
```

Tests use local HTTP fixtures and a generated video, without fetching signed URLs.
