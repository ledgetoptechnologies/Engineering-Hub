import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { StageBar, StageLegend } from "./charts";
import type { Hub } from "./hub";
import { WorkBoard } from "./pages/ProjectPages";
import type { Task } from "./types";

it("describes a claim without implying active work", () => {
  const task = { id: "task-1", title: "An owned task", status: "claimed", owner: "codex" } as Task;
  const chart = renderToStaticMarkup(<><StageBar tasks={[task]} label="Tasks" /><StageLegend tasks={[task]} /></>);
  expect(chart).toContain('aria-label="Tasks: 1 claimed"');
  expect(chart).toContain("Claimed");
  expect(chart).not.toContain("In progress");
  const board = renderToStaticMarkup(<WorkBoard hub={{ session: null } as Hub} tasks={[task]} />);
  expect(board).toContain('aria-label="Claimed"');
  expect(board).toContain("<h3>Claimed</h3>");
  expect(board).not.toContain("In progress");
});
