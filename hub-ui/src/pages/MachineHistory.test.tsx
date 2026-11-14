import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { percent, type HistorySeries } from "../infrastructureMetrics";
import { Chart, selectHistoryDisks } from "./MachineHistory";

const at = (seconds: number) => new Date(seconds * 1000).toLocaleString();

describe("history snapshot labels", () => {
  it("shows a trailing gap as not reported at its timestamp, retaining the historical peak", () => {
    const html = renderToStaticMarkup(<Chart title="CPU" lines={[{ name: "cpu", slot: 1, points: [[1000, 0.7], [1030, null]] }]}
      range="1h" format={percent} ratio />);
    expect(html).toContain(`Latest sample: not reported at ${at(1030)}`);
    expect(html).toContain(`CPU: latest sample not reported at ${at(1030)}, peak 70%`);
    expect(html).not.toContain("70% now");
  });
  it("keeps a zero sample and its old timestamp in both legend and accessible summary", () => {
    const html = renderToStaticMarkup(<Chart title="Network" lines={[
      { name: "Down", slot: 1, points: [[1000, 0.5], [1030, 0]] },
      { name: "Up", slot: 2, points: [[1000, null], [1030, null]] },
    ]} range="1h" format={percent} ratio />);
    expect(html).toContain(`Down latest sample 0% at ${at(1030)}, peak 50%`);
    expect(html).toContain(`Up latest sample not reported at ${at(1030)}, peak not reported`);
    expect(html).toContain(`<strong>0% at ${at(1030)}</strong>`);
    expect(html).toContain(`<strong>not reported at ${at(1030)}</strong>`);
  });
});

describe("disk selection", () => {
  it("ranks final values ahead of missing values, including a real zero", () => {
    const disk = (label: string, points: HistorySeries["points"]): HistorySeries => ({ key: "disk", label, unit: "ratio", points });
    const disks = [disk("stale-high", [[1000, 1], [1030, null]]), disk("zero", [[1000, 0.9], [1030, 0]]),
      disk("high", [[1000, 0.1], [1030, 0.9]]), disk("medium", [[1000, 0.1], [1030, 0.4]]),
      disk("low", [[1000, 0.1], [1030, 0.1]]), disk("none", [[1000, null], [1030, null]])];
    expect(selectHistoryDisks(disks).map(item => item.label)).toEqual(["high", "medium", "low", "zero"]);
    expect(disks.map(item => item.label)[0]).toBe("stale-high");
  });
});
