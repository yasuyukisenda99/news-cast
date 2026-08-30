"""Google Cloud Text-to-Speech による音声合成

Gemini TTSと違い1リクエストが小さく、話者ごとに声が完全固定されるため、
チャンク境界での声のブレや巨大レスポンスによる通信断が起きにくい。
"""

import base64
import io
import re
import time
import wave

import requests

TTS_ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class CloudTTSError(RuntimeError):
    pass


def get_access_token(sa_info: dict) -> str:
    """サービスアカウント情報からアクセストークンを取得する"""
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=[SCOPE]
    )
    creds.refresh(Request())
    return creds.token


def parse_turns(script: str, speakers: list[str]) -> list[tuple[str, str]]:
    """台本を (話者, セリフ) の並びに分解し、同じ話者が続く場合はまとめる"""
    pattern = re.compile(rf"^\s*({'|'.join(map(re.escape, speakers))})\s*[:：]\s*(.+)$")
    turns: list[tuple[str, str]] = []
    for line in script.splitlines():
        line = line.strip()
        if not line:
            continue
        m = pattern.match(line)
        if m:
            speaker, text = m.group(1), m.group(2).strip()
        elif turns:
            # 話者ラベルの無い行は直前の発言の続きとして扱う
            speaker, text = turns[-1][0], line
            turns[-1] = (speaker, f"{turns[-1][1]} {text}")
            continue
        else:
            continue
        if turns and turns[-1][0] == speaker:
            turns[-1] = (speaker, f"{turns[-1][1]} {text}")
        else:
            turns.append((speaker, text))
    return turns


def split_long_text(text: str, limit: int = 180) -> list[str]:
    """発言を文単位に分割する。

    Chirp 3: HD は1文が長すぎると400エラーになるため、
    まず句点で必ず区切り、それでも長い文は読点で、
    さらに長ければ強制的に文字数で割る。
    """
    # 句点・感嘆符・疑問符の直後で区切る（閉じ括弧が続く場合はそこまで含める）
    sentences = re.split(r"(?<=[。！？])(?![」』）】\)])", text)
    sentences = [s.strip() for s in sentences if s and s.strip()]

    pieces: list[str] = []
    for sentence in sentences:
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue
        # 読点で分割を試みる
        current = ""
        for part in re.split(r"(?<=[、，])", sentence):
            if current and len(current) + len(part) > limit:
                pieces.append(current)
                current = part
            else:
                current += part
        if current:
            pieces.append(current)

    # まだ長いものは文字数で強制分割
    result: list[str] = []
    for p in pieces:
        while len(p) > limit:
            result.append(p[:limit])
            p = p[limit:]
        if p:
            result.append(p)
    return result or [text]


def wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, int, int]:
    """WAVバイト列から生PCM・サンプルレート・チャンネル数を取り出す"""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise CloudTTSError(f"想定外のサンプル幅: {wf.getsampwidth()}バイト")
        return wf.readframes(wf.getnframes()), wf.getframerate(), wf.getnchannels()


def synthesize_one(
    text: str, voice_name: str, token: str, rate: int,
    speaking_rate: float, pitch: float, log=print, attempts: int = 4,
) -> bytes:
    """1つの発言を合成して生PCMで返す"""
    body = {
        "input": {"text": text},
        "voice": {"languageCode": "ja-JP", "name": voice_name},
        "audioConfig": {
            "audioEncoding": "LINEAR16",
            "sampleRateHertz": rate,
            "speakingRate": speaking_rate,
        },
    }
    # Chirp 3: HD はピッチ調整に非対応のため、指定があるときだけ渡す
    if pitch:
        body["audioConfig"]["pitch"] = pitch

    last = None
    for attempt in range(attempts):
        try:
            resp = requests.post(
                TTS_ENDPOINT,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json=body,
                timeout=120,
            )
            if resp.status_code == 200:
                audio = resp.json().get("audioContent")
                if not audio:
                    raise CloudTTSError("audioContentが空でした")
                pcm, got_rate, channels = wav_to_pcm(base64.b64decode(audio))
                if channels != 1:
                    raise CloudTTSError(f"想定外のチャンネル数: {channels}")
                return pcm, got_rate
            if resp.status_code in (429, 500, 503):
                last = CloudTTSError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                raise CloudTTSError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        except CloudTTSError:
            raise
        except Exception as e:
            last = e
        if attempt < attempts - 1:
            wait = 5 * 2 ** attempt
            log(f"    再試行します（{wait}秒待機）: {last}")
            time.sleep(wait)
    raise CloudTTSError(f"合成に失敗しました: {last}")


def synthesize_script(cfg: dict, script: str, sa_info: dict, log=print) -> bytes:
    """台本全体を合成して結合したPCMを返す"""
    tts_cfg = cfg["gemini"].get("cloud_tts", {})
    hosts = cfg["gemini"]["hosts"]
    rate = tts_cfg.get("sample_rate", 24000)
    turn_gap = tts_cfg.get("gap_seconds", 0.35)        # 話者が変わるときの間
    sentence_gap = tts_cfg.get("sentence_gap", 0.12)   # 同じ発言内の文と文の間
    limit = tts_cfg.get("max_sentence_chars", 180)

    voices = {
        h["speaker"]: h.get("cloud_voice", f"ja-JP-Chirp3-HD-{h['voice']}")
        for h in hosts
    }
    speeds = {h["speaker"]: h.get("speaking_rate", 1.0) for h in hosts}
    pitches = {h["speaker"]: h.get("pitch", 0.0) for h in hosts}

    turns = parse_turns(script, [h["speaker"] for h in hosts])
    if not turns:
        raise CloudTTSError("台本から発言を取り出せませんでした")

    token = get_access_token(sa_info)
    turn_silence = b"\x00" * (int(rate * turn_gap) * 2)
    sentence_silence = b"\x00" * (int(rate * sentence_gap) * 2)

    pieces_per_turn = [split_long_text(t, limit) for _, t in turns]
    total_requests = sum(len(p) for p in pieces_per_turn)
    log(f"台本を{len(turns)}発言 / {total_requests}リクエストに分解しました")

    out = bytearray()
    done = 0
    for i, ((speaker, _), pieces) in enumerate(zip(turns, pieces_per_turn)):
        if out:
            out += turn_silence
        for j, piece in enumerate(pieces):
            if j > 0:
                out += sentence_silence
            pcm, got_rate = synthesize_one(
                piece, voices[speaker], token, rate,
                speeds[speaker], pitches[speaker], log=log,
            )
            if got_rate != rate:
                raise CloudTTSError(f"サンプルレート不一致: {got_rate} != {rate}")
            out += pcm
            done += 1
            if done % 20 == 0:
                log(f"  {done}/{total_requests} 完了")

    log(f"合成完了（音声{len(out) / (rate * 2):.1f}秒）")
    return bytes(out)
