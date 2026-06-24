#!/usr/bin/env python3
"""Export one YouTube transcript into a manual transcript JSON file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from build_index import fetch_transcript, load_yt_dlp, normalize_transcript_segments, preferred_youtube_lang


def export_transcript(video_id: str, output: Path, lang_order: list[str]) -> dict[str, object]:
    youtube_lang = preferred_youtube_lang(lang_order)
    yt_dlp = load_yt_dlp()
    options = {
        "extractor_args": {"youtube": {"lang": [youtube_lang]}},
        "ignoreerrors": False,
        "no_warnings": True,
        "quiet": True,
        "skip_download": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        segments = normalize_transcript_segments(fetch_transcript(ydl, video_id, lang_order))
    if not segments:
        raise SystemExit(f"No transcript segments exported for {video_id}")

    payload: dict[str, object] = {
        "videoId": video_id,
        "transcriptSegments": segments,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-id", required=True, help="YouTube video ID to export.")
    parser.add_argument("--output", type=Path, required=True, help="Output JSON path.")
    parser.add_argument("--languages", default="ja,ja-JP,en", help="Comma-separated subtitle language preference order.")
    args = parser.parse_args()

    payload = export_transcript(
        video_id=args.video_id,
        output=args.output,
        lang_order=[lang.strip() for lang in args.languages.split(",") if lang.strip()],
    )
    print(f"Wrote {len(payload['transcriptSegments'])} transcript segments to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
