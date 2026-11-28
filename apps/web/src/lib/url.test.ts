import { describe, expect, it } from "vitest";
import { safeUrl } from "./url";

describe("safeUrl", () => {
  it("keeps web and mail links", () => {
    expect(safeUrl("https://jobs.example.com/1")).toBe("https://jobs.example.com/1");
    expect(safeUrl("http://example.com")).toBe("http://example.com/");
    expect(safeUrl("mailto:me@example.com")).toBe("mailto:me@example.com");
  });

  it("drops script and data links, and non-strings", () => {
    expect(safeUrl("javascript:alert(1)")).toBeUndefined();
    expect(safeUrl(" JavaScript:alert(1)")).toBeUndefined();
    expect(safeUrl("data:text/html,<script>alert(1)</script>")).toBeUndefined();
    expect(safeUrl("")).toBeUndefined();
    expect(safeUrl(undefined)).toBeUndefined();
    expect(safeUrl(42)).toBeUndefined();
  });
});
