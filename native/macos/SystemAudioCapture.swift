@preconcurrency import AVFoundation
import CoreMedia
import Darwin
import Foundation
import ScreenCaptureKit

// Align both SCK audio outputs on the stream clock. A bounded hold-back absorbs
// ordinary callback skew; a stalled clock is an error, not an unbounded buffer.
private struct CaptureMixer {
    private static let capacity = 16_000 * 6
    private static let holdback: Int64 = 8_000
    private var ring = [Int32](repeating: 0, count: CaptureMixer.capacity)
    private var head: Int64?
    private var tail: Int64 = 0

    mutating func push(_ pcm: UnsafeBufferPointer<Int16>, at time: CMTime) throws -> [Int16] {
        let seconds = time.seconds
        guard seconds.isFinite, seconds >= 0, seconds < 1.0e10 else { throw CaptureError.badAudioFormat }
        let frame = Int64((seconds * 16_000).rounded())
        if head == nil { head = max(0, frame - Self.holdback); tail = frame }
        guard let start = head, frame + Int64(pcm.count) - start <= Int64(Self.capacity) else {
            throw CaptureError.usage("音声入力の時刻が大きくずれたため、収音を終了しました。")
        }
        for offset in pcm.indices {
            let destination = frame + Int64(offset)
            guard destination >= start else { continue }
            let slot = Int(destination % Int64(Self.capacity))
            ring[slot] += Int32(pcm[offset])
        }
        tail = max(tail, frame + Int64(pcm.count))
        return drain(until: tail - Self.holdback)
    }

    mutating func finish() -> [Int16] { drain(until: tail) }

    private mutating func drain(until end: Int64) -> [Int16] {
        guard let start = head, start < end else { return [] }
        var output = [Int16](); output.reserveCapacity(Int(end - start))
        for index in start..<end {
            let slot = Int(index % Int64(Self.capacity))
            output.append(Int16(clamping: ring[slot])); ring[slot] = 0
        }
        head = end
        return output
    }
}

