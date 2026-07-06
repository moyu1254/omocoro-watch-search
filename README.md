# オモウォのあの回

`オモウォのあの回` は、YouTube チャンネル「ニュース! オモコロウォッチ」の動画を、タイトル・概要欄・字幕・タグ・コメントなどから検索できる静的サイトです。

- 公開サイト: https://omowatch.com/
- 参考: https://tokura.app/

## 特徴

- 動画内のキーワード検索に対応
- タイトル、概要欄、字幕、タグ、チャプター、コメント（関連度順の上位20件）を横断検索

## 更新

- 動画情報とコメントは YouTube Data API から取得
- 字幕本文のみ yt-dlp で取得
- 取得済み字幕は既存の検索インデックスから保持
- `Update search index` が検索データを生成し、差分があれば commit/push して GitHub Pages にデプロイ
- `build_index.py` が `index.html` の最新反映回を更新し、`search-index.json` の最新動画と不一致なら失敗

### 手動実行モード

- `fresh`: 最新動画・直近動画の更新、手動字幕反映後の再生成に使う
- `recent`: 通常の軽量補完に使う
- `full`: 全体再検証が必要なときだけ使う

通常の `outputs/omocoro-watch-search/**` への push は、`Deploy search site` でもデプロイできます。

## 手動字幕補完

GitHub Actions runner で YouTube の bot 判定により字幕取得が失敗する場合だけ、ローカルで字幕を取得して `manual_transcripts/<videoId>.json` として反映します。

```bat
cd /d C:\omocoro-watch-search
git pull
python -m pip install -U yt-dlp
python work\scripts\export_transcript.py --video-id VIDEO_ID --output manual_transcripts\VIDEO_ID.json
git add manual_transcripts\VIDEO_ID.json
git commit -m "Add manual transcript for VIDEO_ID"
git push
```

- `VIDEO_ID` に `<>` は付けない
- YouTube URL の `v=` の後ろが videoId
- `nothing to commit` が出た場合は反映済み
- push 後、`Update search index` を `fresh` で手動実行
- Actions ログで `transcript manual fallback: VIDEO_ID` と `manualTranscriptUsedVideos=1` を確認

## データの扱い

- `manual_transcripts/*.json`: 入力データ
- `outputs/omocoro-watch-search/data/search-index.json`: 生成物
- `outputs/omocoro-watch-search/data/search-index.js`: 生成物
- `outputs/omocoro-watch-search/index.html` の最新反映回: 生成処理で更新
- 生成物は手動編集しない

## 注意

- これは非公式サイトです
- 字幕は公式字幕または自動字幕に基づくため、表記ゆれが出ることがあります
