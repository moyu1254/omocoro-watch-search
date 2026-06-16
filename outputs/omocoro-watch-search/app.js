const DATA_URL = "./data/search-index.json";
const MAX_SNIPPETS = 3;
const MAX_FIELD_HITS = 5;
const RESULTS_PAGE_SIZE = 50;

const state = {
  index: null,
  videos: [],
  results: [],
  visibleResults: RESULTS_PAGE_SIZE,
};

const elements = {
  form: document.querySelector("#search-form"),
  query: document.querySelector("#query"),
  status: document.querySelector("#status"),
  results: document.querySelector("#results"),
  resultCount: document.querySelector("#result-count"),
  latestLink: document.querySelector("#latest-link"),
  loadMore: document.querySelector("#load-more"),
};

function normalizeText(value) {
  return String(value || "")
    .normalize("NFKC")
    .toLocaleLowerCase("ja-JP")
    .replace(/\s+/g, " ")
    .trim();
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function safeUrl(value, fallback = "#") {
  if (!String(value || "").trim()) return fallback;
  try {
    const url = new URL(String(value || ""), window.location.href);
    if (url.protocol === "http:" || url.protocol === "https:") {
      return url.href;
    }
  } catch {
    // Use the fallback for malformed URLs.
  }
  return fallback;
}

function formatDate(value) {
  if (!value) return "公開日不明";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ja-JP", { dateStyle: "medium" }).format(date);
}

function dateRank(value) {
  const date = new Date(value || "");
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

function formatTime(seconds) {
  const safeSeconds = Math.max(0, Math.floor(Number(seconds) || 0));
  const mins = Math.floor(safeSeconds / 60);
  const secs = safeSeconds % 60;
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function highlight(text, query) {
  const source = String(text || "");
  const normalizedQuery = normalizeText(query);
  if (!normalizedQuery) return escapeHtml(source);

  const range = findNormalizedRange(source, normalizedQuery);
  if (!range) return escapeHtml(source);

  const before = source.slice(0, range.start);
  const matched = source.slice(range.start, range.end);
  const after = source.slice(range.end);
  return `${escapeHtml(before)}<mark>${escapeHtml(matched)}</mark>${escapeHtml(after)}`;
}

function findNormalizedRange(source, normalizedQuery) {
  let folded = "";
  const map = [];

  for (let index = 0; index < source.length; index += 1) {
    const foldedChar = normalizeText(source[index]);
    for (let offset = 0; offset < foldedChar.length; offset += 1) {
      map.push({ start: index, end: index + 1 });
    }
    folded += foldedChar;
  }

  const matchAt = folded.indexOf(normalizedQuery);
  if (matchAt === -1) return null;

  const start = map[matchAt]?.start ?? matchAt;
  const end = map[matchAt + normalizedQuery.length - 1]?.end ?? start + normalizedQuery.length;
  return { start, end };
}

function makeSnippet(text, query) {
  const source = String(text || "").replace(/\s+/g, " ").trim();
  const normalizedSource = normalizeText(source);
  const normalizedQuery = normalizeText(query);
  const matchAt = normalizedSource.indexOf(normalizedQuery);
  if (matchAt === -1) return source.slice(0, 130);

  const start = Math.max(0, matchAt - 42);
  const end = Math.min(source.length, matchAt + normalizedQuery.length + 68);
  const prefix = start > 0 ? "..." : "";
  const suffix = end < source.length ? "..." : "";
  return `${prefix}${source.slice(start, end)}${suffix}`;
}

function buildVideoUrl(video, seconds = 0) {
  const base = safeUrl(video.url || `https://www.youtube.com/watch?v=${video.videoId}`);
  if (!seconds) return base;
  return `https://www.youtube.com/watch?v=${encodeURIComponent(video.videoId)}&t=${Math.floor(seconds)}s`;
}

function renderLatestLink() {
  const latest = state.videos[0];
  if (!latest || !elements.latestLink) return;
  elements.latestLink.href = buildVideoUrl(latest);
  elements.latestLink.textContent = latest.title || latest.videoId;
}

function readQueryParam() {
  return new URLSearchParams(window.location.search).get("q") || "";
}

function writeQueryParam(query) {
  const url = new URL(window.location.href);
  if (normalizeText(query)) {
    url.searchParams.set("q", query);
  } else {
    url.searchParams.delete("q");
  }
  window.history.replaceState(null, "", url);
}

function countLabel(label, count) {
  return count ? `${label} ${Number(count).toLocaleString("ja-JP")}件` : "";
}

function buildMeta(video) {
  return [
    formatDate(video.publishedAt),
    countLabel("字幕", video.transcriptSegments?.length || 0),
    countLabel("タグ", video.tags?.length || 0),
    countLabel("カテゴリ", video.categories?.length || 0),
    countLabel("チャプター", video.chapters?.length || 0),
    countLabel("コメント", video.comments?.length || 0),
  ].filter(Boolean).map(escapeHtml).join(" · ");
}

function normalizeSearchField(field, defaultLabel = "外部データ") {
  if (typeof field === "string") {
    return { label: defaultLabel, text: field, weight: 15, url: "" };
  }
  if (!field || typeof field !== "object") return null;
  const text = String(field.text || field.value || field.title || field.body || "");
  if (!normalizeText(text)) return null;
  return {
    label: String(field.label || field.source || defaultLabel),
    text,
    weight: Number.isFinite(Number(field.weight)) ? Number(field.weight) : 15,
    url: String(field.url || ""),
  };
}

function buildSearchFields(video) {
  const fields = [
    { label: "タイトル", text: video.title, weight: 100, key: "title" },
    { label: "概要欄", text: video.description, weight: 20 },
    ...(video.tags || []).map((text) => ({ label: "タグ", text, weight: 30 })),
    ...(video.categories || []).map((text) => ({ label: "カテゴリ", text, weight: 25 })),
    ...(video.additionalSearchFields || []).map((field) => normalizeSearchField(field, "API/外部データ")),
    ...(video.comments || []).map((comment) => normalizeSearchField({
      label: comment.author ? `コメント (${comment.author})` : "コメント",
      text: comment.text,
      weight: 12,
    }, "コメント")),
  ].filter(Boolean);

  const seen = new Set();
  return fields.filter((field) => {
    const key = `${field.label}\n${normalizeText(field.text)}`;
    if (!normalizeText(field.text) || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function buildClientSearchText(video) {
  const parts = [
    video.title,
    video.description,
    ...(video.tags || []),
    ...(video.categories || []),
    ...(video.chapters || []).map((chapter) => chapter.title),
    ...(video.additionalSearchFields || []).map((field) => (
      typeof field === "string"
        ? field
        : field?.text || field?.value || field?.title || field?.body
    )),
    ...(video.comments || []).map((comment) => comment.text),
    ...(video.transcriptSegments || []).map((segment) => segment.text),
  ];
  return normalizeText(parts.filter(Boolean).join(" "));
}

function getSearchText(video) {
  if (typeof video.searchText === "string" && video.searchText) {
    return video.searchText;
  }
  return buildClientSearchText(video);
}

function scoreVideo(video, query) {
  const normalizedQuery = normalizeText(query);
  if (!normalizedQuery) return null;
  if (!getSearchText(video).includes(normalizedQuery)) return null;

  let score = 0;
  const matches = [];
  const fieldHits = [];
  const chapterMatches = [];
  let titleHit = false;

  for (const field of buildSearchFields(video)) {
    if (normalizeText(field.text).includes(normalizedQuery)) {
      score += field.weight;
      if (field.key === "title") {
        titleHit = true;
      } else if (fieldHits.length < MAX_FIELD_HITS) {
        fieldHits.push({
          label: field.label,
          text: makeSnippet(field.text, query),
          url: field.url,
        });
      }
    }
  }

  for (const chapter of video.chapters || []) {
    if (normalizeText(chapter.title).includes(normalizedQuery)) {
      score += 60;
      if (chapterMatches.length < MAX_SNIPPETS) {
        chapterMatches.push({
          start: chapter.start || 0,
          title: chapter.title,
        });
      }
    }
  }

  for (const segment of video.transcriptSegments || []) {
    if (normalizeText(segment.text).includes(normalizedQuery)) {
      score += 40;
      if (matches.length < MAX_SNIPPETS) {
        matches.push({
          start: segment.start || 0,
          text: makeSnippet(segment.text, query),
        });
      }
    }
  }

  if (score === 0) return null;

  return {
    video,
    score,
    titleHit,
    fieldHits,
    chapterMatches,
    matches,
  };
}

function renderEmpty(message, countText = "") {
  elements.results.innerHTML = message ? `<div class="empty">${escapeHtml(message)}</div>` : "";
  elements.resultCount.textContent = countText;
  if (elements.loadMore) elements.loadMore.hidden = true;
}

function renderResultCards(results, query) {
  return results.map(({ video, titleHit, fieldHits, chapterMatches, matches }) => `
    <article class="result-card">
      <a class="thumb" href="${escapeHtml(buildVideoUrl(video))}" target="_blank" rel="noopener noreferrer" aria-label="${escapeHtml(video.title)}をYouTubeで開く">
        <img src="${escapeHtml(safeUrl(video.thumbnail || "", ""))}" alt="" loading="lazy">
      </a>
      <div>
        <h3 class="result-title">
          <a href="${escapeHtml(buildVideoUrl(video))}" target="_blank" rel="noopener noreferrer">
            ${titleHit ? highlight(video.title, query) : escapeHtml(video.title)}
          </a>
        </h3>
        <p class="meta">${buildMeta(video)}</p>
        ${fieldHits.map((hit) => `
          <p class="field-hit">
            <span>${escapeHtml(hit.label)}</span>:
            ${hit.url
              ? `<a href="${escapeHtml(safeUrl(hit.url))}" target="_blank" rel="noopener noreferrer">${highlight(hit.text, query)}</a>`
              : highlight(hit.text, query)}
          </p>
        `).join("")}
        ${chapterMatches.length ? `
          <div class="match-list">
            ${chapterMatches.map((match) => `
              <a class="match-link chapter-link" href="${escapeHtml(buildVideoUrl(video, match.start))}" target="_blank" rel="noopener noreferrer">
                <strong>チャプター ${formatTime(match.start)}</strong> ${highlight(match.title, query)}
              </a>
            `).join("")}
          </div>
        ` : ""}
        ${matches.length ? `
          <div class="match-list">
            ${matches.map((match) => `
              <a class="match-link" href="${escapeHtml(buildVideoUrl(video, match.start))}" target="_blank" rel="noopener noreferrer">
                <strong>${formatTime(match.start)}</strong> ${highlight(match.text, query)}
              </a>
            `).join("")}
          </div>
        ` : ""}
      </div>
    </article>
  `).join("");
}

function updateLoadMoreButton() {
  if (!elements.loadMore) return;
  const remaining = Math.max(0, state.results.length - state.visibleResults);
  elements.loadMore.hidden = remaining === 0;
  elements.loadMore.textContent = remaining > 0
    ? `さらに表示（残り${remaining.toLocaleString("ja-JP")}件）`
    : "さらに表示";
}

function renderVisibleResults(query) {
  const visible = state.results.slice(0, state.visibleResults);
  elements.results.innerHTML = renderResultCards(visible, query);
  elements.resultCount.textContent = `${visible.length}/${state.results.length}件`;
  elements.status.textContent = `${visible.length}/${state.results.length}件`;
  updateLoadMoreButton();
}

function renderResults(query, visibleResults = RESULTS_PAGE_SIZE) {
  const normalizedQuery = normalizeText(query);
  if (!normalizedQuery) {
    state.results = [];
    state.visibleResults = RESULTS_PAGE_SIZE;
    renderEmpty();
    elements.status.textContent = "";
    return;
  }

  state.results = state.videos
    .map((video) => scoreVideo(video, query))
    .filter(Boolean)
    .sort((a, b) => b.score - a.score || dateRank(b.video.publishedAt) - dateRank(a.video.publishedAt));
  state.visibleResults = Math.min(visibleResults, state.results.length);

  elements.resultCount.textContent = `${state.results.length}件`;
  elements.status.textContent = `${state.results.length}件`;

  if (!state.results.length) {
    renderEmpty("見つかりませんでした。", "0件");
    return;
  }

  renderVisibleResults(query);
}

async function loadIndex() {
  if (window.SEARCH_INDEX) {
    state.index = window.SEARCH_INDEX;
    state.videos = Array.isArray(state.index.videos) ? state.index.videos : [];
    renderLatestLink();
    renderResults(elements.query.value);
    return;
  }

  try {
    const response = await fetch(DATA_URL, { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.index = await response.json();
    state.videos = Array.isArray(state.index.videos) ? state.index.videos : [];
    renderLatestLink();
    renderResults(elements.query.value);
  } catch (error) {
    elements.status.textContent = "検索データを読み込めませんでした。";
    renderEmpty("検索データを読み込めませんでした。");
    console.error(error);
  }
}

function submitSearch() {
  writeQueryParam(elements.query.value);
  renderResults(elements.query.value);
}

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  submitSearch();
});

if (elements.loadMore) {
  elements.loadMore.addEventListener("click", () => {
    state.visibleResults = Math.min(state.visibleResults + RESULTS_PAGE_SIZE, state.results.length);
    renderVisibleResults(elements.query.value);
  });
}

elements.query.value = readQueryParam();
loadIndex();
