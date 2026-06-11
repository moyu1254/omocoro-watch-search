#!/usr/bin/env python3
"""Build a static search index for the News! Omocoro Watch search prototype."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, unquote
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHANNEL_URL = "https://www.youtube.com/@news_omocorowatch"
DEFAULT_OUTPUT = ROOT / "outputs" / "omocoro-watch-search" / "data" / "search-index.json"
DEFAULT_SITE_URL = "https://moyu1254.github.io/omocoro-watch-search/"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()


def load_yt_dlp():
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "yt-dlp is required. Install it with: python -m pip install yt-dlp"
        ) from exc
    return yt_dlp


def pick_thumbnail(entry: dict[str, Any]) -> str:
    thumbnails = entry.get("thumbnails") or []
    if thumbnails:
        return sorted(thumbnails, key=lambda item: item.get("width") or 0)[-1].get("url") or ""
    video_id = entry.get("id") or entry.get("display_id")
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""


def parse_subtitle_json3(payload: dict[str, Any]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for event in payload.get("events", []):
        pieces = event.get("segs") or []
        text = normalize_text("".join(piece.get("utf8", "") for piece in pieces))
        if not text:
            continue
        start = (event.get("tStartMs") or 0) / 1000
        duration = (event.get("dDurationMs") or 0) / 1000
        segments.append({"start": round(start, 3), "duration": round(duration, 3), "text": text})
    return segments


def format_upload_date(value: str) -> str:
    if not value:
        return ""
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def normalize_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [text for text in (normalize_text(str(value)) for value in values) if text]


def normalize_search_fields(values: Any, default_label: str) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []

    fields: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, str):
            text = normalize_text(value)
            if text:
                fields.append({"label": default_label, "text": text, "weight": 15})
            continue

        if not isinstance(value, dict):
            continue

        text = normalize_text(
            value.get("text")
            or value.get("value")
            or value.get("title")
            or value.get("body")
            or ""
        )
        if not text:
            continue
        fields.append(
            {
                "label": normalize_text(value.get("label") or value.get("source") or default_label),
                "text": text,
                "url": normalize_text(value.get("url") or ""),
                "weight": int(value.get("weight") or 15),
            }
        )
    return fields


def escape_html(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def public_url(site_url: str, path: str = "") -> str:
    return site_url.rstrip("/") + "/" + path.lstrip("/")


def normalize_chapters(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []

    chapters: list[dict[str, Any]] = []
    for chapter in values:
        if not isinstance(chapter, dict):
            continue
        title = normalize_text(chapter.get("title") or "")
        if not title:
            continue
        chapters.append(
            {
                "title": title,
                "start": round(float(chapter.get("start_time") or 0), 3),
                "end": round(float(chapter.get("end_time") or 0), 3),
            }
        )
    return chapters


def request_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
    with urlopen(f"{url}?{query}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def topic_name(topic_url: str) -> str:
    return normalize_text(unquote(topic_url.rstrip("/").rsplit("/", 1)[-1]).replace("_", " "))


def fetch_youtube_api_fields(video_ids: list[str], api_key: str | None) -> dict[str, list[dict[str, Any]]]:
    if not api_key:
        return {}

    fields_by_video: dict[str, list[dict[str, Any]]] = {}
    for group in chunks(video_ids, 50):
        payload = request_json(
            f"{YOUTUBE_API_BASE}/videos",
            {
                "part": "snippet,topicDetails,recordingDetails,contentDetails",
                "id": ",".join(group),
                "key": api_key,
                "maxResults": 50,
            },
        )
        for item in payload.get("items", []):
            video_id = item.get("id")
            if not video_id:
                continue

            snippet = item.get("snippet") or {}
            localized = snippet.get("localized") or {}
            topic_details = item.get("topicDetails") or {}
            recording = item.get("recordingDetails") or {}
            content = item.get("contentDetails") or {}
            fields: list[dict[str, Any]] = []

            for label, text, weight in (
                ("API: ローカライズタイトル", localized.get("title"), 80),
                ("API: ローカライズ概要欄", localized.get("description"), 20),
                ("API: チャンネル名", snippet.get("channelTitle"), 10),
                ("API: 既定言語", snippet.get("defaultLanguage") or snippet.get("defaultAudioLanguage"), 8),
                ("API: 撮影場所", recording.get("locationDescription"), 12),
                ("API: 動画時間", content.get("duration"), 5),
            ):
                text = normalize_text(text or "")
                if text:
                    fields.append({"label": label, "text": text, "weight": weight})

            for topic_url in topic_details.get("topicCategories") or []:
                text = topic_name(topic_url)
                if text:
                    fields.append({"label": "API: トピック", "text": text, "url": topic_url, "weight": 18})

            fields_by_video[video_id] = fields

    return fields_by_video


def fetch_youtube_api_comments(video_id: str, api_key: str | None, limit: int) -> list[dict[str, Any]]:
    if not api_key or limit <= 0:
        return []

    comments: list[dict[str, Any]] = []
    page_token = ""
    while len(comments) < limit:
        payload = request_json(
            f"{YOUTUBE_API_BASE}/commentThreads",
            {
                "part": "snippet",
                "videoId": video_id,
                "key": api_key,
                "maxResults": min(100, limit - len(comments)),
                "order": "relevance",
                "textFormat": "plainText",
                "pageToken": page_token,
            },
        )
        for item in payload.get("items", []):
            snippet = (
                item.get("snippet", {})
                .get("topLevelComment", {})
                .get("snippet", {})
            )
            text = normalize_text(snippet.get("textOriginal") or snippet.get("textDisplay") or "")
            if text:
                comments.append(
                    {
                        "author": normalize_text(snippet.get("authorDisplayName") or ""),
                        "text": text,
                        "publishedAt": normalize_text(snippet.get("publishedAt") or ""),
                        "likeCount": int(snippet.get("likeCount") or 0),
                    }
                )
        page_token = payload.get("nextPageToken") or ""
        if not page_token:
            break

    return comments


def load_extra_search_fields(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if not path:
        return {}

    data = json.loads(path.read_text(encoding="utf-8"))
    videos = data.get("videos", data) if isinstance(data, dict) else {}
    if not isinstance(videos, dict):
        raise SystemExit("--extra-search-json must be an object keyed by video id, or {\"videos\": {...}}.")

    return {
        str(video_id): normalize_search_fields(values, "外部データ")
        for video_id, values in videos.items()
    }


def write_static_seo_files(payload: dict[str, Any], output: Path, site_url: str) -> None:
    site_root = output.parent.parent
    latest_modified = payload.get("generatedAt") or datetime.now(timezone.utc).isoformat()
    videos = payload.get("videos", [])

    rows: list[str] = []
    item_list: list[dict[str, Any]] = []
    for index, video in enumerate(videos, start=1):
        title = escape_html(video.get("title") or "")
        href = escape_html(video.get("url") or "")
        published = escape_html(video.get("publishedAt") or "")
        description = escape_html((video.get("description") or "")[:180])
        rows.append(
            '<article class="video-list-item">'
            f'<h2><a href="{href}" target="_blank" rel="noreferrer">{title}</a></h2>'
            f"<p>{published}</p>"
            f"<p>{description}</p>"
            "</article>"
        )
        item_list.append(
            {
                "@type": "ListItem",
                "position": index,
                "name": video.get("title") or "",
                "url": video.get("url") or "",
            }
        )

    list_json = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "ItemList",
            "name": "ニュース! オモコロウォッチ 収録回一覧",
            "url": public_url(site_url, "videos.html"),
            "numberOfItems": len(videos),
            "itemListElement": item_list,
        },
        ensure_ascii=False,
        indent=2,
    )

    videos_html = f"""<!doctype html>
