#!/usr/bin/env python3
from __future__ import annotations

import errno
import json
import math
import os
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


NETWORK_SANDBOX_ENV = "BLINK_NETWORK_SANDBOX"
BASELINE_WINDOW_SECONDS = 4.0
RECENT_WINDOW_SECONDS = 60.0
SESSION_WINDOW_SECONDS = 300.0
PRUNE_INTERVAL_SECONDS = 1.0
LEFT_EYE_INDICES = (33, 160, 158, 133, 153, 144)
RIGHT_EYE_INDICES = (362, 385, 387, 263, 373, 380)


@dataclass(frozen=True, slots=True)
class Config:
    camera_index: int = 0
    fps: float = 60.0
    width: int = 320
    height: int = 240
    model_path: Path = Path("models/face_landmarker.task")
    log_path: Path = Path("blink-stats.jsonl")
    closed_threshold: float = 0.40
    peak_threshold: float = 0.55
    quick_closed_threshold: float = 0.28
    quick_peak_threshold: float = 0.34
    quick_max_blink_sec: float = 0.18
    quick_min_pulse: float = 0.12
    aperture_min_drop: float = 0.030
    aperture_closed_ratio: float = 0.72
    aperture_reopen_ratio: float = 0.82
    open_threshold: float = 0.25
    open_margin: float = 0.10
    look_down_threshold: float = 0.45
    look_down_open_threshold: float = 0.20
    look_down_eye_open_threshold: float = 0.20
    min_blink_sec: float = 0.015
    max_blink_sec: float = 0.45
    blink_rate_limit_sec: float = 0.0
    max_faces: int = 1
    min_face_detection_confidence: float = 0.5
    min_face_presence_confidence: float = 0.5
    min_tracking_confidence: float = 0.5


CONFIG = Config()


def validate_config(config: Config) -> None:
    checks = (
        (0 < config.fps <= 60, "fps must be 1...60"),
        (config.width > 0 and config.height > 0, "width and height must be > 0"),
        (0 < config.open_threshold < config.closed_threshold < 1, "blink thresholds invalid"),
        (0 < config.peak_threshold < 1, "peak threshold invalid"),
        (
            0 < config.quick_closed_threshold < config.closed_threshold,
            "quick closed threshold invalid",
        ),
        (0 < config.quick_peak_threshold < config.peak_threshold, "quick peak threshold invalid"),
        (0 < config.quick_min_pulse, "quick pulse threshold invalid"),
        (0 < config.aperture_min_drop, "aperture drop threshold invalid"),
        (
            0 < config.aperture_closed_ratio < config.aperture_reopen_ratio < 1,
            "aperture invalid",
        ),
        (0 <= config.open_margin, "open margin invalid"),
        (
            0 < config.min_blink_sec < config.quick_max_blink_sec <= config.max_blink_sec,
            "blink duration invalid",
        ),
        (0 <= config.blink_rate_limit_sec, "blink rate limit invalid"),
        (0 <= config.look_down_threshold <= 1, "look-down threshold invalid"),
        (0 <= config.look_down_open_threshold <= 1, "look-down open threshold invalid"),
        (0 <= config.look_down_eye_open_threshold <= 1, "look-down aperture threshold invalid"),
        (config.max_faces >= 1, "max faces must be >= 1"),
    )
    for ok, message in checks:
        if not ok:
            raise RuntimeError(f"bad config: {message}")


def iso_timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def fmt(value: float | None) -> str:
    if value is None:
        return "na"
    return f"{value:.2f}"


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    return percentile_sorted(sorted(values), p)


