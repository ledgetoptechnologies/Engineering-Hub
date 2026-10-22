import { createRoot } from "react-dom/client";
import { ModuleFrame } from "../../src/modules/ModuleFrame";
import "../../src/styles.css";

const params = new URLSearchParams(location.search);
const name = params.get("case") ?? "good";
const log: unknown[] = [];
(window as unknown as { __lab: unknown[] }).__lab = log;

createRoot(document.getElementById("root")!).render(
  <ModuleFrame title="Test module" src={`http://modules.test/m/${name}/index.html`}
    capabilities={["read:dataset/crowns", "submit:response"]}
    host={{
      getData: async (capability, key) => { log.push({ getData: [capability, key] }); return { rows: [1, 2, 3] }; },
      submit: async (item, payload, revision) => { log.push({ submit: [item, payload, revision] }); return { revision: revision + 1 }; },
    }}
    confirmSubmit={async (payload) => { log.push({ confirm: payload }); return params.get("decline") !== "1"; }} />,
);
