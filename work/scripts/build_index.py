#!/usr/bin/env python3
"""Build a static search index for the News! Omocoro Watch search prototype."""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, unquote, urlparse
from urllib.request import urlopen
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHANNEL_URL = "https://www.youtube.com/@news_omocorowatch"
DEFAULT_CHANNEL_HANDLE = "@news_omocorowatch"
DEFAULT_OUTPUT = ROOT / "outputs" / "omocoro-watch-search" / "data" / "search-index.json"
DEFAULT_SITE_URL = "https://omowatch.com/"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UI_LANG_FALLBACKS = {
    "ja-JP": "ja",
    "en-US": "en",
    "en-GB": "en-GB",
}
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
DEFAULT_RETRY_RECENT_COUNT = 5
DEFAULT_RETRY_RECENT_DAYS = 14
UPDATE_MODES = ("fresh", "recent", "full")
SUBTITLE_FORMAT_PREFERENCE = ("json3", "srv3", "srv2", "vtt")
BOT_BLOCK_PATTERNS = (
    "confirm you are not a bot",
    "confirm you're not a bot",
    "sign in to confirm",
    "ログインして bot ではないことを確認",
    "bot ではないことを確認",
    "年齢確認",
    "age-restricted",
    "age restricted",
)


class TranscriptUnavailableError(RuntimeError):
    """Raised when caption tracks exist but none use a supported format."""


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()


def preferred_youtube_lang(lang_order: list[str]) -> str:
    for lang in lang_order:
        candidate = YOUTUBE_UI_LANG_FALLBACKS.get(lang, lang)
        if candidate:
            return candidate
    return "ja"


def load_yt_dlp():
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "yt-dlp is required for transcript extraction. Install it with: python -m pip install yt-dlp"
        ) from exc
    return yt_dlp


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


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def local_xml_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def parse_subtitle_xml(text: str) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(text)
    segments: list[dict[str, Any]] = []
    for node in root.iter():
        name = local_xml_name(node.tag)
        if name not in ("text", "p"):
            continue
        raw_text = html_lib.unescape("".join(node.itertext()))
        caption_text = normalize_text(raw_text)
        if not caption_text:
            continue
        attrs = node.attrib
        if "start" in attrs:
            start = parse_float(attrs.get("start"))
        else:
            start = parse_float(attrs.get("t")) / 1000
        if "dur" in attrs:
            duration = parse_float(attrs.get("dur"))
        else:
            duration = parse_float(attrs.get("d")) / 1000
        segments.append(
            {"start": round(start, 3), "duration": round(duration, 3), "text": caption_text}
        )
    return segments


def parse_vtt_timestamp(value: str) -> float:
    match = re.fullmatch(r"(?:(\d+):)?(\d{2}):(\d{2})[\.,](\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid VTT timestamp: {value}")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    milliseconds = int(match.group(4))
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def clean_vtt_text(lines: list[str]) -> str:
    text = "\n".join(lines)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    return normalize_text(text)


def parse_subtitle_vtt(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index].strip("\ufeff")
        if not line.strip() or line.startswith(("WEBVTT", "Kind:", "Language:")):
            index += 1
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        if "-->" not in line:
            index += 1
            if index >= len(lines):
                break
            line = lines[index].strip()
        if "-->" not in line:
            continue
        start_raw, end_raw = line.split("-->", 1)
        start = parse_vtt_timestamp(start_raw.strip())
        end = parse_vtt_timestamp(end_raw.strip().split()[0])
        index += 1
        cue_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            cue_lines.append(lines[index])
            index += 1
        cue_text = clean_vtt_text(cue_lines)
        if cue_text:
            segments.append(
                {
                    "start": round(start, 3),
                    "duration": round(max(0, end - start), 3),
                    "text": cue_text,
                }
            )
    return segments


def parse_subtitle_payload(ext: str, text: str) -> list[dict[str, Any]]:
    if ext == "json3":
        return parse_subtitle_json3(json.loads(text))
    if ext in ("srv3", "srv2"):
        return parse_subtitle_xml(text)
    if ext == "vtt":
        return parse_subtitle_vtt(text)
    raise ValueError(f"Unsupported subtitle format: {ext}")


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


def parse_iso_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10] + "T00:00:00+00:00")
    except ValueError:
        return None


def days_since(value: str) -> int | None:
    parsed = parse_iso_date(value)
    if not parsed:
        return None
    return (datetime.now(timezone.utc) - parsed).days


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


