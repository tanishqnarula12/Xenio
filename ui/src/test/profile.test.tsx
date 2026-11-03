import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import type { ProfileData, ProfileEntry } from "../api/types";
import { FakeApi, renderApp } from "./fakes";

function entry(over: Partial<ProfileEntry> = {}): ProfileEntry {
  return { id: "e1", category: "identity", field: "email", label: "Email", sensitive: false, value: "asha@example.com", masked: false,
    source: "mission_control", source_words: "You added this in Mission Control", created: "2026-11-01T10:00:00", updated: "2026-11-01T10:00:00", corrected: false, ...over };
}

function data(over: Partial<ProfileData> = {}): ProfileData {
  return { available: true, protected_by: "the macOS Keychain and AES-256 on this Mac", categories: { identity: "Identity and contact", other: "Other details" },
    suggestions: [{ field: "email", label: "Email", category: "identity" }], entries: [], last_form: null, ...over };
}

describe("profile", () => {
  it("is in the navigation and says how details are kept", async () => {
    const api = new FakeApi();
    api.profile.mockResolvedValue(data());
    await renderApp(api, "profile");
    expect(await screen.findByRole("heading", { name: "Profile" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Profile" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText(/Nothing saved yet/)).toBeInTheDocument();
    expect(screen.getByText(/Passwords, codes, tokens and payment details are refused/)).toBeInTheDocument();
  });

  it("shows a detail with where it came from, by kind", async () => {
    const api = new FakeApi();
    api.profile.mockResolvedValue(data({ entries: [entry({ source: "chat", source_words: "You said this in a conversation", corrected: true })] }));
    await renderApp(api, "profile");
    expect(await screen.findByText("asha@example.com")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Identity and contact" })).toBeInTheDocument();
    expect(screen.getByText(/You said this in a conversation/)).toHaveTextContent("corrected");
  });

  it("adds a detail", async () => {
    const api = new FakeApi();
    api.profile.mockResolvedValue(data());
    await renderApp(api, "profile");
    await userEvent.click(await screen.findByRole("button", { name: "Add a detail" }));
    const form = screen.getByRole("form", { name: "Add a detail to your profile" });
    await userEvent.type(within(form).getByLabelText("Name"), "Email");
    await userEvent.type(within(form).getByLabelText("Value"), "asha@example.com");
    await userEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.changeProfile).toHaveBeenCalledWith({ action: "add", category: "identity", label: "Email", value: "asha@example.com", field: "email" }));
  });

  it("shows why a secret is refused and keeps what was typed", async () => {
    const api = new FakeApi();
    api.profile.mockResolvedValue(data());
    api.changeProfile.mockRejectedValue(new ApiError("'Password' is a password, code or payment detail: XENIO never stores those", 422));
    await renderApp(api, "profile");
    await userEvent.click(await screen.findByRole("button", { name: "Add a detail" }));
    const form = screen.getByRole("form", { name: "Add a detail to your profile" });
    await userEvent.type(within(form).getByLabelText("Name"), "Password");
    await userEvent.type(within(form).getByLabelText("Value"), "x");
    await userEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/XENIO never stores those/)).toBeInTheDocument();
    expect(within(form).getByLabelText("Name")).toHaveValue("Password");
  });

  it("corrects a detail", async () => {
    const api = new FakeApi();
    api.profile.mockResolvedValue(data({ entries: [entry()] }));
    await renderApp(api, "profile");
    await userEvent.click(await screen.findByRole("button", { name: "Correct Email" }));
    const value = screen.getByLabelText("Value");
    await userEvent.clear(value);
    await userEvent.type(value, "new@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.changeProfile).toHaveBeenCalledWith({ action: "update", id: "e1", label: "Email", value: "new@example.com" }));
  });

  it("deletes only after asking", async () => {
    const api = new FakeApi();
    api.profile.mockResolvedValue(data({ entries: [entry()] }));
    await renderApp(api, "profile");
    await userEvent.click(await screen.findByRole("button", { name: "Delete Email" }));
    expect(api.changeProfile).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("can no longer put it into any form");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.changeProfile).toHaveBeenCalledWith({ action: "delete", id: "e1" }));
  });

  it("keeps a sensitive value hidden until asked, and hides it again", async () => {
    const api = new FakeApi();
    api.profile.mockResolvedValue(data({ entries: [entry({ id: "p", category: "other", label: "Passport number", sensitive: true, masked: true, value: "••••••67" })] }));
    api.changeProfile.mockResolvedValue({ ok: true, value: "A1234567" });
    await renderApp(api, "profile");
    expect(await screen.findByText("••••••67")).toBeInTheDocument();
    expect(screen.queryByText("A1234567")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show Passport number" }));
    expect(await screen.findByText("A1234567")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Hide Passport number" }));
    expect(screen.queryByText("A1234567")).not.toBeInTheDocument();
  });

  it("says when the Keychain will not give the details, never an empty profile", async () => {
    const api = new FakeApi();
    api.profile.mockResolvedValue(data({ available: false, reason: "The macOS Keychain refused: is it locked?" }));
    await renderApp(api, "profile");
    expect(await screen.findByText(/is it locked/)).toBeInTheDocument();
    expect(screen.queryByText(/Nothing saved yet/)).not.toBeInTheDocument();
  });

  it("shows the last form by names and outcomes, and never claims a submission", async () => {
    const api = new FakeApi();
    api.profile.mockResolvedValue(data({ last_form: { at: 1, window: "Apply", heading: "Apply for Engineer", submitted: false, timing_ms: 1200,
      fields: [{ label: "Email", status: "filled", from: "Email" }, { label: "Password", status: "never" }, { label: "T-shirt size", status: "question" }] } }));
    await renderApp(api, "profile");
    expect(await screen.findByText("Apply for Engineer")).toBeInTheDocument();
    expect(screen.getByText(/1 filled · Not submitted/)).toBeInTheDocument();
    expect(screen.getByText(/Filled and checked/)).toBeInTheDocument();
    expect(screen.getByText("Yours to do")).toBeInTheDocument();
    expect(screen.getByText("XENIO needs your answer")).toBeInTheDocument();
  });
});
