// XENIO Orb: XENIO's floating orb over the user's apps, the global summon shortcut, and the Mac's location.
//
// Started by XENIO (app/overlay.py) as a child process; it reads one JSON line on stdin ({"url": ..., "lock": ...})
// and quits when that pipe closes (XENIO stopped or crashed: never an orphan). The orb itself is XENIO's own web page
// (the UI server's ?view=orb) in a borderless, non-activating floating panel: it never takes the keyboard focus from
// the app being used, joins every Space and sits beside full-screen apps where macOS allows it, and is excluded from
// screenshots (so "look at my screen" never sees it). The page decides what to show from XENIO's real state; this
// helper only moves, sizes, shows and hides the panel, and reports what it could set up.
//
// The quick chat is the same page in the same panel: while it is open the panel may take the keyboard (like Spotlight,
// without activating this app). Mission Control is XENIO's full page in a real window, opened from there.
//
// Shortcut: triple-press Space (an event tap: needs Accessibility, which macOS grants to the app XENIO runs from) or,
// without any permission, Control-Option-Space (a registered hot key). See TripleSpace.swift for what typing costs.
//
// No private API is used. macOS decides what may float over full-screen and protected surfaces; where it does not
// allow it, the orb is simply not there (the page and notifications still work).

import Carbon.HIToolbox
import Cocoa
import CoreLocation
import WebKit

struct Launch: Decodable {
    let url: String
    let lock: String?
}

private let postedMark: Int64 = 0x58454E49  // "XENI": events this helper re-posts, which its own tap must let through

final class OrbPanel: NSPanel {
    /// Only while the quick chat is open: then it takes the keyboard like Spotlight does, without activating XENIO Orb
    /// (the user's app stays the active app). Otherwise the orb never takes the keyboard from the user's app.
    var keyable = false
    var onCloseKey: (() -> Void)?
    override var canBecomeKey: Bool { keyable }
    override var canBecomeMain: Bool { false }

    /// XENIO Orb is not the active app while the user types here, so its menu may not see the editing shortcuts:
    /// they are handled here. Command-W closes the chat, never the orb.
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        guard keyable, isKeyWindow, event.type == .keyDown,
              event.modifierFlags.intersection(.deviceIndependentFlagsMask).contains(.command) else {
            return super.performKeyEquivalent(with: event)
        }
        let shift = event.modifierFlags.contains(.shift)
        let action: Selector?
        switch event.charactersIgnoringModifiers?.lowercased() {
        case "c": action = #selector(NSText.copy(_:))
        case "v": action = #selector(NSText.paste(_:))
        case "x": action = #selector(NSText.cut(_:))
        case "a": action = #selector(NSText.selectAll(_:))
        case "z": action = shift ? Selector(("redo:")) : Selector(("undo:"))
        case "w":
            onCloseKey?()
            return true
        default: action = nil
        }
        if let action, NSApp.sendAction(action, to: nil, from: self) { return true }
        return super.performKeyEquivalent(with: event)
    }
}

private let graphite = NSColor(calibratedRed: 0.05, green: 0.055, blue: 0.065, alpha: 1)

/// WKUserContentController keeps its handler strongly: this breaks the cycle.
final class WeakHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?
    init(_ target: WKScriptMessageHandler) { self.target = target }
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        target?.userContentController(c, didReceive: m)
    }
}

