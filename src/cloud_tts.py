
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


def split_long_text(text: str, limit: int = 900) -> list[str]:
    """長すぎる発言を句点で分割する（1リクエストの上限対策）"""
    if len(text) <= limit:
        return [text]
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[。！？])", text):
        if not sentence:
            continue
        if current and len(current) + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        pieces.append(current)
    return pieces


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
    gap = tts_cfg.get("gap_seconds", 0.35)
    voices = {
        h["speaker"]: h.get("cloud_voice", f"ja-JP-Chirp3-HD-{h['voice']}")
        for h in hosts
    }
    speeds = {h["speaker"]: h.get("speaking_rate", 1.0) for h in hosts}
    pitches = {h["speaker"]: h.get("pitch", 0.0) for h in hosts}

    turns = parse_turns(script, [h["speaker"] for h in hosts])
    if not turns:
        raise CloudTTSError("台本から発言を取り出せませんでした")
    log(f"台本を{len(turns)}個の発言に分解しました")

    token = get_access_token(sa_info)
    silence = b"\x00" * int(rate * 2 * gap)
    out = bytearray()
    total = 0

    for i, (speaker, text) in enumerate(turns, 1):
        for piece in split_long_text(text):
            pcm, got_rate = synthesize_one(
                piece, voices[speaker], token, rate,
                speeds[speaker], pitches[speaker], log=log,
            )
            if got_rate != rate:
                raise CloudTTSError(f"サンプルレート不一致: {got_rate} != {rate}")
            if out:
                out += silence
            out += pcm
            total += len(piece)
        if i % 10 == 0 or i == len(turns):
            log(f"  {i}/{len(turns)} 発言まで完了")

    log(f"合成完了（{total}文字 / 音声{len(out) / (rate * 2):.1f}秒）")
    return bytes(out)
