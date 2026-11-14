// XENIO's password vault. All of this runs here, in this native program. XENIO's Python, its web page, and every model and agent are on the far side
// of a pipe that carries only "filled" or "refused", never a password. There is no command that gives one back.
//
//   A random 256-bit key encrypts every login (AES-256-GCM, each bound to its own id and website, so a login cannot be moved to another site in the file).
//   That key is itself sealed twice:
//     - by your fingerprint: a key that lives inside the Secure Enclave chip and is unusable without your finger. Software cannot get around that.
//     - by your secret password: stretched with PBKDF2-SHA256, 4,000,000 rounds and a random salt, and never stored anywhere.
//   Passwords are encrypted and not hashed, because a hash cannot be turned back into the password a website needs. The stretched secret is the hash-like part.
import Cocoa
import CommonCrypto
import CryptoKit
import Foundation
import LocalAuthentication

let vaultRounds = 4_000_000
let minimumSecret = 10

enum VaultError: Error {
    case refused(String)
    case cancelled
    var words: String {
        switch self {
        case .refused(let why): return why
        case .cancelled: return "you cancelled"
        }
    }
}

struct TouchSeal: Codable { var enclave: String; var ephemeral: String; var box: String }
struct VaultItem: Codable { var id: String; var host: String; var label: String; var added: String; var box: String; var account: String? }  // account: your email or username, readable by XENIO because you asked for that. The password is only ever in the box
struct VaultFile: Codable { var version: Int; var salt: String; var rounds: Int; var sealed: String; var touch: TouchSeal?; var items: [VaultItem] }
struct Login: Codable { var username: String; var password: String }

/// What a website has to be, for a saved login to go anywhere near it. accounts.google.com may use a login saved for google.com. google.com.evil.com may not.
func hostMatches(_ page: String, saved: String) -> Bool {
    let p = page.lowercased(), s = saved.lowercased()
    return p == s || p.hasSuffix("." + s)
}

