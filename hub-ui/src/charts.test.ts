import { afterEach, expect, it, vi } from "vitest";
import { dailyCounts } from "./charts";

afterEach(() => vi.unstubAllEnvs());

it.each([
  ["2026-11-01T23:30:00-06:00", "2026-11-02T00:30:00-06:00", "2026-11-02T12:00:00-06:00"],
  ["2026-03-08T23:30:00-05:00", "2026-03-09T00:30:00-05:00", "2026-03-09T12:00:00-05:00"],
])("groups by local calendar day across DST: %s", (first, second, now) => {
  vi.stubEnv("TZ", "America/Chicago");
  const series = dailyCounts([first, second, "invalid", "2025-01-01T00:00:00Z"], 2, new Date(now));
  expect(series.map((point) => point.count)).toEqual([1, 1]);
  expect(series[0].day.getDate()).toBe(new Date(first).getDate());
});
