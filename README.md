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
- 最新動画の字幕は一時的に未取得になる場合があります

## 手動字幕バックフィル

GitHub Actions で bot 判定された動画だけ、ローカルで字幕を取得します。

```bash
python work/scripts/export_transcript.py --video-id ayJ4SzJV0lc --output manual_transcripts/ayJ4SzJV0lc.json
```

生成した `manual_transcripts/<videoId>.json` を commit/push し、`Update search index` を `fresh` で手動実行します。

## 注意

- これは非公式サイトです
- 字幕は公式字幕または自動字幕に基づくため、表記ゆれが出ることがあります