func cleanHost(_ raw: String) -> String? {
    var text = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    if !text.contains("://") { text = "https://" + text }
    guard let host = URL(string: text)?.host?.lowercased(), !host.isEmpty else { return nil }
    let name = host.hasPrefix("www.") ? String(host.dropFirst(4)) : host
    let parts = name.split(separator: ".")
    if name == "127.0.0.1" { return name }
    guard parts.count >= 2, name.range(of: #"^[a-z0-9.-]+$"#, options: .regularExpression) != nil else { return nil }
    // a login saved for "co.uk" would match every company in Britain
    let shared: Set<String> = ["co.uk", "org.uk", "ac.uk", "gov.uk", "co.in", "org.in", "net.in", "ac.in", "gov.in", "com.au", "net.au", "org.au", "co.nz", "co.jp", "com.br", "com.cn", "co.za", "com.sg", "com.mx"]
    if shared.contains(name) || parts.allSatisfy({ Int($0) != nil }) { return nil }
    return name
}

final class Vault {
    let folder: URL
    let file: URL
    var data: VaultFile?

    init(folder: String) {
        self.folder = URL(fileURLWithPath: folder, isDirectory: true)
        file = self.folder.appendingPathComponent("vault.json")
        data = (try? Data(contentsOf: file)).flatMap { try? JSONDecoder().decode(VaultFile.self, from: $0) }
    }

    private func save() throws {
        guard let data else { return }
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        _ = chmod(folder.path, 0o700)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let bytes = try encoder.encode(data)
        let temp = folder.appendingPathComponent("vault.tmp-\(UUID().uuidString)")
        guard FileManager.default.createFile(atPath: temp.path, contents: nil, attributes: [.posixPermissions: 0o600]) else { throw VaultError.refused("the vault could not be written") }
        let handle = try FileHandle(forWritingTo: temp)
        try handle.write(contentsOf: bytes)
        try handle.synchronize()
        try handle.close()
        guard rename(temp.path, file.path) == 0 else { throw VaultError.refused("the vault could not be saved") }
        _ = chmod(file.path, 0o600)
    }

    private static func stretch(_ secret: String, salt: Data, rounds: Int) throws -> SymmetricKey {
        var out = Data(count: 32)
        let bytes = Array(secret.utf8)
        let status = out.withUnsafeMutableBytes { outPtr in
            salt.withUnsafeBytes { saltPtr in
                bytes.withUnsafeBytes { secretPtr in
                    CCKeyDerivationPBKDF(CCPBKDFAlgorithm(kCCPBKDF2), secretPtr.bindMemory(to: Int8.self).baseAddress, bytes.count, saltPtr.bindMemory(to: UInt8.self).baseAddress, salt.count, CCPseudoRandomAlgorithm(kCCPRFHmacAlgSHA256), UInt32(rounds), outPtr.bindMemory(to: UInt8.self).baseAddress, 32)
                }
            }
        }
        guard status == kCCSuccess else { throw VaultError.refused("the secret could not be stretched") }
        return SymmetricKey(data: out)
    }

    private static func seal(_ plain: Data, with key: SymmetricKey, bound: String) throws -> String {
        let box = try AES.GCM.seal(plain, using: key, authenticating: Data(bound.utf8))
        guard let combined = box.combined else { throw VaultError.refused("sealing failed") }
        return combined.base64EncodedString()
    }

    private static func open(_ text: String, with key: SymmetricKey, bound: String) throws -> Data {
        guard let raw = Data(base64Encoded: text), let box = try? AES.GCM.SealedBox(combined: raw) else { throw VaultError.refused("the vault file is damaged") }
        return try AES.GCM.open(box, using: key, authenticating: Data(bound.utf8))
    }

    private static func keyData(_ key: SymmetricKey) -> Data { key.withUnsafeBytes { Data($0) } }
    private static func itemBinding(_ id: String, _ host: String) -> String { "xenio-vault-v1|item|\(id)|\(host)" }
    private static let secretBinding = "xenio-vault-v1|key|secret"
    private static let touchBinding = "xenio-vault-v1|key|touch"

    func create(secret: String) throws -> SymmetricKey {
        guard data == nil else { throw VaultError.refused("there is already a vault here") }
        guard secret.count >= minimumSecret else { throw VaultError.refused("the secret password needs at least \(minimumSecret) characters") }
        var salt = Data(count: 16)
        guard salt.withUnsafeMutableBytes({ SecRandomCopyBytes(kSecRandomDefault, 16, $0.baseAddress!) }) == errSecSuccess else { throw VaultError.refused("no secure random numbers") }
        let key = SymmetricKey(size: .bits256)
        let stretched = try Vault.stretch(secret, salt: salt, rounds: vaultRounds)
        data = VaultFile(version: 1, salt: salt.base64EncodedString(), rounds: vaultRounds, sealed: try Vault.seal(Vault.keyData(key), with: stretched, bound: Vault.secretBinding), touch: nil, items: [])
        try save()
        return key
    }

    func unlock(secret: String) throws -> SymmetricKey {
        guard let data, let salt = Data(base64Encoded: data.salt) else { throw VaultError.refused("there is no vault yet") }
        let stretched = try Vault.stretch(secret, salt: salt, rounds: data.rounds)
        guard let raw = try? Vault.open(data.sealed, with: stretched, bound: Vault.secretBinding) else { throw VaultError.refused("that is not the secret password") }
        return SymmetricKey(data: raw)
    }

    static func touchAvailable() -> Bool {
        var problem: NSError?
        return SecureEnclave.isAvailable && LAContext().canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &problem)
    }

    static func fingerControl() throws -> SecAccessControl {
        var problem: Unmanaged<CFError>?
        guard let control = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly, [.privateKeyUsage, .biometryCurrentSet], &problem) else { throw VaultError.refused("this Mac cannot lock a key to a fingerprint") }
        return control
    }

    func enrolTouch(key: SymmetricKey) throws {
        guard Vault.touchAvailable() else { throw VaultError.refused("Touch ID is not available on this Mac") }
        let enclave = try SecureEnclave.P256.KeyAgreement.PrivateKey(accessControl: try Vault.fingerControl())
        let ephemeral = P256.KeyAgreement.PrivateKey()
        let shared = try ephemeral.sharedSecretFromKeyAgreement(with: enclave.publicKey)
        let wrap = shared.hkdfDerivedSymmetricKey(using: SHA256.self, salt: Data(), sharedInfo: Data(Vault.touchBinding.utf8), outputByteCount: 32)
        data?.touch = TouchSeal(enclave: enclave.dataRepresentation.base64EncodedString(), ephemeral: ephemeral.publicKey.rawRepresentation.base64EncodedString(), box: try Vault.seal(Vault.keyData(key), with: wrap, bound: Vault.touchBinding))
        try save()
    }

    var touchReady: Bool { data?.touch != nil }

    func setTouch(_ seal: TouchSeal?) throws {
        data?.touch = seal
        try save()
    }

    /// Blocks until your finger answers, or does not. Never call it on the main thread of a browser.
    func unlockWithTouch(reason: String, mayAsk: Bool = true) throws -> SymmetricKey {
        guard let touch = data?.touch, let blob = Data(base64Encoded: touch.enclave), let other = Data(base64Encoded: touch.ephemeral) else { throw VaultError.refused("Touch ID is not set up for this vault") }
        let context = LAContext()
        context.localizedReason = reason
        context.interactionNotAllowed = !mayAsk
        do {
            let enclave = try SecureEnclave.P256.KeyAgreement.PrivateKey(dataRepresentation: blob, authenticationContext: context)
            let shared = try enclave.sharedSecretFromKeyAgreement(with: try P256.KeyAgreement.PublicKey(rawRepresentation: other))
            let wrap = shared.hkdfDerivedSymmetricKey(using: SHA256.self, salt: Data(), sharedInfo: Data(Vault.touchBinding.utf8), outputByteCount: 32)
            return SymmetricKey(data: try Vault.open(touch.box, with: wrap, bound: Vault.touchBinding))
        } catch let problem as LAError where problem.code == .userCancel {
            throw VaultError.cancelled
        } catch let problem as LAError where problem.code == .systemCancel || problem.code == .appCancel {
            throw VaultError.refused("macOS cancelled the fingerprint request, which it does when the Mac is locked or another app comes forward")  // you did not say no, so nobody should be told you did
        } catch {
            FileHandle.standardError.write(Data("touch: failed, \(type(of: error)) \((error as NSError).domain) \((error as NSError).code)\n".utf8))
            throw VaultError.refused("your fingerprint did not unlock it")
        }
    }

    func add(key: SymmetricKey, host raw: String, label: String, login: Login) throws -> VaultItem {
        guard let host = cleanHost(raw) else { throw VaultError.refused("that is not a website XENIO can tie a login to") }
        guard !login.password.isEmpty else { throw VaultError.refused("there is no password to save") }
        data?.items.removeAll { $0.host == host && (try? self.reveal(key: key, item: $0).username) == login.username }  // the same login again replaces the old one
        let id = UUID().uuidString.lowercased().prefix(12).description
        let plain = try JSONEncoder().encode(login)
        let item = VaultItem(id: id, host: host, label: String(label.prefix(60)), added: ISO8601DateFormatter().string(from: Date()), box: try Vault.seal(plain, with: key, bound: Vault.itemBinding(id, host)), account: String(login.username.prefix(120)))
        data?.items.append(item)
        try save()
        return item
    }

    /// Stays in this process, for filling a page. Nothing that goes out on a pipe ever comes from here.
    fileprivate func reveal(key: SymmetricKey, item: VaultItem) throws -> Login {
        guard let plain = try? Vault.open(item.box, with: key, bound: Vault.itemBinding(item.id, item.host)), let login = try? JSONDecoder().decode(Login.self, from: plain) else { throw VaultError.refused("that login has been tampered with, or the file is damaged, so it was not used") }
        return login
    }

    func login(key: SymmetricKey, item: VaultItem) throws -> Login { try reveal(key: key, item: item) }

    func remove(key: SymmetricKey, id: String) throws {
        guard let item = data?.items.first(where: { $0.id == id }) else { throw VaultError.refused("there is no such login") }
        _ = try reveal(key: key, item: item)
        data?.items.removeAll { $0.id == id }
        try save()
    }

    func item(id: String) -> VaultItem? { data?.items.first { $0.id == id } }

    func publicState() -> [String: Any] {
        ["ok": true, "exists": data != nil, "touch_available": Vault.touchAvailable(), "touch_ready": touchReady,
         "items": (data?.items ?? []).map { ["id": $0.id, "host": $0.host, "label": $0.label, "added": $0.added, "account": $0.account ?? ""] }]
    }
}