final class OrbApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler, WKNavigationDelegate,
                    WKUIDelegate, CLLocationManagerDelegate {
    let launch: Launch
    var panel: OrbPanel!
    var web: WKWebView!
    var size = NSSize(width: 96, height: 96)
    var dragMonitor: Any?
    var dragStart: (mouse: NSPoint, origin: NSPoint)?

    // shortcut
    var shortcut = "triple_space"
    var interval: TimeInterval = 0.45
    var spaceTap: SpaceTap?
    var hotKey: EventHotKeyRef?
    var hotKeyHandler: EventHandlerRef?
    var shortcutStatus = "off"

    // Mission Control: XENIO's full page in a real window (opened from the quick chat or the orb)
    var appWindow: NSWindow?
    var appWeb: WKWebView?
    var appURL: URL?
    var appFocused = false  // while it is in front, the orb steps aside: the window shows everything
    var wantPanel = false  // what the orb page last asked for (shown or hidden)

    // location
    var locator: CLLocationManager?
    var wantLocation = false

    init(launch: Launch) {
        self.launch = launch
    }

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)  // no Dock icon, no menu bar takeover (until Mission Control opens)
        buildMenu()
        buildPanel()
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged),
                                               name: NSApplication.didChangeScreenParametersNotification, object: nil)
        applyShortcut("triple_space")
    }

    func buildPanel() {
        let frame = NSRect(origin: savedOrigin(for: size), size: size)
        panel = OrbPanel(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.sharingType = .none  // never in screenshots or screen sharing
        panel.delegate = self
        panel.onCloseKey = { [weak self] in self?.send(["type": "closeChat"]) }

        let content = NSView(frame: NSRect(origin: .zero, size: size))
        content.wantsLayer = true
        content.layer?.cornerRadius = 24
        content.layer?.masksToBounds = true
        content.layer?.backgroundColor = graphite.cgColor

        let config = WKWebViewConfiguration()
        config.userContentController.add(WeakHandler(self), name: "xenio")
        web = WKWebView(frame: content.bounds, configuration: config)
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.underPageBackgroundColor = graphite
        content.addSubview(web)
        panel.contentView = content
        if let url = URL(string: launch.url) {
            web.load(URLRequest(url: url))
        }
    }

    // MARK: messages from the page

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        switch type {
        case "ready":
            sendStatus()
        case "show":
            wantPanel = true
            resize(to: dimension(body), show: !appFocused || panel.keyable)
        case "size":
            resize(to: dimension(body), show: panel.isVisible)
        case "hide":
            wantPanel = false
            panel.orderOut(nil)
        case "chat":
            setChat((body["on"] as? Bool) ?? false)
        case "openApp":
            openMissionControl(route: body["route"] as? String)
        case "dragStart":
            beginDrag()
        case "config":
            if let s = body["shortcut"] as? String, s != shortcut { applyShortcut(s) }
            if let ms = body["interval_ms"] as? Double {
                interval = max(0.2, min(1.0, ms / 1000))
                spaceTap?.setInterval(interval)
            }
            if let loc = body["location"] as? Bool { setLocation(loc) }
        case "open":
            if let s = body["url"] as? String, let url = URL(string: s), url.host == "127.0.0.1" {
                NSWorkspace.shared.open(url)
            }
        default:
            break
        }
    }

    /// A command from XENIO itself (stdin): a summon on XENIO's behalf (Mission Control's "Quick chat"), with the way
    /// to talk when it is known ("chat" or "voice"), or Mission Control.
    func command(_ body: [String: Any]) {
        switch body["type"] as? String {
        case "summon":
            var mode: String?
            if let m = body["mode"] as? String, ["chat", "voice"].contains(m) { mode = m }
            summon(via: "xenio", mode: mode)
        case "openApp":
            openMissionControl()
        default:
            break
        }
    }

    func dimension(_ body: [String: Any]) -> NSSize {
        let w = (body["width"] as? Double) ?? Double(size.width)
        let h = (body["height"] as? Double) ?? Double(size.height)
        let room = (panel.screen ?? NSScreen.main)?.visibleFrame.size ?? NSSize(width: 1440, height: 900)
        return NSSize(width: max(48, min(560, w, room.width - 16)), height: max(48, min(760, h, room.height - 16)))
    }

    // MARK: the quick chat (in the orb's own panel)

    /// The page opened (or closed) its chat: the panel takes the keyboard for typing - without activating XENIO Orb,
    /// so the user's app stays active and gets the keyboard back when the chat closes. Triple-Space is paused while
    /// the chat has the keyboard (every Space is just a space there).
    func setChat(_ on: Bool) {
        if on {
            panel.keyable = true
            if !panel.isVisible { panel.orderFrontRegardless() }
            panel.makeKey()
            panel.makeFirstResponder(web)
            return
        }
        panel.keyable = false
        if panel.isKeyWindow {
            panel.orderOut(nil)  // hands the keyboard back to the user's app
            if wantPanel { panel.orderFrontRegardless() }
        }
    }

    func send(_ payload: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: payload), let json = String(data: data, encoding: .utf8)
        else { return }
        web.evaluateJavaScript("window.xenioNative && window.xenioNative.receive(\(json))", completionHandler: nil)
    }

    func sendStatus(location: String? = nil) {
        var status: [String: Any] = ["type": "status", "shortcut": shortcut, "shortcut_status": shortcutStatus,
                                     "accessibility": AXIsProcessTrusted(), "screens": NSScreen.screens.count]
        if let location { status["location"] = location }
        send(status)
    }

    // MARK: the panel: size, place, drag

    func resize(to new: NSSize, show: Bool) {
        // keep the corner nearest the screen edge where it is (bottom-right by default): it grows inwards
        let old = panel.frame
        let screen = (panel.screen ?? NSScreen.main)?.visibleFrame ?? old
        let anchorRight = old.midX > screen.midX
        let anchorTop = old.midY > screen.midY
        var origin = old.origin
        if anchorRight { origin.x = old.maxX - new.width }
        if anchorTop { origin.y = old.maxY - new.height }
        size = new
        let frame = clamp(NSRect(origin: origin, size: new))
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.18
            ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
            panel.animator().setFrame(frame, display: true)
        }
        if show && !panel.isVisible {
            panel.orderFrontRegardless()  // over the user's app, without taking the focus
        }
    }

    func beginDrag() {
        dragStart = (NSEvent.mouseLocation, panel.frame.origin)
        if dragMonitor != nil { return }
        dragMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDragged, .leftMouseUp]) { [weak self] event in
            guard let self, let start = self.dragStart else { return event }
            let now = NSEvent.mouseLocation
            if event.type == .leftMouseDragged {
                self.panel.setFrameOrigin(NSPoint(x: start.origin.x + now.x - start.mouse.x, y: start.origin.y + now.y - start.mouse.y))
            } else {
                self.panel.setFrame(self.clamp(self.panel.frame), display: true)
                UserDefaults.standard.set([self.panel.frame.origin.x, self.panel.frame.origin.y], forKey: "orbOrigin")
                if let m = self.dragMonitor { NSEvent.removeMonitor(m) }
                self.dragMonitor = nil
                self.dragStart = nil
            }
            return event
        }
    }

    func savedOrigin(for size: NSSize) -> NSPoint {
        if let saved = UserDefaults.standard.array(forKey: "orbOrigin") as? [Double], saved.count == 2 {
            return clamp(NSRect(x: saved[0], y: saved[1], width: size.width, height: size.height)).origin
        }
        let screen = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        return NSPoint(x: screen.maxX - size.width - 24, y: screen.minY + 24)
    }

    /// Fully inside the visible part of the screen it is mostly on (another display, or one that went away).
    func clamp(_ frame: NSRect) -> NSRect {
        let screens = NSScreen.screens
        let target = screens.max(by: { $0.visibleFrame.intersection(frame).area < $1.visibleFrame.intersection(frame).area })
            ?? NSScreen.main
        guard let area = target?.visibleFrame else { return frame }
        var f = frame
        f.origin.x = min(max(f.origin.x, area.minX + 8), area.maxX - f.width - 8)
        f.origin.y = min(max(f.origin.y, area.minY + 8), area.maxY - f.height - 8)
        return f
    }

    @objc func screensChanged() {
        panel.setFrame(clamp(panel.frame), display: true)
        sendStatus()
    }

    // MARK: only XENIO's own pages in the panel

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, url.host == "127.0.0.1" || url.scheme == "about" {
            decisionHandler(.allow)
            return
        }
        if let url = action.request.url, url.scheme == "https" || url.scheme == "http" {
            NSWorkspace.shared.open(url)  // a link in an answer opens in the browser, never in the orb
        }
        decisionHandler(.cancel)
    }

    /// Recover from a page crash with the page's own address and key (a plain reload would come back without the key).
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        let url = webView === appWeb ? appURL : URL(string: launch.url)
        if let url { webView.load(URLRequest(url: url)) }
    }

    /// target=_blank links (sources under an answer) open in the user's browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, url.scheme == "https" || url.scheme == "http" { NSWorkspace.shared.open(url) }
        return nil
    }

    /// The page's own microphone (used only when XENIO's talk engine is not running): XENIO's page only.
    @available(macOS 12.0, *)
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(origin.host == "127.0.0.1" && type == .microphone ? .grant : .deny)
    }

    // MARK: Mission Control (XENIO's full page in a real window)

    /// The page reads its session key from the address it is opened with, once, as it starts: nothing here may touch
    /// the address before that (an earlier version set the route as soon as WebKit reported the load, which can be
    /// before the page's script ran, and wiped the key).
    func openMissionControl(route: String? = nil) {
        // only XENIO's own page paths ("/orchestration/ID"): nothing else reaches the address or the script below
        let path = route.flatMap { $0.range(of: #"^/[a-z]+(/[A-Za-z0-9_-]+)?$"#, options: .regularExpression) != nil ? $0 : nil }
        if appWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780),
                                  styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            window.title = "XENIO"
            window.minSize = NSSize(width: 720, height: 520)
            window.isReleasedWhenClosed = false
            window.tabbingMode = .disallowed
            window.backgroundColor = graphite
            window.delegate = self
            let web = WKWebView(frame: window.contentLayoutRect, configuration: WKWebViewConfiguration())
            web.autoresizingMask = [.width, .height]
            web.navigationDelegate = self
            web.uiDelegate = self
            web.underPageBackgroundColor = graphite
            window.contentView = web
            if !window.setFrameUsingName("XenioMissionControl") { window.center() }
            window.setFrameAutosaveName("XenioMissionControl")
            appWindow = window
            appWeb = web
            if var parts = URLComponents(string: launch.url) {
                parts.queryItems = nil  // XENIO's full page, not ?view=orb; the session key stays in the fragment
                appURL = parts.url
                if let path, let fragment = parts.fragment { parts.fragment = "\(fragment)&\(path)" }  // the page's route
                if let url = parts.url { web.load(URLRequest(url: url)) }
            }
        } else if let path {
            // the page has long read its key (it is in its session storage): only then may the route change
            appWeb?.evaluateJavaScript("if (sessionStorage.getItem('xenio.session-token')) location.hash = '#\(path)';",
                                       completionHandler: nil)
        }
        NSApp.setActivationPolicy(.regular)  // a real window: in the Dock and Command-Tab while it is open
        if #available(macOS 14.0, *) { NSApp.activate() } else { NSApp.activate(ignoringOtherApps: true) }
        appWindow?.makeKeyAndOrderFront(nil)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard sender === appWindow else { return false }  // the orb's panel is never closed, only hidden
        sender.orderOut(nil)  // kept, not destroyed: opening it again is instant
        NSApp.setActivationPolicy(.accessory)
        return false
    }

    func windowDidBecomeKey(_ notification: Notification) {
        let window = notification.object as? NSWindow
        if window === panel {
            spaceTap?.setPaused(true)  // typing in the quick chat: every Space is just a space
        } else if window === appWindow {
            appFocused = true
            spaceTap?.setPaused(true)
            if !panel.keyable { panel.orderOut(nil) }  // Mission Control shows everything; an open quick chat stays
        }
    }

    func windowDidResignKey(_ notification: Notification) {
        let window = notification.object as? NSWindow
        if window === panel {
            if !appFocused { spaceTap?.setPaused(false) }
        } else if window === appWindow {
            appFocused = false
            if !panel.isKeyWindow { spaceTap?.setPaused(false) }
            if wantPanel { panel.orderFrontRegardless() }
        }
    }

    /// Copy, paste, undo and close need a menu, even in a helper without a menu bar of its own.
    func buildMenu() {
        let main = NSMenu()
        let app = NSMenu(title: "XENIO")
        app.addItem(withTitle: "Hide XENIO", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let window = NSMenu(title: "Window")
        window.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        for menu in [app, edit, window] {
            let item = NSMenuItem()
            item.submenu = menu
            main.addItem(item)
        }
        NSApp.mainMenu = main
    }

    // MARK: the summon shortcut

    func applyShortcut(_ name: String) {
        removeShortcut()
        shortcut = name
        switch name {
        case "triple_space":
            if !installTap() {
                shortcutStatus = "needs_accessibility"
                _ = installHotKey()  // still summonable, without a permission
            } else {
                shortcutStatus = "ok"
            }
        case "ctrl_option_space":
            shortcutStatus = installHotKey() ? "ok" : "unavailable"
        default:
            shortcutStatus = "off"
        }
        sendStatus()
    }

    func removeShortcut() {
        spaceTap?.stop()
        spaceTap = nil
        if let hotKey { UnregisterEventHotKey(hotKey) }
        hotKey = nil
        if let hotKeyHandler { RemoveEventHandler(hotKeyHandler) }
        hotKeyHandler = nil
    }

    func installTap() -> Bool {
        let space = SpaceTap(interval: interval) { [weak self] in self?.summon(via: "triple_space") }
        guard space.start() else { return false }
        spaceTap = space
        return true
    }

    func installHotKey() -> Bool {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let me = Unmanaged.passUnretained(self).toOpaque()
        let status = InstallEventHandler(GetApplicationEventTarget(), { _, _, user in
            guard let user else { return noErr }
            Unmanaged<OrbApp>.fromOpaque(user).takeUnretainedValue().summon(via: "hotkey")
            return noErr
        }, 1, &spec, me, &hotKeyHandler)
        guard status == noErr else { return false }
        let id = EventHotKeyID(signature: OSType(0x58454E4F), id: 1)  // "XENO"
        return RegisterEventHotKey(UInt32(kVK_Space), UInt32(controlKey | optionKey), id, GetApplicationEventTarget(),
                                   0, &hotKey) == noErr
    }

    func summon(via: String, mode: String? = nil) {
        var message: [String: Any] = ["type": "summon", "via": via, "at": Date().timeIntervalSince1970 * 1000]
        if let mode { message["mode"] = mode }
        send(message)
    }

    // MARK: location (only when the user switched it on in XENIO's Settings)

    func setLocation(_ on: Bool) {
        wantLocation = on
        guard on else {
            locator?.stopUpdatingLocation()
            locator = nil
            sendStatus(location: "off")
            return
        }
        if locator == nil {
            locator = CLLocationManager()
            locator?.delegate = self
            locator?.desiredAccuracy = kCLLocationAccuracyKilometer
        }
        locate()
    }

    func locate() {
        guard let locator else { return }
        switch locator.authorizationStatus {
        case .notDetermined:
            locator.requestWhenInUseAuthorization()
        case .denied, .restricted:
            sendStatus(location: "denied")
        default:
            locator.requestLocation()
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        if wantLocation { locate() }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let fix = locations.last else { return }
        CLGeocoder().reverseGeocodeLocation(fix) { [weak self] places, _ in
            let place = places?.first
            let label = [place?.locality, place?.administrativeArea, place?.country].compactMap { $0 }.joined(separator: ", ")
            self?.send(["type": "location", "lat": fix.coordinate.latitude, "lon": fix.coordinate.longitude,
                        "accuracy_m": fix.horizontalAccuracy, "label": label])
            self?.sendStatus(location: "ok")
        }
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        sendStatus(location: "failed: \(error.localizedDescription)")
    }
}

/// The triple-Space event tap on its own thread and run loop: keystrokes pass through this callback, so it must never
/// wait for the web page or the main thread. All of its state lives on that thread.
final class SpaceTap {
    private var detector = TripleSpace()
    private var tap: CFMachPort?
    private var loop: CFRunLoop?
    private var heldDown: CGEvent?
    private var heldUp: CGEvent?
    private var releaseTimer: Timer?
    private var paused = false  // the orb's own text box has the keyboard: every key passes untouched
    private let onSummon: () -> Void

    init(interval: TimeInterval, onSummon: @escaping () -> Void) {
        detector.interval = interval
        self.onSummon = onSummon
    }

    /// Starts the tap thread; false when macOS refuses the tap (no Accessibility for the app XENIO runs from).
    func start() -> Bool {
        guard AXIsProcessTrusted() else { return false }
        let ready = DispatchSemaphore(value: 0)
        var ok = false
        let thread = Thread { [self] in
            let mask = (1 << CGEventType.keyDown.rawValue) | (1 << CGEventType.keyUp.rawValue)
            let me = Unmanaged.passUnretained(self).toOpaque()
            if let tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .defaultTap,
                                           eventsOfInterest: CGEventMask(mask), callback: tapCallback, userInfo: me) {
                self.tap = tap
                self.loop = CFRunLoopGetCurrent()
                CFRunLoopAddSource(self.loop, CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0), .commonModes)
                CGEvent.tapEnable(tap: tap, enable: true)
                ok = true
            }
            ready.signal()
            if ok { CFRunLoopRun() }
        }
        thread.name = "xenio-space-tap"
        thread.qualityOfService = .userInteractive
        thread.start()
        ready.wait()
        return ok
    }

    func stop() {
        perform { [self] in
            self.releaseHeld()
            if let tap = self.tap { CGEvent.tapEnable(tap: tap, enable: false); CFMachPortInvalidate(tap) }
            self.tap = nil
            if let loop = self.loop { CFRunLoopStop(loop) }
        }
    }

    func setInterval(_ seconds: TimeInterval) {
        perform { [self] in self.detector.interval = seconds }
    }

    func setPaused(_ value: Bool) {
        perform { [self] in
            if value { self.releaseHeld() }
            self.detector.reset()
            self.paused = value
        }
    }

    private func perform(_ block: @escaping () -> Void) {
        guard let loop else { return }
        CFRunLoopPerformBlock(loop, CFRunLoopMode.commonModes.rawValue, block)
        CFRunLoopWakeUp(loop)
    }

    /// Space presses go through TripleSpace; everything else passes untouched (and releases a held space first).
    func handle(_ type: CGEventType, _ event: CGEvent) -> Unmanaged<CGEvent>? {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return Unmanaged.passUnretained(event)
        }
        if paused || event.getIntegerValueField(.eventSourceUserData) == postedMark {
            return Unmanaged.passUnretained(event)  // typing in the orb itself, or re-posted by this helper
        }
        let isSpace = event.getIntegerValueField(.keyboardEventKeycode) == Int64(kVK_Space)
        let plain = event.flags.intersection([.maskCommand, .maskControl, .maskAlternate, .maskShift, .maskSecondaryFn]).isEmpty
        let repeating = event.getIntegerValueField(.keyboardEventAutorepeat) != 0
        let now = ProcessInfo.processInfo.systemUptime
        let decision: SpaceDecision
        if type == .keyDown {
            decision = isSpace && plain && !repeating ? detector.spaceDown(at: now) : detector.otherKey()
        } else if isSpace {
            decision = detector.spaceUp()
        } else {
            decision = .pass
        }
        switch decision {
        case .pass:
            return Unmanaged.passUnretained(event)
        case .hold:
            if type == .keyDown {
                heldDown = event.copy()
                heldUp = nil
                releaseTimer?.invalidate()
                let timer = Timer(timeInterval: detector.interval + 0.02, repeats: false) { [weak self] _ in
                    guard let self else { return }
                    if self.detector.timeout(at: ProcessInfo.processInfo.systemUptime) { self.releaseHeld() }
                }
                RunLoop.current.add(timer, forMode: .common)
                releaseTimer = timer
            } else {
                heldUp = event.copy()
            }
            return nil
        case .swallow:
            return nil
        case .summon:
            releaseTimer?.invalidate()
            heldDown = nil
            heldUp = nil
            DispatchQueue.main.async(execute: onSummon)
            return nil
        case .releaseHeldThenPass:
            releaseHeld()  // the held space first, then this key, in order
            if let copy = event.copy() { post(copy) }
            return nil
        }
    }

    private func releaseHeld() {
        releaseTimer?.invalidate()
        releaseTimer = nil
        if let down = heldDown { post(down) }
        if let up = heldUp { post(up) }
        heldDown = nil
        heldUp = nil
    }

    private func post(_ event: CGEvent) {
        event.setIntegerValueField(.eventSourceUserData, value: postedMark)
        event.post(tap: .cgSessionEventTap)
    }
}

