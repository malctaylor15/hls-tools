# Media folder template (optional)

For a single file containing all playlist links, use [playlists.example.txt](../playlists.example.txt) instead.

Make one folder per recording. Put any combination of these inputs inside it:

```text
media/
  My Recording/
    master.txt          # Chrome "Copy as cURL (bash)" for a master playlist
    video.m3u8          # saved playlist response; optional
    audio.url            # a plain playlist URL on one line; optional
    subtitles.txt        # Chrome cURL request for subtitles; optional
```

Use **one file per playlist**. A `.txt` file can contain one Chrome cURL command or one plain URL. A `.url` file contains one plain URL. A `.m3u8` file contains the playlist response beginning with `#EXTM3U`. A master playlist can supply video, audio, and subtitle variants by itself; you do not have to copy every child playlist as well.

If a saved `.m3u8` uses relative segment paths, put its original playlist URL in a sidecar file named `video.m3u8.base-url.txt`. Captured requests and signed URLs expire, so refresh them if the catalog reports HTTP 401, 403, or 410.

From the repository root:

```bash
python3 hls_catalog.py list 'media/My Recording'
python3 hls_catalog.py download 'media/My Recording' --kind video
python3 hls_catalog.py download 'media/My Recording' --kind audio
python3 hls_catalog.py download 'media/My Recording' --kind subtitles --format srt
```

The list assigns a number to each track. Use `--track 3` to choose a listed track, `--quality lowest` for the lowest resolution video, or `--audio-track 2` to pair a specific audio track with video. Downloads go to the repository's `downloads-YYYY-MM-DD/` folder, with names based on the media folder. Use `--output PATH` to choose another existing destination directory.

Folders under `media/` and `requests/` are ignored by Git to keep captured URLs and headers local.