// System capture never requests a mic unless the caller explicitly opts in.
@available(macOS 13.0, *)
final class AudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private let directory: URL?
    private let pcmStdout: Bool
    private let onFailure: (Error) -> Void
    private let lock = NSLock()
    private var samples: [Int16] = []
    private var sequence = 0
    var partCount: Int { sequence }
    private var failed: Error?
    private var stream: SCStream?
    private var microphoneEngine: AVAudioEngine?
    private var mixer: CaptureMixer?
    private let chunkSize = 16_000 * 5
    private let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)!

    init(directory: URL?, pcmStdout: Bool, onFailure: @escaping (Error) -> Void) {
        self.directory = directory; self.pcmStdout = pcmStdout; self.onFailure = onFailure
    }

    func start(filter: SCContentFilter, includeMicrophone: Bool, shouldStart: () -> Bool) async throws {
        let config = SCStreamConfiguration()
        config.width = 2; config.height = 2; config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.capturesAudio = true
        config.sampleRate = 16_000
        config.channelCount = 1
        config.excludesCurrentProcessAudio = true
        if includeMicrophone {
            guard #available(macOS 15.0, *) else { throw CaptureError.usage("Mac再生音とマイクの同時収音にはmacOS 15以降が必要です。") }
            try await Self.allowMicrophone()
            config.captureMicrophone = true
            mixer = CaptureMixer()
        }
        guard shouldStart() else { throw CaptureError.cancelled }
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        let queue = DispatchQueue(label: "system-audio.capture", qos: .userInitiated)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        if #available(macOS 15.0, *), includeMicrophone {
            try stream.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue)
        }
        self.stream = stream
        try await stream.startCapture()
    }

    private static func allowMicrophone() async throws {
        guard await AVCaptureDevice.requestAccess(for: .audio) else {
            throw CaptureError.usage("マイクへのアクセスが許可されていません。システム設定のマイク権限を確認してください。")
        }
    }

    func startMicrophoneOnly(shouldStart: () -> Bool) async throws {
        try await Self.allowMicrophone()
        guard shouldStart() else { throw CaptureError.cancelled }
        let engine = AVAudioEngine()
        let node = engine.inputNode
        let format = node.outputFormat(forBus: 0)
        guard format.channelCount > 0, format.sampleRate > 0 else {
            throw CaptureError.usage("マイク入力がありません。Macのサウンド入力を確認してください。")
        }
        node.installTap(onBus: 0, bufferSize: 2_048, format: nil) { [weak self] source, _ in
            guard let self else { return }
            do {
                let converted = try Self.convert(source, target: self.target)
                guard let channel = converted.int16ChannelData?[0] else { throw CaptureError.badAudioFormat }
                try self.appendInput(UnsafeBufferPointer(start: channel, count: Int(converted.frameLength)))
            } catch { self.recordFailure(error) }
        }
        do { try engine.start() }
        catch { node.removeTap(onBus: 0); throw error }
        microphoneEngine = engine
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        if type != .audio {
            guard #available(macOS 15.0, *), type == .microphone else { return }
        }
        guard CMSampleBufferIsValid(sampleBuffer), let description = CMSampleBufferGetFormatDescription(sampleBuffer),
              CMAudioFormatDescriptionGetStreamBasicDescription(description) != nil else { recordFailure(CaptureError.badAudioFormat); return }
        // Preserve the supplied LPCM buffer layout before downmixing/resampling.
        do {
            let converted = try Self.convert(sampleBuffer, target: target)
            guard let channel = converted.int16ChannelData?[0] else { throw CaptureError.badAudioFormat }
            try appendInput(UnsafeBufferPointer(start: channel, count: Int(converted.frameLength)), at: sampleBuffer.presentationTimeStamp)
        } catch { recordFailure(error) }
    }

    fileprivate func appendInput(_ pcm: UnsafeBufferPointer<Int16>, at time: CMTime = .invalid) throws {
        lock.lock(); defer { lock.unlock() }
        guard failed == nil else { return }
        if mixer != nil {
            let aligned = try mixer!.push(pcm, at: time)
            samples.append(contentsOf: aligned)
        } else {
            samples.append(contentsOf: pcm)
        }
        while samples.count >= chunkSize { try writePending(chunkSize) }
    }

    // Keep the in-memory audio until the destination is committed.
    private func writePending(_ count: Int) throws {
        try write(Array(samples.prefix(count)))
        samples.removeFirst(count)
    }

    fileprivate static func convert(_ buffer: CMSampleBuffer, target: AVAudioFormat) throws -> AVAudioPCMBuffer {
        guard let description = CMSampleBufferGetFormatDescription(buffer),
              let asbdPointer = CMAudioFormatDescriptionGetStreamBasicDescription(description) else { throw CaptureError.badAudioFormat }
        guard let sourceFormat = AVAudioFormat(streamDescription: asbdPointer) else { throw CaptureError.badAudioFormat }
        let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(buffer))
        guard frames > 0, frames <= UInt32(Int32.max),
              let source = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: frames) else { throw CaptureError.badAudioFormat }
        source.frameLength = frames
        let copyStatus = CMSampleBufferCopyPCMDataIntoAudioBufferList(buffer, at: 0, frameCount: Int32(frames), into: source.mutableAudioBufferList)
        guard copyStatus == noErr else { throw CaptureError.badAudioFormat }
        return try convert(source, target: target)
    }

    private static func convert(_ source: AVAudioPCMBuffer, target: AVAudioFormat) throws -> AVAudioPCMBuffer {
        let sourceFormat = source.format
        guard let converter = AVAudioConverter(from: sourceFormat, to: target) else { throw CaptureError.badAudioFormat }
        converter.downmix = sourceFormat.channelCount > target.channelCount
        let capacity = AVAudioFrameCount(Double(source.frameLength) * target.sampleRate / sourceFormat.sampleRate + 64)
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { throw CaptureError.badAudioFormat }
        var supplied = false
        var conversionError: NSError?
        let conversionStatus = converter.convert(to: output, error: &conversionError) { _, inputStatus in
            if supplied { inputStatus.pointee = AVAudioConverterInputStatus.noDataNow; return nil }
            supplied = true; inputStatus.pointee = AVAudioConverterInputStatus.haveData; return source
        }
        if let conversionError { throw conversionError }
        guard conversionStatus != AVAudioConverterOutputStatus.error else { throw CaptureError.badAudioFormat }
        return output
    }

    private func write(_ pcm: [Int16]) throws {
        guard !pcm.isEmpty else { return }
        if pcmStdout {
            try writeAllToStdout(encodePCM16LE(pcm))
            sequence += 1
            return
        }
        guard let directory else { throw CaptureError.badOutput }
        let name = String(format: "part-%06d.wav", sequence + 1)
        let url = directory.appendingPathComponent(name)
        guard !FileManager.default.fileExists(atPath: url.path) else { throw CaptureError.existingOutput(name) }
        let wav = Self.encodeWav(pcm)
        try writeNewFile(wav, to: url)
        sequence += 1
    }
    static func encodeWav(_ pcm: [Int16]) -> Data {
        var wav = Data(); wav.append(contentsOf: Array("RIFF".utf8)); wav.appendLE(UInt32(36 + pcm.count * 2))
        wav.append(contentsOf: Array("WAVEfmt ".utf8)); wav.appendLE(UInt32(16)); wav.appendLE(UInt16(1)); wav.appendLE(UInt16(1))
        wav.appendLE(UInt32(16_000)); wav.appendLE(UInt32(32_000)); wav.appendLE(UInt16(2)); wav.appendLE(UInt16(16))
        wav.append(contentsOf: Array("data".utf8)); wav.appendLE(UInt32(pcm.count * 2))
        for sample in pcm { wav.appendLE(UInt16(bitPattern: sample)) }
        return wav
    }

    func finish() async throws {
        if let microphoneEngine {
            microphoneEngine.inputNode.removeTap(onBus: 0)
            microphoneEngine.stop()
        }
        if let stream {
            do { try await stream.stopCapture() }
            catch { recordFailure(error) }
        }
        try complete()
    }
    fileprivate func complete() throws {
        lock.lock(); defer { lock.unlock() }
        if let mixedTail = mixer?.finish() { samples.append(contentsOf: mixedTail) }
        if let failed {
            // Preserve the recoverable tail in the selected destination, but never
            // publish a completion marker or open a different destination.
            while !samples.isEmpty {
                do { try writePending(min(samples.count, chunkSize)) }
                catch { break }
            }
            throw failed
        }
        if !samples.isEmpty { try writePending(samples.count) }
        guard sequence > 0 else { throw CaptureError.noAudio }
        if pcmStdout { return }
        guard let directory else { throw CaptureError.badOutput }
        let marker = directory.appendingPathComponent("recording-complete.json")
        guard !FileManager.default.fileExists(atPath: marker.path) else { throw CaptureError.existingOutput(marker.lastPathComponent) }
        try writeNewFile(JSONSerialization.data(withJSONObject: ["version": 1, "parts": sequence], options: [.sortedKeys]), to: marker, isCompletionMarker: true)
    }
    func stream(_ stream: SCStream, didStopWithError error: Error) { recordFailure(error) }
    fileprivate func recordFailure(_ error: Error) {
        lock.lock(); let notify = failed == nil; if notify { failed = error }; lock.unlock()
        if notify { onFailure(error) }
    }
}

