import "./style.css";
import { createMockAdapter, createTauriAdapter, isTauri } from "./adapters.js";
import { DesktopController, lodgeRouteFromDeepLink } from "./controller.js";
import { requestAndSendDemo } from "./notifications.js";
import { mountPixelScene } from "./scene.js";

const adapter = isTauri() ? createTauriAdapter() : createMockAdapter();
const controller = new DesktopController(adapter);
let pendingRoute = controller.lodgeUrl;
const app = document.querySelector("#app");

app.innerHTML = `
  <section class="shell" aria-live="polite">
    <div class="brand"><span class="mark">W</span><span>woltspace</span><span class="preview">${isTauri() ? "desktop" : "browser preview"}</span></div>
    <div class="card">
      <div class="eyebrow">DESKTOP LODGE</div>
      <h1 id="title">Opening the lodge</h1>
      <p id="detail">Checking the trail to your local Woltspace engine.</p>
      <div class="progress"><span id="progress"></span></div>
      <div id="actions" class="actions"></div>
      <details id="diagnostics" hidden><summary>Engine logs</summary><pre id="logs"></pre></details>
    </div>
    <footer><span><b></b> Local only · 127.0.0.1:7777</span><button id="notify">Try a notification</button><button id="reveal">Show data folder</button></footer>
  </section>`;

mountPixelScene(app);

const title = document.querySelector("#title");
const detail = document.querySelector("#detail");
const progress = document.querySelector("#progress");
const actions = document.querySelector("#actions");

function setMessage(heading, body) { title.textContent = heading; detail.textContent = body; }
function button(label, handler, primary = false) {
  const element = document.createElement("button");
  element.textContent = label;
  if (primary) element.className = "primary";
  element.addEventListener("click", handler);
  actions.append(element);
}

async function showLogs() {
  const diagnostics = document.querySelector("#diagnostics");
  document.querySelector("#logs").textContent = await adapter.engineLogs(200);
  diagnostics.hidden = false;
  diagnostics.open = true;
}

async function boot() {
  actions.replaceChildren();
  progress.style.width = "20%";
  setMessage("Opening the lodge", "Checking the trail to your local Woltspace engine.");
  try {
    const result = await controller.launch({ onProgress(message) { detail.textContent = message; progress.style.width = "65%"; } });
    if (result.kind === "ready") {
      progress.style.width = "100%";
      setMessage("The lodge is open", isTauri() ? "Taking you there now…" : "Preview complete — the desktop app would now load the shared web lodge.");
      if (isTauri()) window.location.replace(pendingRoute);
      else button("Restart preview", boot, true);
    } else if (result.kind === "docker-missing") {
      setMessage("Docker is needed", "Install Docker Desktop, open it, then try the trail again.");
      button("Open Docker guide", () => window.open("https://docs.docker.com/desktop/setup/install/mac-install/", "_blank"), true);
      button("Check again", boot);
    } else if (result.kind === "docker-stopped") {
      setMessage("Docker is still starting", "Docker Desktop did not become ready. You can open it manually, then check again.");
      button("Check again", boot, true);
    } else {
      setMessage("The lodge is taking a while", "The engine started, but the lodge has not answered yet.");
      button("Try again", boot, true); button("View logs", showLogs);
    }
  } catch (error) {
    setMessage("The trail is blocked", String(error));
    button("Try again", boot, true); button("View logs", showLogs);
  }
}

document.querySelector("#reveal").addEventListener("click", () => adapter.revealDataFolder());
document.querySelector("#notify").addEventListener("click", async (event) => {
  const result = await requestAndSendDemo();
  event.currentTarget.textContent = result === "granted" ? "Notification sent" : "Notifications unavailable";
});

if (isTauri()) {
  const { getCurrent, onOpenUrl } = await import("@tauri-apps/plugin-deep-link");
  const route = (urls) => {
    if (!urls?.length) return;
    pendingRoute = lodgeRouteFromDeepLink(urls[0]);
    if (window.location.origin === new URL(controller.lodgeUrl).origin) window.location.assign(pendingRoute);
  };
  route(await getCurrent());
  await onOpenUrl(route);
}

boot();