def percentile_sorted(ordered: list[float], p: float) -> float | None:
    if len(ordered) == 1:
        return ordered[0]
    p = min(max(p, 0.0), 1.0)
    pos = p * (len(ordered) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    frac = pos - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * frac


def average_pair(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return (left + right) / 2.0


def max_present(values: tuple[float | None, ...]) -> float | None:
    best: float | None = None
    for value in values:
        if value is not None and (best is None or value > best):
            best = value
    return best


def sanitize_log_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise TypeError("refusing to log non-finite float")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("refusing to log bytes-like data")
    if hasattr(value, "__array_interface__"):
        raise TypeError("refusing to log array-like data")
    if isinstance(value, list):
        return [sanitize_log_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_log_value(item) for item in value]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("log keys must be strings")
            sanitized[key] = sanitize_log_value(item)
        return sanitized
    raise TypeError(f"refusing to log unsupported value type: {type(value).__name__}")


def require_network_sandbox() -> None:
    if os.environ.get(NETWORK_SANDBOX_ENV) == "1":
        return
    raise RuntimeError(
        "refusing to run without the deny-network launcher; "
        "use ./blink-detector, not python blink_detector.py"
    )


def check_privacy() -> int:
    require_network_sandbox()
    import socket

    try:
        with socket.create_connection(("1.1.1.1", 80), timeout=0.5):
            pass
    except PermissionError as exc:
        if exc.errno == errno.EPERM:
            print("privacy check passed: outbound network is blocked")
            return 0
        raise
    except OSError as exc:
        raise RuntimeError(
            f"privacy check inconclusive: expected EPERM from deny-network sandbox, got {exc}"
        ) from exc
    raise RuntimeError("privacy check failed: outbound network connection succeeded")


def max_optional(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def min_optional(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


@dataclass(frozen=True, slots=True)
class TimedValue:
    visible_seconds: float
    value: float


@dataclass(frozen=True, slots=True)
class BlinkEvent:
    visible_seconds: float


def recent_blink_events(
    events: deque[BlinkEvent],
    visible_seconds: float,
    window_seconds: float,
) -> list[BlinkEvent]:
    cutoff = visible_seconds - window_seconds
    recent: list[BlinkEvent] = []
    for event in reversed(events):
        if event.visible_seconds < cutoff:
            break
        recent.append(event)
    return recent


@dataclass(frozen=True, slots=True)
class FrameMetrics:
    face_count: int
    blink_left: float | None = None
    blink_right: float | None = None
    look_down_left: float | None = None
    look_down_right: float | None = None
    look_up_left: float | None = None
    look_up_right: float | None = None
    look_in_left: float | None = None
    look_in_right: float | None = None
    look_out_left: float | None = None
    look_out_right: float | None = None
    eye_open_left: float | None = None
    eye_open_right: float | None = None

    @property
    def has_blink_scores(self) -> bool:
        return self.blink_left is not None and self.blink_right is not None

    @property
    def blink_scores(self) -> tuple[float, float] | None:
        if self.blink_left is None or self.blink_right is None:
            return None
        return self.blink_left, self.blink_right

    @property
    def face_visible(self) -> bool:
        return self.face_count == 1 and self.has_blink_scores

    @property
    def blink_full_score(self) -> float | None:
        scores = self.blink_scores
        if scores is None:
            return None
        return min(scores)

    @property
    def blink_avg_score(self) -> float | None:
        scores = self.blink_scores
        if scores is None:
            return None
        return (scores[0] + scores[1]) / 2.0

    @property
    def look_down_score(self) -> float | None:
        return average_pair(self.look_down_left, self.look_down_right)

    @property
    def look_up_score(self) -> float | None:
        return average_pair(self.look_up_left, self.look_up_right)

    @property
    def look_side_score(self) -> float | None:
        return max_present(
            (
                self.look_in_left,
                self.look_in_right,
                self.look_out_left,
                self.look_out_right,
            )
        )

    @property
    def eye_open_score(self) -> float | None:
        return average_pair(self.eye_open_left, self.eye_open_right)


@dataclass(slots=True)
class BlinkCandidate:
    start_seconds: float
    reopen_threshold: float
    peak_score: float
    peak_avg_score: float
    local_open_score: float
    min_eye_open_score: float | None
    local_eye_open_score: float | None
    look_down_score: float | None
    look_away_score: float | None


class BlinkCounter:
    def __init__(self, config: Config, log_handle: TextIO | None = None) -> None:
        self.config = config
        self.log_handle = log_handle
        self.session_id = os.urandom(16).hex()
        self.start_seconds = time.monotonic()
        self.local_sample_limit = max(24, int(config.fps * 0.8))
        self.visible_seconds = 0.0
        self.last_visible_frame_seconds: float | None = None
        self.total_blinks = 0
        self.last_counted_wall_seconds: float | None = None
        self.last_counted_visible_seconds: float | None = None
        self.longest_no_blink_visible_seconds = 0.0
        self.blink_events: deque[BlinkEvent] = deque()
        self.open_score_samples: deque[TimedValue] = deque()
        self.eye_open_samples: deque[TimedValue] = deque()
        self.open_score_samples_by_gaze: dict[tuple[int, int], deque[TimedValue]] = {}
        self.eye_open_samples_by_gaze: dict[tuple[int, int], deque[TimedValue]] = {}
        self.last_prune_visible_seconds = 0.0
        self.active_blink: BlinkCandidate | None = None

    def log_session_start(self) -> None:
        if self.log_handle is None:
            return
        self._write_record(
            {
                "type": "session_start",
                "session_id": self.session_id,
                "engine": "mediapipe_face_landmarker",
                "config": self.config_summary(),
                "privacy": {
                    "contains_frames": False,
                    "contains_images": False,
                    "contains_landmark_points": False,
                    "contains_derived_numeric_stats_only": True,
                },
                "blink_event_fields": {
                    "visible_sec": "seconds with one visible face and usable blendshapes",
                    "since_previous_blink_visible_sec": "visible seconds since previous blink",
                    "longest_no_blink_visible_sec": "longest visible-time gap without a blink",
                    "bpm_60s": "blink rate over last 60 visible seconds",
                    "bpm_5m": "blink rate over last 5 visible minutes",
                    "bpm_all_visible": "blink rate over all visible time",
                    "visible_pct": "percent of elapsed wall time where a face was usable",
                },
            },
        )

    def config_summary(self) -> dict[str, Any]:
        return {
            "fps": self.config.fps,
            "width": self.config.width,
            "height": self.config.height,
            "model": str(self.config.model_path),
            "closed_threshold": self.config.closed_threshold,
            "peak_threshold": self.config.peak_threshold,
            "quick_closed_threshold": self.config.quick_closed_threshold,
            "quick_peak_threshold": self.config.quick_peak_threshold,
            "quick_max_blink_sec": self.config.quick_max_blink_sec,
            "quick_min_pulse": self.config.quick_min_pulse,
            "aperture_min_drop": self.config.aperture_min_drop,
            "aperture_closed_ratio": self.config.aperture_closed_ratio,
            "aperture_reopen_ratio": self.config.aperture_reopen_ratio,
            "open_threshold": self.config.open_threshold,
            "min_blink_sec": self.config.min_blink_sec,
            "max_blink_sec": self.config.max_blink_sec,
            "blink_rate_limit_sec": self.config.blink_rate_limit_sec,
            "look_down_threshold": self.config.look_down_threshold,
            "look_down_eye_open_threshold": self.config.look_down_eye_open_threshold,
        }

    def _write_record(self, record: dict[str, Any]) -> None:
        if self.log_handle is None:
            return
        sanitized = sanitize_log_value(record)
        if not isinstance(sanitized, dict):
            raise TypeError("log record must be an object")
        sanitized.setdefault("ts", iso_timestamp())
        self.log_handle.write(json.dumps(sanitized, separators=(",", ":")) + "\n")
        self.log_handle.flush()

    def log_session_end(self, now_seconds: float) -> None:
        elapsed_seconds = max(now_seconds - self.start_seconds, 0.001)
        visible_pct = min(100.0, max(0.0, self.visible_seconds / elapsed_seconds * 100.0))
        current_gap = self.current_no_blink_gap()
        longest_gap = max(self.longest_no_blink_visible_seconds, current_gap)
        lifetime_bpm = (
            self.total_blinks / self.visible_seconds * 60.0
            if self.visible_seconds > 0
            else 0.0
        )
        self._write_record(
            {
                "type": "session_end",
                "session_id": self.session_id,
                "elapsed_sec": elapsed_seconds,
                "visible_sec": self.visible_seconds,
                "visible_pct": visible_pct,
                "blinks_total": self.total_blinks,
                "current_no_blink_visible_sec": current_gap,
                "longest_no_blink_visible_sec": longest_gap,
                "bpm_all_visible": lifetime_bpm,
            },
        )

    def update(self, metrics: FrameMetrics, now_seconds: float) -> BlinkEvent | None:
        if not metrics.face_visible:
            self.last_visible_frame_seconds = None
            self.active_blink = None
            return None

        if (
            self.last_visible_frame_seconds is not None
            and now_seconds - self.last_visible_frame_seconds <= 1.0
        ):
            self.visible_seconds += now_seconds - self.last_visible_frame_seconds
        self.last_visible_frame_seconds = now_seconds

        blink_score = metrics.blink_full_score
        blink_avg = metrics.blink_avg_score
        if blink_score is None or blink_avg is None:
            return None

        look_down_score = metrics.look_down_score
        look_up_score = metrics.look_up_score
        look_side_score = metrics.look_side_score
        look_away_score = max_present((look_down_score, look_up_score, look_side_score))
        eye_open_score = metrics.eye_open_score
        gaze_bucket = self._gaze_bucket(look_down_score, look_up_score, look_side_score)

        gaze_open = self._local_open_score(gaze_bucket)
        gaze_eye_open = self._local_eye_open_score(gaze_bucket)
        local_open = gaze_open or self._local_open_score(None) or self.config.open_threshold
        local_eye_open = gaze_eye_open or self._local_eye_open_score(None)
        has_gaze_eye_baseline = gaze_eye_open is not None
        allow_relative_detection = gaze_open is not None or gaze_bucket == (0, 0)
        closed_now = self._is_candidate_start(
            blink_score,
            blink_avg,
            local_open,
            eye_open_score,
            local_eye_open,
            look_down_score,
            look_away_score,
            has_gaze_eye_baseline,
            allow_relative_detection,
        )
        event: BlinkEvent | None = None

        if closed_now:
            if self.active_blink is None:
                self.active_blink = BlinkCandidate(
                    start_seconds=now_seconds,
                    reopen_threshold=self._dynamic_reopen_threshold(local_open),
                    peak_score=blink_score,
                    peak_avg_score=blink_avg,
                    local_open_score=local_open,
                    min_eye_open_score=eye_open_score,
                    local_eye_open_score=local_eye_open,
                    look_down_score=look_down_score,
                    look_away_score=look_away_score,
                )
            else:
                self.active_blink.peak_score = max(self.active_blink.peak_score, blink_score)
                self.active_blink.peak_avg_score = max(self.active_blink.peak_avg_score, blink_avg)
                if eye_open_score is not None:
                    self.active_blink.min_eye_open_score = min_optional(
                        self.active_blink.min_eye_open_score,
                        eye_open_score,
                    )
                self.active_blink.look_down_score = max_optional(
                    self.active_blink.look_down_score,
                    look_down_score,
                )
                self.active_blink.look_away_score = max_optional(
                    self.active_blink.look_away_score,
                    look_away_score,
                )
        elif self.active_blink is not None:
            duration = now_seconds - self.active_blink.start_seconds
            if eye_open_score is not None:
                self.active_blink.min_eye_open_score = min_optional(
                    self.active_blink.min_eye_open_score,
                    eye_open_score,
                )
            self.active_blink.look_down_score = max_optional(
                self.active_blink.look_down_score,
                look_down_score,
            )
            self.active_blink.look_away_score = max_optional(
                self.active_blink.look_away_score,
                look_away_score,
            )
            reopened_by_blink_score = blink_score <= self.active_blink.reopen_threshold
            reopened_by_aperture = self._is_aperture_reopened(self.active_blink, eye_open_score)
            if reopened_by_blink_score or reopened_by_aperture:
                if self._is_countable_blink(duration, self.active_blink, reopened_by_aperture):
                    event = self._count_blink(now_seconds)
                self.active_blink = None
            elif duration > self.config.max_blink_sec:
                self.active_blink = None
        else:
            self._record_open_sample(gaze_bucket, blink_score, eye_open_score)

        self._prune_samples(force=False)
        return event

    def _is_strict_blink_peak(self, blink_score: float, blink_avg: float) -> bool:
        return (
            blink_score >= self.config.closed_threshold
            and blink_avg >= self.config.peak_threshold
        )

    def _is_quick_blink_peak(
        self,
        blink_score: float,
        blink_avg: float,
        local_open: float,
        look_away_score: float | None,
    ) -> bool:
        if (
            blink_score < self.config.quick_closed_threshold
            or blink_avg < self.config.quick_peak_threshold
        ):
            return False
        if blink_score - local_open < self.config.quick_min_pulse:
            return False
        if (
            look_away_score is not None
            and look_away_score >= self.config.look_down_threshold
            and local_open < self.config.look_down_open_threshold
        ):
            return False
        return True

    def _is_aperture_blink_peak(
        self,
        eye_open_score: float | None,
        local_eye_open: float | None,
        look_down_score: float | None,
    ) -> bool:
        if not self._is_aperture_closed(eye_open_score, local_eye_open):
            return False
        if (
            look_down_score is not None
            and look_down_score >= self.config.look_down_threshold
            and local_eye_open >= self.config.look_down_eye_open_threshold
        ):
            return False
        return True

    def _is_candidate_start(
        self,
        blink_score: float,
        blink_avg: float,
        local_open: float,
        eye_open_score: float | None,
        local_eye_open: float | None,
        look_down_score: float | None,
        look_away_score: float | None,
        has_gaze_baseline: bool,
        allow_relative_detection: bool,
    ) -> bool:
        return (
            self._is_strict_blink_peak(blink_score, blink_avg)
            or (
                allow_relative_detection
                and self._is_quick_blink_peak(blink_score, blink_avg, local_open, look_away_score)
            )
            or (
                has_gaze_baseline
                and self._is_aperture_blink_peak(
                    eye_open_score,
                    local_eye_open,
                    look_down_score,
                )
            )
        )

    def _is_countable_blink(
        self,
        duration: float,
        candidate: BlinkCandidate,
        reopened_by_aperture: bool,
    ) -> bool:
        if not (self.config.min_blink_sec <= duration <= self.config.max_blink_sec):
            return False
        if self._is_strict_blink_peak(candidate.peak_score, candidate.peak_avg_score):
            return True
        if (
            duration <= self.config.quick_max_blink_sec
            and self._is_quick_blink_peak(
                candidate.peak_score,
                candidate.peak_avg_score,
                candidate.local_open_score,
                candidate.look_away_score,
            )
        ):
            return True
        return (
            reopened_by_aperture
            and duration <= self.config.quick_max_blink_sec
            and self._is_aperture_countable(candidate)
        )

    def _is_aperture_closed(
        self,
        eye_open_score: float | None,
        local_eye_open: float | None,
    ) -> bool:
        if eye_open_score is None or local_eye_open is None or local_eye_open <= 0:
            return False
        return (
            local_eye_open - eye_open_score >= self.config.aperture_min_drop
            and eye_open_score / local_eye_open <= self.config.aperture_closed_ratio
        )

    def _is_aperture_countable(self, candidate: BlinkCandidate) -> bool:
        return self._is_aperture_closed(
            candidate.min_eye_open_score,
            candidate.local_eye_open_score,
        )

    def _is_aperture_reopened(
        self,
        candidate: BlinkCandidate,
        eye_open_score: float | None,
    ) -> bool:
        if eye_open_score is None or candidate.local_eye_open_score is None:
            return False
        return eye_open_score >= candidate.local_eye_open_score * self.config.aperture_reopen_ratio

    def _dynamic_reopen_threshold(self, local_open: float) -> float:
        return max(
            self.config.open_threshold,
            min(self.config.closed_threshold - 0.02, local_open + self.config.open_margin),
        )

    def _gaze_bucket(
        self,
        look_down_score: float | None,
        look_up_score: float | None,
        look_side_score: float | None,
    ) -> tuple[int, int]:
        down = look_down_score or 0.0
        up = look_up_score or 0.0
        side = look_side_score or 0.0
        if down >= self.config.look_down_threshold and down >= up:
            vertical = 1
        elif up >= self.config.look_down_threshold:
            vertical = -1
        else:
            vertical = 0
        horizontal = 1 if side >= self.config.look_down_threshold else 0
        return vertical, horizontal

    def _record_open_sample(
        self,
        gaze_bucket: tuple[int, int],
        blink_score: float,
        eye_open_score: float | None,
    ) -> None:
        sample = TimedValue(self.visible_seconds, blink_score)
        self.open_score_samples.append(sample)
        self.open_score_samples_by_gaze.setdefault(gaze_bucket, deque()).append(sample)
        if eye_open_score is not None:
            eye_sample = TimedValue(self.visible_seconds, eye_open_score)
            self.eye_open_samples.append(eye_sample)
            self.eye_open_samples_by_gaze.setdefault(gaze_bucket, deque()).append(eye_sample)

    def _local_score(self, samples: deque[TimedValue], p: float) -> float | None:
        cutoff = self.visible_seconds - BASELINE_WINDOW_SECONDS
        recent: list[float] = []
        for sample in reversed(samples):
            if sample.visible_seconds < cutoff or len(recent) >= self.local_sample_limit:
                break
            recent.append(sample.value)
        if len(recent) < 2:
            return None
        return percentile(recent, p)

    def _local_open_score(self, gaze_bucket: tuple[int, int] | None) -> float | None:
        if gaze_bucket is None:
            return self._local_score(self.open_score_samples, 0.75)
        samples = self.open_score_samples_by_gaze.get(gaze_bucket)
        if samples is None:
            return None
        return self._local_score(samples, 0.75)

    def _local_eye_open_score(self, gaze_bucket: tuple[int, int] | None) -> float | None:
        if gaze_bucket is None:
            return self._local_score(self.eye_open_samples, 0.70)
        samples = self.eye_open_samples_by_gaze.get(gaze_bucket)
        if samples is None:
            return None
        return self._local_score(samples, 0.70)

    def _count_blink(self, now_seconds: float) -> BlinkEvent | None:
        if (
            self.last_counted_wall_seconds is not None
            and now_seconds - self.last_counted_wall_seconds < self.config.blink_rate_limit_sec
        ):
            return None

        gap_for_longest = self.current_no_blink_gap()
        seconds_since_previous_blink = (
            None
            if self.last_counted_visible_seconds is None
            else gap_for_longest
        )
        self.longest_no_blink_visible_seconds = max(
            self.longest_no_blink_visible_seconds,
            gap_for_longest,
        )
        event = BlinkEvent(visible_seconds=self.visible_seconds)
        self.total_blinks += 1
        self.last_counted_wall_seconds = now_seconds
        self.last_counted_visible_seconds = self.visible_seconds
        self.blink_events.append(event)
        self._write_blink_output(seconds_since_previous_blink, now_seconds)
        return event

    def current_no_blink_gap(self) -> float:
        if self.last_counted_visible_seconds is None:
            return self.visible_seconds
        return max(0.0, self.visible_seconds - self.last_counted_visible_seconds)

    def _write_blink_output(
        self,
        seconds_since_previous_blink: float | None,
        now_seconds: float,
    ) -> None:
        self._prune_samples(force=True)
        elapsed_seconds = max(now_seconds - self.start_seconds, 0.001)
        visible_minutes = self.visible_seconds / 60.0
        visible_pct = min(100.0, max(0.0, self.visible_seconds / elapsed_seconds * 100.0))
        lifetime_bpm = (
            self.total_blinks / self.visible_seconds * 60.0
            if self.visible_seconds > 0
            else 0.0
        )

        recent60 = recent_blink_events(
            self.blink_events,
            self.visible_seconds,
            RECENT_WINDOW_SECONDS,
        )
        recent300 = list(self.blink_events)
        window60 = min(RECENT_WINDOW_SECONDS, max(self.visible_seconds, 1.0))
        window300 = min(SESSION_WINDOW_SECONDS, max(self.visible_seconds, 1.0))
        bpm60 = len(recent60) / window60 * 60.0
        bpm300 = len(recent300) / window300 * 60.0
        since_text = (
            f"{seconds_since_previous_blink:.2f}s"
            if seconds_since_previous_blink is not None
            else "na"
        )
        line = (
            f"time={iso_timestamp()} visible={fmt(visible_minutes)}min "
            f"visiblePct={fmt(visible_pct)} blinks={self.total_blinks} "
            f"sinceBlink={since_text} "
            f"longestNoBlink={fmt(self.longest_no_blink_visible_seconds)}s "
            f"bpm60={fmt(bpm60)} bpm5m={fmt(bpm300)} bpmAll={fmt(lifetime_bpm)}"
        )

        print(line, flush=True)
        self._write_record(
            {
                "type": "blink",
                "session_id": self.session_id,
                "elapsed_sec": elapsed_seconds,
                "visible_sec": self.visible_seconds,
                "visible_pct": visible_pct,
                "blinks_total": self.total_blinks,
                "since_previous_blink_visible_sec": seconds_since_previous_blink,
                "longest_no_blink_visible_sec": self.longest_no_blink_visible_seconds,
                "blinks_60s": len(recent60),
                "blinks_5m": len(recent300),
                "bpm_60s": bpm60,
                "bpm_5m": bpm300,
                "bpm_all_visible": lifetime_bpm,
            },
        )

    def _prune_samples(self, force: bool) -> None:
        if (
            not force
            and self.visible_seconds - self.last_prune_visible_seconds < PRUNE_INTERVAL_SECONDS
        ):
            return
        self.last_prune_visible_seconds = self.visible_seconds
        cutoff = self.visible_seconds - SESSION_WINDOW_SECONDS
        while self.open_score_samples and self.open_score_samples[0].visible_seconds < cutoff:
            self.open_score_samples.popleft()
        while self.eye_open_samples and self.eye_open_samples[0].visible_seconds < cutoff:
            self.eye_open_samples.popleft()
        self._prune_sample_buckets(self.open_score_samples_by_gaze, cutoff)
        self._prune_sample_buckets(self.eye_open_samples_by_gaze, cutoff)
        while self.blink_events and self.blink_events[0].visible_seconds < cutoff:
            self.blink_events.popleft()

    def _prune_sample_buckets(
        self,
        buckets: dict[tuple[int, int], deque[TimedValue]],
        cutoff: float,
    ) -> None:
        empty_buckets: list[tuple[int, int]] = []
        for key, samples in buckets.items():
            while samples and samples[0].visible_seconds < cutoff:
                samples.popleft()
            if not samples:
                empty_buckets.append(key)
        for key in empty_buckets:
            del buckets[key]


class LatestFrameCamera:
    def __init__(
        self,
        cv2_module: Any,
        camera_index: int,
        width: int,
        height: int,
        fps: float,
    ) -> None:
        self.cv2 = cv2_module
        backend = getattr(self.cv2, "CAP_AVFOUNDATION", 0)
        capture = self.cv2.VideoCapture(camera_index, backend)
        if not capture.isOpened():
            capture.release()
            capture = self.cv2.VideoCapture(camera_index)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(f"cannot open camera index {camera_index}")
        self.capture = capture

        self.capture.set(self.cv2.CAP_PROP_FRAME_WIDTH, width)
        self.capture.set(self.cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.capture.set(self.cv2.CAP_PROP_FPS, fps)
        self.capture.set(self.cv2.CAP_PROP_BUFFERSIZE, 1)

        self._frame_ready = threading.Condition()
        self._running = threading.Event()
        self._running.set()
        self._latest_frame: Any | None = None
        self._latest_id = 0
        self._thread = threading.Thread(target=self._read_loop, name="blink-camera", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def read_latest_after(self, last_frame_id: int, timeout: float) -> tuple[int, Any | None]:
        with self._frame_ready:
            if self._latest_id == last_frame_id and self._running.is_set():
                self._frame_ready.wait(timeout)
            return self._latest_id, self._latest_frame

    def stop(self) -> None:
        self._running.clear()
        with self._frame_ready:
            self._frame_ready.notify_all()
        self._thread.join(timeout=2.0)
        with self._frame_ready:
            self._latest_frame = None
        self.capture.release()

    def _read_loop(self) -> None:
        while self._running.is_set():
            ok, frame = self.capture.read()
            if not ok:
                time.sleep(0.02)
                continue
            with self._frame_ready:
                self._latest_id += 1
                self._latest_frame = frame
                self._frame_ready.notify_all()


def import_runtime_deps() -> tuple[Any, Any, Any, Any, Any]:
    require_network_sandbox()
    matplotlib_cache = Path(".build/matplotlib").resolve()
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))

    try:
        import cv2
        import mediapipe as mp
        import numpy as np
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision
    except ImportError as exc:
        raise RuntimeError(
            f"missing dependency: {exc}. Run `make setup` first."
        ) from exc
    missing_cv2 = [
        name
        for name in ("VideoCapture", "cvtColor", "COLOR_BGR2RGB", "CAP_PROP_FRAME_WIDTH")
        if not hasattr(cv2, name)
    ]
    if missing_cv2:
        raise RuntimeError(
            "OpenCV install is incomplete; missing "
            + ", ".join(missing_cv2)
            + ". Run `make setup` to repair the local venv."
        )
    return cv2, mp, np, python, vision


def landmark_distance(left: Any, right: Any) -> float:
    return math.hypot(float(left.x) - float(right.x), float(left.y) - float(right.y))


def eye_open_ratio(landmarks: Any, indices: tuple[int, int, int, int, int, int]) -> float | None:
    try:
        outer = landmarks[indices[0]]
        upper_outer = landmarks[indices[1]]
        upper_inner = landmarks[indices[2]]
        inner = landmarks[indices[3]]
        lower_inner = landmarks[indices[4]]
        lower_outer = landmarks[indices[5]]
    except (IndexError, TypeError):
        return None

    horizontal = landmark_distance(outer, inner)
    if horizontal <= 1e-6:
        return None
    vertical = (
        landmark_distance(upper_outer, lower_outer)
        + landmark_distance(upper_inner, lower_inner)
    ) * 0.5
    return vertical / horizontal


def blendshape_metrics(result: Any) -> FrameMetrics:
    face_count = len(result.face_landmarks or [])
    if face_count != 1 or not result.face_blendshapes:
        return FrameMetrics(face_count=face_count)

    landmarks = result.face_landmarks[0]
    eye_open_left = eye_open_ratio(landmarks, LEFT_EYE_INDICES)
    eye_open_right = eye_open_ratio(landmarks, RIGHT_EYE_INDICES)
    blink_left = blink_right = None
    look_down_left = look_down_right = look_up_left = look_up_right = None
    look_in_left = look_in_right = look_out_left = look_out_right = None
    for category in result.face_blendshapes[0]:
        match category.category_name:
            case "eyeBlinkLeft":
                blink_left = float(category.score)
            case "eyeBlinkRight":
                blink_right = float(category.score)
            case "eyeLookDownLeft":
                look_down_left = float(category.score)
            case "eyeLookDownRight":
                look_down_right = float(category.score)
            case "eyeLookUpLeft":
                look_up_left = float(category.score)
            case "eyeLookUpRight":
                look_up_right = float(category.score)
            case "eyeLookInLeft":
                look_in_left = float(category.score)
            case "eyeLookInRight":
                look_in_right = float(category.score)
            case "eyeLookOutLeft":
                look_out_left = float(category.score)
            case "eyeLookOutRight":
                look_out_right = float(category.score)

    return FrameMetrics(
        face_count=face_count,
        blink_left=blink_left,
        blink_right=blink_right,
        look_down_left=look_down_left,
        look_down_right=look_down_right,
        look_up_left=look_up_left,
        look_up_right=look_up_right,
        look_in_left=look_in_left,
        look_in_right=look_in_right,
        look_out_left=look_out_left,
        look_out_right=look_out_right,
        eye_open_left=eye_open_left,
        eye_open_right=eye_open_right,
    )


def make_landmarker(config: Config, python_tasks: Any, vision: Any) -> Any:
    model_path = config.model_path.expanduser()
    if not model_path.exists():
        raise RuntimeError(
            f"model missing: {model_path}. Run `make setup` to download it locally."
        )

    options = vision.FaceLandmarkerOptions(
        base_options=python_tasks.BaseOptions(
            model_asset_path=str(model_path),
            delegate=python_tasks.BaseOptions.Delegate.CPU,
        ),
        running_mode=vision.RunningMode.VIDEO,
        num_faces=config.max_faces,
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=False,
        min_face_detection_confidence=config.min_face_detection_confidence,
        min_face_presence_confidence=config.min_face_presence_confidence,
        min_tracking_confidence=config.min_tracking_confidence,
    )
    return vision.FaceLandmarker.create_from_options(options)


def limit_opencv_threads(cv2_module: Any) -> None:
    set_num_threads = getattr(cv2_module, "setNumThreads", None)
    if callable(set_num_threads):
        set_num_threads(1)


def run_detector(config: Config = CONFIG) -> int:
    cv2, mp, np, python_tasks, vision = import_runtime_deps()
    limit_opencv_threads(cv2)

    log_path = config.log_path.expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    camera: LatestFrameCamera | None = None
    landmarker: Any | None = None
    try:
        with log_path.open("a", encoding="utf-8") as log_handle:
            landmarker = make_landmarker(config, python_tasks, vision)
            camera = LatestFrameCamera(
                cv2,
                config.camera_index,
                config.width,
                config.height,
                config.fps,
            )
            camera.start()

            counter = BlinkCounter(config, log_handle)
            counter.log_session_start()
            print(
                f"running engine=mediapipe camera_index={config.camera_index} "
                f"fps={int(config.fps)} size={config.width}x{config.height}",
                flush=True,
            )
            print(f"logging stats to {log_path}", flush=True)
            print(
                "fields: one line per counted blink; bpm uses face-visible time; Ctrl-C to stop",
                flush=True,
            )

            stop = False

            def stop_signal(_signum: int, _frame: Any) -> None:
                nonlocal stop
                stop = True

            signal.signal(signal.SIGINT, stop_signal)
            signal.signal(signal.SIGTERM, stop_signal)

            last_frame_id = -1
            last_processed_seconds = 0.0
            frame_interval = 1.0 / config.fps
            timestamp_start = time.monotonic()

            while not stop:
                now = time.monotonic()
                next_process_seconds = last_processed_seconds + frame_interval
                if last_processed_seconds > 0 and now < next_process_seconds:
                    time.sleep(min(0.01, next_process_seconds - now))
                    continue

                frame_id, frame = camera.read_latest_after(last_frame_id, timeout=0.1)
                if frame is None or frame_id == last_frame_id:
                    continue

                last_frame_id = frame_id
                now = time.monotonic()
                last_processed_seconds = now
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                rgb = np.ascontiguousarray(rgb)
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                timestamp_ms = int((now - timestamp_start) * 1000)
                result = landmarker.detect_for_video(image, timestamp_ms)
                counter.update(blendshape_metrics(result), now)
                frame = None
                rgb = None
                image = None
                result = None
            counter.log_session_end(time.monotonic())
        return 0
    finally:
        if camera is not None:
            camera.stop()
        if landmarker is not None:
            landmarker.close()


def check_runtime(config: Config = CONFIG) -> int:
    cv2, _mp, _np, python_tasks, vision = import_runtime_deps()
    limit_opencv_threads(cv2)
    landmarker = make_landmarker(config, python_tasks, vision)
    landmarker.close()
    print("runtime check passed")
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        print(
            "error: blink-detector has no command-line options; edit CONFIG in blink_detector.py",
            file=sys.stderr,
        )
        return 2
    try:
        validate_config(CONFIG)
        return run_detector(CONFIG)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
