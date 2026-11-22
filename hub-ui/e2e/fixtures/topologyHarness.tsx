import { createRoot } from "react-dom/client";
import type { Hub } from "../../src/hub";
import { TopologyPage } from "../../src/pages/TopologyPage";
import "../../src/styles.css";

const hub = {
  demo: false,
  session: null,
  data: {},
  clearPrivateWorkspace() {},
} as unknown as Hub;
const root = document.getElementById("root");
if (!root) throw new Error("Missing topology test root");
createRoot(root).render(<TopologyPage hub={hub} actions={null} />);
