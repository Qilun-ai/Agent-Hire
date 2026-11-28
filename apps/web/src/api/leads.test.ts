import { describe, expect, it } from "vitest";
import { leadsApi } from "./leads";
import type { ApiFetch } from "../types";

function fakeApi(body: unknown, seen: string[] = []): ApiFetch {
  return async (path) => {
    seen.push(path);
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  };
}

describe("leadsApi.page", () => {
  it("reads the plain array the API returns without a page", async () => {
    const seen: string[] = [];
    const page = await leadsApi.page(fakeApi([{ job_id: "a", title: "A", company: "X", status: "applied" }], seen), { view: "all" });
    expect(page.items).toHaveLength(1);
    expect(page.has_more).toBe(false);
    expect(seen[0]).toBe("/api/v1/leads?view=all");
  });

  it("reads the paged object and knows when another page follows", async () => {
    const page = await leadsApi.page(fakeApi({ items: [{ job_id: "a" }], page: 1, pages: 3 }), { view: "applied", page: 1, limit: 1 });
    expect(page.items).toHaveLength(1);
    expect(page.has_more).toBe(true);
  });

  it("throws the server's detail on failure", async () => {
    const api: ApiFetch = async () => new Response(JSON.stringify({ detail: "nope" }), { status: 422 });
    await expect(leadsApi.page(api)).rejects.toThrow("nope");
  });
});
