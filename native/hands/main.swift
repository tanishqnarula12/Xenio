// xenio-hands: what lets XENIO's agents use a computer without touching yours.
//
//   xenio-hands browser [--width N --height N --agent NAME --color #hex --live PATH --allow-local PORT --profile NAME --visible]
//       A real WebKit browser with no window on your screen, driven by one JSON line per command. Each agent gets its own process and its
//       own private cookies, so agents really do work side by side. No profile means nothing is kept. Reading a page gives text, which costs
//       a model a fraction of what a screenshot does.
//   xenio-hands cursors
//       A see-through layer over your screen showing a labelled cursor for an agent working on your real desktop. It draws what it is told by
//       XENIO, from real actions, and it never moves the mouse or takes a click.
import Cocoa
import CryptoKit
import WebKit

func say(_ value: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: value), let line = String(data: data, encoding: .utf8) else { return }
    print(line)
    fflush(stdout)
}

func option(_ name: String) -> String? {
    guard let at = CommandLine.arguments.firstIndex(of: "--\(name)"), at + 1 < CommandLine.arguments.count else { return nil }
    return CommandLine.arguments[at + 1]
}

func flag(_ name: String) -> Bool { CommandLine.arguments.contains("--\(name)") }

// ---- what runs inside the page ------------------------------------------------------------------------------------------

