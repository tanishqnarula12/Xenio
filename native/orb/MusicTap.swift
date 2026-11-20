// Music mode: the orb moves with what the Mac is playing. macOS's own system-audio capture (ScreenCaptureKit) hands over
// the sound; only its loudness and its beats are measured, here, and sent to the orb. Nothing is recorded, kept or sent
// anywhere, XENIO's own voice is left out, and it runs only while the person has music mode on. It needs the Screen &
// System Audio Recording permission, the same one XENIO asks for to look at the screen.

import CoreMedia
import Foundation
import ScreenCaptureKit

@available(macOS 13.0, *)
final class MusicTap: NSObject, SCStreamOutput, SCStreamDelegate {
    /// loudness 0...1, bass 0...1, and whether a beat landed; on the main queue, about 30 times a second
    var onLevel: ((Double, Double, Bool) -> Void)?
    var onStop: ((String) -> Void)?
    private var stream: SCStream?
    private let queue = DispatchQueue(label: "local.xenio.orb.music")
    private var bassState: Float = 0
    private var bassAverage: Double = 0
    private var lastBeat: CFTimeInterval = 0
    private var lastSent: CFTimeInterval = 0

    func start(_ done: @escaping (String?) -> Void) {
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: true) { [weak self] content, error in
            guard let self else { return }
            guard let display = content?.displays.first else {
                DispatchQueue.main.async { done(error?.localizedDescription ?? "macOS gave no screen to listen through") }
                return
            }
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let config = SCStreamConfiguration()
            config.capturesAudio = true
            config.excludesCurrentProcessAudio = true  // never XENIO's own voice
            config.sampleRate = 48_000
            config.channelCount = 2
            config.width = 2  // the picture is not wanted: the smallest there is, once a second
            config.height = 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            let made = SCStream(filter: filter, configuration: config, delegate: self)
            do {
                try made.addStreamOutput(self, type: .audio, sampleHandlerQueue: self.queue)
            } catch {
                DispatchQueue.main.async { done(error.localizedDescription) }
                return
            }
            made.startCapture { error in
                DispatchQueue.main.async { done(error?.localizedDescription) }
            }
            self.stream = made
        }
    }

    func stop() {
        stream?.stopCapture(completionHandler: nil)
        stream = nil
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        DispatchQueue.main.async { [weak self] in self?.onStop?(error.localizedDescription) }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, buffer.isValid else { return }
        var sum: Float = 0, bassSum: Float = 0, count = 0
        try? buffer.withAudioBufferList { list, _ in
            for audio in list {
                guard let data = audio.mData else { continue }
                let n = Int(audio.mDataByteSize) / MemoryLayout<Float>.size
                let samples = data.assumingMemoryBound(to: Float.self)
                for i in 0..<n {
                    let x = samples[i]
                    sum += x * x
                    bassState += 0.012 * (x - bassState)  // a simple low-pass: below about 100 Hz, where the kick drum is
                    bassSum += bassState * bassState
                }
                count += n
            }
        }
        guard count > 0 else { return }
        let rms = Double(sqrt(sum / Float(count))), bass = Double(sqrt(bassSum / Float(count)))
        let level = min(1, max(0, (20 * log10(rms + 1e-9) + 48) / 42))   // -48 dB and below is silence, -6 dB is full
        let low = min(1, max(0, (20 * log10(bass + 1e-9) + 54) / 44))
        bassAverage += (low - bassAverage) * 0.04
        let now = CACurrentMediaTime()
        let beat = low > 0.25 && low > bassAverage * 1.35 && now - lastBeat > 0.22
        if beat { lastBeat = now }
        guard beat || now - lastSent > 1.0 / 30 else { return }
        lastSent = now
        DispatchQueue.main.async { [weak self] in self?.onLevel?(level, low, beat) }
    }
}
