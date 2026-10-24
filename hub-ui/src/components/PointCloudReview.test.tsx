import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { loadPointCloud, MAX_POINT_BYTES, parsePointCloud, PointCloudReview } from "./PointCloudReview";

const url = "/api/hub/v1/interactive-reviews/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/assets/bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee";
const sample = { schema: "pointcloud.v1", units: "meters", points: [[1, 2, 3], [4, 5, 6, 255, 0, 128]] };
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });

describe("trusted pointcloud preview", () => {
  it("accepts bounded finite coordinates and RGB points without interpreting units as HTML", () => {
    expect(parsePointCloud(sample)).toEqual(sample);
    expect(parsePointCloud({ ...sample, units: "<b>meters</b>" }).units).toBe("<b>meters</b>");
    expect(parsePointCloud({ schema: "pointcloud.v1", points: [[Number.MAX_VALUE, 0, -Number.MAX_VALUE]] }).points).toHaveLength(1);
  });

  it.each([
    null, [], { ...sample, schema: "las" }, { ...sample, points: [] },
    { ...sample, points: [[1, 2]] }, { ...sample, points: [[1, 2, 3, 0]] },
    { ...sample, points: [[NaN, 2, 3]] }, { ...sample, points: [[Infinity, 2, 3]] },
    { ...sample, points: [[1, 2, 3, 256, 0, 0]] }, { ...sample, points: [[1, 2, 3, -1, 0, 0]] },
    { ...sample, points: [["1", 2, 3]] }, { ...sample, units: "m\n" },
    { ...sample, units: "x".repeat(33) }, { ...sample, source: "https://foreign.invalid" },
  ])("rejects malformed or unregistered point data", value => {
    expect(() => parsePointCloud(value)).toThrow();
  });

  it("rejects more than100000 points", () => {
    expect(() => parsePointCloud({ ...sample, points: Array(100001).fill([0, 0, 0]) })).toThrow();
  });

  it.each(["https://foreign.invalid" + url, "//foreign.invalid" + url, url + "?token=x", url + "#asset",
    url.replace("/assets/", "/assets/../"), url.replace("assets/", "assets/%62"), url + "\n", "/api/hub/v1/notes"]) (
    "rejects noncanonical or foreign URL before fetch", async invalid => {
      const fetcher = vi.fn();
      await expect(loadPointCloud(invalid, new AbortController().signal, fetcher)).rejects.toThrow();
      expect(fetcher).not.toHaveBeenCalled();
    });

  it("fetches only the fixed same-origin endpoint without CSRF or a range", async () => {
    const signal = new AbortController().signal;
    const fetcher = vi.fn(async () => json(sample));
    expect(await loadPointCloud(url, signal, fetcher)).toEqual(sample);
    expect(fetcher).toHaveBeenCalledWith(url, { signal, credentials: "same-origin", redirect: "error",
      cache: "no-store", headers: { Accept: "application/json" } });
  });

  it("cancels unsupported binary media without reading the body", async () => {
    let cancelled = false;
    const response = new Response(new ReadableStream({ cancel() { cancelled = true; } }),
      { headers: { "Content-Type": "application/octet-stream" } });
    await expect(loadPointCloud(url, new AbortController().signal, async () => response)).rejects.toThrow();
    expect(cancelled).toBe(true);
  });

  it("enforces declared and actual byte bounds", async () => {
    await expect(loadPointCloud(url, new AbortController().signal, async () => new Response("{}", {
      headers: { "Content-Type": "application/json", "Content-Length": String(MAX_POINT_BYTES + 1) },
    }))).rejects.toThrow();
    let cancelled = false;
    const response = new Response(new ReadableStream({ start(controller) {
      controller.enqueue(new Uint8Array(MAX_POINT_BYTES)); controller.enqueue(new Uint8Array(1));
    }, cancel() { cancelled = true; } }), { headers: { "Content-Type": "application/json" } });
    await expect(loadPointCloud(url, new AbortController().signal, async () => response)).rejects.toThrow();
    expect(cancelled).toBe(true);
  });

  it("aborts an in-progress stream and releases it", async () => {
    let cancelled = false;
    const controller = new AbortController();
    const response = new Response(new ReadableStream({ cancel() { cancelled = true; } }),
      { headers: { "Content-Type": "application/json" } });
    const pending = loadPointCloud(url, controller.signal, async () => response);
    await Promise.resolve(); controller.abort();
    await expect(pending).rejects.toThrow();
    expect(cancelled).toBe(true);
  });

  it("renders accessible initial instructions without executing data or fetching on SSR", () => {
    const markup = renderToStaticMarkup(<PointCloudReview url={url} />);
    expect(markup).toContain("Drag with a mouse or finger");
    expect(markup).toContain("not a measurement tool");
    expect(markup).toContain('role="status"');
    expect(markup).not.toContain("<script");
  });
});
