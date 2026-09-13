// VFP: Interface entry point — takes the session token and mounts the terminal screen.
// Changes when: global setup of the interface changes.
// Anti-goal:
// 1. Rendering the terminal without a token — every request would fail with 401.

import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/700.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { takeSessionToken } from "./session";
import "./styles.css";

const token = takeSessionToken();
const root = createRoot(document.getElementById("root")!);

// A relaunched terminal may reuse this tab and change only the fragment, which does not reload the page.
window.addEventListener("hashchange", () => {
  if (/(?:^#|&)t=/.test(window.location.hash)) {
    takeSessionToken();
    window.location.reload();
  }
});

root.render(
  <StrictMode>
    {token ? (
      <App token={token} />
    ) : (
      <div className="screen centered">
        <p>Нет ключа сессии.</p>
        <p className="muted">Запустите терминал через ludik.cmd — он сам откроет эту страницу.</p>
      </div>
    )}
  </StrictMode>,
);
