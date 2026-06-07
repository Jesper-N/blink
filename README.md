# Blink

Blink is a local webcam blink counter that runs entirely on your machine. It is meant for
tracking how often you blink over longer sessions, so you can spot whether you may be
under-blinking. I made it to check whether my dry eyes might be related to not blinking enough.

It uses MediaPipe Face Landmarker blendshapes and a simple eye aperture check to count full
blinks. It accounts for head tilt, roll, camera angle, forward/back movement, side-to-side
movement, and looking down or up, as long as one face stays visible to the webcam.

The project is intentionally small: one setup command, one run command, no command line tuning.
If you want different settings, edit `CONFIG` in `blink_detector.py`.

## privacy

- Webcam frames stay in memory.
- No frames, screenshots, preview windows, images, videos, or landmark dumps are written.
- Logs contain derived blink and gaze numbers only.
- The included launcher uses macOS `sandbox-exec` to deny outbound networking.
- The detector refuses to run through `python blink_detector.py`; use `./blink-detector`.
- Network is only used by `make setup` to install dependencies and download the local model.

## setup

```bash
make setup
```

This creates `.venv`, installs the Python dependencies, downloads the MediaPipe face model into
`models/`, and builds the `blink-detector` launcher.

## run

```bash
./blink-detector
```

The script prints one line per counted blink and appends JSONL stats to `blink-stats.jsonl`.

There are no runtime flags. This is deliberate. The launcher is also the privacy boundary, so
the normal path should stay boring and hard to misuse.

## output

Useful fields:

- `blinks`: total counted blinks
- `sinceBlink`: visible seconds since the previous counted blink
- `longestNoBlink`: longest visible-time gap without a blink in the session
- `bpm60`, `bpm5m`, `bpmAll`: blink rate using face-visible time
- `visible`: total face-visible time
- `visiblePct`: percent of elapsed time where one usable face was visible

The JSONL log also records `session_start` and `session_end` rows. The `session_end` row includes
the current no-blink gap at the moment the script stops.

## settings

Edit `CONFIG` near the top of `blink_detector.py` for camera index, FPS, frame size, thresholds,
model path, or log path.