def is_blocked_transcript_error(reason: str) -> bool:
    lowered = str(reason or "").casefold()
    return any(pattern.casefold() in lowered for pattern in BOT_BLOCK_PATTERNS)


def should_fetch_transcript(
    existing: dict[str, Any] | None,
    published_at: str,
    position: int,
    retry_missing_transcripts: bool,
    retry_recent_count: int,
    retry_recent_days: int,
    update_mode: str,
) -> tuple[bool, str]:
    if has_search_value((existing or {}).get("transcriptSegments")):
        return False, "existing transcript preserved"
    if not existing:
        return True, "new video"
    age_days = days_since(published_at)
    if update_mode == "full":
        if retry_missing_transcripts:
            return True, "missing transcript retry"
        return False, "full mode missing transcript retry disabled"
    if update_mode in ("fresh", "recent") and retry_recent_count > 0 and position <= retry_recent_count:
        return True, "recent missing transcript"
    if update_mode in ("fresh", "recent") and age_days is not None and age_days <= 30:
        return True, "missing transcript within 30 days"
    if age_days is not None and retry_recent_days > 0 and age_days <= retry_recent_days:
        return True, "fresh missing transcript"
    return False, "missing transcript retry disabled"


def should_fetch_comments(
    existing: dict[str, Any] | None,
    published_at: str,
    position: int,
    update_mode: str,
    comments_per_video: int,
) -> tuple[bool, str]:
    if comments_per_video <= 0:
        return False, "comments disabled"
    if not existing:
        return True, "new video"
    age_days = days_since(published_at)
    if update_mode == "fresh":
        if position <= 5:
            return True, "fresh recent video"
        if age_days is not None and age_days <= 3:
            return True, "fresh within 3 days"
    if update_mode == "recent":
        if position <= 5:
            return True, "recent video"
        if age_days is not None and age_days <= 14:
            return True, "recent within 14 days"
    return False, "existing comments preserved"


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


def build_search_text(video: dict[str, Any]) -> str:
    parts: list[str] = [
        str(video.get("title") or ""),
        str(video.get("description") or ""),
    ]
    parts.extend(str(value) for value in video.get("tags") or [])
    parts.extend(str(value) for value in video.get("categories") or [])
    parts.extend(str(chapter.get("title") or "") for chapter in video.get("chapters") or [] if isinstance(chapter, dict))
    parts.extend(
        str(field.get("text") or field.get("value") or field.get("title") or field.get("body") or "")
        for field in video.get("additionalSearchFields") or []
        if isinstance(field, dict)
    )
    parts.extend(
        str(comment.get("text") or "")
        for comment in video.get("comments") or []
        if isinstance(comment, dict)
    )
    parts.extend(
        str(segment.get("text") or "")
        for segment in video.get("transcriptSegments") or []
        if isinstance(segment, dict)
    )
    return normalize_text(" ".join(part for part in parts if part)).casefold()


def payload_statistics(payload: dict[str, Any], output: Path) -> dict[str, Any]:
    videos = payload.get("videos") or []
    total_videos = len(videos)
    total_transcript_segments = list_item_count(videos, "transcriptSegments")
    return {
        "indexFileSizeBytes": output.stat().st_size if output.exists() else 0,
        "totalVideos": total_videos,
        "totalTranscriptSegments": total_transcript_segments,
        "totalComments": list_item_count(videos, "comments"),
        "averageTranscriptSegmentsPerVideo": (
            round(total_transcript_segments / total_videos, 2) if total_videos else 0
        ),
        "videosWithoutTranscripts": sum(
            1 for video in videos if not has_search_value(video.get("transcriptSegments"))
        ),
    }


def print_payload_statistics(payload: dict[str, Any], output: Path) -> None:
    stats = payload_statistics(payload, output)
    size_mb = stats["indexFileSizeBytes"] / (1024 * 1024)
    print(f"indexFileSizeBytes={stats['indexFileSizeBytes']}", file=sys.stderr)
    print(f"indexFileSizeMB={size_mb:.2f}", file=sys.stderr)
    print(f"totalVideos={stats['totalVideos']}", file=sys.stderr)
    print(f"totalTranscriptSegments={stats['totalTranscriptSegments']}", file=sys.stderr)
    print(f"totalComments={stats['totalComments']}", file=sys.stderr)
    print(
        f"averageTranscriptSegmentsPerVideo={stats['averageTranscriptSegmentsPerVideo']}",
        file=sys.stderr,
    )
    print(f"videosWithoutTranscripts={stats['videosWithoutTranscripts']}", file=sys.stderr)


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


