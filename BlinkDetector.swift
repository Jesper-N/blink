import AVFoundation
import CoreGraphics
import CoreMedia
import CoreVideo
import Foundation
import Vision

struct Config {
    var cameraIndex = 0
    var fps = 30.0
    var reportEvery = 10.0
    var calibrateSeconds = 8.0
    var minClosedFrames = 1
    var minBlinkSeconds = 0.015
    var maxBlinkSeconds = 0.80
    var blinkRateLimitSeconds = 2.0
    var closedThreshold: Double?
    var openThreshold: Double?
    var width: Int32 = 320
    var height: Int32 = 240
    var logFilePath: String? = "blink-stats.jsonl"
    var listCameras = false
    var showHelp = false

    static func parse(_ args: [String]) throws -> Config {
        var config = Config()
        var index = 1

        func value(after flag: String) throws -> String {
            guard index + 1 < args.count else {
                throw CLIError.badArgument("\(flag) needs a value")
            }
            index += 1
            return args[index]
        }

        func intValue(after flag: String) throws -> Int {
            let rawValue = try value(after: flag)
            guard let parsed = Int(rawValue) else {
                throw CLIError.badArgument("\(flag) needs an integer")
            }
            return parsed
        }

        func int32Value(after flag: String) throws -> Int32 {
            let rawValue = try value(after: flag)
            guard let parsed = Int32(rawValue) else {
                throw CLIError.badArgument("\(flag) needs an integer")
            }
            return parsed
        }

        func doubleValue(after flag: String) throws -> Double {
            let rawValue = try value(after: flag)
            guard let parsed = Double(rawValue) else {
                throw CLIError.badArgument("\(flag) needs a number")
            }
            return parsed
        }

        while index < args.count {
            let arg = args[index]
            switch arg {
            case "--camera-index":
                config.cameraIndex = try intValue(after: arg)
            case "--fps":
                config.fps = try doubleValue(after: arg)
            case "--report-every":
                config.reportEvery = try doubleValue(after: arg)
            case "--calibrate-seconds":
                config.calibrateSeconds = try doubleValue(after: arg)
            case "--min-closed-frames":
                config.minClosedFrames = try intValue(after: arg)
            case "--min-blink-sec":
                config.minBlinkSeconds = try doubleValue(after: arg)
            case "--max-blink-sec":
                config.maxBlinkSeconds = try doubleValue(after: arg)
            case "--blink-rate-limit-sec":
                config.blinkRateLimitSeconds = try doubleValue(after: arg)
            case "--closed-threshold":
                config.closedThreshold = try doubleValue(after: arg)
            case "--open-threshold":
                config.openThreshold = try doubleValue(after: arg)
            case "--width":
                config.width = try int32Value(after: arg)
            case "--height":
                config.height = try int32Value(after: arg)
            case "--log-file":
                config.logFilePath = try value(after: arg)
            case "--no-log-file":
                config.logFilePath = nil
            case "--list-cameras":
                config.listCameras = true
            case "--help", "-h":
                config.showHelp = true
            default:
                throw CLIError.badArgument("unknown option: \(arg)")
            }
            index += 1
        }

        guard config.cameraIndex >= 0 else { throw CLIError.badArgument("--camera-index must be >= 0") }
        guard config.fps > 0, config.fps <= 60 else { throw CLIError.badArgument("--fps must be 1...60") }
        guard config.reportEvery > 0 else { throw CLIError.badArgument("--report-every must be > 0") }
        guard config.calibrateSeconds >= 0 else { throw CLIError.badArgument("--calibrate-seconds must be >= 0") }
        guard config.minClosedFrames >= 1 else { throw CLIError.badArgument("--min-closed-frames must be >= 1") }
        guard config.minBlinkSeconds > 0 else { throw CLIError.badArgument("--min-blink-sec must be > 0") }
        guard config.maxBlinkSeconds > config.minBlinkSeconds else {
            throw CLIError.badArgument("--max-blink-sec must be greater than --min-blink-sec")
        }
        guard config.blinkRateLimitSeconds >= 0 else {
            throw CLIError.badArgument("--blink-rate-limit-sec must be >= 0")
        }
        if let logFilePath = config.logFilePath, logFilePath.isEmpty {
            throw CLIError.badArgument("--log-file cannot be empty")
        }

        return config
    }
}