// ---- native windows: what you type goes straight here, and never through XENIO or a web page -------------------------------

func bringUp() {
    if NSApp.activationPolicy() != .regular { NSApp.setActivationPolicy(.accessory) }
    if !flag("no-activate") { NSApp.activate(ignoringOtherApps: true) }  // a check that the window draws has no need to take your keyboard
}

func secretField(_ placeholder: String) -> NSSecureTextField {
    let field = NSSecureTextField(frame: CGRect(x: 0, y: 0, width: 300, height: 24))
    field.placeholderString = placeholder
    return field
}

func stack(_ views: [NSView]) -> NSStackView {
    let box = NSStackView(frame: CGRect(x: 0, y: 0, width: 300, height: CGFloat(views.count) * 32))
    box.orientation = .vertical
    box.spacing = 8
    box.alignment = .leading
    box.distribution = .fillEqually
    views.forEach { box.addArrangedSubview($0); $0.widthAnchor.constraint(equalToConstant: 300).isActive = true }
    return box
}

func ask(_ title: String, _ detail: String, _ fields: [NSView], confirm: String) -> Bool {
    if flag("no-windows") { return false }  // tests never put anything on your screen
    bringUp()
    let alert = NSAlert()
    alert.messageText = title
    alert.informativeText = detail
    if let lock = NSImage(systemSymbolName: "lock.shield.fill", accessibilityDescription: "Vault") { alert.icon = lock }
    alert.addButton(withTitle: confirm)
    alert.addButton(withTitle: "Cancel")
    alert.accessoryView = stack(fields)
    alert.window.level = .floating
    alert.window.initialFirstResponder = fields.first
    return alert.runModal() == .alertFirstButtonReturn
}