private func tapCallback(proxy: CGEventTapProxy, type: CGEventType, event: CGEvent,
                         user: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    guard let user else { return Unmanaged.passUnretained(event) }
    return Unmanaged<SpaceTap>.fromOpaque(user).takeUnretainedValue().handle(type, event)
}

extension NSRect {
    var area: CGFloat { isNull ? 0 : width * height }
}

// MARK: - start

if CommandLine.arguments.contains("--selftest") {
    let failures = tripleSpaceSelfTest()
    failures.forEach { print("FAIL: \($0)") }
    print(failures.isEmpty ? "selftest ok" : "selftest failed")
    exit(failures.isEmpty ? 0 : 1)
}

guard let line = readLine(), let data = line.data(using: .utf8),
      let launch = try? JSONDecoder().decode(Launch.self, from: data) else {
    FileHandle.standardError.write("XENIO Orb: expected one JSON line on stdin\n".data(using: .utf8)!)
    exit(2)
}

// one orb at a time: a second helper (a second XENIO) exits at once
if let lock = launch.lock {
    let fd = open(lock, O_CREAT | O_RDWR, 0o600)
    if fd < 0 || flock(fd, LOCK_EX | LOCK_NB) != 0 {
        FileHandle.standardError.write("XENIO Orb: already running\n".data(using: .utf8)!)
        exit(3)
    }
}

let app = NSApplication.shared
let delegate = OrbApp(launch: launch)

// XENIO keeps stdin open while it runs and may send commands on it, one JSON object a line ({"type": "summon", "mode":
// "chat"}); when it closes, the orb goes too
Thread.detachNewThread {
    while let line = readLine() {
        guard let data = line.data(using: .utf8),
              let command = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { continue }
        DispatchQueue.main.async { delegate.command(command) }
    }
    DispatchQueue.main.async { NSApp.terminate(nil) }
}

app.delegate = delegate
app.run()