enum CLIError: Error, CustomStringConvertible {
    case badArgument(String)
    case camera(String)

    var description: String {
        switch self {
        case .badArgument(let message), .camera(let message):
            return message
        }
    }
}

func usage() -> String {
    """
    BlinkDetector: local-only blink rate counter for macOS webcam.

    Build:
      make

    Run:
      ./blink-detector

    Options:
      --list-cameras              List local video devices, then exit
      --camera-index N            Camera index from --list-cameras (default: 0)
      --fps N                     Process frames per second, 1...60 (default: 30)
      --report-every SEC          Print status interval (default: 10)
      --calibrate-seconds SEC     Visible-face calibration time (default: 8)
      --min-closed-frames N       Closed-eye frames needed before blink (default: 1)
      --closed-threshold R        Manual closed-eye ratio threshold
      --open-threshold R          Manual open-eye ratio threshold
      --min-blink-sec SEC         Minimum blink duration (default: 0.015)
      --max-blink-sec SEC         Maximum blink duration (default: 0.80)
      --blink-rate-limit-sec SEC  Minimum seconds between counted blinks (default: 2.0)
      --width N                   Requested capture width (default: 320)
      --height N                  Requested capture height (default: 240)
      --log-file PATH             Append JSONL stats to file (default: blink-stats.jsonl)
      --no-log-file               Disable file logging
      --help                      Show help

    Privacy:
      No network APIs. No screenshots. No image/video files. No preview window.
      Frames stay in process memory and are discarded after landmark analysis.
      Log file stores only compact derived metrics needed for later analysis.
    """
}

func videoDevices() -> [AVCaptureDevice] {
    let discovery = AVCaptureDevice.DiscoverySession(
        deviceTypes: [.builtInWideAngleCamera, .external],
        mediaType: .video,
        position: .unspecified
    )
    if discovery.devices.isEmpty, let fallback = AVCaptureDevice.default(for: .video) {
        return [fallback]
    }
    return discovery.devices
}

func listCameras() {
    let devices = videoDevices()
    if devices.isEmpty {
        print("No video cameras found.")
        return
    }

    for (index, device) in devices.enumerated() {
        print("[\(index)] \(device.localizedName) (\(device.uniqueID))")
    }
}

func nowSeconds() -> Double {
    ProcessInfo.processInfo.systemUptime
}

func percentile(_ values: [Double], _ p: Double) -> Double? {
    guard !values.isEmpty else { return nil }
    return percentile(sortedValues: values.sorted(), p)
}

func percentile(sortedValues: [Double], _ p: Double) -> Double? {
    guard !sortedValues.isEmpty else { return nil }
    let clamped = min(max(p, 0.0), 1.0)
    let position = clamped * Double(sortedValues.count - 1)
    let lower = Int(floor(position))
    let upper = Int(ceil(position))
    if lower == upper {
        return sortedValues[lower]
    }
    let fraction = position - Double(lower)
    return sortedValues[lower] + (sortedValues[upper] - sortedValues[lower]) * fraction
}

struct ValueStats {
    let count: Int
    let min: Double?
    let max: Double?
    let avg: Double?
    let p10: Double?
    let p50: Double?
    let p90: Double?
}

func valueStats(_ values: [Double]) -> ValueStats {
    guard !values.isEmpty else {
        return ValueStats(count: 0, min: nil, max: nil, avg: nil, p10: nil, p50: nil, p90: nil)
    }

    let sorted = values.sorted()
    let sum = values.reduce(0.0, +)
    return ValueStats(
        count: values.count,
        min: sorted.first,
        max: sorted.last,
        avg: sum / Double(values.count),
        p10: percentile(sortedValues: sorted, 0.10),
        p50: percentile(sortedValues: sorted, 0.50),
        p90: percentile(sortedValues: sorted, 0.90)
    )
}

func jsonNumber(_ value: Double?) -> Any {
    guard let value, value.isFinite else { return NSNull() }
    return value
}

func statsJSON(_ stats: ValueStats) -> [String: Any] {
    [
        "count": stats.count,
        "min": jsonNumber(stats.min),
        "max": jsonNumber(stats.max),
        "avg": jsonNumber(stats.avg),
        "p10": jsonNumber(stats.p10),
        "p50": jsonNumber(stats.p50),
        "p90": jsonNumber(stats.p90)
    ]
}

