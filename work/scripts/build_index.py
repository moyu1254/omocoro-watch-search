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
from urllib.parse import urlencode, unquote, urlparse
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHANNEL_URL = "https://www.youtube.com/@news_omocorowatch"
DEFAULT_OUTPUT = ROOT / "outputs" / "omocoro-watch-search" / "data" / "search-index.json"
DEFAULT_SITE_URL = "https://omowatch.com/"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UI_LANG_FALLBACKS = {
    "ja-JP": "ja",
    "en-US": "en",
    "en-GB": "en-GB",
}
JAPANESE_TEXT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f]")
PRESERVE_WHEN_EMPTY_FIELDS = [
    "publishedAt",
    "description",
    "tags",
    "categories",
    "chapters",
    "additionalSearchFields",
    "comments",
    "transcriptSegments",
]
CRITICAL_SEARCH_FIELDS = ["description", "tags", "transcriptSegments", "publishedAt"]


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()


def preferred_youtube_lang(lang_order: list[str]) -> str:
    for lang in lang_order:
        candidate = YOUTUBE_UI_LANG_FALLBACKS.get(lang, lang)
        if candidate:
            return candidate
    return "ja"


def contains_japanese(value: str) -> bool:
    return bool(JAPANESE_TEXT_RE.search(value or ""))


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
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", value):
        return value[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def format_unix_date(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def pick_published_at(*sources: dict[str, Any]) -> str:
    for source in sources:
        for key in ("upload_date", "release_date", "modified_date", "publishedAt"):
            published = format_upload_date(str(source.get(key) or ""))
            if published:
                return published
        for key in ("timestamp", "release_timestamp", "modified_timestamp"):
            published = format_unix_date(source.get(key))
            if published:
                return published
    return ""


def first_non_empty_value_with_source(*items: tuple[Any, str]) -> tuple[Any, str]:
    for value, source in items:
        if has_search_value(value):
            return value, source
    return "", ""


def normalize_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [text for text in (normalize_text(str(value)) for value in values) if text]


def first_non_empty_string(*values: Any) -> str:
    for value in values:
        text = normalize_text(str(value or ""))
        if text:
            return text
    return ""


def first_non_empty_list(*values: Any) -> list[str]:
    for value in values:
        items = normalize_list(value)
        if items:
            return items
    return []


def first_non_empty_chapters(*values: Any) -> list[dict[str, Any]]:
    for value in values:
        items = normalize_chapters(value)
        if items:
            return items
    return []


def has_search_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(normalize_text(value))
    if isinstance(value, list):
        return bool(value)
    return value not in (None, "", [])


def load_existing_payload(output: Path) -> dict[str, Any]:
    if not output.exists():
        return {}
    try:
        data = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Existing index could not be loaded; continuing without merge: {exc}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def videos_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    videos = payload.get("videos") or []
    if not isinstance(videos, list):
        return {}
    return {
        str(video.get("videoId")): video
        for video in videos
        if isinstance(video, dict) and video.get("videoId")
    }


def merge_video_with_existing(
    current: dict[str, Any],
    existing: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    if not existing:
        return current, []

    merged = {**existing, **current}
    restored_fields: list[str] = []
    for field in PRESERVE_WHEN_EMPTY_FIELDS:
        if has_search_value(current.get(field)) or not has_search_value(existing.get(field)):
            continue
        merged[field] = existing.get(field)
        restored_fields.append(field)
    return merged, restored_fields


def looks_like_flat_video_entry(info: dict[str, Any]) -> bool:
    return not any(
        (
            has_search_value(info.get("description")),
            has_search_value(info.get("tags")),
            has_search_value(info.get("categories")),
            has_search_value(info.get("chapters")),
            has_search_value(info.get("subtitles")),
            has_search_value(info.get("automatic_captions")),
        )
    )


def choose_preferred_title(info_title: Any, entry_title: Any) -> str:
    info_text = normalize_text(str(info_title or ""))
    entry_text = normalize_text(str(entry_title or ""))

    if info_text and entry_text:
        info_has_japanese = contains_japanese(info_text)
        entry_has_japanese = contains_japanese(entry_text)
        if entry_has_japanese and not info_has_japanese:
            return entry_text
        if info_has_japanese and not entry_has_japanese:
            return info_text
        return info_text

    return info_text or entry_text


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
        try:
            weight = int(value.get("weight") or 15)
        except (TypeError, ValueError):
            weight = 15

        fields.append(
            {
                "label": normalize_text(value.get("label") or value.get("source") or default_label),
                "text": text,
                "url": normalize_text(value.get("url") or ""),
                "weight": weight,
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


def safe_href(value: str, fallback: str = "#") -> str:
    parsed = urlparse(str(value or ""))
    if parsed.scheme in ("http", "https"):
        return str(value)
    return fallback


def script_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2).replace("</", "<\\/")


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


def pick_api_thumbnail(thumbnails: Any) -> str:
    if not isinstance(thumbnails, dict):
        return ""

    ranked = []
    for thumbnail in thumbnails.values():
        if not isinstance(thumbnail, dict):
            continue
        url = normalize_text(thumbnail.get("url") or "")
        if url:
            ranked.append((int(thumbnail.get("width") or 0), url))
    if not ranked:
        return ""
    return sorted(ranked)[-1][1]


def parse_youtube_api_item(item: dict[str, Any]) -> dict[str, Any]:
    snippet = item.get("snippet") or {}
    localized = snippet.get("localized") or {}
    topic_details = item.get("topicDetails") or {}
    recording = item.get("recordingDetails") or {}
    content = item.get("contentDetails") or {}
    statistics = item.get("statistics") or {}
    localizations = item.get("localizations") or {}

    fields: list[dict[str, Any]] = []
    for label, text, weight in (
        ("API: ローカライズタイトル", localized.get("title"), 80),
        ("API: ローカライズ概要欄", localized.get("description"), 20),
        ("API: チャンネル名", snippet.get("channelTitle"), 10),
        ("API: 既定言語", snippet.get("defaultLanguage") or snippet.get("defaultAudioLanguage"), 8),
        ("API: カテゴリID", snippet.get("categoryId"), 12),
        ("API: 撮影場所", recording.get("locationDescription"), 12),
        ("API: 動画時間", content.get("duration"), 5),
        ("API: 再生数", statistics.get("viewCount"), 4),
        ("API: コメント数", statistics.get("commentCount"), 4),
    ):
        text = normalize_text(text or "")
        if text:
            fields.append({"label": label, "text": text, "weight": weight})

    for lang, localization in localizations.items():
        if not isinstance(localization, dict):
            continue
        for key, label, weight in (
            ("title", f"API: {lang} タイトル", 70),
            ("description", f"API: {lang} 概要欄", 18),
        ):
            text = normalize_text(localization.get(key) or "")
            if text:
                fields.append({"label": label, "text": text, "weight": weight})

    for topic_url in topic_details.get("topicCategories") or []:
        text = topic_name(topic_url)
        if text:
            fields.append({"label": "API: トピック", "text": text, "url": topic_url, "weight": 18})

    return {
        "title": first_non_empty_string(localized.get("title"), snippet.get("title")),
        "description": first_non_empty_string(localized.get("description"), snippet.get("description")),
        "publishedAt": format_upload_date(snippet.get("publishedAt") or ""),
        "thumbnail": pick_api_thumbnail(snippet.get("thumbnails")),
        "tags": normalize_list(snippet.get("tags")),
        "categoryId": normalize_text(snippet.get("categoryId") or ""),
        "additionalSearchFields": fields,
    }


def fetch_youtube_api_metadata(video_ids: list[str], api_key: str | None) -> dict[str, dict[str, Any]]:
    if not api_key:
        return {}

    metadata_by_video: dict[str, dict[str, Any]] = {}
    for group in chunks(video_ids, 50):
        payload = request_json(
            f"{YOUTUBE_API_BASE}/videos",
            {
                "part": "snippet,contentDetails,statistics,topicDetails,localizations,recordingDetails",
                "id": ",".join(group),
                "key": api_key,
                "maxResults": 50,
            },
        )
        for item in payload.get("items", []):
            video_id = item.get("id")
            if not video_id:
                continue
            metadata_by_video[video_id] = parse_youtube_api_item(item)

    return metadata_by_video


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
        href = escape_html(safe_href(video.get("url") or ""))
        published = escape_html(video.get("publishedAt") or "")
        description = escape_html((video.get("description") or "")[:180])
        rows.append(
            '<article class="video-list-item">'
            f'<h2><a href="{href}" target="_blank" rel="noopener noreferrer">{title}</a></h2>'
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

    list_json = script_json(
        {
            "@context": "https://schema.org",
            "@type": "ItemList",
            "name": "オモウォのあの回 回一覧",
            "alternateName": [
                "ニュース! オモコロウォッチ 収録回一覧",
                "オモコロウォッチのあの回",
            ],
            "url": public_url(site_url, "videos.html"),
            "numberOfItems": len(videos),
            "itemListElement": item_list,
        }
    )

    videos_html = f"""<!doctype html>
<html lang="ja">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>回一覧 | オモウォのあの回</title>
    <meta name="description" content="オモウォのあの回の回一覧。ニュース! オモコロウォッチのタイトル、公開日、概要欄を掲載しています。">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="{escape_html(public_url(site_url, 'videos.html'))}">
    <link rel="stylesheet" href="./styles.css">
    <script type="application/ld+json">
{list_json}
    </script>
  </head>
  <body>
    <header class="site-header">
      <a class="brand" href="./index.html">オモウォのあの回</a>
    </header>
    <main class="video-list-page">
      <h1>回一覧</h1>
      <p class="source-links"><a href="./index.html">検索</a></p>
      <div class="video-list">
        {''.join(rows)}
      </div>
    </main>
  </body>
</html>
"""
    (site_root / "videos.html").write_text(videos_html, encoding="utf-8")

    about_html = f"""<!doctype html>
<html lang="ja">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>このサイトについて | オモウォのあの回</title>
    <meta name="description" content="オモウォのあの回は、ニュース! オモコロウォッチの動画をキーワードで探せる非公式サイトです。">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="{escape_html(public_url(site_url, 'about.html'))}">
    <link rel="stylesheet" href="./styles.css">
  </head>
  <body>
    <header class="site-header">
      <a class="brand" href="./index.html">オモウォのあの回</a>
    </header>

    <main class="about-page">
      <h1>このサイトについて</h1>
      <p class="about-copy">オモウォのあの回は、「ニュース！オモコロウォッチ」の動画をキーワードで探せる非公式サイトです。タイトル、概要欄、字幕、コメントなどをもとに、見たい回を探せます。</p>
      <p class="source-links"><a href="./index.html">検索ページへ戻る</a></p>
    </main>
  </body>
</html>
"""
    (site_root / "about.html").write_text(about_html, encoding="utf-8")

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
  <url>
    <loc>{escape_html(public_url(site_url, 'about.html'))}</loc>
    <lastmod>{escape_html(latest_modified[:10])}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.6</priority>
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


def iter_caption_tracks(captions: dict[str, Any], lang_order: list[str]):
    seen: set[str] = set()

    for lang in lang_order:
        for key, candidates in captions.items():
            if key in seen:
                continue
            if key == lang or key.startswith(f"{lang}-"):
                seen.add(key)
                yield candidates

    for key, candidates in captions.items():
        if key in seen:
            continue
        if key == "ja" or key.startswith("ja-"):
            seen.add(key)
            yield candidates

    for key, candidates in captions.items():
        if key in seen:
            continue
        seen.add(key)
        yield candidates


def fetch_transcript(ydl: Any, info: dict[str, Any], lang_order: list[str]) -> list[dict[str, Any]]:
    subtitles = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}

    for captions in (subtitles, automatic):
        for candidates in iter_caption_tracks(captions, lang_order):
            json3 = next((item for item in candidates if item.get("ext") == "json3"), None)
            if not json3:
                continue
            data = ydl.urlopen(json3["url"]).read().decode("utf-8", errors="replace")
            return parse_subtitle_json3(json.loads(data))

    return []


def update_homepage_latest_link(payload: dict[str, Any], output: Path) -> None:
    index_path = output.parent.parent / "index.html"
    if not index_path.exists():
        return

    videos = payload.get("videos") or []
    if not videos:
        return

    latest = videos[0]
    latest_url = safe_href(latest.get("url") or "")
    latest_title = escape_html(latest.get("title") or "")
    html = index_path.read_text(encoding="utf-8")
    html = re.sub(
        r'(<a id="latest-link" href=")[^"]+(" target="_blank" rel="noopener noreferrer">)(.*?)(</a>)',
        lambda match: f'{match.group(1)}{escape_html(latest_url)}{match.group(2)}{latest_title}{match.group(4)}',
        html,
        count=1,
        flags=re.DOTALL,
    )
    index_path.write_text(html, encoding="utf-8")


def update_homepage_site_url(output: Path, site_url: str) -> None:
    index_path = output.parent.parent / "index.html"
    if not index_path.exists():
        return

    root_url = public_url(site_url)
    search_url = public_url(site_url, "?q={search_term_string}")
    html = index_path.read_text(encoding="utf-8")
    html = re.sub(
        r'(<link rel="canonical" href=")[^"]+(">)',
        lambda match: f'{match.group(1)}{escape_html(root_url)}{match.group(2)}',
        html,
        count=1,
    )
    html = re.sub(
        r'(<meta property="og:url" content=")[^"]+(">)',
        lambda match: f'{match.group(1)}{escape_html(root_url)}{match.group(2)}',
        html,
        count=1,
    )
    html = re.sub(
        r'("url":\s*")[^"]+(")',
        lambda match: f'{match.group(1)}{escape_html(root_url)}{match.group(2)}',
        html,
        count=1,
    )
    html = re.sub(
        r'("target":\s*")[^"]+(")',
        lambda match: f'{match.group(1)}{escape_html(search_url)}{match.group(2)}',
        html,
        count=1,
    )
    index_path.write_text(html, encoding="utf-8")


def non_empty_count(videos: list[dict[str, Any]], field: str) -> int:
    return sum(1 for video in videos if has_search_value(video.get(field)))


def list_item_count(videos: list[dict[str, Any]], field: str) -> int:
    return sum(len(video.get(field) or []) for video in videos if isinstance(video.get(field), list))


def validate_against_existing(payload: dict[str, Any], existing_payload: dict[str, Any]) -> None:
    existing_by_id = videos_by_id(existing_payload)
    current_by_id = videos_by_id(payload)
    overlap_ids = sorted(set(existing_by_id) & set(current_by_id))
    if not overlap_ids:
        return

    existing_overlap = [existing_by_id[video_id] for video_id in overlap_ids]
    current_overlap = [current_by_id[video_id] for video_id in overlap_ids]
    for field in CRITICAL_SEARCH_FIELDS:
        existing_count = non_empty_count(existing_overlap, field)
        current_count = non_empty_count(current_overlap, field)
        if current_count < existing_count:
            raise SystemExit(
                f"Generated index lost {field} coverage for existing videos: "
                f"{current_count}/{existing_count}. Aborting to avoid shipping a degraded search index."
            )

    existing_segments = list_item_count(existing_overlap, "transcriptSegments")
    current_segments = list_item_count(current_overlap, "transcriptSegments")
    if existing_segments and current_segments < existing_segments * 0.9:
        raise SystemExit(
            "Generated index has sharply fewer transcript segments for existing videos: "
            f"{current_segments}/{existing_segments}. Aborting to avoid shipping a degraded search index."
        )


def validate_payload(payload: dict[str, Any], existing_payload: dict[str, Any] | None = None) -> None:
    videos = payload.get("videos") or []
    if not videos:
        raise SystemExit("No videos found in generated search index.")

    description_count = sum(1 for video in videos if normalize_text(video.get("description") or ""))
    tags_count = sum(1 for video in videos if video.get("tags"))
    transcript_count = sum(1 for video in videos if video.get("transcriptSegments"))

    if description_count == 0:
        raise SystemExit("Generated index has no descriptions. Aborting to avoid shipping a degraded search index.")
    if tags_count == 0:
        raise SystemExit("Generated index has no tags. Aborting to avoid shipping a degraded search index.")
    if transcript_count == 0:
        raise SystemExit("Generated index has no transcript segments. Aborting to avoid shipping a degraded search index.")

    latest = videos[0]
    latest_title = normalize_text(latest.get("title") or "")
    if not latest_title:
        raise SystemExit("Latest video title is empty.")

    if existing_payload:
        validate_against_existing(payload, existing_payload)


def fetch_playlist_entries(channel_url: str, max_videos: int | None) -> list[dict[str, Any]]:
    return fetch_playlist_entries_for_lang(channel_url, max_videos, "ja")


def fetch_playlist_entries_for_lang(channel_url: str, max_videos: int | None, youtube_lang: str) -> list[dict[str, Any]]:
    yt_dlp = load_yt_dlp()
    playlist_url = channel_url.rstrip("/") + "/videos"
    options = {
        "extract_flat": "in_playlist",
        "extractor_args": {"youtube": {"lang": [youtube_lang]}},
        "ignoreerrors": True,
        "quiet": True,
        "skip_download": True,
    }
    if max_videos:
        options["playlistend"] = max_videos
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(playlist_url, download=False)
    if not info:
        raise SystemExit("Unable to fetch playlist entries. Aborting without updating the search index.")
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
    youtube_lang = preferred_youtube_lang(lang_order)
    existing_payload = load_existing_payload(output)
    existing_videos = videos_by_id(existing_payload)
    entries = fetch_playlist_entries_for_lang(channel_url, max_videos, youtube_lang)
    video_ids = [entry.get("id") or entry.get("url") for entry in entries if entry.get("id") or entry.get("url")]
    try:
        api_metadata = fetch_youtube_api_metadata(video_ids, youtube_api_key)
    except Exception as exc:  # noqa: BLE001 - keep yt-dlp-only updates usable.
        print(f"YouTube API metadata failed; continuing without API metadata: {exc}", file=sys.stderr)
        api_metadata = {}
    extra_fields = load_extra_search_fields(extra_search_json)
    videos: list[dict[str, Any]] = []
    metadata_failed_count = 0
    api_fallback_count = 0
    restored_video_count = 0
    restored_published_at_count = 0
    new_video_count = 0

    options = {
        "extractor_args": {"youtube": {"lang": [youtube_lang]}},
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

            metadata_failed = False
            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            except Exception as exc:  # noqa: BLE001 - keep the rest of the channel usable.
                print(f"  metadata failed: {exc}", file=sys.stderr)
                metadata_failed = True
                info = entry
            if not info:
                print("  metadata unavailable; keeping flat playlist entry", file=sys.stderr)
                metadata_failed = True
                info = entry
            elif looks_like_flat_video_entry(info):
                print("  metadata incomplete; treating as flat playlist entry", file=sys.stderr)
                metadata_failed = True
            if metadata_failed:
                metadata_failed_count += 1

            try:
                transcript = fetch_transcript(ydl, info, lang_order)
            except Exception as exc:  # noqa: BLE001
                print(f"  transcript failed: {exc}", file=sys.stderr)
                transcript = []

            try:
                comments = fetch_youtube_api_comments(video_id, youtube_api_key, comments_per_video)
            except Exception as exc:  # noqa: BLE001
                print(f"  comments failed: {exc}", file=sys.stderr)
                comments = []

            api_video = api_metadata.get(video_id, {})
            additional_search_fields = [
                *(api_video.get("additionalSearchFields") or []),
                *extra_fields.get(video_id, []),
            ]
            chosen_title, title_source = first_non_empty_value_with_source(
                (choose_preferred_title(info.get("title"), entry.get("title")), "yt-dlp"),
                (api_video.get("title"), "api"),
            )
            if normalize_text(info.get("title") or "") != normalize_text(entry.get("title") or ""):
                print(
                    "  title mismatch:"
                    f" entry={entry.get('title', '')!r}"
                    f" info={info.get('title', '')!r}"
                    f" chosen={chosen_title!r}",
                    file=sys.stderr,
                )

            description, description_source = first_non_empty_value_with_source(
                (first_non_empty_string(info.get("description"), entry.get("description")), "yt-dlp"),
                (api_video.get("description"), "api"),
            )
            tags, tags_source = first_non_empty_value_with_source(
                (first_non_empty_list(info.get("tags"), entry.get("tags")), "yt-dlp"),
                (api_video.get("tags"), "api"),
            )
            published_at, published_at_source = first_non_empty_value_with_source(
                (pick_published_at(info, entry), "yt-dlp"),
                (api_video.get("publishedAt"), "api"),
            )
            thumbnail = pick_thumbnail(info or entry)
            thumbnail_source = "yt-dlp"
            if api_video.get("thumbnail") and not ((info or {}).get("thumbnails") or entry.get("thumbnails")):
                thumbnail = api_video["thumbnail"]
                thumbnail_source = "api"
            api_used_fields = [
                field
                for field, source in (
                    ("title", title_source),
                    ("description", description_source),
                    ("tags", tags_source),
                    ("publishedAt", published_at_source),
                    ("thumbnail", thumbnail_source),
                )
                if source == "api"
            ]
            if api_used_fields:
                api_fallback_count += 1
                print(f"  filled from YouTube API: {', '.join(api_used_fields)}", file=sys.stderr)

            current_video = {
                "videoId": video_id,
                "title": chosen_title,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "thumbnail": thumbnail,
                "publishedAt": published_at,
                "description": description,
                "tags": tags,
                "categories": first_non_empty_list(info.get("categories"), entry.get("categories")),
                "chapters": first_non_empty_chapters(info.get("chapters"), entry.get("chapters")),
                "additionalSearchFields": additional_search_fields,
                "comments": comments,
                "transcriptSegments": transcript,
            }
            existing_video = existing_videos.get(video_id)
            merged_video, restored_fields = merge_video_with_existing(current_video, existing_video)
            if restored_fields:
                restored_video_count += 1
                if "publishedAt" in restored_fields:
                    restored_published_at_count += 1
                print(f"  restored from existing index: {', '.join(restored_fields)}", file=sys.stderr)
            elif not existing_video:
                new_video_count += 1
                sparse_fields = [
                    field
                    for field in ("publishedAt", "description", "tags", "categories", "transcriptSegments")
                    if not has_search_value(current_video.get(field))
                ]
                if sparse_fields:
                    print(
                        "  new video has limited search data: "
                        f"{', '.join(sparse_fields)}",
                        file=sys.stderr,
                    )
            videos.append(merged_video)

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

    print(f"metadataFailedVideos={metadata_failed_count}", file=sys.stderr)
    print(f"apiFallbackVideos={api_fallback_count}", file=sys.stderr)
    print(f"restoredFromExistingVideos={restored_video_count}", file=sys.stderr)
    print(f"restoredPublishedAtVideos={restored_published_at_count}", file=sys.stderr)
    print(f"newVideos={new_video_count}", file=sys.stderr)

    validate_payload(payload, existing_payload)
    write_outputs(payload, output)
    write_static_seo_files(payload, output, site_url)
    update_homepage_site_url(output, site_url)
    update_homepage_latest_link(payload, output)
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
