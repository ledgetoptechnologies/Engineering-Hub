import React from "react";
import { createRoot } from "react-dom/client";
import { HubApp } from "./App";
import "./styles.css";

class Boundary extends React.Component<
  { children: React.ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <main className="recovery">
        <h1>Unable to display this snapshot.</h1>
        <p>
          The API response could not be displayed safely. No action was
          submitted.
        </p>
        <button onClick={() => location.reload()}>Reload the workspace</button>
      </main>
    ) : (
      this.props.children
    );
  }
}

createRoot(document.getElementById("root")!).render(
  <Boundary>
    <HubApp />
  </Boundary>,
);
