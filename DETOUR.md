# 朝の寄り道（仮）—14日間の実験

既存の「朝のAI・業界ニュース便」と並行して、実在する人や発見を題材にした約7分のAIホスト2人の会話を配信します。情報の取り逃しや仕事への有用性を聴く動機にせず、会話自体と意外な発見を楽しめるか確かめます。

## 変更の範囲

基準: `yasuyukisenda99/news-cast`、コミット `29aa14d2f077710bc5289c700c96690197ae2ffe`。
ブランチ: `experiment/morning-detour`。既存 `config.yaml`、ニュース収集・台本生成、`daily-podcast.yml`、既存公開データは変更していません。

| 機能 | 実装 |
| --- | --- |
| 番組設定 | `config/detour.yaml`。モデル名と既存TTSの補助設定を `config.yaml` から継承 |
| 題材収集 | `config/detour-topics.json` の16候補から、未使用かつ直近と異なるカテゴリを優先。URL本文を毎回取得し、題材の入口を資料で確認 |
| 台本・検証 | `src/generate_detour.py`。1800〜2600字、対等な2人の掛け合い。資料中の根拠引用との一致確認と、別の生成呼び出しで全体の事実・会話・FOMO・架空体験を審査 |
| 音声 | 既存 `synthesize` / `cloud_tts.py` / `pcm_to_mp3` を再利用。2人の声を固定。実測5分30秒〜8分30秒以外は公開しない |
| RSS | 既存 `build_feed` に任意の出力先を追加。既存呼び出し時の出力は同一 |
| ページ・評価 | `src/detour_assets/` と `docs/detour/`。音声・台本・出典と端末内保存の軽い評価 |
| 自動実行 | `morning-detour.yml`。JST 07:30、開始日から14暦日だけ生成。ニュース便と同じconcurrencyで書き込みを直列化 |
| 自動テスト | `tests/test_detour.py`、`test-detour.yml` |

既存コードの変更は、RSS/ページの出力先を引数で指定する追加と、Gemini TTSの読み方を新番組だけ上書きできる追加です。Cloud TTSの処理本体は変更していません。

新番組の全公開物は以下に分離します。

- ページ: `https://yasuyukisenda99.github.io/news-cast/detour/`
- RSS: `https://yasuyukisenda99.github.io/news-cast/detour/feed.xml`
- メタデータ: `docs/detour/episodes.json`
- 音声: `docs/detour/episodes/detour-YYYY-MM-DD.mp3`（GUIDも番組固有）
- 公開台本と資料URL: `docs/detour/episodes/YYYY-MM-DD.json`

これらは適用・mainへのマージ・Pages反映後のURLです。今回の作業では公開していません。ニュース便のトップページにはリンクを追加していないので、新番組へは上記URLから直接アクセスしてください。

## パッチの適用

手元の変更を退避・コミットしたクリーンなチェックアウトで行います。パッチを任意の場所に置き、以下の `/path/to/morning-detour.patch` をその絶対パスに置き換えてください。

```sh
git clone https://github.com/yasuyukisenda99/news-cast.git
cd news-cast
git switch -c experiment/morning-detour
git apply --check /path/to/morning-detour.patch
git apply /path/to/morning-detour.patch
```

`--check` で競合が出たら適用を止め、既存の更新内容と照合してください。ニュース便は毎日自動コミットされるので、mainのコミットが先に進んでいても不自然ではありません。

## 設定と検証

