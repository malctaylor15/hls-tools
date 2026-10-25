# Repository Guidelines

## Project Structure & Module Organization

This repository keeps its Python modules and tests at the top level. `hls_download.py` downloads completed, unencrypted HLS video playlists and assembles MP4 files with FFmpeg. `subtitles_download.py` downloads WebVTT segments and exports VTT, SRT, or TXT; it reuses playlist and HTTP helpers from `hls_download.py`. The matching tests are `test_hls_download.py` and `test_subtitles_download.py`. Start with `README.md`; `HLS_README.md` and `SUBTITLES_README.md` document each tool's inputs and limits. Captured requests and signed playlists are local inputs, not repository fixtures.

## Development and Test Commands

Use Python 3; the scripts have no third-party Python dependencies. Install `ffmpeg` and `ffprobe` on `PATH` for video downloads and end-to-end video tests. Subtitle downloads need only Python.

```bash
python3 -m unittest -v
python3 hls_download.py --playlist-file path/to/video.m3u8 --inspect
python3 subtitles_download.py --playlist-file path/to/subtitles.m3u8 --inspect
```

The first command runs both test modules. The `--inspect` commands validate and summarize playlists without downloading segments. For actual downloads, see the examples in the README files; output files are never overwritten.

## Coding Style & Naming Conventions

Follow the existing standard-library-only Python style: four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and `test_` prefixes for test methods. Keep argument parsing in each module's `main()` and shared playlist/network behavior in `hls_download.py`. No formatter or linter is configured; keep changes consistent with neighboring code.

## Testing Guidelines

Use `unittest` with local HTTP servers, temporary directories, and generated media where needed. Add focused `test_*` methods in the matching test file for parsing, credential scoping, failure handling, and output behavior. Tests should not depend on live signed URLs in the example files; those links can expire. Run `python3 -m unittest -v` before submitting changes.

## Commits, Pull Requests & Captured Requests

Use short, imperative commit subjects that describe the change. In pull requests, summarize behavior, list the test command and result, and note any changes to supported playlist features or output formats. Redact cookies, authorization headers, and signed URLs from new fixtures, logs, and screenshots before sharing them.