func expandedLogPath(_ path: String) -> String {
    if path == "~" {
        return FileManager.default.homeDirectoryForCurrentUser.path
    }
    if path.hasPrefix("~/") {
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(String(path.dropFirst(2)))
            .path
    }
    return path
}

final class StatsLogger {
    let path: String
    private let handle: FileHandle
    private let timestampFormatter = ISO8601DateFormatter()
    private let newline = Data("\n".utf8)

    init(path: String) throws {
        let expandedPath = expandedLogPath(path)
        let url = URL(fileURLWithPath: expandedPath)
        let directory = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: url.path) {
            _ = FileManager.default.createFile(atPath: url.path, contents: nil)
        }

        self.path = url.path
        self.handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
    }

    deinit {
        try? handle.close()
    }

    func write(_ object: [String: Any]) {
        var record = object
        if record["ts"] == nil {
            record["ts"] = timestampFormatter.string(from: Date())
        }

        guard JSONSerialization.isValidJSONObject(record) else { return }

        do {
            let data = try JSONSerialization.data(withJSONObject: record)
            handle.write(data)
            handle.write(newline)
        } catch {
            fputs("log write failed: \(error)\n", stderr)
        }
    }
}

struct TimedValue {
    let visibleSeconds: Double
    let value: Double
}

struct BlinkEvent {
    let visibleSeconds: Double
    let durationSeconds: Double
}

func percent(_ numerator: Int, _ denominator: Int) -> Double {
    guard denominator > 0 else { return 0.0 }
    return Double(numerator) / Double(denominator) * 100.0
}

final class BlinkCounter {
    private enum EyeState {
        case open
        case closed
    }

    private let config: Config
    private let sessionID = UUID().uuidString
    private let startSeconds = nowSeconds()
    private let logger: StatsLogger?
    private var baselineOpenRatio: Double?
    private var calibrationSamples: [Double] = []
    private var visibleSeconds = 0.0
    private var lastVisibleFrameSeconds: Double?
    private var lastReportSeconds = nowSeconds()
    private var totalBlinks = 0
    private var blinkEvents: [BlinkEvent] = []
    private var eyeState = EyeState.open
    private var closedFrameCount = 0
    private var blinkStartedSeconds: Double?
    private var lastCountedBlinkSeconds: Double?
    private var lastEyeRatio: Double?
    private var faceVisible = false
    private var processedFrames = 0
    private var framesAtLastReport = 0
    private var visibleFrames = 0
    private var noFaceFrames = 0
    private var multiFaceFrames = 0
    private var noLandmarkFrames = 0
    private var eyeRatioSamples: [TimedValue] = []
    private var blinkDurationSumSeconds = 0.0
    private var lastBlinkDurationSeconds: Double?
    private var lastFaceCount = 0

    init(config: Config) throws {
        self.config = config
        if let logFilePath = config.logFilePath {
            self.logger = try StatsLogger(path: logFilePath)
        } else {
            self.logger = nil
        }
    }

    var logPath: String? {
        logger?.path
    }

    func printTerminalFieldLegend() {
        print("fields: face visible; faceCount detected faces; procFps processed frames/s; visible face-visible minutes; visiblePct face-visible wall-time%; blinks counted; bpm60/bpm5m/bpmAll blink rates using visible time; eye current eye-open ratio; eye60 p10/p50/p90; blinkMs60 avg/p90 milliseconds")
        fflush(stdout)
    }

