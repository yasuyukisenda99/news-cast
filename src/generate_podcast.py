#!/usr/bin/env python3
"""news-cast: 業界ニュースを毎朝2人対話のポッドキャストに変換するパイプライン

流れ:
  1. Google News RSS / 任意のRSSから直近の記事を収集
  2. Gemini APIで2人ホストの対話台本（+タイトル・概要）を生成
  3. Geminiマルチスピーカー TTS で音声化（PCM → MP3）
  4. docs/ 配下にMP3とポッドキャストRSSフィードを出力（GitHub Pagesで配信）
"""

import argparse
import base64
import datetime as dt
import email.utils
import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

import feedparser
import requests
import yaml

import cloud_tts

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "docs"
EPISODES_DIR = DOCS_DIR / "episodes"
META_PATH = DOCS_DIR / "episodes.json"

JST = dt.timezone(dt.timedelta(hours=9))
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# 再試行しても回復が見込めるHTTPステータス（サーバー側の一時的な不調）
RETRYABLE_STATUS = {500, 502, 503, 504}
MAX_ATTEMPTS = 5


class GeminiError(RuntimeError):
    """Gemini APIがエラーを返したときの例外（HTTPステータスを保持する）"""

    def __init__(self, model: str, status: int, detail: str):
        self.model = model
        self.status = status
        super().__init__(f"{model}: HTTP {status} {detail}")


class QuotaExceeded(RuntimeError):
    """APIの利用枠を使い切ったときの例外"""


class InvalidResponse(RuntimeError):
    """通信は成功したが、応答の中身が期待した形でないときの例外（再試行対象）"""


# ----------------------------------------------------------------------------
# 共通ユーティリティ
# ----------------------------------------------------------------------------

def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def log(msg: str) -> None:
    print(f"[news-cast] {msg}", flush=True)


# ----------------------------------------------------------------------------
# 1. ニュース収集
# ----------------------------------------------------------------------------

def _entry_datetime(entry) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)
    return None


def collect_news(cfg: dict) -> list[dict]:
    news_cfg = cfg["news"]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=news_cfg["hours_back"])

    feed_urls: list[str] = []
    locale = news_cfg.get("google_news_locale", "en")
    params = (
        "hl=ja&gl=JP&ceid=JP:ja" if locale == "ja"
        else "hl=en-US&gl=US&ceid=US:en"
    )
    for q in news_cfg.get("google_news_queries", []):
        feed_urls.append(
            f"https://news.google.com/rss/search?q={quote(q)}&{params}"
        )
    feed_urls.extend(news_cfg.get("rss_feeds", []) or [])

    articles: list[dict] = []
    seen_titles: set[str] = set()

    for url in feed_urls:
        try:
            feed = feedparser.parse(url)
        except Exception as e:  # ネットワーク不調時も他フィードは継続
            log(f"フィード取得に失敗: {url} ({e})")
            continue
        for entry in feed.entries:
            published = _entry_datetime(entry)
            if published and published < cutoff:
                continue
            title = strip_html(entry.get("title", ""))
            if not title:
                continue
            # Google Newsは「記事名 - 媒体名」形式。媒体名を分離する
            source = ""
            if " - " in title:
                title, source = title.rsplit(" - ", 1)
            source = strip_html(
                entry.get("source", {}).get("title", "") or source
            )
            norm = re.sub(r"\W+", "", title.lower())[:60]
            if not norm or norm in seen_titles:
                continue
            seen_titles.add(norm)
            articles.append({
                "title": title.strip(),
                "source": source.strip() or "不明",
                "link": entry.get("link", ""),
                "summary": strip_html(entry.get("summary", ""))[:220],
                "published": published or cutoff,
            })

    articles.sort(key=lambda a: a["published"], reverse=True)
    articles = articles[: news_cfg["max_articles"]]
    log(f"記事を{len(articles)}件収集しました")
    return articles


# ----------------------------------------------------------------------------
# 2. Gemini API 呼び出し（REST・モデルフォールバック付き）
# ----------------------------------------------------------------------------

