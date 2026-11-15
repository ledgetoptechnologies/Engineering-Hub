import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { visibleTopics, type TopicEntry } from "../topicsApi";
import { TopicList } from "./SystemPages";

const server: TopicEntry[] = [{ topic: "hub-topic", last_activity_at: null, peer_count: 2, source: "server" }];

describe("topic directory provenance", () => {
  it("derives each selection afresh without mutating or polluting the server list", () => {
    const snapshot = structuredClone(server);
    const first = visibleTopics(server, [], "local-one");
    const second = visibleTopics(server, ["local-one"], "local-two");
    expect(first.map(entry => [entry.topic, entry.source])).toEqual([["local-one", "device"], ["hub-topic", "server"]]);
    expect(second.map(entry => [entry.topic, entry.source])).toEqual([["local-two", "device"], ["hub-topic", "server"]]);
    expect(visibleTopics(server, [], "hub-topic")).toEqual(snapshot);
    expect(server).toEqual(snapshot);
  });
  it("visibly distinguishes an unlisted local selection from the server directory", () => {
    const html = renderToStaticMarkup(<TopicList topics={visibleTopics(server, [], "local-topic")}
      current="local-topic" onSelect={() => {}} hasServer />);
    expect(html).toMatch(/local-topic<\/span><span class="topic-meta">This device/);
    expect(html).toMatch(/hub-topic<\/span><span class="topic-meta">Hub directory · 2 agents/);
    expect(html).toContain('aria-current="true"');
    expect(html).toContain("This device entries are local selections.");
  });
  it("labels the local fallback when no directory is available", () => {
    const topics = visibleTopics(null, ["local-one"], "local-two");
    expect(topics.map(entry => entry.source)).toEqual(["device", "device"]);
    const html = renderToStaticMarkup(<TopicList topics={topics} current="local-two" onSelect={() => {}} hasServer={false} />);
    expect(html).toContain("Topics saved or selected on this device.");
    expect(html).not.toContain("Hub directory entries are available");
  });
});