def request_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
    try:
        with urlopen(f"{url}?{query}", timeout=30) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"YouTube Data API request failed: HTTP {exc.code} {body}") from exc


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def extract_channel_handle(channel_url: str) -> str:
    parsed = urlparse(channel_url or "")
    path = parsed.path.strip("/")
    if path.startswith("@"):
        return path.split("/", 1)[0]
    return ""


def fetch_youtube_api_channel(
    api_key: str,
    channel_id: str,
    channel_handle: str,
) -> dict[str, str]:
    params: dict[str, Any] = {
        "part": "snippet,contentDetails",
        "key": api_key,
    }
    if channel_id:
        params["id"] = channel_id
    elif channel_handle:
        params["forHandle"] = channel_handle
    else:
        raise SystemExit("Either --channel-id, --channel-handle, or --channel-url with a handle is required.")

    payload = request_json(f"{YOUTUBE_API_BASE}/channels", params)
    items = payload.get("items") or []
    if not items and params.get("forHandle", "").startswith("@"):
        params["forHandle"] = params["forHandle"].lstrip("@")
        payload = request_json(f"{YOUTUBE_API_BASE}/channels", params)
        items = payload.get("items") or []
    if not items:
        raise SystemExit("YouTube Data API did not return a channel. Check the channel identifier.")

    item = items[0]
    snippet = item.get("snippet") or {}
    related = (item.get("contentDetails") or {}).get("relatedPlaylists") or {}
    resolved_channel_id = normalize_text(item.get("id") or channel_id)
    uploads_playlist_id = normalize_text(related.get("uploads") or "")
    if not uploads_playlist_id:
        raise SystemExit("YouTube Data API response did not include an uploads playlist ID.")

    return {
        "id": resolved_channel_id,
        "name": first_non_empty_string(snippet.get("title"), "ニュース! オモコロウォッチ"),
        "url": f"https://www.youtube.com/channel/{resolved_channel_id}" if resolved_channel_id else DEFAULT_CHANNEL_URL,
        "uploadsPlaylistId": uploads_playlist_id,
    }


def fetch_youtube_api_upload_video_ids(
    uploads_playlist_id: str,
    api_key: str,
    max_videos: int | None,
) -> list[str]:
    video_ids: list[str] = []
    page_token = ""
    while True:
        payload = request_json(
            f"{YOUTUBE_API_BASE}/playlistItems",
            {
                "part": "contentDetails",
                "playlistId": uploads_playlist_id,
                "key": api_key,
                "maxResults": 50,
                "pageToken": page_token,
            },
        )
        for item in payload.get("items") or []:
            video_id = normalize_text((item.get("contentDetails") or {}).get("videoId") or "")
            if video_id:
                video_ids.append(video_id)
                if max_videos and len(video_ids) >= max_videos:
                    return video_ids
        page_token = payload.get("nextPageToken") or ""
        if not page_token:
            break
    return video_ids


def parse_timecode(value: str) -> float | None:
    parts = value.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        minutes, seconds = numbers
        return float(minutes * 60 + seconds)
    hours, minutes, seconds = numbers
    return float(hours * 3600 + minutes * 60 + seconds)