def _gemini_call(model: str, body: dict, api_key: str, timeout: int = 600) -> dict:
    resp = requests.post(
        f"{GEMINI_BASE}/{model}:generateContent",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise GeminiError(model, resp.status_code, resp.text[:300])
    return resp.json()


def gemini_with_fallback(
    models: list[str], body: dict, api_key: str, validate=None
) -> dict:
    """モデルを順に試す。

    503などの一時的な混雑のときだけ待って再試行し、
    429（利用枠の超過）や404（モデル不在）のように待っても直らないものは
    即座に打ち切る。無駄な再試行で利用枠を消費しないための設計。
    """
    last_err: Exception | None = None
    for model in models:
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = _gemini_call(model, body, api_key)
                if validate is not None:
                    validate(response)
                return response
            except InvalidResponse as e:
                last_err = e
                log(f"モデル {model}: 応答が不正（試行{attempt + 1}）: {e}")
                if attempt == MAX_ATTEMPTS - 1:
                    break
                time.sleep(min(10 * 2 ** attempt, 60))
            except GeminiError as e:
                last_err = e
                if e.status == 429:
                    log(f"モデル {model}: 利用枠の上限に達しました。再試行しません")
                    break
                if e.status not in RETRYABLE_STATUS:
                    log(f"モデル {model}: 回復不能なエラー（HTTP {e.status}）。次のモデルへ")
                    log(f"  詳細: {e}")
                    break
                if attempt == MAX_ATTEMPTS - 1:
                    log(f"モデル {model}: {MAX_ATTEMPTS}回試しましたが混雑が続いています。次のモデルへ")
                    break
                wait = min(10 * 2 ** attempt, 60)
                log(f"モデル {model} が混雑中（試行{attempt + 1}）。{wait}秒待って再試行します")
                time.sleep(wait)
            except Exception as e:  # ネットワーク断など
                last_err = e
                if attempt == MAX_ATTEMPTS - 1:
                    break
                log(f"モデル {model} で通信エラー（試行{attempt + 1}）: {e}")
                time.sleep(min(10 * 2 ** attempt, 60))

    if isinstance(last_err, GeminiError) and last_err.status == 429:
        raise QuotaExceeded(
            "APIの利用枠を使い切りました。日付が変わればリセットされます。"
            "残量は https://ai.dev/rate-limit で確認できます"
        )
    raise RuntimeError(f"全モデルで生成に失敗しました: {last_err}")


def _extract_text(response: dict) -> str:
    candidates = response.get("candidates") or []
    if not candidates:
        block = response.get("promptFeedback", {}).get("blockReason", "不明")
        raise InvalidResponse(f"候補が空です（blockReason={block}）")
    cand = candidates[0]
    content = cand.get("content")
    if not content or not content.get("parts"):
        reason = cand.get("finishReason", "不明")
        raise InvalidResponse(f"本文が含まれていません（finishReason={reason}）")
    text = "".join(p.get("text", "") for p in content["parts"])
    if not text.strip():
        raise InvalidResponse("本文が空でした")
    return text


# ----------------------------------------------------------------------------
# 3. 対話台本の生成
# ----------------------------------------------------------------------------

def build_script(cfg: dict, articles: list[dict], api_key: str) -> dict:
    pod = cfg["podcast"]
    hosts = cfg["gemini"]["hosts"]
    a, b = hosts[0], hosts[1]
    today = dt.datetime.now(JST).strftime("%Y年%m月%d日（%a）")
    target_chars = pod["episode_minutes"] * 330

    lines = []
    for i, art in enumerate(articles, 1):
        lines.append(
            f"{i}. {art['title']}（出典: {art['source']}）\n   概要: {art['summary']}"
        )
    articles_block = "\n".join(lines)

    prompt = f"""あなたはニュースポッドキャストの放送作家です。
以下の本日のニュース一覧をもとに、2人のホストによる日本語の対話台本を作成してください。

# 番組情報
- 番組名: {pod['title']}
- 放送日: {today}
- 目安の長さ: 約{pod['episode_minutes']}分（およそ{target_chars}文字）

# ホスト
- {a['speaker']}（{a['display']}）: {a['persona']}
- {b['speaker']}（{b['display']}）: {b['persona']}

# 本日のニュース一覧
{articles_block}

# 構成の指示
1. オープニング: 日付と番組名、今日の見どころを一言
2. メイントピック: 特に重要そうな2〜3件を選び、背景や意味合いを掛け合いで深掘り
3. クイックヘッドライン: 残りの記事をテンポよく紹介
4. クロージング: 短いまとめと締めの挨拶

# 厳守事項
- ニュースは必ず自分の言葉で要約・言い換えること。記事の文章をそのまま読み上げない
- 各ニュースで出典メディア名に軽く触れる
- 会話は自然に。相槌・質問・軽い感想を交えるが、事実の捏造はしない
- 台本の各行は必ず「{a['speaker']}: 」または「{b['speaker']}: 」で始める（それ以外の記号・ト書き・見出しは入れない）

# 出力形式
次のJSONオブジェクトのみを出力してください:
{{"title": "エピソードタイトル（{today}を含む簡潔なもの）", "description": "エピソード概要（150文字以内）", "script": "{a['speaker']}: ...\\n{b['speaker']}: ...（台本全体）"}}"""

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    response = gemini_with_fallback(
        cfg["gemini"]["script_models"], body, api_key, validate=_extract_text
    )
    text = _extract_text(response)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise RuntimeError(f"台本JSONの解析に失敗しました: {text[:200]}")
        data = json.loads(match.group(0))

    for key in ("title", "description", "script"):
        if not data.get(key):
            raise RuntimeError(f"台本JSONに {key} がありません")
    log(f"台本を生成しました（{len(data['script'])}文字）: {data['title']}")
    return data


# ----------------------------------------------------------------------------
# 4. マルチスピーカーTTS（分割合成 → PCM結合 → MP3）
# ----------------------------------------------------------------------------

def _split_script(script: str, chunk_chars: int) -> list[str]:
    lines = [ln.strip() for ln in script.splitlines() if ln.strip()]
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) > chunk_chars:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def _extract_audio(response: dict) -> str:
    """音声応答からbase64データを取り出す。取り出せなければ再試行対象の例外"""
    candidates = response.get("candidates") or []
    if not candidates:
        block = response.get("promptFeedback", {}).get("blockReason", "不明")
        raise InvalidResponse(f"候補が空です（blockReason={block}）")

    cand = candidates[0]
    reason = cand.get("finishReason", "")
    content = cand.get("content")
    if not content or not content.get("parts"):
        raise InvalidResponse(f"音声が含まれていません（finishReason={reason or '不明'}）")

    for part in content["parts"]:
        data = part.get("inlineData", {}).get("data")
        if data:
            return data
    raise InvalidResponse(f"inlineDataが見つかりません（finishReason={reason or '不明'}）")