    func logSessionStart() {
        logger?.write([
            "type": "session_start",
            "session_id": sessionID,
            "config": [
                "fps": config.fps,
                "report_every_sec": config.reportEvery,
                "calibrate_seconds": config.calibrateSeconds,
                "min_closed_frames": config.minClosedFrames,
                "min_blink_sec": config.minBlinkSeconds,
                "max_blink_sec": config.maxBlinkSeconds,
                "blink_rate_limit_sec": config.blinkRateLimitSeconds,
                "closed_threshold_manual": jsonNumber(config.closedThreshold),
                "open_threshold_manual": jsonNumber(config.openThreshold),
                "width": Int(config.width),
                "height": Int(config.height)
            ],
            "privacy": [
                "contains_frames": false,
                "contains_images": false,
                "contains_landmark_points": false,
                "contains_derived_metrics_only": true
            ],
            "report_fields": [
                "elapsed_sec": "seconds since run started",
                "visible_sec": "seconds with one visible face and eye landmarks",
                "visible_pct": "percent of wall time with visible face",
                "face_visible": "whether current processed frame is countable",
                "face_count": "faces detected in current processed frame",
                "processed_fps": "actual landmark frames processed per second",
                "frame_quality_pct": "visible/no-face/multi-face/no-landmark frame percentages",
                "blinks_total": "counted blinks after burst de-duplication",
                "blinks_60s": "counted blinks in last 60 visible seconds",
                "blinks_5m": "counted blinks in last 5 visible minutes",
                "bpm_60s": "blink rate over last 60 visible seconds",
                "bpm_5m": "blink rate over last 5 visible minutes",
                "bpm_all_visible": "blink rate over all visible time",
                "blink_duration_ms_60s": "blink duration stats in last 60 visible seconds",
                "blink_duration_ms_5m": "blink duration stats in last 5 visible minutes",
                "eye_ratio_current": "current eye-open ratio",
                "eye_ratio_baseline": "calibrated open-eye ratio",
                "eye_ratio_thresholds": "closed/open thresholds used for blink detection",
                "eye_ratio_60s": "eye-open ratio stats in last 60 visible seconds",
                "eye_ratio_5m": "eye-open ratio stats in last 5 visible minutes",
                "calibrating": "true until calibration has enough visible-face data"
            ]
        ])
    }

    func update(face: VNFaceObservation?, faceCount: Int, nowSeconds: Double) {
        processedFrames += 1
        lastFaceCount = faceCount

        guard
            let face,
            let ratio = eyeOpenRatio(face: face)
        else {
            faceVisible = false
            lastEyeRatio = nil
            if faceCount == 0 {
                noFaceFrames += 1
            } else if faceCount > 1 {
                multiFaceFrames += 1
            } else {
                noLandmarkFrames += 1
            }
            lastVisibleFrameSeconds = nil
            resetPartialBlink()
            reportIfNeeded(nowSeconds: nowSeconds)
            return
        }

        faceVisible = true
        visibleFrames += 1
        lastEyeRatio = ratio
        if let last = lastVisibleFrameSeconds, nowSeconds - last <= 1.0 {
            visibleSeconds += nowSeconds - last
        }
        lastVisibleFrameSeconds = nowSeconds
        eyeRatioSamples.append(TimedValue(visibleSeconds: visibleSeconds, value: ratio))

        if needsCalibration {
            calibrationSamples.append(ratio)
            finishCalibrationIfReady()
            reportIfNeeded(nowSeconds: nowSeconds)
            return
        }

        updateBlinkState(ratio: ratio, nowSeconds: nowSeconds)
        reportIfNeeded(nowSeconds: nowSeconds)
    }

    private var needsCalibration: Bool {
        config.calibrateSeconds > 0
            && baselineOpenRatio == nil
            && (config.closedThreshold == nil || config.openThreshold == nil)
    }

    private func finishCalibrationIfReady() {
        guard visibleSeconds >= config.calibrateSeconds else { return }
        guard let baseline = percentile(calibrationSamples, 0.70) else { return }
        baselineOpenRatio = baseline
        print("calibrated open-eye-ratio=\(format(baseline)) closed-threshold=\(format(closedThreshold)) open-threshold=\(format(openThreshold))")
        fflush(stdout)
    }

    private var closedThreshold: Double {
        if let manual = config.closedThreshold { return manual }
        guard let baseline = baselineOpenRatio else { return 0.16 }
        return max(0.05, baseline * 0.72)
    }

    private var openThreshold: Double {
        if let manual = config.openThreshold { return manual }
        guard let baseline = baselineOpenRatio else { return 0.20 }
        return max(closedThreshold + 0.02, baseline * 0.86)
    }

    private func updateBlinkState(ratio: Double, nowSeconds: Double) {
        if ratio < closedThreshold {
            closedFrameCount += 1
            if eyeState == .open && closedFrameCount >= config.minClosedFrames {
                eyeState = .closed
                blinkStartedSeconds = nowSeconds
            }
            return
        }

        if ratio > openThreshold {
            if eyeState == .closed, let start = blinkStartedSeconds {
                let duration = nowSeconds - start
                if duration >= config.minBlinkSeconds && duration <= config.maxBlinkSeconds {
                    countBlinkIfAllowed(duration: duration, nowSeconds: nowSeconds)
                }
            }

            eyeState = .open
            closedFrameCount = 0
            blinkStartedSeconds = nil
            adaptBaseline(with: ratio)
        }
    }

