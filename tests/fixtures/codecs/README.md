# Codec decoder fixtures

These committed MP4 files are authored, test-only H.264 and H.265 decoder
fixtures. Regenerate them from the repository root with:

```sh
PYTHONPATH=/Users/sb/tools/rembgGUI/.worktrees/lgpl-media-stack/src \
UV_CACHE_DIR=/private/tmp/matteloop-uv-cache \
uv run --no-sync python tests/fixtures/codecs/generate.py
```

`generate.py` encodes every fixture twice in a temporary directory and rejects
any byte mismatch before updating the committed file. It uses `libx264` and
`libx265` only in this development-time generator; neither encoder library
ships in MatteLoop, and these fixture files are never packaged with the app.

Both files contain two 64×48 RGB-authored frames at 2 fps (time base 1/2),
encoded as `yuv420p` video in MP4. Their colour metadata is BT.709 matrix and
primaries (1), sRGB transfer (13), and limited range (1).

| File | Codec | SHA-256 |
| --- | --- | --- |
| `h264-sdr.mp4` | H.264 | `0b96a9e0aaf2ebb470bac23a746d98d5b19f5e59f9592f6e1a9e2659eee83064` |
| `h265-sdr.mp4` | H.265 / HEVC | `98261fe0d518cd1de414b7b50a6c44b677d59eb99fd7046455a24d7f9c619785` |

## Licence

The authored fixture content and `generate.py` are released under the 0BSD
licence:

```text
Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
```
