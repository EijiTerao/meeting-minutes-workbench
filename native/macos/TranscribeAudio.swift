// AVAudioConverter consumes the PCM buffer synchronously; its Objective-C
// buffer type does not declare Sendable even for that callback.
@preconcurrency import AVFoundation
import Darwin
import Foundation
import Speech

private enum CLIError: Error, CustomStringConvertible {
    case usage
    case invalidPCM(String)
    case unsupportedOS
    case unavailable
    case localeNotInstalled(String)
    case invalidInput(String)
    case existingOutput(String)
    case emptyTranscript

    var description: String {
        switch self {
        case .usage:
            return "Usage: workbench-apple-transcribe --input <16k-mono-PCM16 WAV or completed recording folder> --output <new TXT> --language <ja|ko> | --pcm-stdin --language <ja|ko>"
        case .invalidPCM(let detail): return "Invalid PCM stdin: \(detail)"
        case .unsupportedOS: return "Apple on-device transcription requires macOS 26 or later."
        case .unavailable: return "Apple SpeechTranscriber is unavailable on this Mac."
        case .localeNotInstalled(let locale): return "No installed Apple SpeechTranscriber assets for \(locale); no assets were downloaded."
        case .invalidInput(let detail): return "Invalid input: \(detail)"
        case .existingOutput(let path): return "Refusing to overwrite existing output: \(path)"
        case .emptyTranscript: return "Speech recognition returned no transcript; no output was written."
        }
    }
}

private enum InputMode {
    case files(input: URL, output: URL)
    case pcmStdin
}

private struct Options {
    let mode: InputMode
    let language: String
    let allowEmpty: Bool

    init(arguments: [String]) throws {
        var values: [String: String] = [:]
        var pcmStdin = false
        var allowEmpty = false
        var index = 0
        while index < arguments.count {
            let key = arguments[index]
            if key == "--pcm-stdin", !pcmStdin {
                pcmStdin = true
                index += 1
                continue
            }
            if key == "--allow-empty", !allowEmpty {
                allowEmpty = true
                index += 1
                continue
            }
            guard ["--input", "--output", "--language"].contains(key), index + 1 < arguments.count,
                  values[key] == nil else { throw CLIError.usage }
            values[key] = arguments[index + 1]
            index += 2
        }
        guard let language = values["--language"], ["ja", "ko"].contains(language) else { throw CLIError.usage }
        if pcmStdin, values.count == 1 {
            self.mode = .pcmStdin
        } else if !pcmStdin, !allowEmpty, let input = values["--input"], let output = values["--output"], values.count == 3 {
            self.mode = .files(input: URL(fileURLWithPath: input).standardizedFileURL,
                               output: URL(fileURLWithPath: output).standardizedFileURL)
        } else { throw CLIError.usage }
        self.language = language
        self.allowEmpty = allowEmpty
    }
}

private func isSymbolicLink(_ url: URL) -> Bool {
    var metadata = stat()
    guard lstat(url.path, &metadata) == 0 else { return false }
    return (metadata.st_mode & S_IFMT) == S_IFLNK
}