    private func countBlinkIfAllowed(duration: Double, nowSeconds: Double) {
        if
            let lastCountedBlinkSeconds,
            nowSeconds - lastCountedBlinkSeconds < config.blinkRateLimitSeconds
        {
            return
        }

        totalBlinks += 1
        lastCountedBlinkSeconds = nowSeconds
        blinkEvents.append(BlinkEvent(visibleSeconds: visibleSeconds, durationSeconds: duration))
        blinkDurationSumSeconds += duration
        lastBlinkDurationSeconds = duration
        pruneBlinkEvents()
    }

    private func adaptBaseline(with ratio: Double) {
        guard config.closedThreshold == nil, config.openThreshold == nil else { return }
        if let baseline = baselineOpenRatio {
            baselineOpenRatio = baseline * 0.995 + ratio * 0.005
        } else if config.calibrateSeconds == 0 {
            baselineOpenRatio = ratio
        }
    }

    private func resetPartialBlink() {
        eyeState = .open
        closedFrameCount = 0
        blinkStartedSeconds = nil
    }

    private func pruneBlinkEvents() {
        let cutoff = visibleSeconds - 300.0
        blinkEvents.removeAll { $0.visibleSeconds < cutoff }
    }

    private func pruneWindowSamples() {
        let cutoff = visibleSeconds - 300.0
        eyeRatioSamples.removeAll { $0.visibleSeconds < cutoff }
    }