func askForSecret(reason: String) throws -> String {
    let field = secretField("Your secret password")
    guard ask("Unlock the XENIO vault", reason, [field], confirm: "Unlock") else { throw VaultError.cancelled }
    return field.stringValue
}

func askForNewSecret() throws -> String {
    let one = secretField("A secret password, at least \(minimumSecret) characters"), two = secretField("The same again")
    let detail = "This opens the vault when your fingerprint cannot. It is not stored anywhere and it cannot be recovered: if you lose it and your fingerprint stops working, what is in the vault is gone."
    guard ask("Create your XENIO vault", detail, [one, two], confirm: "Create") else { throw VaultError.cancelled }
    guard one.stringValue == two.stringValue else { throw VaultError.refused("the two passwords were not the same") }
    return one.stringValue
}

func askForLogin(host: String) throws -> (String, String, Login) {
    let site = NSTextField(frame: CGRect(x: 0, y: 0, width: 300, height: 24)); site.placeholderString = "Website, like linkedin.com"; site.stringValue = host
    let name = NSTextField(frame: CGRect(x: 0, y: 0, width: 300, height: 24)); name.placeholderString = "A name for it (optional)"
    let user = NSTextField(frame: CGRect(x: 0, y: 0, width: 300, height: 24)); user.placeholderString = "Username or email"
    let pass = secretField("Password")
    guard ask("Add a login to your vault", "It is encrypted before it leaves this window. No agent, model or XENIO itself can read it back: it can only ever be placed on this website.", [site, name, user, pass], confirm: "Save") else { throw VaultError.cancelled }
    return (site.stringValue, name.stringValue, Login(username: user.stringValue, password: pass.stringValue))
}

/// macOS cancels a fingerprint request at once (systemCancel) unless it comes from the active app, and a real one was gone within a second. So the helper is active for as long as it asks, and it keeps its run loop turning while it waits.
func askForFinger(_ vault: Vault, reason: String) throws -> SymmetricKey {
    if NSApp.activationPolicy() == .prohibited { NSApp.setActivationPolicy(.accessory) }
    NSApp.finishLaunching()
    NSApp.activate(ignoringOtherApps: true)
    return try waitOffMain { try vault.unlockWithTouch(reason: reason) }
}

/// Runs work on another thread and waits for it here, keeping windows alive meanwhile. The answer comes back through a lock and never through the main queue: called from inside a main-queue block, that queue cannot run anything else until the block ends, so an answer sent that way never arrives. That is how saving a login once hung for ever after the fingerprint had been accepted.
final class WaitBox<T>: @unchecked Sendable {
    let lock = NSLock()
    var value: Result<T, Error>?
}