def synthesize(cfg: dict, script: str, api_key: str) -> bytes:
    """設定に応じてCloud TTS / Gemini TTSを切り替える"""
    provider = cfg["gemini"].get("tts_provider", "cloud")
    if provider == "cloud":
        raw = os.environ.get("GCP_SA_KEY", "")
        if not raw:
            raise RuntimeError(
                "環境変数 GCP_SA_KEY が設定されていません"
                "（GitHubのSecretsにサービスアカウントのJSONを登録してください）"
            )
        try:
            sa_info = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"GCP_SA_KEY のJSONを読めませんでした: {e}")
        log("Google Cloud Text-to-Speech で合成します")
        return cloud_tts.synthesize_script(cfg, script, sa_info, log=log)
    log("Gemini TTS で合成します")
    return _synthesize_gemini(cfg, script, api_key)


def _synthesize_gemini(cfg: dict, script: str, api_key: str) -> bytes:
    gem = cfg["gemini"]
    hosts = gem["hosts"]
    speaker_configs = [
        {
            "speaker": h["speaker"],
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": h["voice"]}},
        }
        for h in hosts
    ]
    # チャンクをまたいでも同じ演技になるよう、収録状況ごと固定して指示する
    directions = "／".join(
        f"{h['speaker']}は{h.get('voice_direction', h['persona'])}"
        for h in hosts
    )
    style = (
        "以下は毎朝配信されるニュースPodcastの台本の一部です。"
        "番組は全編を通じて同じ2人が、同じスタジオ・同じマイクで収録しています。"
        "声の高さ、話す速度、声量、マイクとの距離感を最初から最後まで完全に一定に保ってください。"
        "感情を誇張せず、抑制の効いた落ち着いた一定のトーンで、"
        "ニュース番組のアナウンサーのように読み上げてください。\n"
        f"話者の演出指示: {directions}\n\n"
    )

    chunks = _split_script(script, gem["tts_chunk_chars"])
    log(f"TTSを{len(chunks)}チャンクで合成します")

    silence = b"\x00" * int(24000 * 2 * 0.4)  # チャンク間に0.4秒の間
    pcm = b""
    for i, chunk in enumerate(chunks, 1):
        body = {
            "contents": [{"parts": [{"text": style + chunk}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "temperature": gem.get("tts_temperature", 0.2),
                "speechConfig": {
                    "multiSpeakerVoiceConfig": {
                        "speakerVoiceConfigs": speaker_configs
                    }
                },
            },
        }
        response = gemini_with_fallback(
            gem["tts_models"], body, api_key, validate=_extract_audio
        )
        data = _extract_audio(response)
        if pcm:
            pcm += silence
        pcm += base64.b64decode(data)
        log(f"  チャンク{i}/{len(chunks)} 完了")
    return pcm


def pcm_to_mp3(pcm: bytes, out_path: Path) -> None:
    """Gemini TTSの出力（s16le / 24kHz / mono）を音量を均一化しつつMP3に変換"""
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1",
            "-i", "pipe:0", "-af", "dynaudnorm=f=150:g=9:m=30:p=0.9",
            "-codec:a", "libmp3lame", "-b:a", "96k",
            str(out_path),
        ],
        input=pcm,
        check=True,
        capture_output=True,
    )


def mp3_duration_seconds(path: Path) -> int:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        return int(float(out))
    except Exception:
        return 0


