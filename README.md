# 🎙️ news-cast

毎朝、指定した業界キーワードのニュースを自動収集し、AIホスト2人の掛け合いによる**NotebookLM風ポッドキャスト**を生成・配信するパイプラインです。

- 収集: Google News RSS検索 + 任意のRSSフィード
- 台本: Gemini API（2人ホストの対話形式・日本語）
- 音声: Geminiマルチスピーカー TTS → MP3
- 配信: GitHub Pages 上のポッドキャストRSSフィード（お手持ちのポッドキャストアプリで購読可能）
- 実行: GitHub Actions（毎朝7:00 JST・完全自動）

## アーキテクチャ

```
GitHub Actions (毎朝7:00 JST)
   │
   ├─ 1. collect_news()   Google News RSS / 任意RSSから直近24hの記事を収集・重複排除
   ├─ 2. build_script()   Geminiで2人ホストの対話台本 + タイトル/概要をJSON生成
   ├─ 3. synthesize()     マルチスピーカーTTSで音声合成（長い台本は分割→PCM結合）
   ├─ 4. pcm_to_mp3()     ffmpegでMP3化（96kbps）
   └─ 5. build_feed()     docs/feed.xml（RSS）と docs/index.html を更新 → commit & push
                              ↓
                        GitHub Pages で配信
                              ↓
                  ポッドキャストアプリが自動で新エピソードを取得
```

## セットアップ（約10分）

### 1. Gemini APIキーを取得
[Google AI Studio](https://aistudio.google.com/) で APIキーを発行します（無料枠あり）。

### 2. GitHubリポジトリを作成してこの一式をpush
GitHub Pages（無料プラン）は**公開リポジトリ**が前提です。MP3と台本由来のフィードが公開される点に注意してください。非公開にしたい場合は後述のS3構成へ。

```bash
git init && git add -A && git commit -m "init"
git branch -M main
git remote add origin https://github.com/<あなたのID>/news-cast.git
git push -u origin main
```

### 3. シークレットを登録
リポジトリの **Settings → Secrets and variables → Actions** で
`GEMINI_API_KEY` という名前でAPIキーを登録します。

### 4. GitHub Pages を有効化
**Settings → Pages** で Source を `Deploy from a branch`、
Branch を `main` / フォルダを `/docs` に設定します。

### 5. config.yaml を編集
- `podcast.base_url` を `https://<あなたのID>.github.io/news-cast` に変更
- `news.google_news_queries` を集めたいキーワードに変更
- 好みで番組名・ホストの声（`gemini.hosts[].voice`）・長さを調整

### 6. 動作確認
**Actions タブ → daily-podcast → Run workflow** で手動実行。
数分後に `docs/episodes/` にMP3が生成され、`https://<あなたのID>.github.io/news-cast/` で確認できます。

## ポッドキャストアプリで購読する

フィードURLは `https://<あなたのID>.github.io/news-cast/feed.xml` です。

- **Apple Podcasts**: ライブラリ → 「…」→「URLで番組をフォロー」
- **Overcast / Pocket Casts**: 「Add URL」からフィードURLを追加

以後、毎朝の新エピソードが自動でアプリに届きます。

## ローカルでのテスト

```bash
pip install -r requirements.txt
export GEMINI_API_KEY="..."
python src/generate_podcast.py --skip-tts   # 台本生成までを確認（音声化しない）
python src/generate_podcast.py              # フル実行（要ffmpeg）
```

## カスタマイズのヒント

- **声の変更**: `gemini.hosts[].voice` にGeminiのプリビルド音声名（Kore, Puck, Zephyr, Charon, Leda, Orus, Aoede など）を指定。ペルソナ文を変えると話し口調も変わります
- **長さ**: `podcast.episode_minutes` を変更（台本の目安文字数に反映）
- **情報源の追加**: 特定メディアのRSSを `news.rss_feeds` に追加（各メディアのRSS配信ページでURLを確認）
- **モデル**: `gemini.script_models` / `gemini.tts_models` は上から順に試すフォールバック方式。新モデルが出たら先頭に追加するだけでOK

## 制約・注意点

- Gemini TTSはプレビュー段階のため、モデル名や仕様が変わる可能性があります（フォールバックである程度吸収します）。無料枠のレート制限は[公式ドキュメント](https://ai.google.dev/gemini-api/docs/rate-limits)を確認してください
- GitHub Actionsのcronは混雑時に遅延することがあります（±30分程度は許容を）
- 生成台本は「記事を自分の言葉で要約する」よう指示していますが、配信を広く公開する場合は各メディアの利用条件にもご配慮ください
- MP3はリポジトリに蓄積されるため、`podcast.keep_episodes` で保持数を制限しています（既定14件）

## 発展: S3 + CloudFront 構成へ

長期運用や非公開配信には、`docs/` への書き出し後に `aws s3 sync docs/ s3://<bucket>/ --delete` を
ワークフローに1ステップ足すだけで移行できます（`base_url` をCloudFrontのURLに変更）。
署名付きURLやIP制限をかければ社内限定配信も可能です。
