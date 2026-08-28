"""社内動画向けの効果音パレットを決定的に生成する（v3）。

同じスクリプトから常に同じ WAV が出る（乱数はシード固定、定数のみ）。
`manifest.json` の sha256 と duration_ms もここが書き直すので、音を変えたら
このスクリプトを実行してコミットする。

v3 の変更: Schroeder リバーブへの差し替え、RMS を揃えた書き出し、
`tick`（ビート表示）/ `whoosh`（シーンの繋ぎ）/ `chart`（グラフ描画）の追加。
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
OUTPUT_DIR = SOUND_DIR / "professional"


def _envelope(t: float, duration: float, attack: float = 0.025, release: float = 0.35) -> float:
    rise = min(1.0, t / max(attack, 0.001))
    fall = min(1.0, max(0.0, duration - t) / max(release, 0.001))
    return math.sin(rise * math.pi / 2) * math.sin(fall * math.pi / 2)


def _tone(
    left: list[float],
    right: list[float],
    *,
    start: float,
    duration: float,
    frequency: float,
    amplitude: float,
    pan: float = 0.0,
    decay: float = 2.0,
) -> None:
    begin = int(start * RATE)
    end = min(len(left), begin + int(duration * RATE))
    for index in range(begin, end):
        t = (index - begin) / RATE
        env = _envelope(t, duration) * math.exp(-decay * t / duration)
        phase = 2 * math.pi * frequency * t
        # 基音だけの電子音を避け、弱い倍音で柔らかい打楽器感を作る。
        sample = math.sin(phase) + 0.28 * math.sin(phase * 2.01) + 0.11 * math.sin(phase * 3.98)
        sample *= amplitude * env
        left[index] += sample * math.sqrt((1.0 - pan) / 2)
        right[index] += sample * math.sqrt((1.0 + pan) / 2)


def _sweep(
    left: list[float],
    right: list[float],
    *,
    start: float,
    duration: float,
    low_hz: float,
    high_hz: float,
    amplitude: float,
    seed: int,
    pan_from: float = -0.5,
    pan_to: float = 0.5,
) -> None:
    rng = random.Random(seed)
    begin = int(start * RATE)
    end = min(len(left), begin + int(duration * RATE))
    phase = 0.0
    smooth_noise = 0.0
    for index in range(begin, end):
        t = (index - begin) / RATE
        x = t / duration
        frequency = low_hz * ((high_hz / low_hz) ** x)
        phase += 2 * math.pi * frequency / RATE
        smooth_noise = smooth_noise * 0.965 + rng.uniform(-1, 1) * 0.035
        env = math.sin(math.pi * x) ** 1.5
        sample = (0.65 * math.sin(phase) + 0.8 * smooth_noise) * amplitude * env
        pan = pan_from + (pan_to - pan_from) * x
        left[index] += sample * math.sqrt((1.0 - pan) / 2)
        right[index] += sample * math.sqrt((1.0 + pan) / 2)


def _comb(samples: list[float], delay_sec: float, feedback: float) -> list[float]:
    """フィードバックコムフィルタ1本（Schroeder リバーブの構成要素）。"""
    delay = max(1, int(delay_sec * RATE))
    out = list(samples)
    for index in range(delay, len(out)):
        out[index] += out[index - delay] * feedback
    return out


def _allpass(samples: list[float], delay_sec: float, gain: float) -> list[float]:
    """オールパスフィルタ1本（響きを密にして金属感を消す）。"""
    delay = max(1, int(delay_sec * RATE))
    out = list(samples)
    for index in range(delay, len(out)):
        out[index] = -gain * samples[index] + samples[index - delay] + gain * out[index - delay]
    return out


def _reverb(left: list[float], right: list[float]) -> None:
    """Schroeder リバーブ（並列コム4本 + 直列オールパス2本）。

    v2 は3タップのステレオクロスディレイで、山びこ的な反射が耳に付いた。
    定数はすべて固定なので出力は決定的（同じ入力から同じ WAV が出る）。
    """
    for channel, offset in ((left, 0.0), (right, 0.0011)):
        dry = channel.copy()
        wet = [0.0] * len(channel)
        for delay_sec, feedback in (
            (0.0297, 0.78),
            (0.0371, 0.75),
            (0.0411, 0.72),
            (0.0437, 0.70),
        ):
            combed = _comb(dry, delay_sec + offset, feedback)
            for index in range(len(wet)):
                wet[index] += combed[index] * 0.25
        wet = _allpass(wet, 0.0050 + offset, 0.7)
        wet = _allpass(wet, 0.0017 + offset, 0.7)
        for index in range(len(channel)):
            channel[index] = dry[index] + wet[index] * 0.18


def _render(kind: str, variant: int, duration: float) -> tuple[list[float], list[float]]:
    left = [0.0] * int(duration * RATE)
    right = [0.0] * int(duration * RATE)
    shift = (variant - 1) * 24.0
    if kind == "startup":
        for start, note, pan in ((0.00, 220, -0.35), (0.20, 330, 0.0), (0.43, 494, 0.35)):
            _tone(
                left,
                right,
                start=start,
                duration=0.85,
                frequency=note + shift,
                amplitude=0.32,
                pan=pan,
            )
        _sweep(
            left,
            right,
            start=0.0,
            duration=0.75,
            low_hz=90,
            high_hz=280,
            amplitude=0.08,
            seed=100 + variant,
        )
    elif kind == "transition":
        _sweep(
            left,
            right,
            start=0.0,
            duration=0.68,
            low_hz=170 + shift,
            high_hz=760 + shift,
            amplitude=0.24,
            seed=200 + variant,
        )
        _tone(
            left, right, start=0.32, duration=0.35, frequency=440 + shift, amplitude=0.16, pan=0.25
        )
    elif kind == "accent":
        _tone(
            left,
            right,
            start=0.02,
            duration=0.43,
            frequency=520 + shift,
            amplitude=0.42,
            pan=-0.12,
            decay=3.4,
        )
        _tone(
            left,
            right,
            start=0.07,
            duration=0.34,
            frequency=780 + shift,
            amplitude=0.18,
            pan=0.18,
            decay=4.2,
        )
    elif kind == "swipe":
        _sweep(
            left,
            right,
            start=0.0,
            duration=0.62,
            low_hz=210,
            high_hz=1_250,
            amplitude=0.28,
            seed=300,
        )
    elif kind == "branch":
        _tone(
            left,
            right,
            start=0.03,
            duration=0.62,
            frequency=294,
            amplitude=0.31,
            pan=-0.35,
            decay=3.0,
        )
        _tone(
            left,
            right,
            start=0.24,
            duration=0.50,
            frequency=440,
            amplitude=0.28,
            pan=0.35,
            decay=3.0,
        )
    elif kind == "caution":
        base = 220 + shift
        _tone(
            left,
            right,
            start=0.03,
            duration=0.43,
            frequency=base,
            amplitude=0.30,
            pan=-0.15,
            decay=2.8,
        )
        _tone(
            left,
            right,
            start=0.39,
            duration=0.46,
            frequency=base * 0.89,
            amplitude=0.27,
            pan=0.15,
            decay=2.8,
        )
    elif kind == "complete":
        for start, note, pan in ((0.02, 392, -0.3), (0.20, 494, 0.0), (0.39, 587, 0.3)):
            _tone(
                left,
                right,
                start=start,
                duration=0.72,
                frequency=note + shift,
                amplitude=0.28,
                pan=pan,
                decay=2.4,
            )
    elif kind == "error":
        _tone(
            left,
            right,
            start=0.02,
            duration=0.35,
            frequency=165,
            amplitude=0.34,
            pan=-0.1,
            decay=2.0,
        )
        _tone(
            left,
            right,
            start=0.27,
            duration=0.40,
            frequency=139,
            amplitude=0.31,
            pan=0.1,
            decay=2.0,
        )
    elif kind == "ending":
        for start, note, pan in (
            (0.00, 262, -0.35),
            (0.16, 330, 0.0),
            (0.32, 392, 0.35),
            (0.62, 523, 0.0),
        ):
            _tone(
                left,
                right,
                start=start,
                duration=1.0,
                frequency=note,
                amplitude=0.25,
                pan=pan,
                decay=1.8,
            )
    elif kind == "tick":
        # ビート表示に同期する極短のクリック。主張させず、輪郭だけ立てる。
        _tone(
            left, right, start=0.0, duration=0.05,
            frequency=1180 + shift * 4, amplitude=0.30, decay=7.0,
        )
        _sweep(
            left, right, start=0.0, duration=0.045,
            low_hz=2600, high_hz=900, amplitude=0.05, seed=700 + variant,
        )
    elif kind == "whoosh":
        # シーン間のディップ（0.35 秒）に合わせた短い風切り音。
        _sweep(
            left, right, start=0.0, duration=0.34,
            low_hz=180, high_hz=1500, amplitude=0.13,
            seed=800 + variant, pan_from=-0.6, pan_to=0.6,
        )
        _sweep(
            left, right, start=0.06, duration=0.26,
            low_hz=1400, high_hz=240, amplitude=0.07, seed=820 + variant,
            pan_from=0.4, pan_to=-0.4,
        )
    elif kind == "chart":
        # 棒が伸びるのに合わせて上がるライザー。
        for index, note in enumerate((392, 523, 659)):
            _tone(
                left, right, start=0.10 * index, duration=0.55,
                frequency=note + shift, amplitude=0.20, pan=-0.25 + 0.25 * index,
            )
        _sweep(
            left, right, start=0.0, duration=0.62,
            low_hz=220, high_hz=980, amplitude=0.06, seed=900 + variant,
        )
    _reverb(left, right)
    return left, right


#: 書き出し時の RMS 目標（dBFS）。素材ごとの体感音量を揃える。
TARGET_RMS_DBFS = -20.0


def _write(path: Path, left: list[float], right: list[float]) -> None:
    """ピークを揃えたうえで、RMS も目標へ寄せる。

    v2 はピーク正規化だけだったので、短い click 系と長い pad 系で体感音量が
    大きく違っていた。ピーク上限（0.82）は保ったまま RMS を目標へ近づける。
    """
    peak = max(0.001, *(abs(v) for v in left), *(abs(v) for v in right))
    scale = min(1.0, 0.82 / peak)
    count = len(left) + len(right)
    energy = sum(v * v for v in left) + sum(v * v for v in right)
    rms = math.sqrt(energy / count) * scale if count else 0.0
    if rms > 0.0:
        target = 10 ** (TARGET_RMS_DBFS / 20)
        # 上げすぎてピークが割れないよう、ピーク基準の上限を超えない範囲で寄せる。
        scale = min(scale * (target / rms), 0.999 / peak)
    frames = bytearray()
    for l_value, r_value in zip(left, right, strict=True):
        frames += struct.pack(
            "<hh",
            int(max(-1.0, min(1.0, l_value * scale)) * 32767),
            int(max(-1.0, min(1.0, r_value * scale)) * 32767),
        )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(frames)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = SOUND_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    durations = {
        "startup": 1.30,
        "transition": 0.72,
        "accent": 0.48,
        "swipe": 0.68,
        "branch": 0.78,
        "caution": 0.90,
        "complete": 1.10,
        "error": 0.72,
        "ending": 1.65,
        "tick": 0.10,
        "whoosh": 0.36,
        "chart": 0.68,
    }
    for entry in manifest["assets"]:
        kind = entry["categories"][0]
        variant = 2 if entry["id"].endswith("_02") else 1
        duration = durations[kind]
        target = SOUND_DIR / entry["path"]
        left, right = _render(kind, variant, duration)
        _write(target, left, right)
        entry["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        entry["duration_ms"] = round(duration * 1000)
        entry["attribution"] = "Abist internal SFX pack v3 (layered synthesis)"
    manifest["preset"] = "professional-v3"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