func waitOffMain<T>(_ work: @escaping () throws -> T) throws -> T {
    let box = WaitBox<T>()
    DispatchQueue.global().async {
        let result = Result { try work() }
        box.lock.lock(); box.value = result; box.lock.unlock()
    }
    while true {
        box.lock.lock(); let value = box.value; box.lock.unlock()
        if let value { return try value.get() }
        _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
    }
}

/// Your finger if it is set up, and your secret password if not, or if your finger cannot.
func unlockAsking(_ vault: Vault, reason: String) throws -> SymmetricKey {
    if vault.touchReady && Vault.touchAvailable() {
        do { return try askForFinger(vault, reason: reason) } catch VaultError.cancelled { throw VaultError.cancelled } catch {}
    }
    return try vault.unlock(secret: try askForSecret(reason: reason))
}

// ---- the command line. Each command prints one line of JSON, and none of them prints anything secret -----------------------

func readSecrets() -> [String: String] {
    guard CommandLine.arguments.contains("--secrets-on-stdin"), let line = readLine(), let data = line.data(using: .utf8), let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: String] else { return [:] }
    return parsed
}

func runVault() -> Never {
    func finish(_ value: [String: Any]) -> Never { say(value); exit(value["ok"] as? Bool == true ? 0 : 1) }
    guard CommandLine.arguments.count > 2, let folder = option("dir") else { finish(["ok": false, "error": "usage: vault <command> --dir FOLDER"]) }
    let vault = Vault(folder: folder)
    let given = readSecrets()  // for tests. Whoever can use this must already know the secret password, so it gives them nothing they did not have
    func openIt(_ reason: String) throws -> SymmetricKey {
        if let secret = given["secret"] { return try vault.unlock(secret: secret) }
        return try unlockAsking(vault, reason: reason)
    }
    do {
        switch CommandLine.arguments[2] {
        case "status":
            finish(vault.publicState())
        case "setup":
            let secret = try given["secret"] ?? askForNewSecret()
            let key = try vault.create(secret: secret)
            var state = vault.publicState()
            if given["secret"] == nil && Vault.touchAvailable() {
                try vault.enrolTouch(key: key)
                state = vault.publicState()
            }
            finish(state)
        case "touch":
            let key = try openIt("Turn on Touch ID for your XENIO vault")
            let before = vault.data?.touch
            try vault.enrolTouch(key: key)
            do {  // it is not on until your finger has really opened it once
                let again = try askForFinger(vault, reason: "Check that your fingerprint opens your XENIO vault")
                guard again == key else { throw VaultError.refused("your fingerprint did not open it") }
            } catch {
                try vault.setTouch(before)
                throw error
            }
            finish(vault.publicState())
        case "add":
            let key = try openIt("Add a login to your XENIO vault")
            let (host, label, login) = given["password"] != nil ? (option("host") ?? "", option("label") ?? "", Login(username: given["username"] ?? "", password: given["password"]!)) : try askForLogin(host: option("host") ?? "")
            let item = try vault.add(key: key, host: host, label: label, login: login)
            finish(["ok": true, "id": item.id, "host": item.host, "label": item.label])
        case "remove":
            let key = try openIt("Remove a login from your XENIO vault")
            try vault.remove(key: key, id: option("id") ?? "")
            finish(vault.publicState())
        case "fill-window":
            // your email and password, placed into a window of your own browser, after your yes. It is checked first that the window really shows that website, over HTTPS
            guard let item = vault.item(id: option("item") ?? "") else { throw VaultError.refused("there is no such login in the vault") }
            guard let pid = Int32(option("pid") ?? "") else { throw VaultError.refused("which browser was not said") }
            let page = try BrowserPage(pid: pid, title: option("title") ?? "")
            let shown = page.url()
            guard let url = shown, let host = url.host?.lowercased(), hostMatches(host, saved: item.host), url.scheme?.lowercased() == "https" || (option("allow-local") != nil && host == "127.0.0.1" && String(url.port ?? 0) == option("allow-local")) else {
                throw VaultError.refused("that window does not show \(item.host) over a secure connection, so nothing was placed")
            }
            let key: SymmetricKey
            if let secret = given["secret"] { key = try vault.unlock(secret: secret) } else { key = try unlockAsking(vault, reason: "Place your email and password for \(item.host)") }
            let login = try vault.login(key: key, item: item)
            finish(try page.fill(login, host: item.host, allowLocal: option("allow-local")))
        case "check-wait":
            // a test: waiting from inside a main-queue block, which is where a browser's handlers run, has to come back
            DispatchQueue.main.async {
                let got = try? waitOffMain { () -> Int in Thread.sleep(forTimeInterval: 0.3); return 42 }
                finish(["ok": got == 42, "got": got ?? 0])
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 8) { finish(["ok": false, "error": "it never came back"]) }
            RunLoop.main.run()
            finish(["ok": false])
        case "check-host":
            finish(["ok": true, "clean": cleanHost(option("saved") ?? "") ?? "", "matches": hostMatches(option("page") ?? "", saved: cleanHost(option("saved") ?? "") ?? "-")])
        case "check-finger-lock":
            // Makes a throwaway key locked to a fingerprint and tries to use it without asking anyone: this has to fail. No window, no finger, nothing kept.
            guard Vault.touchAvailable() else { finish(["ok": true, "available": false]) }
            let enclave = try SecureEnclave.P256.KeyAgreement.PrivateKey(accessControl: try Vault.fingerControl())
            let other = P256.KeyAgreement.PrivateKey().publicKey
            let context = LAContext()
            context.interactionNotAllowed = true
            let reopened = try SecureEnclave.P256.KeyAgreement.PrivateKey(dataRepresentation: enclave.dataRepresentation, authenticationContext: context)
            var used = false, why = ""
            do { _ = try reopened.sharedSecretFromKeyAgreement(with: other); used = true } catch { why = String(describing: type(of: error)) }
            finish(["ok": true, "available": true, "used_without_finger": used, "refusal": why])
        default:
            finish(["ok": false, "error": "unknown vault command"])
        }
    } catch let problem as VaultError {
        finish(["ok": false, "error": problem.words])
    } catch {
        finish(["ok": false, "error": "the vault could not do that"])
    }
}