let pageHelpers = #"""
(function () {
  if (window.__xenio) return;
  const NAME = window.__xenioAgent || "Agent", COLOR = window.__xenioColor || "#ff5a36";
  const interactive = "a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],[contenteditable=true],[onclick]";
  const shown = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0"; };
  const clean = (t, n) => String(t || "").replace(/\s+/g, " ").trim().slice(0, n || 80);
  const labelled = (e) => { const by = e.getAttribute("aria-labelledby"); const named = by && document.getElementById(by); return clean(e.getAttribute("aria-label") || (named && named.innerText) || (e.labels && e.labels[0] && e.labels[0].innerText) || e.innerText || (!secret(e) && e.value) || e.placeholder || e.title || e.alt || e.name || e.id); };
  const secret = (e) => { const t = (e.type || "").toLowerCase(), a = (e.autocomplete || "").toLowerCase(); return e.hasAttribute("data-xenio-filled") || t === "password" || /^cc-|one-time-code|current-password|new-password/.test(a); };
  const kindOf = (e) => { const t = e.tagName.toLowerCase(), r = e.getAttribute("role"); return r || (t === "a" ? "link" : t === "input" ? (e.type || "text") + " field" : t === "textarea" ? "text box" : t === "select" ? "list" : t); };
  let cursor = null;
  const draw = () => {
    if (cursor && cursor.isConnected) return cursor;
    cursor = document.createElement("div");
    cursor.setAttribute("aria-hidden", "true");
    cursor.style.cssText = "position:fixed;left:0;top:0;z-index:2147483647;pointer-events:none;transform:translate(24px,24px)";
    cursor.innerHTML = '<svg width="22" height="22" viewBox="0 0 22 22" style="display:block;filter:drop-shadow(0 1px 2px rgba(0,0,0,.5))"><path d="M3 2l15 8.5-6.5 1.5L8 18z" fill="' + COLOR + '" stroke="#fff" stroke-width="1.4"/></svg><div style="margin:2px 0 0 14px;padding:2px 7px;border-radius:9px;background:' + COLOR + ';color:#fff;font:600 11px -apple-system,sans-serif;white-space:nowrap">' + NAME.replace(/[<>&]/g, "") + " · agent cursor</div>";
    document.documentElement.appendChild(cursor);
    return cursor;
  };
  const ripple = (x, y) => {
    const dot = document.createElement("div");
    dot.style.cssText = "position:fixed;z-index:2147483646;pointer-events:none;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;border:2px solid " + COLOR + ";left:" + x + "px;top:" + y + "px;transition:all .4s ease-out";
    document.documentElement.appendChild(dot);
    requestAnimationFrame(() => { dot.style.transform = "scale(1.8)"; dot.style.opacity = "0"; });
    setTimeout(() => dot.remove(), 450);
  };
  const point = (e) => { e.scrollIntoView({ block: "center", inline: "center", behavior: "auto" }); const r = e.getBoundingClientRect(); return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) }; };
  const moveTo = (p) => { draw().style.transform = "translate(" + p.x + "px," + p.y + "px)"; };
  const find = (ref) => document.querySelector('[data-xenio-ref="' + ref + '"]');
  const byText = (text) => { const want = clean(text, 200).toLowerCase(); let best = null; for (const e of document.querySelectorAll(interactive)) { if (!shown(e)) continue; const said = labelled(e).toLowerCase(); if (said === want) return e; if (!best && said.includes(want)) best = e; } return best; };
  window.__xenio = {
    snapshot(limit) {
      const lines = []; let next = window.__xenioNext || 1, used = 0;
      const add = (l) => { if (used + l.length > limit) return false; lines.push(l); used += l.length + 1; return true; };
      const walk = (node) => {
        if (node.nodeType !== 1 || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|SVG|PATH)$/i.test(node.tagName) || node === cursor) return true;
        if (!shown(node) && !node.querySelector) return true;
        const tag = node.tagName.toLowerCase();
        if (node.matches(interactive) && shown(node)) {
          let ref = node.getAttribute("data-xenio-ref");
          if (!ref) { ref = String(next++); node.setAttribute("data-xenio-ref", ref); }
          const facts = [];
          if (node.hasAttribute("data-xenio-filled")) facts.push("filled from the CEO's vault: you cannot see it");
          else if (secret(node)) facts.push("private: a person types this, not you");
          else if (/^(input|textarea|select)$/.test(tag) && node.value && node.type !== "submit" && node.type !== "button") facts.push("value=" + JSON.stringify(clean(node.value, 120)));
          if (node.checked) facts.push("checked");
          if (node.disabled) facts.push("disabled");
          if (tag === "select") facts.push("options: " + Array.from(node.options).slice(0, 12).map((o) => clean(o.text, 30)).join(" | "));
          if (tag === "a" && node.href) { try { const u = new URL(node.href); if (u.host !== location.host) facts.push("to " + u.host); } catch (e) {} }
          if (!add("[" + ref + "] " + kindOf(node) + ' "' + labelled(node) + '"' + (facts.length ? " (" + facts.join(", ") + ")" : ""))) return false;
          if (tag !== "summary" && tag !== "a" && tag !== "button") return true;
          return true;
        }
        if (/^h[1-6]$/.test(tag) && shown(node)) { const t = clean(node.innerText, 160); if (t && !add("#".repeat(+tag[1]) + " " + t)) return false; return true; }
        if (/^(p|li|td|th|blockquote|figcaption|dd|dt|label)$/.test(tag) && shown(node) && !node.querySelector(interactive)) { const t = clean(node.innerText, 220); if (t && !add(t)) return false; return true; }
        for (const child of node.children) { if (!walk(child)) return false; }
        return true;
      };
      const whole = document.body ? walk(document.body) : true;
      window.__xenioNext = next;
      return { lines: lines.join("\n"), cut: !whole, url: location.href, title: document.title, scroll: Math.round(scrollY), height: Math.round(document.documentElement.scrollHeight), view: innerHeight };
    },
    text(limit) { return { text: (document.body ? document.body.innerText : "").slice(0, limit), url: location.href, title: document.title }; },
    target(ref, text) { const e = ref ? find(ref) : byText(text); if (!e) return { error: "That is not on the page any more: read the page again." }; return { ref: ref, label: labelled(e), kind: kindOf(e), secret: secret(e) }; },
    click(ref, text) {
      const e = ref ? find(ref) : byText(text);
      if (!e) return { error: "That is not on the page any more: read the page again." };
      const p = point(e); moveTo(p); ripple(p.x, p.y);
      const opts = { bubbles: true, cancelable: true, view: window, clientX: p.x, clientY: p.y, button: 0 };
      if (e.focus) e.focus({ preventScroll: true });
      for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) { try { e.dispatchEvent(new (type.startsWith("pointer") ? PointerEvent : MouseEvent)(type, opts)); } catch (x) {} }
      e.click();
      return { x: p.x, y: p.y, label: labelled(e), kind: kindOf(e) };
    },
    type(ref, text, clear, submit) {
      const e = find(ref);
      if (!e) return { error: "That is not on the page any more: read the page again." };
      if (secret(e)) return { error: "That is a private field. A person types that themselves." };
      const p = point(e); moveTo(p); e.focus({ preventScroll: true });
      if (e.isContentEditable) {
        if (clear) { document.execCommand("selectAll"); document.execCommand("delete"); }
        document.execCommand("insertText", false, text);
      } else {
        const proto = e.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : e.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
        const set = Object.getOwnPropertyDescriptor(proto, "value").set;
        set.call(e, (clear ? "" : e.value) + text);
        e.dispatchEvent(new InputEvent("input", { bubbles: true, data: text, inputType: "insertText" }));
        e.dispatchEvent(new Event("change", { bubbles: true }));
      }
      const now = e.isContentEditable ? e.innerText : e.value;
      if (submit) { const form = e.form; if (form && form.requestSubmit) form.requestSubmit(); else { for (const t of ["keydown", "keypress", "keyup"]) e.dispatchEvent(new KeyboardEvent(t, { key: "Enter", code: "Enter", bubbles: true })); } }
      return { x: p.x, y: p.y, value: clean(now, 200), label: labelled(e) };
    },
    choose(ref, value) {
      const e = find(ref);
      if (!e || e.tagName !== "SELECT") return { error: "That is not a list on the page." };
      const want = clean(value, 120).toLowerCase();
      const hit = Array.from(e.options).find((o) => clean(o.text, 120).toLowerCase() === want || o.value.toLowerCase() === want) || Array.from(e.options).find((o) => clean(o.text, 120).toLowerCase().includes(want));
      if (!hit) return { error: "That list has no such option." };
      const p = point(e); moveTo(p); ripple(p.x, p.y);
      e.value = hit.value; e.dispatchEvent(new Event("input", { bubbles: true })); e.dispatchEvent(new Event("change", { bubbles: true }));
      return { x: p.x, y: p.y, value: clean(hit.text, 120), label: labelled(e) };
    },
    key(name) { const e = document.activeElement || document.body; for (const t of ["keydown", "keypress", "keyup"]) e.dispatchEvent(new KeyboardEvent(t, { key: name, code: name, bubbles: true, cancelable: true })); return { key: name }; },
    scroll(dy) { window.scrollBy({ top: dy, behavior: "auto" }); return { scroll: Math.round(scrollY), height: Math.round(document.documentElement.scrollHeight), view: innerHeight }; },
    audit() {
      const faults = [], W = document.documentElement.clientWidth;
      const width = document.documentElement.scrollWidth;
      if (width > W + 2) faults.push("the page is " + (width - W) + "px wider than the window, so it scrolls sideways");
      const lump = (e) => clean(e.innerText, 50);
      const texts = Array.from(document.querySelectorAll("h1,h2,h3,h4,p,li,a,button,label,span,blockquote,figcaption")).filter((e) => shown(e) && e.children.length === 0 && clean(e.innerText).length > 1).slice(0, 400);
      let wide = 0;
      for (const e of texts) { const r = e.getBoundingClientRect(); if (r.right > W + 2 || r.left < -2) { if (wide++ < 3) faults.push("text runs off the side: \"" + lump(e) + "\""); } }
      let covered = 0, tiny = 0, faint = 0;
      const lum = (c) => { const m = c.match(/[\d.]+/g); if (!m) return null; const a = m.length > 3 ? +m[3] : 1; if (a === 0) return null; const f = (v) => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); }; return { l: .2126 * f(+m[0]) + .7152 * f(+m[1]) + .0722 * f(+m[2]), a }; };
      for (const e of texts) {
        const r = e.getBoundingClientRect(), s = getComputedStyle(e);
        if (parseFloat(s.fontSize) < 11 && tiny++ < 2) faults.push("text under 11px: \"" + lump(e) + "\"");
        let box = e, back = null;
        while (box && !back) { const c = lum(getComputedStyle(box).backgroundColor); const img = getComputedStyle(box).backgroundImage !== "none"; if (img) break; if (c && c.a > .9) back = c; box = box.parentElement; }
        const front = lum(s.color);
        if (back && front) { const hi = Math.max(back.l, front.l), lo = Math.min(back.l, front.l), ratio = (hi + .05) / (lo + .05); if (ratio < 3 && parseFloat(s.opacity) > .5 && faint++ < 3) faults.push("text is hard to read (contrast " + ratio.toFixed(1) + ":1): \"" + lump(e) + "\""); }
        if (r.top >= 0 && r.bottom <= innerHeight && r.width > 8 && r.height > 8) {
          const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          if (hit && hit !== e && !e.contains(hit) && !hit.contains(e) && hit !== cursor && !hit.closest("a,button") && clean(hit.innerText).length > 1 && covered++ < 3) faults.push("\"" + lump(e) + "\" is covered by \"" + lump(hit) + "\"");
        }
      }
      for (const img of document.images) { if (img.complete && img.naturalWidth === 0 && img.src && faults.length < 14) faults.push("a broken image: " + clean(img.src, 80)); }
      const fonts = Array.from(new Set(texts.slice(0, 120).map((e) => getComputedStyle(e).fontFamily.split(",")[0].replace(/["']/g, "").trim()))).slice(0, 6);
      const body = getComputedStyle(document.body);
      const looks = { fonts, background: body.backgroundColor, color: body.color, words: clean(document.body.innerText, 100000).split(" ").length, height: document.documentElement.scrollHeight };
      return { faults: faults.slice(0, 14), looks, width: W };
    },
  };
})();
"""#

// ---- the browser --------------------------------------------------------------------------------------------------------

// what a login does inside the page: it runs in a world of its own that the page's scripts cannot reach or wrap, and returns no values
let fillLogin = #"""
const shown = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 2 && r.height > 2 && s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0"; };
const fields = Array.from(document.querySelectorAll("input")).filter((e) => shown(e) && !e.disabled && !e.readOnly);
const pass = fields.find((e) => e.type === "password") || null;
const usable = (e) => /^(text|email|tel|)$/.test(e.getAttribute("type") || "");
const said = (e) => [e.name, e.id, e.autocomplete, e.placeholder, e.getAttribute("aria-label"), e.type].join(" ");
let user = fields.find((e) => usable(e) && /user|email|e-mail|login|identifier|account|phone/i.test(said(e))) || null;
if (!user && pass) { const before = fields.slice(0, fields.indexOf(pass)).filter(usable); user = before[before.length - 1] || null; }
if (!user && !pass) { const only = fields.filter(usable); if (only.length === 1) user = only[0]; }
const put = (e, value) => {
  e.focus();
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(e, value);
  e.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText" }));
  e.dispatchEvent(new Event("change", { bubbles: true }));
  e.setAttribute("data-xenio-filled", "1");
};
const filled = [];
if (user && username && !user.hasAttribute("data-xenio-filled")) { put(user, username); filled.push("username"); }
if (pass) { put(pass, password); filled.push("password"); }
const target = pass || user;
let pressed = false;
if (target && go) {
  const form = target.form;
  const buttons = Array.from((form || document).querySelectorAll("button,input[type=submit],[role=button]")).filter(shown);
  const button = buttons.find((b) => (b.type === "submit" && b.tagName !== "A") && /sign|log|next|continue|submit|enter|go/i.test((b.innerText || b.value || b.getAttribute("aria-label") || "") + " ")) || buttons.find((b) => /^(sign in|log ?in|next|continue|submit)$/i.test(((b.innerText || b.value || b.getAttribute("aria-label") || "") + "").trim())) || buttons.find((b) => b.type === "submit");
  if (button) { button.click(); pressed = true; } else if (form && form.requestSubmit) { form.requestSubmit(); pressed = true; }
}
return { filled, pressed, password_asked: !!pass, username_asked: !!user };
"""#

// after a fill, so that a "show password" eye on the page cannot turn it into words an agent could read
let lockFilled = #"""
(function () { document.querySelectorAll("input[data-xenio-filled]").forEach((e) => {
  if (e.__xenioLocked) return; e.__xenioLocked = true;
  const kind = e.type; Object.defineProperty(e, "type", { get: () => kind, set: () => {} });
  const set = e.setAttribute.bind(e); e.setAttribute = (name, value) => { if (String(name).toLowerCase() !== "type") set(name, value); };
}); return {ok: true}; })()
"""#

// only when a person is signing in by hand in a window they can see, and only from real key presses and clicks. It runs out of reach of the page's own scripts
let offerToSave = #"""
(function () {
  let name = "";
  const note = (e) => { if (!e.isTrusted) return; const t = e.target; if (t && t.tagName === "INPUT" && t.type !== "password" && /^(text|email|tel)$/.test(t.type) && t.value && /user|email|login|identifier|account|phone/i.test([t.name, t.id, t.autocomplete, t.placeholder, t.type].join(" "))) name = t.value; };
  const send = (e) => { if (!e.isTrusted) return; const p = Array.from(document.querySelectorAll("input[type=password]")).find((x) => x.value); if (p) window.webkit.messageHandlers.xenioSave.postMessage({ username: name, password: p.value }); };
  document.addEventListener("input", note, true);
  document.addEventListener("submit", send, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target && e.target.tagName === "INPUT") send(e); }, true);
  document.addEventListener("click", (e) => { if (e.target && e.target.closest && e.target.closest("button,input[type=submit],[role=button]")) send(e); }, true);
})();
"""#

final class Browser: NSObject, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, WKScriptMessageHandler, NSWindowDelegate {
    let width: CGFloat, height: CGFloat
    let view: WKWebView
    let window: NSWindow
    let live: String?
    let localPort: String?
    var loading: ((_ error: String?) -> Void)?
    var dialogs: [String] = []
    var pending = 0
    var refused: [String] = []
    var activity: NSObjectProtocol?
    var vault: Vault?
    var signingIn = false
    var handedOver: Int?
    var handback: NSRect = .zero
    var offered = Set<String>()

    init(width: CGFloat, height: CGFloat, profile: String?, visible: Bool) {
        self.width = width
        self.height = height
        live = option("live")
        localPort = option("allow-local")
        let config = WKWebViewConfiguration()
        if let profile, #available(macOS 14.0, *) {
            config.websiteDataStore = WKWebsiteDataStore(forIdentifier: Browser.identifier(profile))
        } else {
            config.websiteDataStore = .nonPersistent()
        }
        config.preferences.javaScriptCanOpenWindowsAutomatically = false
        if #available(macOS 14.0, *) { config.preferences.inactiveSchedulingPolicy = .none }  // an agent's page has to keep running, animations and timers too, with nobody looking at it
        let setup = "window.__xenioAgent = \(Browser.quoted(option("agent") ?? "Agent")); window.__xenioColor = \(Browser.quoted(option("color") ?? "#ff5a36"));" + pageHelpers
        config.userContentController.addUserScript(WKUserScript(source: setup, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
        if option("vault-dir") != nil {
            config.userContentController.addUserScript(WKUserScript(source: offerToSave, injectionTime: .atDocumentEnd, forMainFrameOnly: true, in: .defaultClient))
        }
        view = WKWebView(frame: CGRect(x: 0, y: 0, width: width, height: height), configuration: config)
        let keepRunning = Selector(("_setWindowOcclusionDetectionEnabled:"))
        if view.responds(to: keepRunning) {  // WebKit's own switch for this. Without it a window nobody can see stops drawing, and a page that animates in looks empty
            typealias Setter = @convention(c) (AnyObject, Selector, Bool) -> Void
            unsafeBitCast(view.method(for: keepRunning), to: Setter.self)(view, keepRunning, false)
        }
        view.customUserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
        if visible {
            let corner = NSScreen.main?.visibleFrame ?? CGRect(x: 0, y: 0, width: 1440, height: 900)
            let at = flag("corner") ? CGPoint(x: corner.maxX - width - 16, y: corner.minY + 16) : CGPoint(x: 120, y: 120)
            window = NSWindow(contentRect: CGRect(origin: at, size: CGSize(width: width, height: height)), styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
            window.title = option("window-title") ?? "XENIO: sign in here. Only you type. Close this window when you are done."
            window.isReleasedWhenClosed = false
        } else {
            window = NSWindow(contentRect: CGRect(x: -12000, y: -12000, width: width, height: height), styleMask: [.borderless], backing: .buffered, defer: false)
        }
        window.contentView = view
        super.init()
        if let folder = option("vault-dir") {
            vault = Vault(folder: folder)
            config.userContentController.add(self, contentWorld: .defaultClient, name: "xenioSave")
        }
        window.delegate = self
        view.navigationDelegate = self
        view.uiDelegate = self
        if visible {
            NSApp.setActivationPolicy(.regular)
            if flag("no-activate") { window.orderFrontRegardless() } else {
                window.makeKeyAndOrderFront(nil)
                NSApp.activate(ignoringOtherApps: true)
            }
            NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: window, queue: .main) { _ in exit(0) }
        } else {
            window.orderBack(nil)
        }
        // without this macOS puts a hidden browser to sleep after a few minutes, and it stops answering
        activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiatedAllowingIdleSystemSleep, .latencyCritical], reason: "an agent is using this browser")
        let list = #"[{"trigger":{"url-filter":"^https?://(localhost|127\\.|10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|169\\.254\\.|\\[::1\\]|0\\.0\\.0\\.0)"},"action":{"type":"block"}}]"#
        if localPort == nil {
            WKContentRuleListStore.default().compileContentRuleList(forIdentifier: "xenio-no-private-network", encodedContentRuleList: list) { rules, _ in
                if let rules { self.view.configuration.userContentController.add(rules) }
            }
        }
    }

    static func identifier(_ name: String) -> UUID {
        var bytes = [UInt8](repeating: 0, count: 16)
        for (index, value) in name.utf8.enumerated() { bytes[index % 16] = bytes[index % 16] &* 31 &+ value }
        bytes[6] = (bytes[6] & 0x0f) | 0x50
        bytes[8] = (bytes[8] & 0x3f) | 0x80
        return UUID(uuid: (bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7], bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]))
    }

    static func quoted(_ text: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: [text])) ?? Data("[\"\"]".utf8)
        return String(String(data: data, encoding: .utf8)!.dropFirst().dropLast())
    }

    // ---- signing in from the vault ------------------------------------------------------------------------------------------

    /// HTTPS, and the website the login was saved for. Asked of WebKit at the moment of filling, not of anything an agent said.
    func pageMayHave(_ item: VaultItem) -> Bool {
        guard let url = view.url, let host = url.host?.lowercased(), hostMatches(host, saved: item.host) else { return false }
        if url.scheme?.lowercased() == "https" { return true }
        return localPort != nil && host == "127.0.0.1" && String(url.port ?? 0) == localPort
    }

    func signIn(_ c: [String: Any], _ id: Int) {
        func refuse(_ why: String) { say(["id": id, "ok": false, "error": why]) }
        guard let vault = vault else { refuse("this browser has no vault"); return }
        vault.data = (try? Data(contentsOf: vault.file)).flatMap { try? JSONDecoder().decode(VaultFile.self, from: $0) }
        guard let item = vault.item(id: c["item"] as? String ?? "") else { refuse("there is no such login in the vault"); return }
        guard pageMayHave(item) else { refuse("this page is not \(item.host) over a secure connection, so that login was not touched"); return }
        let reason = "\(option("agent") ?? "An agent") wants to sign in to \(item.host)"
        let overPipe = flag("allow-secret-over-pipe") ? c["secret"] as? String : nil  // tests only: nothing in XENIO ever knows this
        let go = c["submit"] as? Bool ?? true
        let needAsking = !(vault.touchReady && Vault.touchAvailable())
        if overPipe == nil && needAsking {  // a window has to be on the main thread, and the browser waits while you type
            do { finishUnlock(try unlockAsking(vault, reason: reason), item, vault, go, id) } catch let problem as VaultError { refuse(problem.words) } catch { refuse("the vault could not be opened") }
            quietAgain()
            return
        }
        if overPipe == nil {  // macOS only lets the active app ask for a finger
            if NSApp.activationPolicy() == .prohibited { NSApp.setActivationPolicy(.accessory) }
            NSApp.activate(ignoringOtherApps: true)
        }
        DispatchQueue.global().async {
            do {
                let key: SymmetricKey
                if let overPipe { key = try vault.unlock(secret: overPipe) } else { key = try vault.unlockWithTouch(reason: reason) }
                DispatchQueue.main.async { self.quietAgain(); self.finishUnlock(key, item, vault, go, id) }
            } catch VaultError.cancelled {
                DispatchQueue.main.async { self.quietAgain(); refuse("the CEO cancelled the fingerprint request") }
            } catch {
                if overPipe != nil {
                    DispatchQueue.main.async { refuse("that is not the secret password") }
                    return
                }
                DispatchQueue.main.async {  // the finger did not work: your secret password is the other way in
                    do { self.finishUnlock(try vault.unlock(secret: try askForSecret(reason: reason)), item, vault, go, id) } catch let problem as VaultError { refuse(problem.words) } catch { refuse("the vault could not be opened") }
                    self.quietAgain()
                }
            }
        }
    }

    /// Back out of the way, and back to whatever you were doing, unless this is a window you are using.
    func quietAgain() {
        guard handedOver == nil, window.frame.minX < -5000 else { return }
        NSApp.hide(nil)
        NSApp.setActivationPolicy(.prohibited)
    }

    func finishUnlock(_ key: SymmetricKey, _ item: VaultItem, _ vault: Vault, _ go: Bool, _ id: Int) {
        guard let login = try? vault.login(key: key, item: item) else { say(["id": id, "ok": false, "error": "that login has been tampered with, or the file is damaged, so it was not used"]); return }
        signingIn = true
        fillRound(login, item, go, id, round: 0, filled: [])
    }

    /// One or two pages: some sites ask for the name first and the password on the next screen. All of it after one fingerprint, and the host is checked again each time.
    func fillRound(_ login: Login, _ item: VaultItem, _ go: Bool, _ id: Int, round: Int, filled: [String]) {
        guard pageMayHave(item) else {
            signingIn = false
            say(["id": id, "ok": false, "error": "the page moved away from \(item.host) during sign-in, so the password was not placed", "filled": filled])
            return
        }
        view.callAsyncJavaScript(fillLogin, arguments: ["username": login.username, "password": login.password, "go": go], in: nil, in: .defaultClient) { result in
            let out = (try? result.get()) as? [String: Any] ?? [:]
            let now = filled + (out["filled"] as? [String] ?? [])
            self.view.evaluateJavaScript(lockFilled, completionHandler: nil)
            if now.contains("password") || !go || round >= 3 {
                let done = (out["pressed"] as? Bool ?? false) && now.contains("password")
                DispatchQueue.main.asyncAfter(deadline: .now() + (done ? 0.8 : 0.1)) {
                    self.signingIn = false
                    var reply: [String: Any] = ["filled": now, "submitted": done]
                    if now.isEmpty { reply["error"] = "no login form was found on this page" } else if go && !now.contains("password") { reply["error"] = "the password box never appeared" }
                    self.settled(reply, id)
                }
                return
            }
            self.waitForPassword(login, item, go, id, round: round + 1, filled: now, waited: 0)
        }
    }

    func waitForPassword(_ login: Login, _ item: VaultItem, _ go: Bool, _ id: Int, round: Int, filled: [String], waited: Double) {
        view.evaluateJavaScript("!!Array.from(document.querySelectorAll('input[type=password]')).find(e => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2; })") { value, _ in
            if value as? Bool == true || waited > 12 {
                self.fillRound(login, item, go, id, round: round, filled: filled)
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { self.waitForPassword(login, item, go, id, round: round, filled: filled, waited: waited + 0.4) }
            }
        }
    }

    // ---- save this password? -------------------------------------------------------------------------------------------------

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "xenioSave", !signingIn, window.isVisible, window.frame.minX > -5000, let vault, let host = view.url?.host.flatMap(cleanHost), offered.insert(host).inserted, let body = message.body as? [String: String], let password = body["password"], !password.isEmpty else { return }
        guard vault.data != nil else { return }
        let username = body["username"] ?? ""
        DispatchQueue.main.async {
            bringUp()
            let alert = NSAlert()
            alert.messageText = "Save this login to your XENIO vault?"
            alert.informativeText = "For \(host)\(username.isEmpty ? "" : ", as \(username)"). It is encrypted, and only your fingerprint or secret password can release it. No agent can ever read it."
            if let lock = NSImage(systemSymbolName: "lock.shield.fill", accessibilityDescription: "Vault") { alert.icon = lock }
            alert.addButton(withTitle: "Save")
            alert.addButton(withTitle: "Not now")
            alert.window.level = .floating
            guard alert.runModal() == .alertFirstButtonReturn else { return }
            let login = Login(username: username, password: password)
            let reason = "Save your login for \(host)"
            guard vault.touchReady && Vault.touchAvailable() else {
                self.saved(Result { try vault.unlock(secret: try askForSecret(reason: reason)) }, vault, host, login)
                return
            }
            NSApp.activate(ignoringOtherApps: true)
            DispatchQueue.global().async {  // the finger is waited for elsewhere, and this handler returns at once, so the window keeps going and the answer can arrive
                let unlocked = Result { try vault.unlockWithTouch(reason: reason) }
                DispatchQueue.main.async { self.saved(unlocked, vault, host, login) }
            }
        }
    }

    func saved(_ unlocked: Result<SymmetricKey, Error>, _ vault: Vault, _ host: String, _ login: Login) {
        var key = try? unlocked.get()
        if key == nil, case .failure(let problem) = unlocked, !(problem is VaultError && "\(problem)".contains("cancelled")) {
            key = try? vault.unlock(secret: try askForSecret(reason: "Your fingerprint did not work. Save your login for \(host) with your secret password"))
        }
        let before = window.title
        if let key, (try? vault.add(key: key, host: host, label: "", login: login)) != nil {
            window.title = "Saved: your login for \(host) is in your XENIO vault"
            say(["event": "saved_login", "host": host])
        } else {
            window.title = "Not saved: your login for \(host) was not saved"
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { self.window.title = before }
    }

    // ---- handing the window to you, for a code, a captcha or anything else only a person should do --------------------------

    func handOver(_ note: String, _ id: Int) {
        handedOver = id
        handback = window.frame
        NSApp.setActivationPolicy(.regular)
        window.styleMask = [.titled, .closable, .resizable, .miniaturizable]
        window.title = "XENIO: your turn. \(note) Close this window when you are done, and the agent carries on."
        let screen = NSScreen.main?.visibleFrame ?? CGRect(x: 100, y: 100, width: width, height: height)
        window.setFrame(CGRect(x: screen.midX - view.frame.width / 2, y: screen.midY - view.frame.height / 2, width: view.frame.width, height: view.frame.height + 28), display: true)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard let id = handedOver else { return true }
        handedOver = nil
        window.styleMask = [.borderless]
        window.setFrame(handback, display: false)
        window.orderBack(nil)
        NSApp.setActivationPolicy(.prohibited)
        settled(["returned": true], id, grace: 0.1)
        return false
    }

    func allowed(_ url: URL?) -> Bool {
        guard let url, let scheme = url.scheme?.lowercased() else { return false }
        if ["about", "blob", "data"].contains(scheme) { return true }
        guard scheme == "http" || scheme == "https", let host = url.host?.lowercased() else { return false }
        let isPrivate = host == "localhost" || host == "0.0.0.0" || host == "::1" || host.hasPrefix("127.") || host.hasPrefix("10.") || host.hasPrefix("192.168.") || host.hasPrefix("169.254.") || host.range(of: #"^172\.(1[6-9]|2[0-9]|3[01])\."#, options: .regularExpression) != nil || host.hasSuffix(".local")
        if isPrivate { return localPort != nil && (host == "127.0.0.1" || host == "localhost") && String(url.port ?? 0) == localPort }
        return true
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if action.targetFrame == nil, allowed(action.request.url) { webView.load(action.request); decisionHandler(.cancel); return }
        if allowed(action.request.url) { decisionHandler(.allow) } else { refused.append(action.request.url?.absoluteString ?? ""); decisionHandler(.cancel) }
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        decisionHandler(response.canShowMIMEType ? .allow : .download)
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
        refused.append("a download: " + (navigationResponse.response.url?.absoluteString ?? ""))
        download.cancel { _ in }
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) { completionHandler(nil) }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { let done = loading; loading = nil; done?(nil) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { let done = loading; loading = nil; done?(error.localizedDescription) }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { let done = loading; loading = nil; done?(error.localizedDescription) }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { let done = loading; loading = nil; done?("the page crashed") }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if allowed(action.request.url) { webView.load(action.request) }
        return nil
    }
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) { dialogs.append("alert: " + message); completionHandler() }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) { dialogs.append("asked and answered No: " + message); completionHandler(false) }
    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) { dialogs.append("asked for text and got none: " + prompt); completionHandler(nil) }
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) { dialogs.append("asked for a file, and none was given"); completionHandler(nil) }

    func call(_ script: String, _ done: @escaping ([String: Any]) -> Void) {
        let run = "(function(){ if (!window.__xenio) { \(Browser.setup()) } return JSON.stringify(\(script)); })()"
        view.evaluateJavaScript(run) { value, error in
            if let text = value as? String, let data = text.data(using: .utf8), let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { done(parsed) } else { done(["error": error?.localizedDescription ?? "the page did not answer"]) }
        }
    }

    static func setup() -> String { pageHelpers }

    func picture(_ path: String, quality: Double, _ done: @escaping (Bool) -> Void) {
        let config = WKSnapshotConfiguration()
        config.snapshotWidth = NSNumber(value: Double(view.frame.width))
        view.takeSnapshot(with: config) { image, _ in
            guard let image, let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff) else { done(false); return }
            let jpeg = path.lowercased().hasSuffix(".jpg") || path.lowercased().hasSuffix(".jpeg")
            let data = jpeg ? rep.representation(using: .jpeg, properties: [.compressionFactor: quality]) : rep.representation(using: .png, properties: [:])
            guard let data else { done(false); return }
            let temp = path + ".tmp"
            do {
                try data.write(to: URL(fileURLWithPath: temp))
                _ = rename(temp, path)
                done(true)
            } catch { done(false) }
        }
    }

    func settled(_ reply: [String: Any], _ id: Int, grace: Double = 0.35) {
        DispatchQueue.main.asyncAfter(deadline: .now() + grace) {
            self.afterLoad {
                var out = reply
                out["id"] = id
                out["ok"] = out["error"] == nil
                out["url"] = self.view.url?.absoluteString ?? ""
                out["title"] = self.view.title ?? ""
                if !self.dialogs.isEmpty { out["dialogs"] = self.dialogs; self.dialogs = [] }
                if !self.refused.isEmpty { out["refused"] = self.refused; self.refused = [] }
                if let live = self.live {
                    self.picture(live, quality: 0.55) { _ in say(out) }
                } else {
                    say(out)
                }
            }
        }
    }

    func afterLoad(_ then: @escaping () -> Void, waited: Double = 0) {
        if !view.isLoading || waited > 15 { then(); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { self.afterLoad(then, waited: waited + 0.1) }
    }

    // down the page and back, so that anything waiting to be scrolled into view has been
    func sweep(_ id: Int, step: Int) {
        if step >= 8 {
            call("(function(){ window.scrollTo(0,0); return {ok:true}; })()") { _ in
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { say(["id": id, "ok": true]) }
            }
            return
        }
        call("(function(){ window.scrollTo(0, document.documentElement.scrollHeight * \(step) / 8); return {ok:true}; })()") { _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { self.sweep(id, step: step + 1) }
        }
    }

    func handle(_ c: [String: Any]) {
        let id = c["id"] as? Int ?? 0
        let ref = Browser.quoted(String(describing: c["ref"] ?? ""))
        let text = Browser.quoted(c["text"] as? String ?? "")
        switch c["cmd"] as? String ?? "" {
        case "open":
            guard let raw = c["url"] as? String, let url = URL(string: raw), allowed(url) else { say(["id": id, "ok": false, "error": "XENIO does not open that address"]); return }
            var answered = false
            let finish: (String?) -> Void = { problem in
                if answered { return }
                answered = true
                self.settled(problem == nil ? [:] : ["error": problem!], id, grace: 0.25)
            }
            loading = finish
            view.load(URLRequest(url: url, timeoutInterval: 25))
            DispatchQueue.main.asyncAfter(deadline: .now() + 25) { finish(nil) }
        case "snapshot":
            call("window.__xenio.snapshot(\(c["limit"] as? Int ?? 7000))") { var o = $0; o["id"] = id; o["ok"] = o["error"] == nil; say(o) }
        case "read":
            call("window.__xenio.text(\(c["limit"] as? Int ?? 12000))") { var o = $0; o["id"] = id; o["ok"] = o["error"] == nil; say(o) }
        case "target":
            call("window.__xenio.target(\(ref), \(text))") { var o = $0; o["id"] = id; o["ok"] = o["error"] == nil; say(o) }
        case "click":
            call("window.__xenio.click(\(ref), \(text))") { self.settled($0, id) }
        case "type":
            let clear = (c["clear"] as? Bool ?? true) ? "true" : "false", submit = (c["submit"] as? Bool ?? false) ? "true" : "false"
            call("window.__xenio.type(\(ref), \(text), \(clear), \(submit))") { self.settled($0, id) }
        case "choose":
            call("window.__xenio.choose(\(ref), \(text))") { self.settled($0, id) }
        case "key":
            call("window.__xenio.key(\(Browser.quoted(c["key"] as? String ?? "Enter")))") { self.settled($0, id) }
        case "scroll":
            call("window.__xenio.scroll(\(c["dy"] as? Int ?? 600))") { self.settled($0, id, grace: 0.15) }
        case "back":
            view.goBack()
            settled([:], id)
        case "audit":
            call("window.__xenio.audit()") { var o = $0; o["id"] = id; o["ok"] = o["error"] == nil; say(o) }
        case "settle":
            // a window nobody sees does not run animations, so anything that fades in would look missing in a picture. This ends them all
            let css = "*,*::before,*::after{transition-duration:0s!important;transition-delay:0s!important;animation-duration:.001s!important;animation-delay:0s!important;animation-iteration-count:1!important;scroll-behavior:auto!important}"
            call("(function(){ const s=document.createElement('style'); s.textContent=\(Browser.quoted(css)); document.head.appendChild(s); document.querySelectorAll('html,body').forEach(e=>{}); return {ok:true}; })()") { _ in
                self.sweep(id, step: 0)
            }
        case "size":
            let w = CGFloat(c["w"] as? Int ?? Int(width)), h = CGFloat(c["h"] as? Int ?? Int(height))
            window.setContentSize(CGSize(width: w, height: h))
            view.frame = CGRect(x: 0, y: 0, width: w, height: h)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { say(["id": id, "ok": true]) }
        case "shot":
            picture(c["path"] as? String ?? "", quality: 0.72) { say(["id": id, "ok": $0]) }
        case "sign_in":
            signIn(c, id)
        case "hand_over":
            handOver(String((c["text"] as? String ?? "").prefix(120)), id)
        case "quit":
            say(["id": id, "ok": true])
            exit(0)
        default:
            say(["id": id, "ok": false, "error": "unknown command"])
        }
    }
}

// ---- cursors over the real screen ---------------------------------------------------------------------------------------

final class CursorLayer: NSView {
    struct Mark { var x: CGFloat; var y: CGFloat; var toX: CGFloat; var toY: CGFloat; var color: NSColor; var label: String; var action: String; var at: Date }
    // each arrow glides to where its agent acted, like a person's cursor in a shared document
    func step() {
        var moved = false
        for key in marks.keys {
            guard var m = marks[key] else { continue }
            let dx = m.toX - m.x, dy = m.toY - m.y
            if abs(dx) > 0.5 || abs(dy) > 0.5 { m.x += dx * 0.28; m.y += dy * 0.28; marks[key] = m; moved = true } else if m.x != m.toX || m.y != m.toY { m.x = m.toX; m.y = m.toY; marks[key] = m; moved = true }
            if m.action == "click" && Date().timeIntervalSince(m.at) < 0.6 { moved = true }
        }
        if moved { needsDisplay = true }
    }
    var marks: [String: Mark] = [:]
    override var isFlipped: Bool { true }
    override func draw(_ dirty: NSRect) {
        for (_, m) in marks {
            let origin = convert(CGPoint(x: m.x, y: m.y), from: nil)
            let path = NSBezierPath()
            path.move(to: origin)
            path.line(to: CGPoint(x: origin.x + 15, y: origin.y + 8.5))
            path.line(to: CGPoint(x: origin.x + 8.5, y: origin.y + 10))
            path.line(to: CGPoint(x: origin.x + 5, y: origin.y + 16))
            path.close()
            m.color.setFill(); path.fill()
            NSColor.white.setStroke(); path.lineWidth = 1.4; path.stroke()
            if m.action == "click" && Date().timeIntervalSince(m.at) < 0.45 {
                let ring = NSBezierPath(ovalIn: CGRect(x: origin.x - 14, y: origin.y - 14, width: 28, height: 28))
                m.color.setStroke(); ring.lineWidth = 2; ring.stroke()
            }
            let words = NSAttributedString(string: m.label, attributes: [.font: NSFont.systemFont(ofSize: 11, weight: .semibold), .foregroundColor: NSColor.white])
            let box = CGRect(x: origin.x + 14, y: origin.y + 18, width: words.size().width + 14, height: 18)
            m.color.setFill(); NSBezierPath(roundedRect: box, xRadius: 9, yRadius: 9).fill()
            words.draw(at: CGPoint(x: box.minX + 7, y: box.minY + 2))
        }
    }
}

func colour(_ text: String) -> NSColor {
    let hex = text.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
    let n = UInt32(hex, radix: 16) ?? 0xff5a36
    return NSColor(red: CGFloat((n >> 16) & 255) / 255, green: CGFloat((n >> 8) & 255) / 255, blue: CGFloat(n & 255) / 255, alpha: 1)
}

func runCursors() {
    let frame = NSScreen.screens.reduce(CGRect.null) { $0.union($1.frame) }
    let panel = NSPanel(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    panel.isOpaque = false
    panel.backgroundColor = .clear
    panel.hasShadow = false
    panel.ignoresMouseEvents = true
    panel.level = .screenSaver
    panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
    let layer = CursorLayer(frame: CGRect(origin: .zero, size: frame.size))
    panel.contentView = layer
    panel.orderFrontRegardless()
    say(["ok": true, "ready": true, "window": panel.windowNumber])
    let main = NSScreen.screens.first?.frame ?? frame
    Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) { _ in layer.step() }
    DispatchQueue.global().async {
        while let line = readLine() {
            guard let data = line.data(using: .utf8), let c = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let agent = c["agent"] as? String else { continue }
            DispatchQueue.main.async {
                if (c["action"] as? String) == "gone" { layer.marks[agent] = nil } else {
                    // global display points with the origin at the top left of the main display, as CGEvents use, and the layer is flipped to match
                    let x = CGFloat(c["x"] as? Double ?? 0) - frame.minX, y = CGFloat(c["y"] as? Double ?? 0) + (frame.maxY - main.maxY)
                    let was = layer.marks[agent]
                    layer.marks[agent] = .init(x: was?.x ?? x, y: was?.y ?? y, toX: x, toY: y, color: colour(c["color"] as? String ?? "#ff5a36"), label: "\(agent) · agent cursor", action: c["action"] as? String ?? "move", at: Date())
                }
                layer.needsDisplay = true
            }
        }
        exit(0)
    }
}

// ---- a window for checking the real thing, far off the screen, that holds nothing of anyone's -----------------------------

final class TestTarget: NSObject {
    let field = NSTextField(frame: CGRect(x: 20, y: 60, width: 260, height: 24))
    let result = NSTextField(labelWithString: "not pressed")
    var presses = 0
    @objc func press() { presses += 1; result.stringValue = "pressed \(presses): \(field.stringValue)"; say(["pressed": presses, "field": field.stringValue]) }
}

func runTestWindow() {
    let title = option("title") ?? "XENIO test"
    let target = TestTarget()
    let window = NSWindow(contentRect: CGRect(x: -6000 - Double(option("offset") ?? "0")!, y: 2000, width: 300, height: 150), styleMask: [.titled], backing: .buffered, defer: false)
    window.title = title
    target.field.placeholderString = "Note"
    target.field.setAccessibilityLabel("Note")
    let button = NSButton(title: "Save", target: target, action: #selector(TestTarget.press))
    button.frame = CGRect(x: 20, y: 20, width: 100, height: 28)
    target.result.frame = CGRect(x: 130, y: 24, width: 160, height: 20)
    window.contentView?.addSubview(target.field)
    window.contentView?.addSubview(button)
    window.contentView?.addSubview(target.result)
    app.setActivationPolicy(.accessory)
    window.orderBack(nil)
    say(["ready": true, "pid": ProcessInfo.processInfo.processIdentifier])
    objc_setAssociatedObject(window, "t", target, .OBJC_ASSOCIATION_RETAIN)
    DispatchQueue.global().async { while readLine() != nil {}; exit(0) }
}

// ---- start --------------------------------------------------------------------------------------------------------------

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let mode = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ""
if mode == "testwindow" {
    runTestWindow()
} else if mode == "vault" {
    runVault()
} else if mode == "cursors" {
    app.setActivationPolicy(.accessory)
    runCursors()
} else if mode == "browser" {
    let browser = Browser(width: CGFloat(Double(option("width") ?? "1280") ?? 1280), height: CGFloat(Double(option("height") ?? "860") ?? 860), profile: option("profile"), visible: flag("visible"))
    if flag("visible"), let start = option("url"), let url = URL(string: start), browser.allowed(url) { browser.view.load(URLRequest(url: url)) }
    say(["ok": true, "ready": true])
    DispatchQueue.global().async {
        while let line = readLine() {
            guard let data = line.data(using: .utf8), let command = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                say(["ok": false, "error": "not JSON"])
                continue
            }
            DispatchQueue.main.async { browser.handle(command) }
        }
        exit(0)
    }
} else {
    FileHandle.standardError.write(Data("usage: xenio-hands browser|cursors\n".utf8))
    exit(2)
}
app.run()
