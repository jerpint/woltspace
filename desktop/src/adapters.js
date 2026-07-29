const invoke = async (command, args) => {
  const { invoke: tauriInvoke } = await import("@tauri-apps/api/core");
  return tauriInvoke(command, args);
};

export function createTauriAdapter() {
  return {
    dockerDetection: () => invoke("docker_detection"),
    engineStatus: () => invoke("engine_status"),
    launchDockerDesktop: () => invoke("launch_docker_desktop"),
    startEngine: () => invoke("start_engine"),
    pullImage: () => invoke("pull_image"),
    runEngine: () => invoke("run_engine"),
    engineLogs: (tail = 200) => invoke("engine_logs", { tail }),
    revealDataFolder: () => invoke("reveal_data_folder"),
    lodgeReady: async (url) => {
      try {
        const response = await fetch(`${url}/`, { cache: "no-store" });
        return response.ok;
      } catch {
        return false;
      }
    }
  };
}

export function createMockAdapter(options = {}) {
  let status = options.initialStatus ?? "missing";
  let checks = 0;
  return {
    dockerDetection: async () => ({ installed: true, running: true, version: "Docker 27.1 (preview)" }),
    engineStatus: async () => ({ state: status, image: "woltspace/woltspace:latest", container: "woltspace" }),
    launchDockerDesktop: async () => {},
    startEngine: async () => { status = "running"; },
    pullImage: async () => {},
    runEngine: async () => { status = "running"; },
    engineLogs: async () => "[preview] lodge service started\n[preview] listening on 127.0.0.1:7777",
    revealDataFolder: async () => {},
    lodgeReady: async () => { checks += 1; return checks >= (options.readyAfter ?? 3); }
  };
}

export function isTauri() {
  return Boolean(window.__TAURI_INTERNALS__);
}
