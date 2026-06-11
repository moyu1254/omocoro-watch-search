# オモウォのあの回

`オモウォのあの回` は、YouTube チャンネル「ニュース! オモコロウォッチ」の動画を、タイトル・概要欄・字幕・タグ・コメントなどから検索できる静的サイトです。

- 公開サイト: https://moyu1254.github.io/omocoro-watch-search/
- 参考: https://tokura.app/

## 特徴

- 動画内のキーワード検索に対応
- `オモウォ` でも `オモコロウォッチ` でも見つけやすい命名と検索導線
- タイトル、概要欄、字幕、タグ、チャプター、コメントを横断検索
- 検索データは静的 JSON として生成
- そのまま GitHub Pages で公開可能

## 更新方法

更新スクリプトは `work/scripts/build_index.py` です。必要に応じて `work/scripts/update_search_index.ps1` から実行できます。

- 全動画を取り込む: `--all`
- 字幕は取得できるものを優先して収集
- 字幕がない動画も、タイトルや概要欄だけで検索できるように保持
- YouTube Data API を使う場合は `YOUTUBE_API_KEY` を指定

## ファイル構成

- `outputs/omocoro-watch-search/index.html`: 検索トップ
- `outputs/omocoro-watch-search/videos.html`: 収録回一覧
- `outputs/omocoro-watch-search/app.js`: ブラウザ内検索ロジック
- `outputs/omocoro-watch-search/data/search-index.json`: 検索データ本体
- `work/scripts/build_index.py`: 検索データ生成スクリプト

## 注意

- これは非公式サイトです
- 字幕は公式字幕または自動字幕に基づくため、表記ゆれが出ることがあります
- 検索対象は動画本文だけでなく、補助的な外部データも追加できます

