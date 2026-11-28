import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/preact";
import { AiModelSettings } from "./AiModelSettings";
import { GeneralSettings } from "./GeneralSettings";
import type { ApiFetch } from "../types";

afterEach(cleanup);

function routes(table: Record<string, unknown>, calls: { path: string; init?: RequestInit }[] = []): ApiFetch {
  return async (path, init) => {
    calls.push({ path, init });
    const key = Object.keys(table).find((prefix) => path.startsWith(prefix));
    return new Response(JSON.stringify(key ? table[key] : {}), { status: key ? 200 : 404, headers: { "Content-Type": "application/json" } });
  };
}

describe("AiModelSettings", () => {
  it("lists the built-in providers plus those found in the environment, and saves only edited fields", async () => {
    const calls: { path: string; init?: RequestInit }[] = [];
    const api = routes({
      "/api/v1/settings/environment": { providers: [{ id: "gemini", variable: "GEMINI_API", key_setting: "gemini_api_key" }], decision_api: { source: "none" } },
      "/api/v1/settings/secret-previews": {},
      "/api/v1/settings": { llm_provider: "opencode", opencode_model: "m1", opencode_api_key: "__AGENT_HIRE_SECRET_SET__" },
    }, calls);
    render(<AiModelSettings api={api} />);
    expect(await screen.findByText("Gemini")).toBeTruthy();
    expect(screen.getByText("key from GEMINI_API")).toBeTruthy();
    expect(screen.getByRole("radio", { name: /OpenCode/ }).getAttribute("aria-checked")).toBe("true");

    fireEvent.input(screen.getByPlaceholderText("Model ID"), { target: { value: "m2" } });
    fireEvent.click(screen.getByText("Save settings"));
    await waitFor(() => expect(calls.some((call) => call.init?.method === "POST")).toBe(true));
    const saved = JSON.parse(String(calls.find((call) => call.init?.method === "POST")!.init!.body));
    expect(saved).toEqual({ opencode_model: "m2" });
  });
});

describe("GeneralSettings", () => {
  it("switches the theme and keeps the reset locked until DELETE is typed", async () => {
    const api = routes({ "/api/v1/events": [] });
    render(<GeneralSettings api={api} />);
    fireEvent.click(screen.getByRole("radio", { name: /Light/ }));
    expect(document.documentElement.dataset.theme).toBe("light");
    fireEvent.click(screen.getByRole("radio", { name: /Dark/ }));
    expect(document.documentElement.dataset.theme).toBe("dark");

    fireEvent.click(screen.getByText("Delete all data"));
    const confirm = screen.getAllByRole("button", { name: "Delete all data" }).at(-1)!;
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.input(screen.getByLabelText("Type DELETE to confirm"), { target: { value: "DELETE" } });
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
  });
});