def extract_chapters_from_description(description: str) -> list[dict[str, Any]]:
    chapters: list[dict[str, Any]] = []
    for line in str(description or "").splitlines():
        match = re.search(r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+(?P<title>.+)", line)
        if not match:
            continue
        start = parse_timecode(match.group("time"))
        title = normalize_text(match.group("title"))
        if start is None or not title:
            continue
        chapters.append({"title": title, "start": round(start, 3), "end": round(start, 3)})
    for index, chapter in enumerate(chapters[:-1]):
        chapter["end"] = chapters[index + 1]["start"]
    return chapters


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

    category_id = normalize_text(snippet.get("categoryId") or "")
    description = first_non_empty_string(localized.get("description"), snippet.get("description"))

    return {
        "title": first_non_empty_string(localized.get("title"), snippet.get("title")),
        "description": description,
        "publishedAt": format_upload_date(snippet.get("publishedAt") or ""),
        "thumbnail": pick_api_thumbnail(snippet.get("thumbnails")),
        "tags": normalize_list(snippet.get("tags")),
        "categoryId": category_id,
        "categories": [category_id] if category_id else [],
        "chapters": extract_chapters_from_description(description),
        "additionalSearchFields": fields,
    }


def fetch_youtube_api_metadata(video_ids: list[str], api_key: str) -> dict[str, dict[str, Any]]:
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


def fetch_youtube_api_comments(video_id: str, api_key: str, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
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
                "item": {
                    "@type": "VideoObject",
                    "name": video.get("title") or "",
                    "url": video.get("url") or "",
                    "thumbnailUrl": video.get("thumbnail") or "",
                    "uploadDate": video.get("publishedAt") or "",
                    "description": (video.get("description") or "")[:180],
                },
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
    <title>収録回一覧 | オモウォのあの回</title>
    <meta name="description" content="ニュース! オモコロウォッチの収録回一覧です。各動画のタイトル、公開日、概要欄を確認できます。">
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


def caption_language_keys(captions: dict[str, Any]) -> list[str]:
    return sorted(str(key) for key in captions.keys())


def caption_candidate_exts(candidates: Any) -> list[str]:
    if not isinstance(candidates, list):
        return []
    return [str(item.get("ext") or "") for item in candidates if isinstance(item, dict)]


def iter_caption_tracks(captions: dict[str, Any], lang_order: list[str]):
    seen: set[str] = set()

    for lang in lang_order:
        for key, candidates in captions.items():
            if key in seen:
                continue
            if key == lang or key.startswith(f"{lang}-"):
                seen.add(key)
                yield key, candidates

    for key, candidates in captions.items():
        if key in seen:
            continue
        if key == "ja" or key.startswith("ja-"):
            seen.add(key)
            yield key, candidates

    for key, candidates in captions.items():
        if key in seen:
            continue
        seen.add(key)
        yield key, candidates


def log_caption_diagnostics(video_id: str, subtitles: dict[str, Any], automatic: dict[str, Any]) -> None:
    print(f"  transcript diagnostics: videoId={video_id}", file=sys.stderr)
    print(
        f"  transcript diagnostics: subtitles languages={caption_language_keys(subtitles)}",
        file=sys.stderr,
    )
    print(
        f"  transcript diagnostics: automatic_captions languages={caption_language_keys(automatic)}",
        file=sys.stderr,
    )


def select_caption_track(
    subtitles: dict[str, Any],
    automatic: dict[str, Any],
    lang_order: list[str],
) -> tuple[str, str, dict[str, Any]] | None:
    for source_name, captions in (("subtitles", subtitles), ("automatic_captions", automatic)):
        for lang_key, candidates in iter_caption_tracks(captions, lang_order):
            if not isinstance(candidates, list):
                continue
            print(
                f"  transcript diagnostics: candidate {source_name}[{lang_key}] ext="
                f"{caption_candidate_exts(candidates)}",
                file=sys.stderr,
            )
            for ext in SUBTITLE_FORMAT_PREFERENCE:
                for item in candidates:
                    if isinstance(item, dict) and item.get("ext") == ext:
                        return lang_key, ext, item
    return None


def fetch_transcript(ydl: Any, video_id: str, lang_order: list[str]) -> list[dict[str, Any]]:
    info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    if not info:
        return []
    subtitles = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    log_caption_diagnostics(video_id, subtitles, automatic)

    if not subtitles and not automatic:
        print(f"  transcript no captions: {video_id}", file=sys.stderr)
        return []

    selected = select_caption_track(subtitles, automatic, lang_order)
    if not selected:
        raise TranscriptUnavailableError(
            f"caption tracks found but no supported formats: {', '.join(SUBTITLE_FORMAT_PREFERENCE)}"
        )

    lang_key, ext, track = selected
    print(f"  transcript selected language: {lang_key}", file=sys.stderr)
    print(f"  transcript selected format: {ext}", file=sys.stderr)
    url = track.get("url")
    if not url:
        raise RuntimeError(f"selected caption track has no URL: {lang_key} {ext}")
    data = ydl.urlopen(url).read().decode("utf-8", errors="replace")
    segments = parse_subtitle_payload(ext, data)
    if not segments:
        raise RuntimeError(f"{ext} caption parsed no transcript segments")
    return segments


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
    homepage_json = script_json(
        {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "オモウォのあの回",
            "description": "ニュース! オモコロウォッチの動画をタイトル、概要欄、タグ、チャプター、字幕、コメントから検索できる非公式サイトです。",
            "alternateName": [
                "オモコロウォッチのあの回",
                "ニュース! オモコロウォッチ検索",
                "オモコロウォッチ 検索",
            ],
            "url": root_url,
            "inLanguage": "ja",
            "isAccessibleForFree": True,
            "about": {
                "@type": "Thing",
                "name": "ニュース! オモコロウォッチ",
                "url": "https://www.youtube.com/@news_omocorowatch",
            },
            "potentialAction": {
                "@type": "SearchAction",
                "target": {
                    "@type": "EntryPoint",
                    "urlTemplate": search_url,
                },
                "query-input": "required name=search_term_string",
            },
        }
    )
    html = index_path.read_text(encoding="utf-8")
    html = re.sub(
        r"(<title>).*?(</title>)",
        r"\1オモウォのあの回 | ニュース! オモコロウォッチ検索\2",
        html,
        count=1,
        flags=re.DOTALL,
    )
    html = re.sub(
        r'(<meta name="description" content=")[^"]+(">)',
        r"\1ニュース! オモコロウォッチの動画を、タイトル・概要欄・タグ・チャプター・字幕・コメントから検索できる非公式サイトです。\2",
        html,
        count=1,
    )
    html = re.sub(
        r'(<meta property="og:title" content=")[^"]+(">)',
        r"\1オモウォのあの回 | ニュース! オモコロウォッチ検索\2",
        html,
        count=1,
    )
    html = re.sub(
        r'(<meta property="og:description" content=")[^"]+(">)',
        r"\1ニュース! オモコロウォッチの動画を、タイトル・概要欄・タグ・チャプター・字幕・コメントから検索できます。\2",
        html,
        count=1,
    )
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
        r'(<script type="application/ld\+json">)\s*.*?(\s*</script>)',
        lambda match: f"{match.group(1)}\n{homepage_json}\n    {match.group(2).lstrip()}",
        html,
        count=1,
        flags=re.DOTALL,
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
    lost_transcript_ids = [
        video_id
        for video_id in overlap_ids
        if has_search_value(existing_by_id[video_id].get("transcriptSegments"))
        and not has_search_value(current_by_id[video_id].get("transcriptSegments"))
    ]
    if lost_transcript_ids:
        raise SystemExit(
            "Generated index lost transcript segments for existing videos: "
            f"{', '.join(lost_transcript_ids[:10])}. Aborting to avoid shipping a degraded search index."
        )

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
    api_field_counts = {
        "title": sum(1 for video in videos if normalize_text(video.get("title") or "")),
        "url": sum(1 for video in videos if normalize_text(video.get("url") or "")),
        "publishedAt": sum(1 for video in videos if normalize_text(video.get("publishedAt") or "")),
        "thumbnail": sum(1 for video in videos if normalize_text(video.get("thumbnail") or "")),
    }

    if description_count == 0:
        raise SystemExit("Generated index has no descriptions. Aborting to avoid shipping a degraded search index.")
    if tags_count == 0:
        raise SystemExit("Generated index has no tags. Aborting to avoid shipping a degraded search index.")
    if transcript_count == 0:
        raise SystemExit("Generated index has no transcript segments. Aborting to avoid shipping a degraded search index.")
    for field, count in api_field_counts.items():
        if count == 0:
            raise SystemExit(
                f"Generated index has no {field} values. Aborting because YouTube Data API metadata is degraded."
            )

    latest = videos[0]
    latest_title = normalize_text(latest.get("title") or "")
    if not latest_title:
        raise SystemExit("Latest video title is empty.")

    if existing_payload:
        validate_against_existing(payload, existing_payload)


def build_index(
    channel_url: str,
    channel_id: str,
    channel_handle: str,
    uploads_playlist_id: str,
    output: Path,
    max_videos: int | None,
    lang_order: list[str],
    youtube_api_key: str | None,
    comments_per_video: int,
    extra_search_json: Path | None,
    site_url: str,
    retry_missing_transcripts: bool,
    retry_recent_count: int,
    retry_recent_days: int,
    update_mode: str,
) -> dict[str, Any]:
    youtube_lang = preferred_youtube_lang(lang_order)
    if not youtube_api_key:
        raise SystemExit("YOUTUBE_API_KEY is required. YouTube Data API is the primary data source.")

    existing_payload = load_existing_payload(output)
    existing_videos = videos_by_id(existing_payload)
    resolved_handle = channel_handle or extract_channel_handle(channel_url)
    channel = (
        {
            "id": channel_id,
            "name": "ニュース! オモコロウォッチ",
            "url": f"https://www.youtube.com/channel/{channel_id}" if channel_id else (channel_url or DEFAULT_CHANNEL_URL),
            "uploadsPlaylistId": uploads_playlist_id,
        }
        if uploads_playlist_id
        else fetch_youtube_api_channel(youtube_api_key, channel_id, resolved_handle)
    )
    video_ids = fetch_youtube_api_upload_video_ids(channel["uploadsPlaylistId"], youtube_api_key, max_videos)
    if not video_ids:
        raise SystemExit("YouTube Data API returned no videos from the uploads playlist.")

    api_metadata = fetch_youtube_api_metadata(video_ids, youtube_api_key)
    extra_fields = load_extra_search_fields(extra_search_json)
    videos: list[dict[str, Any]] = []
    missing_api_metadata_count = 0
    api_metadata_fetched_count = 0
    transcript_failed_count = 0
    transcript_attempted_count = 0
    transcript_fetched_count = 0
    transcript_restored_count = 0
    skipped_transcript_count = 0
    preserved_transcript_count = 0
    transcript_blocked_count = 0
    transcript_unavailable_count = 0
    videos_without_transcripts = 0
    new_videos_without_transcripts = 0
    comments_failed_count = 0
    comments_fetched_count = 0
    comments_restored_count = 0
    comments_skipped_count = 0
    restored_video_count = 0
    new_video_count = 0
    existing_video_count = 0
    recent_video_count = 0
    fresh_video_count = 0
    transcript_failures: list[tuple[str, str, str]] = []

    options = {
        "extractor_args": {"youtube": {"lang": [youtube_lang]}},
        "ignoreerrors": True,
        "no_warnings": True,
        "quiet": True,
        "skip_download": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }

    for index, video_id in enumerate(video_ids, start=1):
        api_video = api_metadata.get(video_id)
        if not api_video:
            missing_api_metadata_count += 1
            print(f"[{index}/{len(video_ids)}] {video_id} API metadata missing; skipping", file=sys.stderr)
            continue
        api_metadata_fetched_count += 1
        print(f"[{index}/{len(video_ids)}] {video_id} {api_video.get('title', '')}", file=sys.stderr)

        existing_video = existing_videos.get(video_id)
        if existing_video:
            existing_video_count += 1
        else:
            new_video_count += 1
        age_days = days_since(api_video.get("publishedAt") or "")
        if index <= 5 or (age_days is not None and age_days <= 14):
            recent_video_count += 1
        if not existing_video or index <= 5 or (age_days is not None and age_days <= 3):
            fresh_video_count += 1

        fetch_transcript_now, transcript_reason = should_fetch_transcript(
            existing_video,
            api_video.get("publishedAt") or "",
            index,
            retry_missing_transcripts,
            retry_recent_count,
            retry_recent_days,
            update_mode,
        )
        if fetch_transcript_now:
            transcript_attempted_count += 1
            print(f"  transcript fetch: {transcript_reason}", file=sys.stderr)
            try:
                yt_dlp = load_yt_dlp()
                with yt_dlp.YoutubeDL(options) as ydl:
                    transcript = fetch_transcript(ydl, video_id, lang_order)
            except TranscriptUnavailableError as exc:
                reason = str(exc)
                print(f"  transcript unavailable: {reason}", file=sys.stderr)
                transcript_unavailable_count += 1
                transcript = []
            except Exception as exc:  # noqa: BLE001
                reason = str(exc)
                print(f"  transcript failed: {reason}", file=sys.stderr)
                transcript_failures.append((video_id, api_video.get("title", ""), reason))
                transcript_failed_count += 1
                if is_blocked_transcript_error(reason):
                    transcript_blocked_count += 1
                transcript = []
            if transcript:
                transcript_fetched_count += 1
        else:
            skipped_transcript_count += 1
            if existing_video and has_search_value(existing_video.get("transcriptSegments")):
                preserved_transcript_count += 1
            print(f"  transcript skipped: {transcript_reason}", file=sys.stderr)
            transcript = []

        fetch_comments_now, comments_reason = should_fetch_comments(
            existing_video,
            api_video.get("publishedAt") or "",
            index,
            update_mode,
            comments_per_video,
        )
        comments_fetch_succeeded = False
        if fetch_comments_now:
            print(f"  comments fetch: {comments_reason}", file=sys.stderr)
            try:
                comments = fetch_youtube_api_comments(video_id, youtube_api_key, comments_per_video)
                comments_fetch_succeeded = True
            except Exception as exc:  # noqa: BLE001
                print(f"  comments failed: {exc}", file=sys.stderr)
                comments_failed_count += 1
                comments = []
        else:
            comments_skipped_count += 1
            print(f"  comments skipped: {comments_reason}", file=sys.stderr)
            comments = []
        if comments_fetch_succeeded:
            comments_fetched_count += 1

        additional_search_fields = [
            *(api_video.get("additionalSearchFields") or []),
            *extra_fields.get(video_id, []),
        ]
        chapters = api_video.get("chapters") or []
        existing_chapters = existing_video.get("chapters") if existing_video else []
        if (
            isinstance(existing_chapters, list)
            and len(existing_chapters) > len(chapters)
            and has_search_value(existing_chapters)
        ):
            chapters = []

        current_video = {
            "videoId": video_id,
            "title": api_video.get("title") or "",
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": api_video.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "publishedAt": api_video.get("publishedAt") or "",
            "description": api_video.get("description") or "",
            "tags": api_video.get("tags") or [],
            "categories": api_video.get("categories") or [],
            "chapters": chapters,
            "additionalSearchFields": additional_search_fields,
            "comments": comments,
            "transcriptSegments": transcript,
        }
        merged_video, restored_fields = merge_video_with_existing(current_video, existing_video)
        if restored_fields:
            restored_video_count += 1
            if "transcriptSegments" in restored_fields:
                transcript_restored_count += 1
            if "comments" in restored_fields:
                comments_restored_count += 1
            print(f"  restored from existing index: {', '.join(restored_fields)}", file=sys.stderr)
        if not has_search_value(merged_video.get("transcriptSegments")):
            videos_without_transcripts += 1
            if not existing_video:
                new_videos_without_transcripts += 1
        if not existing_video:
            sparse_fields = [
                field
                for field in ("publishedAt", "description", "tags", "categories", "transcriptSegments")
                if not has_search_value(merged_video.get(field))
            ]
            if sparse_fields:
                print(
                    "  new video has limited search data: "
                    f"{', '.join(sparse_fields)}",
                        file=sys.stderr,
                    )
        merged_video["searchText"] = build_search_text(merged_video)
        videos.append(merged_video)

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "channel": {
            "name": channel["name"],
            "url": channel["url"],
        },
        "source": {
            "tool": "youtube-data-api",
            "transcriptTool": "yt-dlp",
            "updateMode": update_mode,
            "transcriptMode": "missing-only",
            "maxVideos": max_videos,
            "subtitleLanguages": lang_order,
            "commentsPerVideo": comments_per_video,
            "retryMissingTranscripts": retry_missing_transcripts,
            "retryRecentCount": retry_recent_count,
            "retryRecentDays": retry_recent_days,
            "extraSearchJson": str(extra_search_json) if extra_search_json else "",
        },
        "videos": videos,
    }

    print(f"totalVideos={len(videos)}", file=sys.stderr)
    print(f"updateMode={update_mode}", file=sys.stderr)
    print(f"newVideos={new_video_count}", file=sys.stderr)
    print(f"existingVideos={existing_video_count}", file=sys.stderr)
    print(f"recentVideos={recent_video_count}", file=sys.stderr)
    print(f"freshVideos={fresh_video_count}", file=sys.stderr)
    print(f"apiMetadataFetchedVideos={api_metadata_fetched_count}", file=sys.stderr)
    print(f"missingApiMetadataVideos={missing_api_metadata_count}", file=sys.stderr)
    print(f"commentsFetchedVideos={comments_fetched_count}", file=sys.stderr)
    print(f"commentsRestoredFromExistingVideos={comments_restored_count}", file=sys.stderr)
    print(f"commentsSkippedVideos={comments_skipped_count}", file=sys.stderr)
    print(f"commentsFailedVideos={comments_failed_count}", file=sys.stderr)
    print(f"transcriptFetchAttemptedVideos={transcript_attempted_count}", file=sys.stderr)
    print(f"transcriptFetchedVideos={transcript_fetched_count}", file=sys.stderr)
    print(f"transcriptFetchFailedVideos={transcript_failed_count}", file=sys.stderr)
    print(f"transcriptRestoredFromExistingVideos={transcript_restored_count}", file=sys.stderr)
    print(f"transcriptSkippedVideos={skipped_transcript_count}", file=sys.stderr)
    print(f"transcriptBlockedVideos={transcript_blocked_count}", file=sys.stderr)
    print(f"transcriptUnavailableVideos={transcript_unavailable_count}", file=sys.stderr)
    print(f"videosWithoutTranscripts={videos_without_transcripts}", file=sys.stderr)
    print(f"newVideosWithoutTranscripts={new_videos_without_transcripts}", file=sys.stderr)
    print(f"skippedTranscriptVideos={skipped_transcript_count}", file=sys.stderr)
    print(f"preservedTranscriptVideos={preserved_transcript_count}", file=sys.stderr)
    if transcript_failures:
        print("transcriptFetchFailed:", file=sys.stderr)
        for failed_video_id, failed_title, reason in transcript_failures:
            print(f"- {failed_video_id} {failed_title}", file=sys.stderr)
            print(f"  reason: {reason}", file=sys.stderr)
    print(f"restoredFromExistingVideos={restored_video_count}", file=sys.stderr)

    validate_payload(payload, existing_payload)
    write_outputs(payload, output)
    print_payload_statistics(payload, output)
    write_static_seo_files(payload, output, site_url)
    update_homepage_site_url(output, site_url)
    update_homepage_latest_link(payload, output)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-url", default=DEFAULT_CHANNEL_URL, help="Compatibility helper used to derive a channel handle when possible.")
    parser.add_argument("--channel-id", default=os.environ.get("YOUTUBE_CHANNEL_ID", ""), help="YouTube channel ID. Optional when --channel-handle or --uploads-playlist-id is set.")
    parser.add_argument("--channel-handle", default=os.environ.get("YOUTUBE_CHANNEL_HANDLE", DEFAULT_CHANNEL_HANDLE), help="YouTube channel handle used with channels.list.")
    parser.add_argument("--uploads-playlist-id", default=os.environ.get("YOUTUBE_UPLOADS_PLAYLIST_ID", ""), help="Uploads playlist ID. Skips channels.list when set.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-videos", type=int, default=30)
    parser.add_argument("--all", action="store_true", help="Fetch every video currently listed on the channel.")
    parser.add_argument("--update-mode", choices=UPDATE_MODES, default="recent", help="Update targeting mode: fresh for Sunday/Monday, recent for weekday catch-up, full for broad consistency checks.")
    parser.add_argument("--languages", default="ja,ja-JP,en", help="Comma-separated subtitle language preference order.")
    parser.add_argument("--youtube-api-key", default=os.environ.get("YOUTUBE_API_KEY", ""), help="Required YouTube Data API key. Defaults to YOUTUBE_API_KEY.")
    parser.add_argument("--comments-per-video", type=int, default=0, help="Top-level YouTube comments to index per video. Use 0 to skip commentThreads.list.")
    parser.add_argument("--retry-missing-transcripts", action="store_true", help="Retry yt-dlp transcript extraction for videos without cached transcript segments.")
    parser.add_argument("--retry-recent-count", type=int, default=DEFAULT_RETRY_RECENT_COUNT, help="Retry transcript extraction for this many recent videos when transcripts are missing.")
    parser.add_argument("--retry-recent-days", type=int, default=DEFAULT_RETRY_RECENT_DAYS, help="Retry transcript extraction for missing videos published within this many days.")
    parser.add_argument("--extra-search-json", type=Path, help="Optional JSON file with additional search fields keyed by video id.")
    parser.add_argument("--site-url", default=os.environ.get("SITE_URL", DEFAULT_SITE_URL), help="Public site URL used in sitemap and structured data.")
    args = parser.parse_args()

    payload = build_index(
        channel_url=args.channel_url,
        channel_id=args.channel_id,
        channel_handle=args.channel_handle,
        uploads_playlist_id=args.uploads_playlist_id,
        output=args.output,
        max_videos=None if args.all else args.max_videos,
        lang_order=[lang.strip() for lang in args.languages.split(",") if lang.strip()],
        youtube_api_key=args.youtube_api_key or None,
        comments_per_video=max(0, args.comments_per_video),
        extra_search_json=args.extra_search_json,
        site_url=args.site_url,
        retry_missing_transcripts=args.retry_missing_transcripts,
        retry_recent_count=max(0, args.retry_recent_count),
        retry_recent_days=max(0, args.retry_recent_days),
        update_mode=args.update_mode,
    )
    print(f"Wrote {len(payload['videos'])} videos to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
