# Blink Detector

Local-only macOS webcam blink counter.

It uses Apple `AVFoundation` and `Vision` face landmarks to count blinks while you work. It only counts when exactly one face and at least one eye landmark are visible, adapts to head tilt/side posture, logs derived stats to JSONL, and never saves or sends webcam frames.

## Privacy

- No cloud APIs.
- No screenshots.
- No preview window.
- No image or video files.
- No landmark point dumps.
- Frames stay in memory and are discarded after analysis.

## Build

```bash
make
```

## Run

```bash
./blink-detector
```

Longer session:

```bash
./blink-detector --report-every 30 --log-file blink-stats.jsonl
```

If camera permission is denied:

```text
System Settings > Privacy & Security > Camera
```

## Output

Example:

```text
face=yes faceCount=1 procFps=29.80 visible=12.50min visiblePct=96.00 blinks=180 bpm60=14.00 bpm5m=14.20 bpmAll=14.40 eye=0.27 eye60(p10/p50/p90)=0.18/0.27/0.31 blinkMs60(avg/p90)=116.00/180.00
```

Important fields:

- `blinks`: total counted blinks after 2-second burst de-duplication
- `bpm60`: blink rate over last 60 visible seconds
- `bpm5m`: blink rate over last 5 visible minutes
- `bpmAll`: blink rate over all visible time
- `visiblePct`: percent of runtime where your face was visible
- `procFps`: actual processed frames per second
- `eye`: current eye-open ratio; lower means more closed
- `blinkMs60`: blink duration stats in milliseconds

## Stats file

Default:

```text
blink-stats.jsonl
```

Use another file:

```bash
./blink-detector --log-file ~/blink-workday.jsonl
```

Disable file logging:

```bash
./blink-detector --no-log-file
```

The JSONL contains only derived metrics: blink counts, blink rates, visible-face time, eye-ratio stats, frame-quality percentages, calibration state, and config. No frames, images, video, screenshots, or landmark points.

## Common options

```bash
./blink-detector --list-cameras
./blink-detector --camera-index 1
./blink-detector --fps 45
./blink-detector --width 640 --height 480
./blink-detector --blink-rate-limit-sec 0
```

## Troubleshooting

- `face=no`: improve lighting, keep your head in webcam view, or move closer.
- Low `procFps`: close heavy apps, lower capture size, or lower `--fps`.
- Missed blinks: try `--fps 45`.
- Counts too low during deliberate rapid blinking: use `--blink-rate-limit-sec 0`.