<html lang="ja">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>収録回一覧 | オモコロウォッチのあの回</title>
    <meta name="description" content="ニュース! オモコロウォッチの収録回一覧。タイトル、公開日、概要欄を掲載しています。">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="{escape_html(public_url(site_url, 'videos.html'))}">
    <link rel="stylesheet" href="./styles.css">
    <script type="application/ld+json">
{list_json}
    </script>
  </head>
  <body>
    <header class="site-header">
      <a class="brand" href="./index.html">オモコロウォッチのあの回</a>
    </header>
    <main class="video-list-page">
      <h1>収録回一覧</h1>
      <p class="source-links"><a href="./index.html">検索</a></p>
      <div class="video-list">
        {''.join(rows)}
      </div>
    </main>
  </body>
</html>
"""
    (site_root / "videos.html").write_text(videos_html, encoding="utf-8")

    sitemap = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{escape_html(public_url(site_url))}</loc>
    <lastmod>{escape_html(latest_modified[:10])}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>{escape_html(public_url(site_url, 'videos.html'))}</loc>
    <lastmod>{escape_html(latest_modified[:10])}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>
"""
    (site_root / "sitemap.xml").write_text(sitemap, encoding="utf-8")

    robots = f"""User-agent: *
Allow: /

Sitemap: {public_url(site_url, 'sitemap.xml')}
"""
    (site_root / "robots.txt").write_text(robots, encoding="utf-8")


def write_outputs(payload: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    output.write_text(json_text, encoding="utf-8")

    js_output = output.with_suffix(".js")
    js_output.write_text(
        "window.SEARCH_INDEX = " + json_text + ";\n",
        encoding="utf-8",
    )


def fetch_transcript(ydl: Any, video_id: str, lang_order: list[str]) -> list[dict[str, Any]]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = ydl.extract_info(url, download=False)
    if not info:
        return []
    subtitles = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}

    for captions in (subtitles, automatic):
        for lang in lang_order:
            candidates = captions.get(lang) or []
            json3 = next((item for item in candidates if item.get("ext") == "json3"), None)
            if not json3:
                continue
            data = ydl.urlopen(json3["url"]).read().decode("utf-8", errors="replace")
            return parse_subtitle_json3(json.loads(data))

    return []


