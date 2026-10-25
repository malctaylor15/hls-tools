# Subtitle and transcript downloads

Use **subtitles_download.py** for a subtitle `.m3u8` playlist. Use
**hls_download.py** for video. Both accept saved playlists, URLs, or Chrome cURL
request files and reuse the same HTTP/download code.

Subtitle downloads require only Python 3 and its standard library; FFmpeg is not
needed. The subtitle tool writes one output file per run: `.vtt` for WebVTT,
`.srt` for common video-player subtitles, or `.txt` for readable transcript text.

## Find the subtitle request

1. Open Chrome DevTools → Network, select **All**, and start the video with English
   captions enabled. Reload with Preserve log enabled if the playlist loaded earlier.
2. Search for **manifest**, **subtitle**, or **m3u8**. The subtitle playlist response
   starts with `#EXTM3U` and lists `.vtt` URLs after `#EXTINF` entries.
3. Copy that playlist request as **cURL (bash)** into a separate file named
   `subtitles-request.txt`. Include just the command, without a pipeline.

Copy the **playlist** request, not a single `110.vtt` or `113.vtt` request. A VTT
segment begins with `WEBVTT`; a playlist begins with `#EXTM3U`.

## Run with a Chrome request file

```bash
cd hls-tools
python3 subtitles_download.py --curl-file subtitles-request.txt --output ~/Downloads/class-subtitles.srt
```

For a plain transcript, change the output filename:

```bash
python3 subtitles_download.py --curl-file subtitles-request.txt --output ~/Downloads/class-transcript.txt
```

Alternatively, after copying a fresh subtitle playlist request:

```bash
pbpaste | python3 subtitles_download.py --curl-file - --output ~/Downloads/class-subtitles.vtt
```

## Run with a saved playlist

Save a fresh subtitle playlist response locally, for example as `subtitles.m3u8`.
Captured playlists are not included in this repository because they may contain
signed URLs that expire.

```bash
python3 subtitles_download.py --playlist-file subtitles.m3u8 --inspect
python3 subtitles_download.py --playlist-file subtitles.m3u8 --output ~/Downloads/class-subtitles.vtt
```

Saved playlists containing relative segment URLs also need
`--base-url 'ORIGINAL_SUBTITLE_PLAYLIST_URL'`.
`--inspect` reads/fetches only the playlist, not subtitle chunks.

## Timing and merging

Chunks can be empty or contain cues overlapping a chunk boundary. The tool skips
empty chunks, preserves cue
times, sorts cues, and removes exact duplicates using timing, text, and settings.
It does **not** add the segment number times its duration to cue timestamps.

`X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000` maps subtitle zero to MPEG-TS
clock 10 seconds (90,000 ticks per second). By default, the tool uses the first
mapping as the reference and preserves that mapping's local subtitle timeline.
Later mapping changes are applied relative to it, including MPEG-TS wraparound.
If every chunk has an identical mapping, the original cue times are
preserved. With no timestamp maps, cue times are kept as written; local-clock
resets without maps are outside this method's supported scope.

The first-map default does not prove alignment with an independently downloaded
video. Check a caption near the start and another near the end. If you know the
video's original MPEG-TS start clock, supply it in seconds, for example
`--video-start-seconds 10`. This is a media clock value, not a desired subtitle
delay. Do not add 10 seconds merely because the map contains `900000`.

VTT output keeps cue markup/settings and STYLE/REGION blocks. SRT and TXT remove
markup and decode text entities. TXT contains cue text without timestamps, one
cue after another; it does not rewrite captions into polished paragraphs.
Repeated speech at different times is preserved. Rolling captions and differently
clipped copies of a cue are not heuristically combined.

## Limits and failures

The input must be a completed, unencrypted WebVTT media playlist. Master playlists,
live streams, initialization segments, byte ranges, and discontinuities are rejected.
Mixed mapped/unmapped chunks are rejected to avoid guessing synchronization.
Existing output files are never overwritten. Empty tracks produce an error instead
of a misleading empty transcript. Download/parse failures retain raw VTT chunks in
a `.hls-subtitles-*` folder beside the output. Retained jobs are not resumed.

Headers and credentials follow the rules in [HLS_README.md](HLS_README.md): cookies
and authorization stay on the original request origin; each segment retains its
own signature. A saved playlist response does not include browser request headers,
so supply Referer/Origin explicitly or use the cURL input when necessary.

## Tests

```bash
python3 -m unittest -v
```

Tests cover empty chunks, overlapping duplicates, global timestamps, timestamp-map
changes/wraparound, all three output formats, malformed inputs, cURL cookie reuse,
local HTTP downloads, retained failure files, and overwrite prevention. They do not
fetch signed URLs.