private final class CaptureStopCoordinator {
    private let lock = NSLock()
    private var source: DispatchSourceSignal?
    private var continuation: CheckedContinuation<Error?, Never>?
    private var isComplete = false
    private var terminalError: Error?

    init() {
        _ = Darwin.signal(SIGINT, SIG_IGN)
        let signalSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        source = signalSource
        signalSource.setEventHandler { [weak self] in self?.complete(error: nil) }
        signalSource.resume()
    }
    func wait() async -> Error? {
        await withCheckedContinuation { continuation in
            lock.lock()
            if isComplete { let error = terminalError; lock.unlock(); continuation.resume(returning: error) }
            else { self.continuation = continuation; lock.unlock() }
        }
    }
    func stop(error: Error) { complete(error: error) }
    func cancel() { complete(error: CaptureError.cancelled) }
    var isPending: Bool {
        lock.lock(); defer { lock.unlock() }
        return !isComplete
    }
    private func complete(error: Error?) {
        lock.lock()
        guard !isComplete else { lock.unlock(); return }
        isComplete = true; terminalError = error
        let continuation = self.continuation; self.continuation = nil
        let source = self.source; self.source = nil
        lock.unlock()
        source?.cancel()
        continuation?.resume(returning: error)
    }
}

private func writeAllToStdout(_ data: Data) throws {
    try data.withUnsafeBytes { raw in
        guard let base = raw.baseAddress else { return }
        var offset = 0
        while offset < raw.count {
            let written = Darwin.write(STDOUT_FILENO, base.advanced(by: offset), raw.count - offset)
            if written < 0 { if errno == EINTR { continue }; throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
            guard written > 0 else { throw POSIXError(.EIO) }
            offset += written
        }
    }
}

private func encodePCM16LE(_ samples: [Int16]) -> Data {
    var data = Data(); data.reserveCapacity(samples.count * 2)
    for sample in samples { data.appendLE(UInt16(bitPattern: sample)) }
    return data
}

private func status(_ message: String, pcmStdout: Bool) {
    let stream = pcmStdout ? stderr : stdout
    fputs(message + "\n", stream); fflush(stream)
}

private enum CaptureError: Error, LocalizedError {
    case badAudioFormat, badOutput, noAudio, existingOutput(String), usage(String), cancelled
    var errorDescription: String? {
        switch self {
        case .badAudioFormat: return "音声形式を16 kHz mono PCMへ変換できませんでした。"
        case .badOutput: return "出力先がありません。"
        case .noAudio: return "保存できる音声がありません。完了マーカーは作成していません。"
        case .existingOutput(let name): return "既存ファイルを上書きしません: \(name)"
        case .usage(let text): return text
        case .cancelled: return "収音処理が中断されました。"
        }
    }
}

private enum SourceSelection: Equatable { case wholeMac, application(String), microphone }
private struct CaptureOptions { let output: String?; let pcmStdout: Bool; let source: SourceSelection; let includeMicrophone: Bool }

private extension Data {
    mutating func appendLE<T: FixedWidthInteger>(_ value: T) { var little = value.littleEndian; Swift.withUnsafeBytes(of: &little) { append(contentsOf: $0) } }
}

private func writeNewFile(_ data: Data, to destination: URL, isCompletionMarker: Bool = false,
                          directorySync: (URL) throws -> Void = syncParentDirectory) throws {
    let temporary = destination.deletingLastPathComponent().appendingPathComponent(".\(destination.lastPathComponent).\(UUID().uuidString).tmp")
    let descriptor = Darwin.open(temporary.path, O_WRONLY | O_CREAT | O_EXCL, S_IRUSR | S_IWUSR)
    guard descriptor >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    var openDescriptor = true
    defer { if openDescriptor { _ = Darwin.close(descriptor) }; _ = Darwin.unlink(temporary.path) }
    try data.withUnsafeBytes { raw in
        guard let base = raw.baseAddress else { return }
        var offset = 0
        while offset < raw.count {
            let written = Darwin.write(descriptor, base.advanced(by: offset), raw.count - offset)
            if written < 0 { if errno == EINTR { continue }; throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
            guard written > 0 else { throw POSIXError(.EIO) }
            offset += written
        }
    }
    guard Darwin.fsync(descriptor) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    guard Darwin.close(descriptor) == 0 else { openDescriptor = false; throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    openDescriptor = false
    guard Darwin.link(temporary.path, destination.path) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    do { try directorySync(destination.deletingLastPathComponent()) }
    catch {
        let syncError = error
        if isCompletionMarker {
            _ = Darwin.unlink(destination.path)
            // Best-effort persistence of marker removal; report the original sync failure either way.
            try? directorySync(destination.deletingLastPathComponent())
        }
        throw syncError
    }
}

private func syncParentDirectory(_ directory: URL) throws {
    let descriptor = Darwin.open(directory.path, O_RDONLY | O_DIRECTORY)
    guard descriptor >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    defer { _ = Darwin.close(descriptor) }
    guard Darwin.fsync(descriptor) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
}

@main struct SystemAudioCapture {
    static func main() async {
        if #available(macOS 13.0, *) {
            do { try await run() }
            catch { fputs("収音失敗: \(error.localizedDescription)\n", stderr); exit(1) }
        } else { fputs("ScreenCaptureKit音声収音にはmacOS 13以降が必要です。\n", stderr); exit(1) }
    }
    static func run() async throws {
        let args = Array(CommandLine.arguments.dropFirst())
        if args.contains("--self-test") {
            guard args == ["--self-test"] else { throw CaptureError.usage("--self-test と他の引数は併用できません。") }
            try selfTest(); print("self-test: PASS"); return
        }
        if args == ["--self-test-pcm-stdout"] {
            try writeAllToStdout(encodePCM16LE([1, -2])); return
        }
        if args.contains("--self-test-sigint") {
            guard args.count == 3, args[0] == "--self-test-sigint", args[1] == "--output" else { throw CaptureError.usage("合成SIGINTテストの引数を確認してください。") }
            let output = URL(fileURLWithPath: args[2], isDirectory: true)
            try FileManager.default.createDirectory(at: output, withIntermediateDirectories: false)
            fputs("synthetic recorder ready; send SIGINT\n", stdout); fflush(stdout)
            let coordinator = CaptureStopCoordinator()
            defer { coordinator.cancel() }
            guard await coordinator.wait() == nil else { throw CaptureError.cancelled }
            let pcm = [Int16](repeating: 123, count: 160)
            try writeNewFile(AudioRecorder.encodeWav(pcm), to: output.appendingPathComponent("part-000001.wav"))
            try writeNewFile(JSONSerialization.data(withJSONObject: ["version": 1, "parts": 1], options: [.sortedKeys]), to: output.appendingPathComponent("recording-complete.json"), isCompletionMarker: true)
            print("synthetic SIGINT finalize: PASS"); return
        }
        if args == ["--list-apps"] { throw CaptureError.usage("--list-apps はScreen Recordingの許可を促す可能性があるため、この版では無効です。アプリのBundle IDを指定してください。") }
        let options = try parseArguments(args)
        let output: URL?
        if let outputPath = options.output {
            let resolved = URL(fileURLWithPath: outputPath, isDirectory: true).standardizedFileURL
            guard !FileManager.default.fileExists(atPath: resolved.path) else { throw CaptureError.usage("出力先は新規フォルダにしてください。既存内容は変更しません: \(resolved.path)") }
            output = resolved
        } else { output = nil; _ = Darwin.signal(SIGPIPE, SIG_IGN) }
        let coordinator = CaptureStopCoordinator()
        defer { coordinator.cancel() }
        let filter: SCContentFilter?
        if options.source == .microphone {
            filter = nil
        } else {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let display = content.displays.first else { throw CaptureError.usage("共有可能な画面がありません。") }
            if case let .application(bundleID) = options.source {
                guard let app = content.applications.first(where: { $0.bundleIdentifier == bundleID }) else {
                    throw CaptureError.usage("指定したアプリが見つかりません: \(bundleID)。Mac全体には切り替えません。")
                }
                filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])
            } else {
                filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
                status("収音対象: Mac全体のシステム音声（基準画面ID \(display.displayID)、複数画面の範囲は未検証）", pcmStdout: options.pcmStdout)
            }
        }
        guard coordinator.isPending else { throw CaptureError.cancelled }
        if let output { try FileManager.default.createDirectory(at: output, withIntermediateDirectories: false) }
        let recorder = AudioRecorder(directory: output, pcmStdout: options.pcmStdout) { coordinator.stop(error: $0) }
        if let filter {
            try await recorder.start(filter: filter, includeMicrophone: options.includeMicrophone) { coordinator.isPending }
        } else {
            try await recorder.startMicrophoneOnly { coordinator.isPending }
        }
        if coordinator.isPending {
            fputs("WORKBENCH_CAPTURE_READY\n", stderr); fflush(stderr)
        }
        status(options.pcmStdout ? "PCM16LEを標準出力へ送信中。Ctrl-Cで停止します。" : "収音中。Ctrl-Cで停止して保存します。出力: \(output!.path)", pcmStdout: options.pcmStdout)
        if let failure = await coordinator.wait() {
            do { try await recorder.finish() } catch { throw failure }
            throw failure
        }
        try await recorder.finish()
        if !options.pcmStdout { status("保存完了: \(output!.path) (\(recorder.partCount) parts)", pcmStdout: false) }
    }
    private static func parseArguments(_ args: [String]) throws -> CaptureOptions {
        var output: String?, pcmStdout = false, confirmation = false, source: SourceSelection?, includeMicrophone = false
        var index = 0
        while index < args.count {
            switch args[index] {
            case "--output":
                guard output == nil, index + 1 < args.count, !args[index + 1].hasPrefix("--") else { throw CaptureError.usage("--output は一度だけ指定し、値を付けてください。") }
                output = args[index + 1]; index += 2
            case "--pcm-stdout":
                guard !pcmStdout else { throw CaptureError.usage("--pcm-stdout の重複指定はできません。") }
                pcmStdout = true; index += 1
            case "--confirm":
                guard !confirmation else { throw CaptureError.usage("--confirm の重複指定はできません。") }
                confirmation = true; index += 1
            case "--include-microphone":
                guard !includeMicrophone else { throw CaptureError.usage("--include-microphone の重複指定はできません。") }
                includeMicrophone = true; index += 1
            case "--microphone-only":
                guard source == nil else { throw CaptureError.usage("--microphone-only はシステム音声モードと併用できません。") }
                source = .microphone; index += 1
            case "--whole-mac":
                guard source == nil else { throw CaptureError.usage("収音対象は --whole-mac または --app-bundle-id の一方だけです。") }
                source = .wholeMac; index += 1
            case "--app-bundle-id":
                guard source == nil, index + 1 < args.count, !args[index + 1].isEmpty, !args[index + 1].hasPrefix("--") else { throw CaptureError.usage("--app-bundle-id は一度だけBundle IDを付けてください。") }
                source = .application(args[index + 1]); index += 2
            default: throw CaptureError.usage("未対応または重複した引数です: \(args[index])")
            }
        }
        guard (output != nil) != pcmStdout, confirmation, let source,
              !(includeMicrophone && source == .microphone) else { throw CaptureError.usage("使い方: (--output DIR | --pcm-stdout) --confirm (--app-bundle-id BUNDLE_ID | --whole-mac) [--include-microphone] または --microphone-only") }
        return CaptureOptions(output: output, pcmStdout: pcmStdout, source: source, includeMicrophone: includeMicrophone)
    }

    private static func testPCMConversion(interleaved: Bool, channels: AVAudioChannelCount) throws {
        let frames: AVAudioFrameCount = 1_024
        let fixture = "\(channels)ch/\(interleaved ? "interleaved" : "planar")"
        func issue(_ reason: String) -> CaptureError { .usage("PCM self-test \(fixture): \(reason)") }
        guard let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000,
                                         channels: channels, interleaved: interleaved),
              let source = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames),
              let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true) else {
            throw issue("cannot prepare formats")
        }
        source.frameLength = frames
        guard let data = source.floatChannelData else { throw issue("cannot address samples") }
        for frame in 0..<Int(frames) {
            let sample = 0.5 * Float(sin(2 * Double.pi * Double(frame) / 48))
            if channels == 1 { data[0][frame] = sample }
            else if interleaved { data[0][2 * frame] = 0; data[0][2 * frame + 1] = sample }
            else { data[0][frame] = 0; data[1][frame] = sample }
        }
        var description: CMAudioFormatDescription?
        let describe = CMAudioFormatDescriptionCreate(allocator: kCFAllocatorDefault, asbd: format.streamDescription,
                  layoutSize: 0, layout: nil, magicCookieSize: 0, magicCookie: nil, extensions: nil,
                  formatDescriptionOut: &description)
        guard describe == noErr, let description else { throw issue("format description status \(describe)") }
        var buffer: CMSampleBuffer?
        let create = CMAudioSampleBufferCreateWithPacketDescriptions(allocator: kCFAllocatorDefault, dataBuffer: nil,
                  dataReady: false, makeDataReadyCallback: nil, refcon: nil, formatDescription: description,
                  sampleCount: Int(frames), presentationTimeStamp: .zero, packetDescriptions: nil,
                  sampleBufferOut: &buffer)
        guard create == noErr, let buffer else { throw issue("sample buffer status \(create)") }
        let copy = CMSampleBufferSetDataBufferFromAudioBufferList(buffer, blockBufferAllocator: kCFAllocatorDefault,
                  blockBufferMemoryAllocator: kCFAllocatorDefault, flags: 0, bufferList: source.audioBufferList)
        let ready = CMSampleBufferSetDataReady(buffer)
        guard copy == noErr, ready == noErr else { throw issue("sample copy status \(copy)/\(ready)") }
        let converted: AVAudioPCMBuffer
        do { converted = try AudioRecorder.convert(buffer, target: target) }
        catch { throw issue("converter: \(error.localizedDescription)") }
        guard let pcm = converted.int16ChannelData?[0], converted.frameLength >= 300 else { throw issue("unexpected output length \(converted.frameLength)") }
        guard (0..<Int(converted.frameLength)).contains(where: { abs(Int(pcm[$0])) > 1_000 }) else { throw issue("right-channel samples not present") }
    }

    static func selfTest() throws {
        try testPCMConversion(interleaved: true, channels: 1)
        try testPCMConversion(interleaved: true, channels: 2)
        try testPCMConversion(interleaved: false, channels: 2)
        var mixer = CaptureMixer()
        let pts = CMTime(value: 16_000, timescale: 16_000)
        let system: [Int16] = [1_000, 20_000, 100]
        let mic: [Int16] = [2_000, 20_000, -100]
        let leading = try system.withUnsafeBufferPointer { try mixer.push($0, at: pts) }
        let overlap = try mic.withUnsafeBufferPointer { try mixer.push($0, at: pts) }
        let mixed = leading + overlap + mixer.finish()
        guard Array(mixed.suffix(3)) == [3_000, .max, 0], mixed.count == 8_003 else { throw CaptureError.badAudioFormat }
        let solo = try parseArguments(["--pcm-stdout", "--confirm", "--microphone-only"])
        let stereo = try parseArguments(["--pcm-stdout", "--confirm", "--whole-mac", "--include-microphone"])
        guard solo.source == .microphone, !solo.includeMicrophone, stereo.includeMicrophone else { throw CaptureError.badAudioFormat }
        let data = AudioRecorder.encodeWav([0, 1, -1])
        guard data.count == 50, String(data: data.prefix(4), encoding: .ascii) == "RIFF",
              data.subdata(in: 8..<12) == Data("WAVE".utf8), data.subdata(in: 44..<50) == Data([0, 0, 1, 0, 255, 255]) else { throw CaptureError.badAudioFormat }
        for invalid in [["--output", "x", "--confirm", "--whole-mac", "--app-bundle-id", "a.b"],
                        ["--output", "x", "--output", "y", "--confirm", "--whole-mac"],
                        ["--output", "x", "--confirm", "--whole-mac", "--unexpected"],
                        ["--output", "x", "--pcm-stdout", "--confirm", "--whole-mac"],
                        ["--pcm-stdout", "--confirm", "--whole-mac", "--pcm-stdout"]] {
            do { _ = try parseArguments(invalid); throw CaptureError.badAudioFormat }
            catch CaptureError.usage { }
        }
        let testDirectory = FileManager.default.temporaryDirectory.appendingPathComponent("native-capture-sync-test-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: testDirectory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: testDirectory) }
        let failDirectorySync: (URL) throws -> Void = { _ in throw POSIXError(.EIO) }
        let wav = testDirectory.appendingPathComponent("part-000001.wav")
        var wavFailed = false
        do { try writeNewFile(data, to: wav, directorySync: failDirectorySync) } catch { wavFailed = true }
        guard wavFailed, try Data(contentsOf: wav) == data else { throw CaptureError.badAudioFormat }
        let marker = testDirectory.appendingPathComponent("recording-complete.json")
        var markerFailed = false
        do { try writeNewFile(Data("{}".utf8), to: marker, isCompletionMarker: true, directorySync: failDirectorySync) } catch { markerFailed = true }
        guard markerFailed, !FileManager.default.fileExists(atPath: marker.path) else { throw CaptureError.badAudioFormat }

        let interrupted = testDirectory.appendingPathComponent("interrupted", isDirectory: true)
        try FileManager.default.createDirectory(at: interrupted, withIntermediateDirectories: false)
        let recorder = AudioRecorder(directory: interrupted, pcmStdout: false) { _ in }
        let tail: [Int16] = [41, -57, 128]
        try tail.withUnsafeBufferPointer { try recorder.appendInput($0) }
        recorder.recordFailure(POSIXError(.EIO))
        var incomplete = false
        do { try recorder.complete() } catch { incomplete = true }
        guard incomplete, !FileManager.default.fileExists(atPath: interrupted.appendingPathComponent("recording-complete.json").path),
              try Data(contentsOf: interrupted.appendingPathComponent("part-000001.wav")) == AudioRecorder.encodeWav(tail) else { throw CaptureError.badAudioFormat }
    }
}
