// @vitest-environment jsdom
/** Employee Phones: the SMS relay phones, adding one (token shown once), and deleting. */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listOtpPhones: vi.fn(),
  addOtpPhone: vi.fn(),
  updateOtpPhone: vi.fn(),
  newOtpPhoneToken: vi.fn(),
  deleteOtpPhone: vi.fn(),
  listOtpPhoneMessages: vi.fn(),
  getOtpPhoneSettings: vi.fn().mockResolvedValue({ assume_online: false }),
  setOtpPhoneSettings: vi.fn().mockResolvedValue({ assume_online: true }),
  bulkImportOtpPhones: vi.fn().mockResolvedValue({ added: [], updated: 0, skipped: [] }),
}));
vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import EmployeePhones from "./EmployeePhones";

const PHONE = {
  id: 4,
  label: "Ravi's phone",
  phone_number: "9876543210",
  employee_id: "E-17",
  corporate_id: "CORP9",
  token_hint: "…a1b2",
  active: true,
  online: true,
  last_ping_at: "2026-11-21T09:00:00",
  last_sms_at: "2026-11-21T09:01:00",
  last_sender: "HDFCBK",
  last_otp: "482913",
  last_otp_at: "2026-11-21T09:01:00",
  created_at: "2026-11-20T10:00:00",
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("EmployeePhones", () => {
  it("lists the phones with their status and last OTP", async () => {
    mocks.listOtpPhones.mockResolvedValue([PHONE, { ...PHONE, id: 5, label: "Spare", online: false, last_otp: null, employee_id: null, corporate_id: null }]);
    render(<EmployeePhones />);

    expect(await screen.findByText("Ravi's phone")).toBeTruthy();
    expect(screen.getByText("Phones (2)")).toBeTruthy();
    expect(screen.getByText("Online")).toBeTruthy();
    expect(screen.getByText("Offline")).toBeTruthy();
    expect(screen.getByText("482913")).toBeTruthy();
    expect(screen.getByText("E-17")).toBeTruthy();
    expect(screen.getByText("CORP9")).toBeTruthy();
  });

  it("adds a phone and shows its token and webhook URL once", async () => {
    mocks.listOtpPhones.mockResolvedValue([]);
    mocks.addOtpPhone.mockResolvedValue({
      ...PHONE,
      token: "tok_secret_123",
      webhook_path: "/api/sms/inbound?token=tok_secret_123",
      ping_path: "/api/sms/ping?token=tok_secret_123",
    });
    render(<EmployeePhones />);
    await screen.findByText("No phones yet.");

    fireEvent.click(screen.getByRole("button", { name: "Add phone" }));
    const form = await screen.findByRole("dialog");
    fireEvent.change(within(form).getByLabelText(/^Label/), { target: { value: "Ravi's phone" } });
    fireEvent.change(within(form).getByLabelText(/^Phone Number/), { target: { value: "9876543210" } });
    fireEvent.change(within(form).getByLabelText(/^Employee ID/), { target: { value: "E-17" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add phone" }));

    await waitFor(() =>
      expect(mocks.addOtpPhone).toHaveBeenCalledWith({
        label: "Ravi's phone",
        phone_number: "9876543210",
        employee_id: "E-17",
        corporate_id: null,
      }),
    );
    const setup = await screen.findByRole("dialog", { name: "Set up Ravi's phone" });
    expect((within(setup).getByLabelText("Token") as HTMLInputElement).value).toBe("tok_secret_123");
    expect((within(setup).getByLabelText("Webhook URL") as HTMLInputElement).value).toBe(
      `${window.location.origin}/api/sms/inbound?token=tok_secret_123`,
    );
    expect((within(setup).getByLabelText("Ping URL") as HTMLInputElement).value).toBe(
      `${window.location.origin}/api/sms/ping?token=tok_secret_123`,
    );
    expect(within(setup).getByRole("button", { name: "Copy Webhook URL" })).toBeTruthy();
    // SMS forwarder (GawkAr) takes a bare URL and the token as a header.
    expect((within(setup).getByLabelText("URL") as HTMLInputElement).value).toBe(
      `${window.location.origin}/api/sms/inbound`,
    );
    expect((within(setup).getByLabelText("Request headers") as HTMLInputElement).value).toBe(
      '{"Authorization": "Bearer tok_secret_123"}',
    );

    fireEvent.click(within(setup).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByText("tok_secret_123")).toBeNull());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("deletes a phone after asking", async () => {
    mocks.listOtpPhones.mockResolvedValue([PHONE]);
    mocks.deleteOtpPhone.mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<EmployeePhones />);
    await screen.findByText("Ravi's phone");

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(mocks.deleteOtpPhone).toHaveBeenCalledWith(4));
    expect(confirm).toHaveBeenCalled();
    await waitFor(() => expect(mocks.listOtpPhones).toHaveBeenCalledTimes(2));
    confirm.mockRestore();
  });

  it("shows the texts a phone forwarded", async () => {
    mocks.listOtpPhones.mockResolvedValue([PHONE]);
    mocks.listOtpPhoneMessages.mockResolvedValue([
      { id: 1, sender: "HDFCBK", body: "482913 is your OTP for CorpAssist.", otp: "482913", received_at: "2026-11-21T09:01:00" },
    ]);
    render(<EmployeePhones />);
    await screen.findByText("Ravi's phone");

    fireEvent.click(screen.getByRole("button", { name: "Messages" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("482913 is your OTP for CorpAssist.")).toBeTruthy();
    expect(mocks.listOtpPhoneMessages).toHaveBeenCalledWith(4);
  });
});
