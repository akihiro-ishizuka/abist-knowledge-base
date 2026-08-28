"""社内動画向けのループ BGM を決定的に生成する。

`generate-video-sfx.py` と同じ方針:

- 純 stdlib のみ・乱数はシード固定。同じスクリプトから常に同じ WAV が出る
- 外部素材を一切含まない（権利がクリアな社内制作物だけ）
- `manifest.json` の sha256 / duration_ms もここが書き直す

**ループの継ぎ目を鳴らさない。** 小節長ちょうどのサンプル数で切り、末尾の数十
ミリ秒を先頭へクロスフェードして波形を連続させる。これをやらないと、繋ぎ目で
「プツッ」と鳴って敷き音として使えない。

BGM は敷くものであって聴かせるものではないので、音数は最小限に保つ。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import struct
import wave
from pathlib import Path

RATE = 48_000
ROOT = Path(__file__).resolve().parents[1]
SOUND_DIR = ROOT / "assets" / "sound-design"
OUTPUT_DIR = SOUND_DIR / "bgm"

#: 継ぎ目を消すためのクロスフェード長（秒）。
CROSSFADE_SEC = 0.08
#: 書き出し時のピーク上限。敷き音なので低く抑える。
PEAK_CEILING = 0.55

#: 音名 -> 周波数（A4=440 の平均律）。
_NOTE_BASE = {"C": -9, "D": -7, "E": -5, "F": -4, "G": -2, "A": 0, "B": 2}


def note_hz(name: str, octave: int) -> float:
    semitones = _NOTE_BASE[name[0]] + (1 if name.endswith("#") else 0)
    return 440.0 * (2 ** ((semitones + (octave - 4) * 12) / 12))


def _pad(
    left: list[float],
    right: list[float],
    *,
    start: float,
    duration: float,
    frequency: float,
    amplitude: float,
    detune_cents: float = 7.0,
) -> None:
    """デチューンした2本のノコギリ波を重ねた柔らかいパッド。"""
    begin = int(start * RATE)
    end = min(len(left), begin + int(duration * RATE))
    ratio = 2 ** (detune_cents / 1200)
    for index in range(begin, end):
        t = (index - begin) / RATE
        x = t / duration
        # 立ち上がり・減衰をゆるく（打鍵感を出さない）
        env = math.sin(math.pi * min(1.0, x * 1.15)) ** 1.2
        sample = 0.0
        for freq, weight in ((frequency, 0.6), (frequency * ratio, 0.4)):
            phase = 2 * math.pi * freq * t
            # ノコギリ波を低次倍音だけで近似（高域が刺さらない）
            sample += weight * (
                math.sin(phase) + 0.35 * math.sin(2 * phase) + 0.15 * math.sin(3 * phase)
            )
        sample *= amplitude * env * 0.4
        left[index] += sample * 0.72
        right[index] += sample * 0.72


def _bass(
    left: list[float],
    right: list[float],
    *,
    start: float,
    duration: float,
    frequency: float,
    amplitude: float,
) -> None:
    begin = int(start * RATE)
    end = min(len(left), begin + int(duration * RATE))
    for index in range(begin, end):
        t = (index - begin) / RATE
        x = t / duration
        env = math.sin(math.pi * min(1.0, x * 1.4)) ** 1.6
        sample = math.sin(2 * math.pi * frequency * t) * amplitude * env
        left[index] += sample
        right[index] += sample


def _pluck(
    left: list[float],
    right: list[float],
    *,
    start: float,
    frequency: float,
    amplitude: float,
    pan: float,
    duration: float = 0.42,
) -> None:
    begin = int(start * RATE)
    end = min(len(left), begin + int(duration * RATE))
    for index in range(begin, end):
        t = (index - begin) / RATE
        env = math.exp(-6.0 * t / duration) * min(1.0, t / 0.006)
        phase = 2 * math.pi * frequency * t
        sample = (math.sin(phase) + 0.22 * math.sin(2 * phase)) * amplitude * env
        left[index] += sample * math.sqrt((1.0 - pan) / 2)
        right[index] += sample * math.sqrt((1.0 + pan) / 2)


def _air(left: list[float], right: list[float], *, amplitude: float, seed: int) -> None:
    """ごく薄い空気感（ローパスしたノイズ）。無音の平板さを消す。"""
    rng = random.Random(seed)
    smooth_l = smooth_r = 0.0
    for index in range(len(left)):
        smooth_l = smooth_l * 0.995 + rng.uniform(-1, 1) * 0.005
        smooth_r = smooth_r * 0.995 + rng.uniform(-1, 1) * 0.005
        left[index] += smooth_l * amplitude
        right[index] += smooth_r * amplitude


def _crossfade_loop(left: list[float], right: list[float]) -> None:
    """末尾を先頭へ溶かして、継ぎ目のクリックを消す。"""
    fade = int(CROSSFADE_SEC * RATE)
    if fade * 2 >= len(left):
        return
    for channel in (left, right):
        for i in range(fade):
            weight = i / fade
            head = channel[i]
            tail = channel[len(channel) - fade + i]
            channel[i] = head * weight + tail * (1.0 - weight)
        del channel[len(channel) - fade :]


def _render(mood: str) -> tuple[list[float], list[float], float]:
    """ムードごとの1ループを組む。戻り値は (左, 右, 尺秒)。"""
    if mood == "neutral":
        bpm, bars, beats_per_bar = 84.0, 4, 4
        chords = [("A", 3, ["A", "C", "E"]), ("F", 3, ["F", "A", "C"])]
    elif mood == "upbeat":
        bpm, bars, beats_per_bar = 104.0, 4, 4
        chords = [("C", 3, ["C", "E", "G"]), ("G", 3, ["G", "B", "D"])]
    else:  # calm
        bpm, bars, beats_per_bar = 72.0, 4, 4
        chords = [("D", 3, ["D", "F", "A"]), ("B", 2, ["B", "D", "F"])]

    beat_sec = 60.0 / bpm
    bar_sec = beat_sec * beats_per_bar
    total_sec = bar_sec * bars
    samples = int(round(total_sec * RATE))
    left = [0.0] * samples
    right = [0.0] * samples

    for bar in range(bars):
        root_name, root_octave, triad = chords[bar % len(chords)]
        start = bar * bar_sec
        for index, name in enumerate(triad):
            _pad(
                left,
                right,
                start=start,
                duration=bar_sec,
                frequency=note_hz(name, 4 if index else 3),
                amplitude=0.20 if mood != "calm" else 0.24,
            )
        if mood != "calm":
            _bass(
                left,
                right,
                start=start,
                duration=bar_sec * 0.9,
                frequency=note_hz(root_name, root_octave - 1),
                amplitude=0.16,
            )
        if mood == "upbeat":
            # まばらなアルペジオ（拍を感じさせるが、前に出過ぎない）
            for step in range(beats_per_bar * 2):
                name = triad[step % len(triad)]
                _pluck(
                    left,
                    right,
                    start=start + step * beat_sec / 2,
                    frequency=note_hz(name, 5),
                    amplitude=0.09,
                    pan=-0.35 if step % 2 else 0.35,
                )

    _air(left, right, amplitude=0.05 if mood == "calm" else 0.035, seed=hash(mood) % 9973)
    _crossfade_loop(left, right)
    return left, right, len(left) / RATE


def _write(path: Path, left: list[float], right: list[float]) -> None:
    peak = max(0.001, *(abs(v) for v in left), *(abs(v) for v in right))
    scale = min(1.0, PEAK_CEILING / peak)
    frames = bytearray()
    for l_value, r_value in zip(left, right, strict=True):
        frames += struct.pack(
            "<hh",
            int(max(-1.0, min(1.0, l_value * scale)) * 32767),
            int(max(-1.0, min(1.0, r_value * scale)) * 32767),
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(frames)


LOOPS = [
    ("bgm_neutral_report_01", "neutral", 84, "報告・進捗向けの落ち着いたパッド"),
    ("bgm_upbeat_tutorial_01", "upbeat", 104, "手順・チュートリアル向けの軽いアルペジオ"),
    ("bgm_calm_training_01", "calm", 72, "研修・教材向けの静かなパッド"),
]
ATTRIBUTION = "Abist internal BGM pack v1 (layered synthesis)"


def main() -> None:
    manifest_path = SOUND_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = []
    for asset_id, mood, bpm, note in LOOPS:
        left, right, duration = _render(mood)
        target = OUTPUT_DIR / f"{asset_id}.wav"
        _write(target, left, right)
        entries.append(
            {
                "id": asset_id,
                "mood": mood,
                "path": f"bgm/{asset_id}.wav",
                "license": "in-house",
                "attribution": ATTRIBUTION,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "duration_ms": round(duration * 1000),
                "bpm": bpm,
                "loop": True,
                "note": note,
            }
        )
    manifest["bgm"] = entries
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for entry in entries:
        print(f"{entry['id']}: {entry['duration_ms']} ms  {entry['sha256'][:12]}")


if __name__ == "__main__":
    main()