    private func reportIfNeeded(nowSeconds: Double) {
        guard nowSeconds - lastReportSeconds >= config.reportEvery else { return }
        let reportInterval = nowSeconds - lastReportSeconds
        lastReportSeconds = nowSeconds
        pruneBlinkEvents()
        pruneWindowSamples()

        let elapsedSeconds = max(nowSeconds - startSeconds, 0.001)
        let visibleMinutes = visibleSeconds / 60.0
        let visiblePct = min(100.0, max(0.0, visibleSeconds / elapsedSeconds * 100.0))
        let lifetimeBPM = visibleSeconds > 0 ? Double(totalBlinks) / visibleSeconds * 60.0 : 0.0
        let window60Seconds = min(60.0, max(visibleSeconds, 1.0))
        let window300Seconds = min(300.0, max(visibleSeconds, 1.0))
        let cutoff60 = visibleSeconds - 60.0
        let recent60Blinks = blinkEvents.filter { $0.visibleSeconds >= cutoff60 }
        let recent300Blinks = blinkEvents
        let rolling60BPM = Double(recent60Blinks.count) / window60Seconds * 60.0
        let rolling300BPM = Double(recent300Blinks.count) / window300Seconds * 60.0
        let processedFPS = reportInterval > 0
            ? Double(processedFrames - framesAtLastReport) / reportInterval
            : 0.0
        framesAtLastReport = processedFrames
        let eye60Stats = valueStats(eyeRatioSamples.filter { $0.visibleSeconds >= cutoff60 }.map(\.value))
        let eye300Stats = valueStats(eyeRatioSamples.map(\.value))
        let blinkDuration60Stats = valueStats(recent60Blinks.map { $0.durationSeconds * 1000.0 })
        let blinkDuration300Stats = valueStats(recent300Blinks.map { $0.durationSeconds * 1000.0 })
        let blinkDurationAvgAllMS = totalBlinks > 0
            ? blinkDurationSumSeconds / Double(totalBlinks) * 1000.0
            : nil
        let calibrationText = baselineOpenRatio == nil && config.calibrateSeconds > 0
            && (config.closedThreshold == nil || config.openThreshold == nil)
            ? " calibrating=\(format(min(visibleSeconds, config.calibrateSeconds)))/\(format(config.calibrateSeconds))s"
            : ""
        let eyeText = lastEyeRatio.map { " eye=\(format($0))" } ?? ""
        let eyeP50Text = eye60Stats.p50.map { format($0) } ?? "na"
        let eyeP10Text = eye60Stats.p10.map { format($0) } ?? "na"
        let eyeP90Text = eye60Stats.p90.map { format($0) } ?? "na"
        let blinkAvgText = blinkDuration60Stats.avg.map { format($0) } ?? "na"
        let blinkP90Text = blinkDuration60Stats.p90.map { format($0) } ?? "na"

        let line = "face=\(faceVisible ? "yes" : "no") faceCount=\(lastFaceCount) procFps=\(format(processedFPS)) visible=\(format(visibleMinutes))min visiblePct=\(format(visiblePct)) blinks=\(totalBlinks) bpm60=\(format(rolling60BPM)) bpm5m=\(format(rolling300BPM)) bpmAll=\(format(lifetimeBPM))\(eyeText) eye60(p10/p50/p90)=\(eyeP10Text)/\(eyeP50Text)/\(eyeP90Text) blinkMs60(avg/p90)=\(blinkAvgText)/\(blinkP90Text)\(calibrationText)"
        print(line)
        fflush(stdout)

        logger?.write([
            "type": "report",
            "session_id": sessionID,
            "elapsed_sec": elapsedSeconds,
            "visible_sec": visibleSeconds,
            "visible_pct": visiblePct,
            "face_visible": faceVisible,
            "face_count": lastFaceCount,
            "processed_fps": processedFPS,
            "frame_quality_pct": [
                "visible": percent(visibleFrames, processedFrames),
                "no_face": percent(noFaceFrames, processedFrames),
                "multi_face": percent(multiFaceFrames, processedFrames),
                "no_landmarks": percent(noLandmarkFrames, processedFrames)
            ],
            "blinks_total": totalBlinks,
            "blinks_60s": recent60Blinks.count,
            "blinks_5m": recent300Blinks.count,
            "bpm_60s": rolling60BPM,
            "bpm_5m": rolling300BPM,
            "bpm_all_visible": lifetimeBPM,
            "last_blink_duration_ms": jsonNumber(lastBlinkDurationSeconds.map { $0 * 1000.0 }),
            "blink_duration_ms_60s": statsJSON(blinkDuration60Stats),
            "blink_duration_ms_5m": statsJSON(blinkDuration300Stats),
            "blink_duration_ms_avg_all": jsonNumber(blinkDurationAvgAllMS),
            "eye_ratio_current": jsonNumber(lastEyeRatio),
            "eye_ratio_baseline": jsonNumber(baselineOpenRatio),
            "eye_ratio_thresholds": [
                "closed": closedThreshold,
                "open": openThreshold
            ],
            "eye_ratio_60s": statsJSON(eye60Stats),
            "eye_ratio_5m": statsJSON(eye300Stats),
            "calibrating": baselineOpenRatio == nil && config.calibrateSeconds > 0
                && (config.closedThreshold == nil || config.openThreshold == nil),
            "calibration_visible_sec": min(visibleSeconds, config.calibrateSeconds)
        ])
    }

    private func eyeOpenRatio(face: VNFaceObservation) -> Double? {
        guard
            let landmarks = face.landmarks,
            let leftEye = landmarks.leftEye,
            let rightEye = landmarks.rightEye
        else {
            return nil
        }

        guard
            let leftRatio = regionOpenRatio(leftEye, face: face),
            let rightRatio = regionOpenRatio(rightEye, face: face)
        else {
            return nil
        }

        return (leftRatio + rightRatio) / 2.0
    }

    private func regionOpenRatio(_ region: VNFaceLandmarkRegion2D, face: VNFaceObservation) -> Double? {
        let points = region.normalizedPoints
        guard let first = points.first, points.count >= 4 else { return nil }

        var minX = first.x
        var maxX = first.x
        var minY = first.y
        var maxY = first.y

        for index in 1..<points.count {
            let point = points[index]
            minX = min(minX, point.x)
            maxX = max(maxX, point.x)
            minY = min(minY, point.y)
            maxY = max(maxY, point.y)
        }

        let width = (maxX - minX) * face.boundingBox.width
        let height = (maxY - minY) * face.boundingBox.height

        guard width > 0 else { return nil }
        return Double(height / width)
    }

    private func format(_ value: Double) -> String {
        String(format: "%.2f", value)
    }
}

