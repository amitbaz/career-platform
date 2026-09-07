import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProfileCredentials } from "@/app/profile-credentials";
import {
  fetchProviderCredentials,
  removeProviderCredential,
  saveProviderCredential,
} from "@/app/api-client";

vi.mock("@/app/api-client", () => ({
  fetchProviderCredentials: vi.fn(),
  removeProviderCredential: vi.fn(),
  saveProviderCredential: vi.fn(),
}));

const fetchCredentials = vi.mocked(fetchProviderCredentials);
const removeCredential = vi.mocked(removeProviderCredential);
const saveCredential = vi.mocked(saveProviderCredential);

const notConfigured = [
  { provider: "gemini" as const, configured: false, updatedAt: null },
  { provider: "brave" as const, configured: false, updatedAt: null },
];

describe("ProfileCredentials", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchCredentials.mockResolvedValue(notConfigured);
  });

  it("shows configured state without rendering a saved value", async () => {
    fetchCredentials.mockResolvedValue([
      { provider: "gemini", configured: true, updatedAt: "2026-09-07T12:00:00.000Z" },
      { provider: "brave", configured: false, updatedAt: null },
    ]);

    render(<ProfileCredentials />);

    expect(await screen.findByText(/^Configured/)).toBeInTheDocument();
    expect(screen.getByText(/Updated/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("saved-gemini-value");
    expect(screen.getByLabelText("Gemini API key")).toHaveValue("");
  });

  it("starts both password fields blank", async () => {
    render(<ProfileCredentials />);

    await waitFor(() => expect(screen.getAllByText("Not configured")).toHaveLength(2));

    expect(screen.getByLabelText("Gemini API key")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("Gemini API key")).toHaveValue("");
    expect(screen.getByLabelText("Brave Search API key")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("Brave Search API key")).toHaveValue("");
  });

  it("saves Gemini, clears the input, and updates status", async () => {
    saveCredential.mockResolvedValue({ provider: "gemini", configured: true, updatedAt: "2026-09-07T12:00:00.000Z" });
    render(<ProfileCredentials />);

    await waitFor(() => expect(screen.getAllByText("Not configured")).toHaveLength(2));
    const gemini = screen.getByRole("group", { name: "Gemini" });
    fireEvent.change(within(gemini).getByLabelText("Gemini API key"), { target: { value: "submitted-gemini-value" } });
    fireEvent.click(within(gemini).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(saveCredential).toHaveBeenCalledWith("gemini", "submitted-gemini-value"));
    expect(within(gemini).getByLabelText("Gemini API key")).toHaveValue("");
    expect(within(gemini).getByText(/^Configured/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("submitted-gemini-value");
  });

  it("replaces a configured Brave key without pre-filling it", async () => {
    fetchCredentials.mockResolvedValue([
      { provider: "gemini", configured: false, updatedAt: null },
      { provider: "brave", configured: true, updatedAt: "2026-09-07T12:00:00.000Z" },
    ]);
    saveCredential.mockResolvedValue({ provider: "brave", configured: true, updatedAt: "2026-09-07T13:00:00.000Z" });
    render(<ProfileCredentials />);

    const brave = await screen.findByRole("group", { name: "Brave Search" });
    const input = within(brave).getByLabelText("Brave Search API key");
    const replace = await within(brave).findByRole("button", { name: "Replace" });
    expect(input).toHaveValue("");
    fireEvent.change(input, { target: { value: "submitted-brave-value" } });
    fireEvent.click(replace);

    await waitFor(() => expect(saveCredential).toHaveBeenCalledWith("brave", "submitted-brave-value"));
    expect(input).toHaveValue("");
    expect(document.body.textContent).not.toContain("submitted-brave-value");
  });

  it("deletes a configured key only after an explicit click", async () => {
    fetchCredentials.mockResolvedValue([
      { provider: "gemini", configured: true, updatedAt: "2026-09-07T12:00:00.000Z" },
      { provider: "brave", configured: false, updatedAt: null },
    ]);
    removeCredential.mockResolvedValue({ provider: "gemini", configured: false, updatedAt: null });
    render(<ProfileCredentials />);

    const gemini = await screen.findByRole("group", { name: "Gemini" });
    const deleteButton = await within(gemini).findByRole("button", { name: "Delete" });
    expect(removeCredential).not.toHaveBeenCalled();
    fireEvent.click(deleteButton);

    await waitFor(() => expect(removeCredential).toHaveBeenCalledWith("gemini"));
    expect(within(gemini).getByText("Not configured")).toBeInTheDocument();
  });

  it("keeps provider errors local and never renders the submitted value", async () => {
    saveCredential.mockRejectedValue(new Error("Credential request failed."));
    render(<ProfileCredentials />);

    await waitFor(() => expect(screen.getAllByText("Not configured")).toHaveLength(2));
    const gemini = screen.getByRole("group", { name: "Gemini" });
    fireEvent.change(within(gemini).getByLabelText("Gemini API key"), { target: { value: "submitted-gemini-value" } });
    fireEvent.click(within(gemini).getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Credential request failed.");
    expect(within(gemini).getByLabelText("Gemini API key")).toHaveValue("");
    expect(document.body.textContent).not.toContain("submitted-gemini-value");
  });
});