@available(macOS 26.0, *)
private func inputFiles(_ url: URL) throws -> [URL] {
    var isDirectory: ObjCBool = false
    guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory) else {
        throw CLIError.invalidInput("path does not exist: \(url.path)")
    }
    if !isDirectory.boolValue { return [url] }

    let marker = url.appendingPathComponent("recording-complete.json")
    guard FileManager.default.fileExists(atPath: marker.path), !isSymbolicLink(marker) else {
        throw CLIError.invalidInput("recording folder has no recording-complete.json")
    }
    let files = try FileManager.default.contentsOfDirectory(at: url, includingPropertiesForKeys: [.isRegularFileKey])
        .filter { $0.lastPathComponent.range(of: #"^part-[0-9]{6}\.wav$"#, options: .regularExpression) != nil }
        .sorted { $0.lastPathComponent < $1.lastPathComponent }
    guard !files.isEmpty else { throw CLIError.invalidInput("recording folder has no part-XXXXXX.wav files") }
    guard let markerData = try? Data(contentsOf: marker),
          let markerObject = try? JSONSerialization.jsonObject(with: markerData) as? [String: Any],
          (markerObject["version"] as? Int) == 1,
          (markerObject["parts"] as? Int) == files.count,
          files.enumerated().allSatisfy({ index, file in
              file.lastPathComponent == String(format: "part-%06d.wav", index + 1) && !isSymbolicLink(file)
          }) else {
        throw CLIError.invalidInput("recording-complete.json does not match the WAV parts")
    }
    return files
}

private func writeVerifiedOutput(_ text: String, to destination: URL) throws {
    let temporary = destination.deletingLastPathComponent()
        .appendingPathComponent(".\(destination.lastPathComponent).tmp-\(UUID().uuidString)")
    let descriptor = open(temporary.path, O_WRONLY | O_CREAT | O_EXCL, S_IRUSR | S_IWUSR)
    guard descriptor >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    var descriptorOpen = true
    var temporaryExists = true
    defer { if temporaryExists { _ = unlink(temporary.path) } }

    do {
        let data = Data(text.utf8)
        try data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var offset = 0
            while offset < raw.count {
                let count = write(descriptor, base.advanced(by: offset), raw.count - offset)
                guard count > 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
                offset += count
            }
        }
        guard fsync(descriptor) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        let closeResult = close(descriptor)
        descriptorOpen = false
        guard closeResult == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }

        let readback = try String(contentsOf: temporary, encoding: .utf8)
        guard readback == text else { throw CLIError.invalidInput("temporary output read-back did not match") }

        guard link(temporary.path, destination.path) == 0 else {
            if errno == EEXIST { throw CLIError.existingOutput(destination.path) }
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        guard unlink(temporary.path) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        temporaryExists = false
    } catch {
        if descriptorOpen { _ = close(descriptor) }
        throw error
    }
}

@available(macOS 26.0, *)
private func recognize(_ file: URL, locale: Locale) async throws -> String {
    let headerHandle = try FileHandle(forReadingFrom: file)
    let header = try headerHandle.read(upToCount: 12) ?? Data()
    try headerHandle.close()
    guard header.count == 12, String(data: header.prefix(4), encoding: .ascii) == "RIFF",
          String(data: header.suffix(4), encoding: .ascii) == "WAVE" else {
        throw CLIError.invalidInput("expected a RIFF/WAVE file: \(file.path)")
    }
    let audio = try AVAudioFile(forReading: file)
    let format = audio.fileFormat
    guard format.sampleRate == 16_000, format.channelCount == 1,
          format.commonFormat == .pcmFormatInt16, audio.length > 0 else {
        throw CLIError.invalidInput("expected non-empty 16 kHz, mono, 16-bit PCM WAV: \(file.path)")
    }

    let transcriber = SpeechTranscriber(locale: locale, preset: .transcription)
    async let transcript = transcriber.results.reduce(into: "") { text, result in
        text += String(result.text.characters)
    }
    let analyzer = SpeechAnalyzer(modules: [transcriber])
    if let lastSample = try await analyzer.analyzeSequence(from: audio) {
        try await analyzer.finalizeAndFinish(through: lastSample)
    } else {
        await analyzer.cancelAndFinishNow()
    }
    return try await transcript
}

@available(macOS 26.0, *)
private func readPCMStdin() throws -> Data {
    let maxBytes = 16_000 * 2 * 30
    var data = Data()
    while true {
        guard let chunk = try FileHandle.standardInput.read(upToCount: min(64 * 1024, maxBytes + 1 - data.count)), !chunk.isEmpty else { break }
        data.append(chunk)
        guard data.count <= maxBytes else { throw CLIError.invalidPCM("audio exceeds 30 seconds") }
    }
    guard !data.isEmpty else { throw CLIError.invalidPCM("input is empty") }
    guard data.count.isMultiple(of: 2) else { throw CLIError.invalidPCM("byte count must be even") }
    return data
}

