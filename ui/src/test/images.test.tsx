import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, it, vi } from "vitest";
import type { ImageJob } from "../api/types";
import { GenerationMark } from "../images/GenerationMark";
import { ImageCard } from "../images/ImageCard";
import { AppController } from "../state/controller";
import { AppProvider } from "../state/context";
import { Store } from "../state/store";
import { FakeApi } from "./fakes";

const job = (over: Partial<ImageJob> = {}): ImageJob => ({ id: "img_1", state: "generating", prompt: "a boat at dawn", size: "1024x1024", ...over });

function show(api: FakeApi) {
  const store = new Store();
  const app = new AppController(api, store);
  return { store, ...render(<AppProvider store={store} app={app}><ImageCard id="img_1" /></AppProvider>) };
}

beforeAll(() => {
  URL.createObjectURL = vi.fn(() => "blob:picture");
  URL.revokeObjectURL = vi.fn();
});

describe("a picture being made", () => {
  it.each([["preparing", "Preparing the prompt"], ["sending", "Sending the request"], ["generating", "Generating"], ["receiving", "Receiving the image"]])("%s says so, with no percentage anywhere", async (state, words) => {
    const api = new FakeApi();
    api.image.mockResolvedValue(job({ state: state as ImageJob["state"] }));
    const { container } = show(api);
    expect(await screen.findByText(new RegExp(words))).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/\d+\s*%/);
    expect(container.querySelector("[role=progressbar]")).toBeNull();
    expect(container.querySelector(".genmark")).not.toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
    expect(api.imageFile).not.toHaveBeenCalled();
  });

  it("can be cancelled, and the mark is decoration a screen reader skips", async () => {
    const api = new FakeApi();
    api.image.mockResolvedValue(job());
    api.changeImage.mockResolvedValue(job({ state: "cancelled" }));
    const { container } = show(api);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(api.changeImage).toHaveBeenCalledWith("img_1", { action: "cancel" });
    expect(container.querySelector(".genmark")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("a finished picture", () => {
  const done = () => { const api = new FakeApi(); api.image.mockResolvedValue(job({ state: "complete", has_image: true })); return api; };

  it("shows the real picture and no animation is left running", async () => {
    const api = done();
    const { container } = show(api);
    expect(await screen.findByRole("img", { name: "a boat at dawn" })).toHaveAttribute("src", "blob:picture");
    expect(container.querySelector(".genmark")).toBeNull();
    expect(api.imageFile).toHaveBeenCalledWith("img_1", expect.anything());
  });

  it("opens larger and closes with Esc", async () => {
    show(done());
    await userEvent.click(await screen.findByRole("button", { name: "See the picture larger" }));
    expect(screen.getByRole("dialog", { name: "The picture, larger" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("is saved only when asked, and says where", async () => {
    const api = done();
    api.changeImage.mockResolvedValue({ saved: "/Users/me/Downloads/a-boat.png" } as never);
    show(api);
    await screen.findByRole("img", { name: "a boat at dawn" });
    expect(api.changeImage).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));
    expect(api.changeImage).toHaveBeenCalledWith("img_1", { action: "save" });
    expect(await screen.findByText(/Saved to \/Users\/me\/Downloads\/a-boat.png/)).toBeInTheDocument();
  });

  it("a variation appears as a new card under the original", async () => {
    const api = done();
    api.changeImage.mockResolvedValue({ jobs: [job({ id: "img_2", state: "generating", parent: "img_1" })] } as never);
    show(api);
    await screen.findByRole("img", { name: "a boat at dawn" });
    await userEvent.click(screen.getByRole("button", { name: /Variation/ }));
    expect(api.changeImage).toHaveBeenCalledWith("img_1", { action: "variation", count: 1 });
    await waitFor(() => expect(api.image).toHaveBeenCalledWith("img_2"));
    expect(await screen.findAllByRole("img", { name: "a boat at dawn" })).toHaveLength(2); // the original is still there, with the new one under it
  });

  it("refining takes the words, and a double click makes one request", async () => {
    const api = done();
    let finish: (v: never) => void = () => undefined;
    api.changeImage.mockImplementation(() => new Promise((resolve) => { finish = resolve as never; }));
    show(api);
    await screen.findByRole("img", { name: "a boat at dawn" });
    await userEvent.click(screen.getByRole("button", { name: /Refine/ }));
    await userEvent.type(screen.getByLabelText("What should change?"), "at night");
    const make = screen.getByRole("button", { name: "Make it" });
    await userEvent.click(make);
    await userEvent.click(make);
    expect(api.changeImage).toHaveBeenCalledTimes(1);
    expect(api.changeImage).toHaveBeenCalledWith("img_1", { action: "refine", change: "at night" });
    await act(async () => finish(job({ id: "img_3" }) as never));
  });

  it("says when it was cut from a square, and keeps its true shape", async () => {
    const api = new FakeApi();
    api.image.mockResolvedValue(job({ state: "complete", size: "1024x576", cropped_from: "1024x1024" }));
    const { container } = show(api);
    expect(await screen.findByText(/1024 × 576, cut from the 1024 × 1024 picture/)).toBeInTheDocument();
    expect((container.querySelector(".image-frame") as HTMLElement).style.aspectRatio).toContain("1024 / 576");
  });

  it("a server error on a button is shown, and the picture stays", async () => {
    const api = done();
    api.changeImage.mockRejectedValue(new Error("Nabh is busy"));
    show(api);
    await screen.findByRole("img", { name: "a boat at dawn" });
    await userEvent.click(screen.getByRole("button", { name: /Regenerate/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Nabh is busy");
    expect(screen.getByRole("img", { name: "a boat at dawn" })).toBeInTheDocument();
  });
});

describe("when it does not work", () => {
  it("shows the real error and offers to try again, with no picture", async () => {
    const api = new FakeApi();
    api.image.mockResolvedValue(job({ state: "failed", error: { kind: "rate_limit", message: "Too many requests right now." } }));
    api.changeImage.mockResolvedValue(job({ id: "img_9" }));
    const { container } = show(api);
    expect(await screen.findByText(/Too many requests right now/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
    expect(container.querySelector(".genmark")).toBeNull();
    expect(api.imageFile).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /Try again/ }));
    expect(api.changeImage).toHaveBeenCalledWith("img_1", { action: "retry" });
  });

  it("no setup says what to do and offers no pointless retry", async () => {
    const api = new FakeApi();
    api.image.mockResolvedValue(job({ state: "failed", error: { kind: "config", message: "Image generation is not set up: add NABH_API_KEY" } }));
    show(api);
    expect(await screen.findByText(/add NABH_API_KEY/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Try again/ })).toBeNull();
  });

  it("cancelled says so", async () => {
    const api = new FakeApi();
    api.image.mockResolvedValue(job({ state: "cancelled" }));
    show(api);
    expect(await screen.findByText("Cancelled.")).toBeInTheDocument();
  });

  it("a live update replaces what was polled", async () => {
    const api = new FakeApi();
    api.image.mockResolvedValue(job({ state: "generating", updated: 1 }));
    const { store } = show(api);
    await screen.findByText(/Generating/);
    act(() => { store.set((s) => ({ images: { ...s.images, img_1: job({ state: "complete", updated: 2 }) } })); });
    expect(await screen.findByRole("img", { name: "a boat at dawn" })).toBeInTheDocument();
  });
});

describe("the mark", () => {
  const mark = () => { const store = new Store(); return render(<AppProvider store={store} app={new AppController(new FakeApi(), store)}><GenerationMark state="generating" /></AppProvider>); };

  it("stands still for those who ask for less motion", () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({ matches: query.includes("reduce"), media: query, onchange: null, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false })) as typeof window.matchMedia;
    try {
      const { container } = mark();
      expect(container.querySelector(".genmark--still")).not.toBeNull();
    } finally { window.matchMedia = original; }
  });

  it("moves otherwise, and never carries a number", () => {
    const { container } = mark();
    expect(container.querySelector(".genmark--generating")).not.toBeNull();
    expect(container.textContent).toBe("");
  });
});

describe("when Nabh is limiting requests", () => {
  it("says it is waiting, in place of the usual words", async () => {
    const api = new FakeApi();
    api.image.mockResolvedValue(job({ state: "sending", note: "Nabh is limiting requests: trying again in 30 s" }));
    show(api);
    expect(await screen.findByText("Nabh is limiting requests: trying again in 30 s")).toBeInTheDocument();
  });
});
