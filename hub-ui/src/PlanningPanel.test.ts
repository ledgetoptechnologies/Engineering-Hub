import { describe, expect, it } from "vitest";
import { groupByProgress } from "./PlanningPanel";

describe("groupByProgress", () => {
  it("puts open work first and moves shipped and declined items into their own groups", () => {
    const items = [
      { id: "a", status: "done" }, { id: "b", status: "proposed" }, { id: "c", status: "in_progress" },
      { id: "d", status: "declined" }, { id: "e", status: "planned" },
    ] as const;
    const groups = groupByProgress([...items]);
    expect(groups.map(group => [group.key, group.items.map(item => item.id)])).toEqual([
      ["open", ["c", "e", "b"]], ["done", ["a"]], ["declined", ["d"]],
    ]);
  });
});