Python 3.12（既存コードも3.12を使用）、ffmpeg / ffprobe、既存requirementsが必要です。まず、秘密情報の不要なテストを実行します。

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
node --check src/detour_assets/feedback.js
python src/generate_detour.py --check-sources
```

回帰テストは上記の基準コミットのコードと比較します。履歴が浅く基準コミットがない場合は `git fetch origin 29aa14d2f077710bc5289c700c96690197ae2ffe` で取得してください。CIでは履歴取得を設定済みです。

必要なSecrets（リポジトリの Settings → Secrets and variables → Actions → Secrets）:

- `GEMINI_API_KEY`: 既存と同じ台本生成用キー。
- `GCP_SA_KEY`: 既存と同じCloud TTSサービスアカウントJSON。Text-to-Speech APIの利用権限が必要。

キーやJSON本文をYAML、Git、チャットへ貼り付けないでください。ローカルでの試聴は、環境変数を安全な方法で設定したうえで行ってください。`.env`を自動で読み込む機能はありません。

新番組のモデル名は既存設定から継承します。今回は実APIを呼んでいないため、継承したモデルがアカウントで利用可能かは初回に確認してください。新番組だけ変更する場合、`config/detour.yaml` の `gemini.script_models` / `gemini.tts_models` を指定できます。

### 公開前の試聴

Gitに送らずローカルの実験ブランチで、JSTの今日を開始日に指定します。

```sh
python src/generate_detour.py --start-date YYYY-MM-DD --skip-tts
python src/generate_detour.py --start-date YYYY-MM-DD
```

`YYYY-MM-DD` は実際の当日の日付に置き換えます。プレビューは `work/detour-preview/` に保存され、RSSや公開フォルダーは更新しません。通常実行はローカルの `docs/detour/` に実音声を作るため、確認用MP3・JSON・episodes.json・更新RSS等をうっかりpushしないよう、試聴は別のクリーンなコピーで行うのが簡単です。実験ブランチで作った音声も本番に含めたい場合、その開始日を本番の開始日として維持してください。

確認すること:

1. 相槌だけの応答、解説と質問の固定化、ニュース読みの口調が少ないか。
2. 「その人はなぜそうしたのか」を資料以上に創作していないか。出典と台本を照合。
3. 7分前後の会話として声・間・読み方を楽しめるか。2人の声は従来から再利用するため、Cloud TTSで望む自然さになるか実耳で判断。
4. 新RSSのenclosureが `/detour/episodes/` を指すか。既存RSS・既存音声が変更されていないか。

音声合成前の台本と編集チェックが不合格なら停止します。音声の尺が範囲外でも再生成は自動で行いません。不要な追加課金を抑えるためで、設定を見直し同日中に再実行できます。根拠引用の一致やAIの審査は誤りの完全な保証ではありません。

### 本番の14日間を開始

検証後、変更をコミットしてブランチをpushし、PRでmainへマージします。

```sh
git add .gitignore .github/workflows/morning-detour.yml .github/workflows/test-detour.yml config DETOUR.md src/generate_detour.py src/generate_podcast.py src/detour_assets tests docs/detour
git commit -m 'Add isolated 14-day morning detour experiment'
git push -u origin experiment/morning-detour
```

GitHub Pagesは既存通り `main /docs` を使用します。Settings → Secrets and variables → Actions → Variablesで、**Repository variable `DETOUR_START_DATE`** に開始日（JST、`YYYY-MM-DD`）を指定します。開始日は、最初の配信をしたい朝の日付です。最初は当日または翌朝を選びます。新ワークフロー `morning-detour` はmainでのみ配信し、既存ニュース便は通常通り動きます。

開始日が空なら生成しません。開始前・開始15日目以降は依存関係のインストールや生成を行いません。開始日を設定した当日ならActionsから手動実行できます。JST07:30は生成開始の目安で、配信完了時刻ではありません。Actionsの遅延やAPI障害があり得ます。

- 期間は開始日をDay 1とする**14暦日**です。失敗した日は翌日に繰り越さず、過去日の穴埋めもしません。
- 同じ日に成功済みなら再実行しても重複生成しません。最大14回。
- 途中停止は `DETOUR_START_DATE` を空にするか、新ワークフローだけ無効化。ニュース便は継続。
- 成功済みの実験の開始日を変更して再利用できません。再実験は公開先と状態ファイルを別に設計します。
- 配信済みの14回は実験後も残し、既存番組の保持処理からは削除されません。
- ブランチ保護でbotのmainへのpushが禁止されている場合は、自動コミットが失敗します。保護を緩める前に、生成物をPRへ送る運用などを別途選択してください。

## 題材の方針と限界

最初の16候補はNobel Prizeの公式略歴・本人の回想を中心に、自然観察、発見、文学、暮らし、人の選択から選びました。例はミツバチの伝達、クラゲの光、植樹運動、詩の翻訳などです。最新ニュースの取得期間フィルターは使いません。

実在する資料が取れることを優先した初期カタログなので、受賞者・科学の話に偏りがあります。一般の人、工芸、食、街の歴史などへ広げる場合は、公式資料や信頼できる取材記事を確認してからカタログと許可ドメインを追加してください。検索結果の見出しだけを根拠にする実装にはしていません。候補のhook自体も事実とは扱わず、本文で裏付けられない入口は採用しない指示です。

本文が十分に取得できる未使用候補を優先順で選ぶため、毎朝の新規ウェブ検索や流行スコアによる選定は行いません。取得失敗は別候補に進み、候補がなくなれば失敗して公開しません。初期16候補の本文取得は2026-10-25に確認しましたが、URLやページ構造の変更で取得できなくなる可能性があります。

資料の原文や根拠引用は公開しません。公開するのは生成台本・出典URL・取得時刻・本文ハッシュです。raw本文は一時的にメモリー上でモデルへ渡し、保存しません。生成・検証は各回2回のGemini呼び出しとTTSになるため、既存ニュース便とAPI利用枠・費用を共有します。共有枠の消費によるニュース便への影響を小さくするためニュース便より後に実行しますが、枠の完全な隔離が必要なら別のAPIプロジェクト・Secretsが必要です。

## 14日間の評価

各回ページで「気に入った」「また明日も聴きたい」を各1選択。翌日、思い出せる話を任意でひと言入力します。回答しない日も許容し、連続記録・未聴催促・FOMOを生む表示は設けません。

回答はlocalStorageに保存し、サーバー送信・外部解析をしません。ページ上に項目ごとの「はい/回答数」と思い出メモ件数を表示し、JSONを書き出せます。ブラウザーのデータ消去やプライベート閲覧では失われることがあります。Podcastアプリからはこの評価ページへ戻って回答します。複数人・複数端末の回答を自動集約する仕組みは含めていません。

実験前に自分なりの継続基準を決めると比較しやすくなります。例えば「回答した回のうち明日も聴きたいが7割程度」「翌日に思い出す話が数回ある」を仮の目安にし、どの会話や題材を覚えたかも見ます。これは統計的な合否判定ではありません。聴かなかった日・生成失敗日・回答しなかった日は別に捉え、欠損を『いいえ』にしないでください。翌日の自由記述には入力時刻を保存しますが、入力が本当に翌日かを強制・検証する仕組みはありません。

## 今回の検証記録

- Python 3.12、13件の自動テスト成功。
- 既存コードを基準コミットから直接読み込み、同じ条件のRSS・HTMLがバイト単位で一致することを確認。
- 既存Gemini TTSの送信引数・PCM出力が同一になることをモックで確認。
- 新番組の公開先・固有GUID・HTMLエスケープ、重複防止、開始前/Day 1/Day 14/Day 15、開始日の不正・変更、題材重複回避、資料取得失敗、台本・編集審査拒否、尺異常時の未公開、プレビュー分離を確認。
- 16候補の公式資料本文を実HTTPで取得できることを確認。全文は納品物に含めていません。
- JavaScript構文検査成功。実ブラウザーで回答入力・保存・集計・再読み込み後の復元を確認。
- この環境にGEMINI_API_KEY/GCP_SA_KEYがないため、実モデル・実TTS・ffmpegによるMP3生成・実RSS購読・GitHub Actions/Pagesへのデプロイは未実施。自然な会話と声の品質は公開前の試聴が必要。
- GitHubの読み取りは成功。push dry-runは認証がなく失敗。GitHubへの変更、PR作成、本番公開は行っていません。