// ---- a page in your own browser, reached the way a screen reader reaches it ----------------------------------------------

final class BrowserPage {
    let app: AXUIElement
    var window: AXUIElement

    init(pid: Int32, title: String) throws {
        guard AXIsProcessTrusted() else { throw VaultError.refused("XENIO needs Accessibility permission to place a login in your browser") }
        app = AXUIElementCreateApplication(pid)
        let windows = (BrowserPage.value(app, "AXWindows") as? [AXUIElement]) ?? []
        let wanted = title.lowercased()
        if let match = windows.first(where: { !wanted.isEmpty && ((BrowserPage.value($0, "AXTitle") as? String) ?? "").lowercased() == wanted }) {
            window = match
        } else if let focused = BrowserPage.value(app, "AXFocusedWindow") {
            window = focused as! AXUIElement
        } else if let first = windows.first {
            window = first
        } else {
            throw VaultError.refused("that browser has no window open")
        }
    }

    static func value(_ element: AXUIElement, _ name: String) -> AnyObject? {
        var out: CFTypeRef?
        return AXUIElementCopyAttributeValue(element, name as CFString, &out) == .success ? out : nil
    }

    func all(_ root: AXUIElement, limit: Int = 6000) -> [AXUIElement] {
        var out: [AXUIElement] = [], queue = [root]
        while !queue.isEmpty && out.count < limit {
            let next = queue.removeFirst()
            out.append(next)
            queue.append(contentsOf: (BrowserPage.value(next, "AXChildren") as? [AXUIElement]) ?? [])
        }
        return out
    }

    var enhanced = false

    func webArea() -> AXUIElement? {
        for attempt in 0..<12 {
            if let area = all(window).first(where: { (BrowserPage.value($0, "AXRole") as? String) == "AXWebArea" }) { return area }
            if attempt == 1 && !enhanced {  // some browsers only build the page's controls once a screen reader says it is there, as VoiceOver does
                enhanced = true
                for name in ["AXManualAccessibility", "AXEnhancedUserInterface"] { _ = AXUIElementSetAttributeValue(app, name as CFString, kCFBooleanTrue) }
            }
            Thread.sleep(forTimeInterval: 0.2)
        }
        return nil
    }

