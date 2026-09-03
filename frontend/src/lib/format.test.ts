import { describe, expect, it } from "vitest";

import { formatDateTimeUtc, formatDuration, formatInr, formatPercent, scoreBand, timeRemaining } from "./format";

describe("formatInr", () => {
  it("formats decimal strings as Indian-rupee amounts with Indian grouping", () => {
    expect(formatInr("2000.00")).toBe("₹2,000.00");
    expect(formatInr("13457.00")).toBe("₹13,457.00");
    expect(formatInr("625145.00")).toBe("₹6,25,145.00");
    expect(formatInr("257.00")).toBe("₹257.00");
  });

  it("accepts numbers", () => {
    expect(formatInr(0)).toBe("₹0.00");
    expect(formatInr(12345.5)).toBe("₹12,345.50");
  });

  it("renders a dash for missing or invalid amounts", () => {
    expect(formatInr(null)).toBe("—");
    expect(formatInr(undefined)).toBe("—");
    expect(formatInr("")).toBe("—");
    expect(formatInr("not-a-number")).toBe("—");
  });
});

describe("formatPercent", () => {
  it("formats rates as percentages with one decimal", () => {
    expect(formatPercent(0.456)).toBe("45.6%");
    expect(formatPercent(1)).toBe("100.0%");
    expect(formatPercent(0)).toBe("0.0%");
  });

  it("renders a dash when there is no rate", () => {
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(undefined)).toBe("—");
    expect(formatPercent(Number.NaN)).toBe("—");
  });
});

describe("formatDuration", () => {
  it("humanizes seconds", () => {
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(90)).toBe("1m 30s");
    expect(formatDuration(3725)).toBe("1h 2m");
    expect(formatDuration(90061)).toBe("1d 1h");
  });

  it("renders a dash for missing values", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
  });
});

describe("timeRemaining", () => {
  const now = new Date("2026-09-02T12:00:00Z");

  it("reports hours and minutes left before the deadline", () => {
    expect(timeRemaining("2026-09-02T14:30:00Z", now)).toEqual({ text: "2h 30m left", expired: false });
    expect(timeRemaining("2026-09-02T12:20:00Z", now)).toEqual({ text: "20m left", expired: false });
  });

  it("reports expired once the deadline passes", () => {
    expect(timeRemaining("2026-09-02T11:59:00Z", now)).toEqual({ text: "expired", expired: true });
    expect(timeRemaining("2026-09-02T12:00:00Z", now)).toEqual({ text: "expired", expired: true });
  });

  it("handles missing or invalid expiry values", () => {
    expect(timeRemaining(null, now)).toEqual({ text: "—", expired: false });
    expect(timeRemaining("bogus", now)).toEqual({ text: "—", expired: false });
  });
});

describe("formatDateTimeUtc", () => {
  it("renders ISO timestamps as stable UTC strings", () => {
    expect(formatDateTimeUtc("2026-09-02T08:57:41.937380+00:00")).toBe("2026-09-02 08:57:41 UTC");
    expect(formatDateTimeUtc("2026-09-03T05:30:00Z")).toBe("2026-09-03 05:30:00 UTC");
  });

  it("renders a dash for missing or invalid timestamps", () => {
    expect(formatDateTimeUtc(null)).toBe("—");
    expect(formatDateTimeUtc("not-a-date")).toBe("—");
  });
});

describe("scoreBand", () => {
  it("matches the backend HIGH/MEDIUM/LOW thresholds", () => {
    expect(scoreBand(96)).toBe("HIGH");
    expect(scoreBand(80)).toBe("HIGH");
    expect(scoreBand(79)).toBe("MEDIUM");
    expect(scoreBand(35)).toBe("MEDIUM");
    expect(scoreBand(34)).toBe("LOW");
    expect(scoreBand(0)).toBe("LOW");
  });

  it("returns null when no score exists", () => {
    expect(scoreBand(null)).toBeNull();
    expect(scoreBand(undefined)).toBeNull();
  });
});
