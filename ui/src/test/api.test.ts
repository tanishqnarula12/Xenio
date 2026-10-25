import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, EventStream, HttpApi, readToken } from "../api/client";
import type { XenioEvent } from "../api/types";
import { Store } from "../state/store";
import { approval } from "./fakes";

const ev = (seq: number, type = "x", id = `e${seq}`, data: Record<string, unknown> = {}): XenioEvent =>
  ({ v: 1, seq, id, type, at: "2026-10-18T10:00:00", data });

afterEach(() => vi.unstubAllGlobals());

describe("the session token", () => {
  it("is taken from the URL fragment, kept for this tab and removed from the address bar", () => {
    window.history.replaceState(null, "", "/#token=AAAAAAAAAAAAAAAAAAAAAAAAAAAA");
    expect(readToken()).toBe("AAAAAAAAAAAAAAAAAAAAAAAAAAAA");
    expect(window.location.hash).toBe("");
    expect(readToken()).toBe("AAAAAAAAAAAAAAAAAAAAAAAAAAAA"); // this tab's copy
  });
});

describe("the API client", () => {
  it("sends the token in a header, never in the URL", async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ ok: 1 }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    await new HttpApi("secret-token-value-xxxxxxxx").snapshot();
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/snapshot");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer secret-token-value-xxxxxxxx");
    expect(url).not.toContain("secret");
  });

  it("turns XENIO's refusals and outages into plain messages", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "XENIO is still answering" }), { status: 409 })));
    await expect(new HttpApi("t".repeat(24)).chat("hi", "text")).rejects.toEqual(new ApiError("XENIO is still answering", 409));
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    await expect(new HttpApi("t".repeat(24)).snapshot()).rejects.toMatchObject({ status: 0 });
  });
});

describe("the event stream", () => {
  it("drops duplicates and stale events, and always passes a reset on", () => {
    const stream = new EventStream("/x", "t", () => {}, () => {});
    stream.start(0);
    stream.stop();
    expect(stream.accept(ev(3))).toBe(true);
    expect(stream.accept(ev(3))).toBe(false); // the same event twice
    expect(stream.accept(ev(2, "x", "other"))).toBe(false); // out of order: already past it
    expect(stream.accept(ev(50, "stream.reset", "reset"))).toBe(true);
    expect(stream.accept(ev(51))).toBe(true);
  });
});

describe("the page's state", () => {
  it("applies each event once", () => {
    const store = new Store();
    store.apply(ev(2, "chat.user", "a", { id: "t1-u", turn: "t1", role: "user", text: "hi", at: "" }));
    store.apply(ev(3, "chat.user", "b", { id: "t1-u", turn: "t1", role: "user", text: "hi", at: "" }));
    store.apply(ev(4, "chat.chunk", "c", { turn: "t1", text: "Hel" }));
    store.apply(ev(5, "chat.chunk", "d", { turn: "t1", text: "lo" }));
    expect(store.get().conversation).toHaveLength(1);
    expect(store.get().streaming?.text).toBe("Hello");
    store.apply(ev(6, "chat.done", "e", { id: "t1-a", turn: "t1", role: "assistant", text: "Hello", ok: true, at: "" }));
    expect(store.get().streaming).toBeNull();
    const a = approval();
    store.apply(ev(7, "approval.requested", "f", a as unknown as Record<string, unknown>));
    store.apply(ev(8, "approval.requested", "g", a as unknown as Record<string, unknown>));
    expect(store.get().approvals).toHaveLength(1);
    store.apply(ev(9, "approval.settled", "h", { id: a.id, status: "denied" }));
    expect(store.get().approvals).toHaveLength(0);
  });

  it("does not treat a lost connection as finished work", () => {
    const store = new Store();
    store.apply(ev(2, "chat.user", "a", { id: "t1-u", turn: "t1", role: "user", text: "hi", at: "" }));
    store.set({ stream: "retrying" });
    expect(store.get().streaming).not.toBeNull(); // still in progress, not "done"
  });
});

describe("reconnecting", () => {
  it("resumes after the last event seen, delivers each event once, and keeps one connection", async () => {
    const { EventStream } = await import("../api/client");
    const block = (seq: number, id: string) => `id: ${seq}\ndata: ${JSON.stringify({ v: 1, seq, id, type: "task.changed", at: "", data: { id: seq } })}\n\n`;
    const urls: string[] = [];
    let open = 0, maxOpen = 0;
    const bodies = [[block(5, "e5"), block(6, "e6")], [block(6, "e6"), block(7, "e7")]];  // the server replays e6 after the drop
    const realFetch = globalThis.fetch;
    globalThis.fetch = (async (url: string) => {
      urls.push(String(url));
      const chunks = bodies.shift() ?? [];
      open += 1;
      maxOpen = Math.max(maxOpen, open);
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          chunks.forEach((c) => controller.enqueue(new TextEncoder().encode(c)));
          if (bodies.length || chunks.length) setTimeout(() => { open -= 1; controller.close(); }, 5);  // then the connection drops
        },
      });
      return new Response(body, { status: 200 });
    }) as typeof fetch;
    const got: string[] = [];
    const statuses: string[] = [];
    const stream = new EventStream("/api/v1/events", "t".repeat(24), (e) => got.push(e.id), (s) => statuses.push(s));
    try {
      stream.start(4);
      await new Promise((r) => setTimeout(r, 1500));  // the first retry waits 1 s
    } finally {
      stream.stop();
      globalThis.fetch = realFetch;
    }
    expect(got).toEqual(["e5", "e6", "e7"]);  // e6 delivered once although sent twice
    expect(urls[0]).toContain("since=4");
    expect(urls[1]).toContain("since=6");  // resumed after the last event seen, not from the start
    expect(maxOpen).toBe(1);  // never two connections at once
    expect(statuses).toContain("retrying");
  });
});
