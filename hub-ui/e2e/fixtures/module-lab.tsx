import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ModuleFrame } from "../../src/modules/ModuleFrame";
import "../../src/styles.css";

const params = new URLSearchParams(location.search);
const log: unknown[] = [];
const lab = window as unknown as { __lab: unknown[]; __resolvers: ((ok: boolean) => void)[]; __switch?: (name: string) => void; __unmount?: () => void };
lab.__lab = log;
lab.__resolvers = [];

function Lab() {
  const [name, setName] = useState(params.get("case") ?? "good");
  const [mounted, setMounted] = useState(true);
  lab.__switch = setName;
  lab.__unmount = () => setMounted(false);
  if (!mounted) return <p>unmounted</p>;
  return (
    <ModuleFrame title="Test module" src={`http://modules.test/m/${name}/index.html`}
      capabilities={["read:dataset/crowns", "submit:response"]}
      host={{
        getData: async (capability, key) => {
          log.push({ getData: [name, capability, key] });
          // ?slowdata=1 keeps reads pending so request limits can be exercised.
          if (params.get("slowdata") === "1") await new Promise(() => {});
          return { rows: [1, 2, 3] };
        },
        submit: async (item, payload, revision) => { log.push({ submit: [name, item, payload, revision] }); return { revision: revision + 1 }; },
      }}
      confirmSubmit={(payload) => {
        log.push({ confirm: payload });
        // ?defer=1 leaves the confirmation open until the test resolves it.
        if (params.get("confirmreject") === "1") return Promise.reject(new Error("dialog failed"));
        if (params.get("defer") === "1") return new Promise<boolean>((resolve) => { lab.__resolvers.push(resolve); });
        return Promise.resolve(params.get("decline") !== "1");
      }} />
  );
}
createRoot(document.getElementById("root")!).render(<Lab />);
