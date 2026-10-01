"""Remove silence + background noise from an audio file.

Pipeline:
  1. ffprobe    -> duration
  2. silencedetect -> list of (start, end) silent ranges
  3. Invert     -> list of non-silent segments (dropping tiny ones)
  4. ffmpeg filter_complex:
       atrim each segment -> concat -> afftdn (denoise) -> mp3
"""

import re
import subprocess
from pathlib import Path

from config import (
    SILENCE_THRESHOLD, MIN_SILENCE_DURATION, MIN_SEGMENT_DURATION,
    ENABLE_NOISE_REDUCTION, NOISE_REDUCTION_DB, NOISE_FLOOR_DB,
)


def get_duration(filename) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         str(filename)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def detect_silence(filename):
    result = subprocess.run(
        ["ffmpeg", "-hide_banner",
         "-i", str(filename),
         "-af",
         f"silencedetect=noise={SILENCE_THRESHOLD}:d={MIN_SILENCE_DURATION}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )

    ranges, start = [], None
    for line in result.stderr.splitlines():
        m_start = re.search(r"silence_start:\s*([0-9.]+)", line)
        m_end   = re.search(r"silence_end:\s*([0-9.]+)",   line)
        if m_start:
            start = float(m_start.group(1))
        elif m_end and start is not None:
            ranges.append((start, float(m_end.group(1))))
            start = None
    return ranges


def create_segments(duration, silence_ranges):
    segments, current = [], 0.0
    for s_start, s_end in silence_ranges:
        if s_start > current:
            segments.append((current, s_start))
        current = s_end
    if current < duration:
        segments.append((current, duration))
    return [(s, e) for (s, e) in segments if (e - s) >= MIN_SEGMENT_DURATION]


def remove_silence(filename, log=print):
    """Clean audio; returns (output_path, stats_dict) or (None, stats_dict)."""
    input_path  = Path(filename)
    output_path = input_path.with_name(input_path.stem + "_nosilence.mp3")

    log(f"Cleaning audio: {input_path.name}")

    stats = {
        "original_duration": 0.0,
        "new_duration": 0.0,
        "removed": 0.0,
        "silence_sections": 0,
        "segments_kept": 0,
    }

    try:
        duration = get_duration(input_path)
        stats["original_duration"] = duration
        log(f"Duration        : {duration:.2f}s")

        silence_ranges = detect_silence(input_path)
        stats["silence_sections"] = len(silence_ranges)
        log(f"Silence found   : {len(silence_ranges)} section(s)")

        segments = create_segments(duration, silence_ranges)
        stats["segments_kept"] = len(segments)

        if not segments:
            log("No audio segments remain.")
            return None, stats

        log(f"Segments kept   : {len(segments)}")

        parts = []
        for i, (start, end) in enumerate(segments):
            parts.append(
                f"[0:a]atrim=start={start}:end={end},"
                f"asetpts=PTS-STARTPTS[a{i}]"
            )
        concat_in = "".join(f"[a{i}]" for i in range(len(segments)))

        filter_complex = (
            ";".join(parts)
            + ";"
            + concat_in
            + f"concat=n={len(segments)}:v=0:a=1[joined]"
        )

        if ENABLE_NOISE_REDUCTION:
            filter_complex += (
                f";[joined]afftdn=nr={NOISE_REDUCTION_DB}"
                f":nf={NOISE_FLOOR_DB}[outa]"
            )
            map_label = "[outa]"
        else:
            map_label = "[joined]"

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-filter_complex", filter_complex,
            "-map", map_label,
            "-c:a", "libmp3lame", "-q:a", "2",
            str(output_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log("FFmpeg stderr:\n" + result.stderr[-2000:])
            return None, stats

        new_duration = get_duration(output_path)
        stats["new_duration"] = new_duration
        stats["removed"]      = duration - new_duration

        log(f"New duration    : {new_duration:.2f}s")
        log(f"Removed         : {duration - new_duration:.2f}s")
        log(f"Saved           : {output_path}")

        return output_path, stats

    except Exception as e:
        log(f"Error: {e}")
        return None, stats