final class CameraBlinkDetector: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let config: Config
    private let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "blink-detector.frames", qos: .userInitiated)
    private let sequenceHandler = VNSequenceRequestHandler()
    private let faceLandmarksRequest = VNDetectFaceLandmarksRequest()
    private let counter: BlinkCounter
    private var lastProcessedSeconds = 0.0

    init(config: Config) throws {
        self.config = config
        self.counter = try BlinkCounter(config: config)
        super.init()
    }

    func start() throws {
        let devices = videoDevices()
        guard devices.indices.contains(config.cameraIndex) else {
            throw CLIError.camera("camera index \(config.cameraIndex) not found; run --list-cameras")
        }

        let device = devices[config.cameraIndex]
        try configureDevice(device)

        session.beginConfiguration()
        let preset = capturePreset(width: config.width, height: config.height)
        session.sessionPreset = session.canSetSessionPreset(preset) ? preset : .low

        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else {
            throw CLIError.camera("cannot add camera input")
        }
        session.addInput(input)

        let output = AVCaptureVideoDataOutput()
        output.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarFullRange
        ]
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)
        guard session.canAddOutput(output) else {
            throw CLIError.camera("cannot add video output")
        }
        session.addOutput(output)

        if let connection = output.connection(with: .video), connection.isVideoMirroringSupported {
            connection.isVideoMirrored = true
        }

        session.commitConfiguration()
        session.startRunning()

        print("running camera=\"\(device.localizedName)\" fps=\(Int(config.fps)) size=\(config.width)x\(config.height)")
        if let logPath = counter.logPath {
            print("logging stats to \(logPath)")
        }
        print("keep face visible for calibration; press Ctrl-C to stop")
        fflush(stdout)
        counter.logSessionStart()
        counter.printTerminalFieldLegend()
    }

    private func configureDevice(_ device: AVCaptureDevice) throws {
        try device.lockForConfiguration()
        defer { device.unlockForConfiguration() }

        let supportsRequestedFPS = device.activeFormat.videoSupportedFrameRateRanges.contains { range in
            range.minFrameRate <= config.fps && config.fps <= range.maxFrameRate
        }

        if supportsRequestedFPS {
            let frameDuration = CMTime(value: 1, timescale: CMTimeScale(config.fps.rounded()))
            device.activeVideoMinFrameDuration = frameDuration
            device.activeVideoMaxFrameDuration = frameDuration
        }
    }

    private func capturePreset(width: Int32, height: Int32) -> AVCaptureSession.Preset {
        if width <= 352 && height <= 288 {
            return .low
        }
        if width <= 640 && height <= 480 {
            return .vga640x480
        }
        if width <= 1280 && height <= 720 {
            return .hd1280x720
        }
        return .high
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        autoreleasepool {
            let timestamp = nowSeconds()
            guard timestamp - lastProcessedSeconds >= 1.0 / config.fps else { return }
            lastProcessedSeconds = timestamp

            guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
                counter.update(face: nil, faceCount: 0, nowSeconds: timestamp)
                return
            }

            do {
                try sequenceHandler.perform([faceLandmarksRequest], on: pixelBuffer, orientation: .up)
                let faces = faceLandmarksRequest.results ?? []
                counter.update(face: faces.count == 1 ? faces[0] : nil, faceCount: faces.count, nowSeconds: timestamp)
            } catch {
                counter.update(face: nil, faceCount: 0, nowSeconds: timestamp)
            }
        }
    }
}

func ensureCameraPermission() -> Bool {
    switch AVCaptureDevice.authorizationStatus(for: .video) {
    case .authorized:
        return true
    case .notDetermined:
        let semaphore = DispatchSemaphore(value: 0)
        var granted = false
        AVCaptureDevice.requestAccess(for: .video) { allowed in
            granted = allowed
            semaphore.signal()
        }
        semaphore.wait()
        return granted
    case .denied, .restricted:
        return false
    @unknown default:
        return false
    }
}

do {
    let config = try Config.parse(CommandLine.arguments)
    if config.showHelp {
        print(usage())
        exit(0)
    }

    if config.listCameras {
        listCameras()
        exit(0)
    }

    guard ensureCameraPermission() else {
        fputs("camera permission denied. Enable Terminal/calling app under System Settings > Privacy & Security > Camera.\n", stderr)
        exit(2)
    }

    let detector = try CameraBlinkDetector(config: config)
    try detector.start()
    RunLoop.main.run()
} catch {
    fputs("error: \(error)\n\n\(usage())\n", stderr)
    exit(1)
}