@available(macOS 26.0, *)
private func recognizePCMStdin(_ locale: Locale) async throws -> String {
    let bytes = try readPCMStdin()
    let inputFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: false)!
    guard let inputBuffer = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: AVAudioFrameCount(bytes.count / 2)),
          let channel = inputBuffer.int16ChannelData?[0] else {
        throw CLIError.invalidPCM("could not allocate an audio buffer")
    }
    inputBuffer.frameLength = AVAudioFrameCount(bytes.count / 2)
    bytes.withUnsafeBytes { raw in
        guard let source = raw.baseAddress else { return }
        memcpy(channel, source, bytes.count)
    }

    let transcriber = SpeechTranscriber(locale: locale, preset: .transcription)
    guard let targetFormat = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber], considering: inputFormat) else {
        throw CLIError.unavailable
    }
    let analysisBuffer: AVAudioPCMBuffer
    if targetFormat == inputFormat {
        analysisBuffer = inputBuffer
    } else {
        guard let converter = AVAudioConverter(from: inputFormat, to: targetFormat) else {
            throw CLIError.invalidPCM("could not convert PCM to the analyzer audio format")
        }
        let capacity = AVAudioFrameCount(ceil(Double(inputBuffer.frameLength) * targetFormat.sampleRate / inputFormat.sampleRate) + 16)
        guard let converted = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity) else {
            throw CLIError.invalidPCM("could not allocate converted audio")
        }
        var supplied = false
        var conversionError: NSError?
        let status = converter.convert(to: converted, error: &conversionError) { _, state in
            if supplied { state.pointee = .noDataNow; return nil }
            supplied = true
            state.pointee = .haveData
            return inputBuffer
        }
        guard status != .error, conversionError == nil, converted.frameLength > 0 else {
            throw conversionError ?? CLIError.invalidPCM("audio conversion failed")
        }
        analysisBuffer = converted
    }

    let analyzer = SpeechAnalyzer(modules: [transcriber])
    async let transcript = transcriber.results.reduce(into: "") { text, result in
        text += String(result.text.characters)
    }
    let stream = AsyncStream<AnalyzerInput> { continuation in
        continuation.yield(AnalyzerInput(buffer: analysisBuffer))
        continuation.finish()
    }
    if let lastSample = try await analyzer.analyzeSequence(stream) {
        try await analyzer.finalizeAndFinish(through: lastSample)
    } else {
        await analyzer.cancelAndFinishNow()
    }
    return try await transcript
}

@available(macOS 26.0, *)
private func run(_ options: Options) async throws {
    guard SpeechTranscriber.isAvailable else { throw CLIError.unavailable }
    let targetLocale = Locale(identifier: options.language == "ja" ? "ja_JP" : "ko_KR")
    let installed = await SpeechTranscriber.installedLocales
    guard installed.contains(where: { $0.identifier == targetLocale.identifier }) else {
        throw CLIError.localeNotInstalled(targetLocale.identifier)
    }
    if case .pcmStdin = options.mode {
        let text = try await recognizePCMStdin(targetLocale).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || options.allowEmpty else { throw CLIError.emptyTranscript }
        print(text, terminator: "")
        return
    }
    guard case let .files(input, output) = options.mode else { throw CLIError.usage }
    guard !FileManager.default.fileExists(atPath: output.path) else { throw CLIError.existingOutput(output.path) }

    let files = try inputFiles(input)
    var transcripts: [String] = []
    for (index, file) in files.enumerated() {
        let text = try await recognize(file, locale: targetLocale)
        transcripts.append("[\(file.lastPathComponent)]\n\(text)")
        fputs("Processed \(index + 1)/\(files.count): \(file.lastPathComponent)\n", stderr)
    }
    let outputText = transcripts.joined(separator: "\n\n") + "\n"
    guard !transcripts.allSatisfy({
        $0.components(separatedBy: "\n").dropFirst().joined().trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }) else {
        throw CLIError.emptyTranscript
    }
    let parent = output.deletingLastPathComponent()
    guard FileManager.default.fileExists(atPath: parent.path) else {
        throw CLIError.invalidInput("output directory does not exist: \(parent.path)")
    }
    try writeVerifiedOutput(outputText, to: output)
    print("Transcription saved and verified: \(output.path)")
}

@main
private struct TranscribeAudio {
    static func main() async {
        do {
            let options = try Options(arguments: Array(CommandLine.arguments.dropFirst()))
            if #available(macOS 26.0, *) {
                try await run(options)
            } else {
                throw CLIError.unsupportedOS
            }
        } catch {
            fputs("Error: \(error)\n", stderr)
            exit(1)
        }
    }
}
