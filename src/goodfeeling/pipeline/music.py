"""Music analysis with librosa: beats, tempo, onset strength, energy, sections, estimated downbeats."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import numpy as np

from goodfeeling.media.ffmpeg import FFmpegError, ffmpeg_bin
from goodfeeling.media.fingerprint import fingerprint
from goodfeeling.schemas.music import MusicAnalysis, MusicSection

log = logging.getLogger(__name__)

SR = 22050
HOP = 512


def decode_audio(path: str | Path, sr: int = SR) -> np.ndarray:
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sr),
        "-f",
        "f32le",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise FFmpegError(f"could not decode {path}: {proc.stderr.decode(errors='replace')[-500:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def _regular_grid(times: np.ndarray, duration: float) -> tuple[np.ndarray, bool]:
    """Fit a constant-tempo grid; keep it only if the tracked beats sit on it within 20 ms."""
    if len(times) < 8:
        return times, False
    index = np.arange(len(times))
    period, phase = np.polyfit(index, times, 1)
    residual = times - (phase + period * index)
    if float(np.sqrt(np.mean(residual**2))) > 0.02:
        return times, False
    first = int(np.ceil(-phase / period))
    count = int(np.floor((duration - phase) / period)) - first + 1
    return phase + period * np.arange(first, first + count), True


def _refine_phase(beats: np.ndarray, y: np.ndarray, sr: int) -> np.ndarray:
    """Shift the whole grid so beats sit on onsets, using a fine-hop onset envelope."""
    import librosa

    fine_hop = 64
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=fine_hop, center=False)
    env_times = librosa.frames_to_time(np.arange(len(env)), sr=sr, hop_length=fine_hop) + 1024 / sr
    best_shift, best_value = 0.0, -1.0
    for shift in np.arange(-0.06, 0.0601, 0.002):
        idx = np.searchsorted(env_times, beats + shift)
        idx = idx[(idx > 0) & (idx < len(env))]
        value = float(env[idx].sum())
        if value > best_value:
            best_shift, best_value = float(shift), value
    return beats + best_shift


def _beat_energy(rms: np.ndarray, beats: np.ndarray, sr: int) -> np.ndarray:
    import librosa

    frames = librosa.time_to_frames(beats, sr=sr, hop_length=HOP)
    frames = np.clip(frames, 0, len(rms) - 1)
    edges = np.append(frames, len(rms))
    energy = np.array([rms[a : max(b, a + 1)].mean() for a, b in zip(edges[:-1], edges[1:], strict=False)])
    return energy / (energy.max() or 1.0)


def _downbeat_phase(onset: np.ndarray, beats: np.ndarray, sr: int) -> tuple[int, float]:
    import librosa

    frames = np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, len(onset) - 1)
    strength = onset[frames]
    scores = [float(strength[p::4].mean()) if len(strength[p::4]) else 0.0 for p in range(4)]
    order = np.argsort(scores)[::-1]
    best, second = scores[order[0]], scores[order[1]]
    return int(order[0]), (best - second) / (best or 1.0)


def _label(sections: list[MusicSection], duration: float) -> int | None:
    if not sections:
        return None
    energies = np.array([s.energy for s in sections])
    median = float(np.median(energies))
    candidates = [i for i, s in enumerate(sections) if i > 0 and s.start_s >= 0.1 * duration] or [0]
    drop = max(candidates, key=lambda i: sections[i].energy)
    for i, section in enumerate(sections):
        section.label = "main"
        if section.energy >= 0.92 * sections[drop].energy and i >= drop:
            section.label = "drop"
    sections[drop].label = "drop"
    if sections[0].energy < median and drop != 0:
        sections[0].label = "intro"
    if drop - 1 > 0 or (drop - 1 == 0 and sections[0].label != "intro"):
        sections[drop - 1].label = "build"
    if len(sections) > 2 and sections[-1].energy < median and sections[-1].label != "drop":
        sections[-1].label = "outro"
    return sections[drop].start_beat


def _sections(
    y: np.ndarray, sr: int, beats: np.ndarray, energy: np.ndarray, phase: int, duration: float
) -> list[MusicSection]:
    import librosa

    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=HOP)
    chroma = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=HOP)
    rms = librosa.feature.rms(y=y, hop_length=HOP)
    features = np.vstack([librosa.util.normalize(mfcc, axis=1), chroma, rms * 4])
    frames = np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, features.shape[1] - 1)
    synced = librosa.util.sync(features, list(frames), aggregate=np.mean)
    k = int(np.clip(round(duration / 25), 3, 8))
    k = min(k, max(1, synced.shape[1] // 8))
    raw = librosa.segment.agglomerative(synced, k) if k > 1 else np.array([0])
    bounds = {0, len(beats)}
    for b in raw:
        snapped = int(round((b - phase) / 4) * 4 + phase)
        if 4 <= snapped <= len(beats) - 4:
            bounds.add(snapped)
    bounds = sorted(bounds)
    sections = []
    for a, b in zip(bounds[:-1], bounds[1:], strict=False):
        if b - a < 4 and sections:
            sections[-1].end_beat = b
            continue
        end_s = float(beats[b]) if b < len(beats) else duration
        sections.append(
            MusicSection(
                label="main",
                start_beat=a,
                end_beat=b,
                start_s=float(beats[a]),
                end_s=end_s,
                energy=float(energy[a:b].mean()),
            )
        )
    for prev, nxt in zip(sections[:-1], sections[1:], strict=False):
        prev.end_beat, prev.end_s = nxt.start_beat, nxt.start_s
    return sections


def analyze_music(path: str | Path, start_bpm: float = 120.0) -> MusicAnalysis:
    import librosa

    path = Path(path)
    y = decode_audio(path)
    duration = len(y) / SR
    onset = librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset, sr=SR, hop_length=HOP, start_bpm=start_bpm)
    tracked = librosa.frames_to_time(beat_frames, sr=SR, hop_length=HOP)
    beats, regular = _regular_grid(np.asarray(tracked, dtype=float), duration)
    beats = _refine_phase(beats, y, SR)
    beats = beats[(beats >= 0) & (beats < duration)]
    if regular and len(beats) > 1:
        tempo_value = 60.0 / float(np.median(np.diff(beats)))
    else:
        tempo_value = float(np.atleast_1d(tempo)[0])
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    energy = _beat_energy(rms, beats, SR)
    phase, confidence = _downbeat_phase(onset, beats, SR)
    sections = _sections(y, SR, beats, energy, phase, duration)
    drop_beat = _label(sections, duration)
    notes = [
        f"Downbeat phase {phase} is an estimate from onset strength (confidence {confidence:.2f}); librosa does "
        "not find bar downbeats.",
        "Section labels (intro/build/drop/main/outro) are an energy heuristic; edit them here or pass --drop-at.",
    ]
    return MusicAnalysis(
        path=str(path.resolve()),
        fingerprint=fingerprint(path),
        duration_s=duration,
        tempo=round(tempo_value, 3),
        beats=[round(float(t), 5) for t in beats],
        grid="regular" if regular else "tracked",
        downbeat_phase=phase,
        sections=sections,
        beat_energy=[round(float(e), 4) for e in energy],
        drop_beat=drop_beat,
        notes=notes,
    )


def apply_drop_override(music: MusicAnalysis, drop_at_s: float) -> MusicAnalysis:
    """Move the drop to the downbeat nearest `drop_at_s` and relabel around it."""
    beats = np.asarray(music.beats)
    downbeats = music.downbeats()
    drop = min(downbeats, key=lambda i: abs(beats[i] - drop_at_s))
    sections = [s.model_copy() for s in music.sections]
    for i, section in enumerate(sections):
        if section.start_beat < drop < section.end_beat:
            tail = section.model_copy(update={"start_beat": drop, "start_s": float(beats[drop])})
            section.end_beat, section.end_s = drop, float(beats[drop])
            sections.insert(i + 1, tail)
            break
    for section in sections:
        if section.label in {"drop", "build"}:
            section.label = "main"
    for i, section in enumerate(sections):
        if section.start_beat == drop:
            section.label = "drop"
            if i > 0 and sections[i - 1].label != "intro":
                sections[i - 1].label = "build"
    notes = [*music.notes, f"Drop set by --drop-at {drop_at_s:.2f}s (beat {drop})."]
    return music.model_copy(update={"sections": sections, "drop_beat": drop, "notes": notes})