# ----------------------------------------------------------------------------
# 5. エピソード管理・RSSフィード生成
# ----------------------------------------------------------------------------

def load_meta() -> list[dict]:
    if META_PATH.exists():
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    return []


def save_meta(meta: list[dict]) -> None:
    META_PATH.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def cleanup_old_episodes(meta: list[dict], keep: int) -> list[dict]:
    meta.sort(key=lambda m: m["date"], reverse=True)
    for old in meta[keep:]:
        path = EPISODES_DIR / old["file"]
        if path.exists():
            path.unlink()
            log(f"古いエピソードを削除: {old['file']}")
    return meta[:keep]


def _fmt_duration(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def build_feed(cfg: dict, meta: list[dict]) -> None:
    pod = cfg["podcast"]
    base = pod["base_url"].rstrip("/")
    now_rfc = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc))

    items = []
    for ep in meta:
        pub = email.utils.format_datetime(
            dt.datetime.fromisoformat(ep["pub_date"])
        )
        url = f"{base}/episodes/{ep['file']}"
        items.append(f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description>{escape(ep['description'])}</description>
      <enclosure url="{escape(url)}" length="{ep['size']}" type="audio/mpeg"/>
      <guid isPermaLink="false">{escape(ep['file'])}</guid>
      <pubDate>{pub}</pubDate>
      <itunes:duration>{_fmt_duration(ep.get('duration', 0))}</itunes:duration>
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(pod['title'])}</title>
    <description>{escape(pod['description'])}</description>
    <link>{escape(base)}/</link>
    <language>{escape(pod['language'])}</language>
    <lastBuildDate>{now_rfc}</lastBuildDate>
    <itunes:author>{escape(pod['author'])}</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <atom:link href="{escape(base)}/feed.xml" rel="self" type="application/rss+xml"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (DOCS_DIR / "feed.xml").write_text(feed, encoding="utf-8")

    rows = "\n".join(
        f'      <li><strong>{escape(ep["title"])}</strong><br>'
        f'<audio controls src="episodes/{escape(ep["file"])}"></audio></li>'
        for ep in meta
    )
    index = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(pod['title'])}</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 2rem auto; padding: 0 1rem;">
  <h1>{escape(pod['title'])}</h1>
  <p>{escape(pod['description'])}</p>
  <p>ポッドキャストアプリ購読用フィード: <a href="feed.xml">feed.xml</a></p>
  <ul style="list-style: none; padding: 0; display: grid; gap: 1rem;">
{rows}
  </ul>
</body></html>
"""
    (DOCS_DIR / "index.html").write_text(index, encoding="utf-8")
    log("feed.xml と index.html を更新しました")


# ----------------------------------------------------------------------------
# メイン
# ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="news-cast pipeline")
    parser.add_argument(
        "--skip-tts", action="store_true",
        help="TTSを実行せず台本生成までを確認する（動作テスト用）",
    )
    args = parser.parse_args()

    cfg = load_config()
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log("環境変数 GEMINI_API_KEY が設定されていません")
        return 1
    if "YOUR_GITHUB_ID" in cfg["podcast"]["base_url"]:
        log("警告: config.yaml の base_url が未設定のままです（フィードURLが無効になります）")

    EPISODES_DIR.mkdir(parents=True, exist_ok=True)

    try:
        return _run(cfg, api_key, args)
    except QuotaExceeded as e:
        log(f"中止: {e}")
        log("本日はここで終了します。明日の自動実行をお待ちください")
        return 1


def _run(cfg: dict, api_key: str, args) -> int:
    articles = collect_news(cfg)
    if not articles:
        log("対象期間内の記事が見つかりませんでした。本日はスキップします")
        return 0

    episode = build_script(cfg, articles, api_key)
    if args.skip_tts:
        print("----- 台本プレビュー -----")
        print(episode["script"][:2000])
        return 0

    today = dt.datetime.now(JST)
    filename = f"{today.strftime('%Y-%m-%d')}.mp3"
    out_path = EPISODES_DIR / filename

    pcm = synthesize(cfg, episode["script"], api_key)
    pcm_to_mp3(pcm, out_path)
    log(f"MP3を書き出しました: {out_path} ({out_path.stat().st_size // 1024} KB)")

    meta = [m for m in load_meta() if m["file"] != filename]
    meta.append({
        "date": today.strftime("%Y-%m-%d"),
        "file": filename,
        "title": episode["title"],
        "description": episode["description"],
        "size": out_path.stat().st_size,
        "duration": mp3_duration_seconds(out_path),
        "pub_date": today.isoformat(),
    })
    meta = cleanup_old_episodes(meta, cfg["podcast"]["keep_episodes"])
    save_meta(meta)
    build_feed(cfg, meta)

    log("完了しました 🎙️")
    return 0


if __name__ == "__main__":
    sys.exit(main())
