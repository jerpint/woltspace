const DEFAULT_LODGE_URL = "http://127.0.0.1:7777";

export class DesktopController {
  constructor(adapter, options = {}) {
    this.adapter = adapter;
    this.lodgeUrl = options.lodgeUrl ?? DEFAULT_LODGE_URL;
    this.pollInterval = options.pollInterval ?? 800;
    this.maxPolls = options.maxPolls ?? 75;
    this.sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  }

  async inspect() {
    const docker = await this.adapter.dockerDetection();
    if (!docker.installed || !docker.running) return { docker, engine: null };
    const engine = await this.adapter.engineStatus();
    return { docker, engine };
  }

  async launch({ onProgress = () => {} } = {}) {
    const snapshot = await this.inspect();
    if (!snapshot.docker.installed) return { kind: "docker-missing", ...snapshot };
    if (!snapshot.docker.running) {
      onProgress("Starting Docker Desktop…");
      try {
        await this.adapter.launchDockerDesktop();
      } catch (error) {
        return { kind: "docker-stopped", launchError: String(error), ...snapshot };
      }
      if (!(await this.waitForDocker())) return { kind: "docker-stopped", ...snapshot };
      snapshot.docker = await this.adapter.dockerDetection();
      snapshot.engine = await this.adapter.engineStatus();
    }

    if (snapshot.engine.state === "running") {
      onProgress("Waking the lodge…");
    } else if (snapshot.engine.state === "stopped") {
      onProgress("Starting the lodge…");
      await this.adapter.startEngine();
    } else {
      onProgress("Fetching the Woltspace engine…");
      await this.adapter.pullImage();
      onProgress("Building the lodge…");
      await this.adapter.runEngine();
    }

    const ready = await this.waitUntilReady();
    return ready ? { kind: "ready", url: this.lodgeUrl } : { kind: "timeout" };
  }

  async waitUntilReady() {
    for (let attempt = 0; attempt < this.maxPolls; attempt += 1) {
      if (await this.adapter.lodgeReady(this.lodgeUrl)) return true;
      await this.sleep(this.pollInterval);
    }
    return false;
  }

  async waitForDocker() {
    for (let attempt = 0; attempt < this.maxPolls; attempt += 1) {
      const docker = await this.adapter.dockerDetection();
      if (docker.running) return true;
      await this.sleep(this.pollInterval);
    }
    return false;
  }
}

export { DEFAULT_LODGE_URL };

export function lodgeRouteFromDeepLink(value, lodgeUrl = DEFAULT_LODGE_URL) {
  try {
    const url = new URL(value);
    if (url.protocol !== "woltspace:") return lodgeUrl;
    if (url.hostname === "session") {
      const session = decodeURIComponent(url.pathname.replace(/^\//, ""));
      return session ? `${lodgeUrl}/tui?session=${encodeURIComponent(session)}` : lodgeUrl;
    }
  } catch { /* Invalid links safely fall back to the lodge. */ }
  return lodgeUrl;
}