    func url() -> URL? {
        guard let area = webArea(), let raw = BrowserPage.value(area, "AXURL") else { return nil }
        if let url = raw as? URL { return url }
        return URL(string: "\(raw)")
    }

    func shown(_ element: AXUIElement) -> Bool {
        var size = CGSize.zero
        if let raw = BrowserPage.value(element, "AXSize") { AXValueGetValue(raw as! AXValue, .cgSize, &size) }
        return size.width > 2 && size.height > 2 && (BrowserPage.value(element, "AXEnabled") as? Bool ?? true)
    }

    func fields() -> (account: AXUIElement?, secret: AXUIElement?) {
        guard let area = webArea() else { return (nil, nil) }
        let boxes = all(area).filter { ["AXTextField", "AXComboBox"].contains(BrowserPage.value($0, "AXRole") as? String ?? "") && shown($0) }
        let secret = boxes.first { (BrowserPage.value($0, "AXSubrole") as? String) == "AXSecureTextField" }
        let plain = boxes.filter { (BrowserPage.value($0, "AXSubrole") as? String) != "AXSecureTextField" }
        let said = { (e: AXUIElement) -> String in ["AXTitle", "AXDescription", "AXPlaceholderValue", "AXIdentifier", "AXHelp"].map { (BrowserPage.value(e, $0) as? String) ?? "" }.joined(separator: " ").lowercased() }
        var account = plain.first { said($0).range(of: #"e-?mail|user|login|phone|identifier|account"#, options: .regularExpression) != nil }
        if account == nil, let secret, let at = boxes.firstIndex(where: { CFEqual($0, secret) }), at > 0 { account = boxes[..<at].last { (BrowserPage.value($0, "AXSubrole") as? String) != "AXSecureTextField" } }
        if account == nil && secret == nil && plain.count == 1 { account = plain.first }
        return (account, secret)
    }

    func put(_ element: AXUIElement, _ text: String) -> Bool {
        _ = AXUIElementSetAttributeValue(element, "AXFocused" as CFString, kCFBooleanTrue)
        Thread.sleep(forTimeInterval: 0.08)
        return AXUIElementSetAttributeValue(element, "AXValue" as CFString, text as CFString) == .success
    }

    func pressNext() -> Bool {
        guard let area = webArea() else { return false }
        let buttons = all(area).filter { (BrowserPage.value($0, "AXRole") as? String) == "AXButton" && shown($0) }
        let named = { (e: AXUIElement) -> String in ((BrowserPage.value(e, "AXTitle") as? String) ?? (BrowserPage.value(e, "AXDescription") as? String) ?? "").trimmingCharacters(in: .whitespaces).lowercased() }
        guard let next = buttons.first(where: { ["next", "continue", "weiter", "suivant"].contains(named($0)) }) else { return false }
        return AXUIElementPerformAction(next, "AXPress" as CFString) == .success
    }

    /// The name first, then the password, on one screen or two. Nothing is pressed after that: signing in is the agent's own step, and it is judged like any other.
    func fill(_ login: Login, host: String, allowLocal: String?) throws -> [String: Any] {
        var filled: [String] = []
        for _ in 0..<3 {
            guard let shownURL = url(), let now = shownURL.host?.lowercased(), hostMatches(now, saved: host) else {
                throw VaultError.refused("the page moved away from \(host), so the password was not placed")
            }
            let found = fields()
            if let account = found.account, !filled.contains("email"), !login.username.isEmpty, put(account, login.username) { filled.append("email") }
            if let secret = found.secret {
                guard put(secret, login.password) else { throw VaultError.refused("the password box would not take it") }
                filled.append("password")
                let count = (BrowserPage.value(secret, "AXNumberOfCharacters") as? Int) ?? -1
                return ["ok": true, "filled": filled, "checked": count == login.password.count ? "the box holds the right number of characters" : "it could not be counted"]
            }
            if filled.contains("email"), pressNext() {
                for _ in 0..<30 { Thread.sleep(forTimeInterval: 0.4); if fields().secret != nil { break } }
                continue
            }
            break
        }
        if filled.isEmpty { throw VaultError.refused("there is no sign-in form on that page") }
        return ["ok": true, "filled": filled, "note": "the password box never appeared"]
    }
}