def fetch_playlist_entries(channel_url: str, max_videos: int | None) -> list[dict[str, Any]]:
    yt_dlp = load_yt_dlp()
    playlist_url = channel_url.rstrip("/") + "/videos"
    options = {
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "quiet": True,
        "skip_download": True,
    }
    if max_videos:
        options["playlistend"] = max_videos
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(playlist_url, download=False)
    return [entry for entry in info.get("entries", []) if entry]


def build_index(
    channel_url: str,
    output: Path,
    max_videos: int | None,
    lang_order: list[str],
    youtube_api_key: str | None,
    comments_per_video: int,
    extra_search_json: Path | None,
    site_url: str,
) -> dict[str, Any]:
    yt_dlp = load_yt_dlp()
    entries = fetch_playlist_entries(channel_url, max_videos)
    video_ids = [entry.get("id") or entry.get("url") for entry in entries if entry.get("id") or entry.get("url")]
    api_fields = fetch_youtube_api_fields(video_ids, youtube_api_key)
    extra_fields = load_extra_search_fields(extra_search_json)
    videos: list[dict[str, Any]] = []

    options = {
        "ignoreerrors": True,
        "no_warnings": True,
        "quiet": True,
        "skip_download": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        for index, entry in enumerate(entries, start=1):
            video_id = entry.get("id") or entry.get("url")
            if not video_id:
                continue
            print(f"[{index}/{len(entries)}] {video_id} {entry.get('title', '')}", file=sys.stderr)

            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            except Exception as exc:  # noqa: BLE001 - keep the rest of the channel usable.
                print(f"  metadata failed: {exc}", file=sys.stderr)
                info = entry
            if not info:
                print("  metadata unavailable; keeping flat playlist entry", file=sys.stderr)
                info = entry

            try:
                transcript = fetch_transcript(ydl, video_id, lang_order)
            except Exception as exc:  # noqa: BLE001
                print(f"  transcript failed: {exc}", file=sys.stderr)
                transcript = []

            try:
                comments = fetch_youtube_api_comments(video_id, youtube_api_key, comments_per_video)
            except Exception as exc:  # noqa: BLE001
                print(f"  comments failed: {exc}", file=sys.stderr)
                comments = []

            additional_search_fields = [
                *api_fields.get(video_id, []),
                *extra_fields.get(video_id, []),
            ]

            videos.append(
                {
                    "videoId": video_id,
                    "title": normalize_text(info.get("title") or entry.get("title") or ""),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "thumbnail": pick_thumbnail(info or entry),
                    "publishedAt": format_upload_date(info.get("upload_date") or entry.get("upload_date") or ""),
                    "description": normalize_text(info.get("description") or ""),
                    "tags": normalize_list(info.get("tags")),
                    "categories": normalize_list(info.get("categories")),
                    "chapters": normalize_chapters(info.get("chapters")),
                    "additionalSearchFields": additional_search_fields,
                    "comments": comments,
                    "transcriptSegments": transcript,
                }
            )

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "channel": {
            "name": "ニュース! オモコロウォッチ",
            "url": channel_url,
        },
        "source": {
            "tool": "yt-dlp",
            "maxVideos": max_videos,
            "subtitleLanguages": lang_order,
            "youtubeDataApi": bool(youtube_api_key),
            "commentsPerVideo": comments_per_video if youtube_api_key else 0,
            "extraSearchJson": str(extra_search_json) if extra_search_json else "",
        },
        "videos": videos,
    }

    write_outputs(payload, output)
    write_static_seo_files(payload, output, site_url)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-url", default=DEFAULT_CHANNEL_URL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-videos", type=int, default=30)
    parser.add_argument("--all", action="store_true", help="Fetch every video currently listed on the channel.")
    parser.add_argument("--languages", default="ja,ja-JP,en", help="Comma-separated subtitle language preference order.")
    parser.add_argument("--youtube-api-key", default=os.environ.get("YOUTUBE_API_KEY", ""), help="Optional YouTube Data API key. Defaults to YOUTUBE_API_KEY.")
    parser.add_argument("--comments-per-video", type=int, default=0, help="Optional top-level YouTube comments to index per video. Requires --youtube-api-key.")
    parser.add_argument("--extra-search-json", type=Path, help="Optional JSON file with additional search fields keyed by video id.")
    parser.add_argument("--site-url", default=os.environ.get("SITE_URL", DEFAULT_SITE_URL), help="Public site URL used in sitemap and structured data.")
    args = parser.parse_args()

    payload = build_index(
        channel_url=args.channel_url,
        output=args.output,
        max_videos=None if args.all else args.max_videos,
        lang_order=[lang.strip() for lang in args.languages.split(",") if lang.strip()],
        youtube_api_key=args.youtube_api_key or None,
        comments_per_video=max(0, args.comments_per_video),
        extra_search_json=args.extra_search_json,
        site_url=args.site_url,
    )
    print(f"Wrote {len(payload['videos'])} videos to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
