import { describe, expect, it, vi } from "vitest";
import { DesktopController, lodgeRouteFromDeepLink } from "../src/controller.js";

function adapter(engine = "missing") {
  return {
    dockerDetection: vi.fn(async () => ({ installed: true, running: true })),
    engineStatus: vi.fn(async () => ({ state: engine })),
    launchDockerDesktop: vi.fn(async () => {}),
    startEngine: vi.fn(async () => {}), pullImage: vi.fn(async () => {}), runEngine: vi.fn(async () => {}),
    lodgeReady: vi.fn(async () => true)
  };
}

describe("DesktopController", () => {
  it("pulls and runs a missing engine before opening the lodge", async () => {
    const api = adapter();
    const result = await new DesktopController(api, { sleep: async () => {} }).launch();
    expect(result.kind).toBe("ready");
    expect(api.pullImage).toHaveBeenCalledOnce();
    expect(api.runEngine).toHaveBeenCalledOnce();
  });

  it("starts an existing stopped container without pulling", async () => {
    const api = adapter("stopped");
    await new DesktopController(api).launch();
    expect(api.startEngine).toHaveBeenCalledOnce();
    expect(api.pullImage).not.toHaveBeenCalled();
  });

  it("reports Docker availability without attempting mutations", async () => {
    const api = adapter();
    api.dockerDetection.mockResolvedValue({ installed: false, running: false });
    const result = await new DesktopController(api).launch();
    expect(result.kind).toBe("docker-missing");
    expect(api.runEngine).not.toHaveBeenCalled();
    expect(api.engineStatus).not.toHaveBeenCalled();
  });

  it("does not inspect the engine when Docker is installed but stopped", async () => {
    const api = adapter();
    api.dockerDetection.mockResolvedValue({ installed: true, running: false });
    const snapshot = await new DesktopController(api).inspect();
    expect(snapshot.engine).toBeNull();
    expect(api.engineStatus).not.toHaveBeenCalled();
  });

  it("launches Docker Desktop and waits for readiness before inspecting the engine", async () => {
    const api = adapter("running");
    api.dockerDetection
      .mockResolvedValueOnce({ installed: true, running: false })
      .mockResolvedValueOnce({ installed: true, running: false })
      .mockResolvedValue({ installed: true, running: true });
    const result = await new DesktopController(api, { sleep: async () => {}, maxPolls: 3 }).launch();
    expect(result.kind).toBe("ready");
    expect(api.launchDockerDesktop).toHaveBeenCalledOnce();
    expect(api.engineStatus).toHaveBeenCalledOnce();
  });

  it("keeps recovery available when Docker Desktop cannot become ready", async () => {
    const api = adapter();
    api.dockerDetection.mockResolvedValue({ installed: true, running: false });
    const result = await new DesktopController(api, { sleep: async () => {}, maxPolls: 2 }).launch();
    expect(result.kind).toBe("docker-stopped");
    expect(api.engineStatus).not.toHaveBeenCalled();
  });
});

describe("deep-link routing", () => {
  it("routes session links into the existing lodge TUI", () => {
    expect(lodgeRouteFromDeepLink("woltspace://session/dawn walk"))
      .toBe("http://127.0.0.1:7777/tui?session=dawn%20walk");
  });
  it("does not route arbitrary protocols", () => {
    expect(lodgeRouteFromDeepLink("https://attacker.invalid/session/nope"))
      .toBe("http://127.0.0.1:7777");
  });
});
