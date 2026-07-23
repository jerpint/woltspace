// ── Boot screen pixel scene ──
// Composes the colony's pixel art (landscape + clouds + bobbing beaver + log)
// into the boot background, mirroring the web lodge's home scene so the icon,
// this screen, and the real lodge all share one look.
import { BG_SPRITE_MAPS, BG_SPRITE_PAL, renderBgSprite } from "./pixel-sprites.js";
import { renderLandscape, renderCloud } from "./pixel-scene.js";

const LAND_SCALE = 5;

export function mountPixelScene(root) {
  const scene = document.createElement("div");
  scene.className = "bg-scene";
  scene.setAttribute("aria-hidden", "true");
  scene.innerHTML = `
    <div class="bg-cloud c1"></div>
    <div class="bg-cloud c2"></div>
    <div class="bg-cloud c3"></div>
    <div id="bg-nature"></div>
    <div class="bg-river-shimmer"></div>
    <div class="bg-log"></div>
    <div class="bg-beaver"></div>`;
  root.append(scene);

  scene.querySelector(".c1").innerHTML = renderCloud("lg", 7);
  scene.querySelector(".c2").innerHTML = renderCloud("sm", 6);
  scene.querySelector(".c3").innerHTML = renderCloud("md", 6);
  scene.querySelector(".bg-log").innerHTML = renderBgSprite(BG_SPRITE_MAPS.log, BG_SPRITE_PAL.lg, 4);
  scene.querySelector(".bg-beaver").innerHTML = renderBgSprite(BG_SPRITE_MAPS.bvA, BG_SPRITE_PAL.bv, 4);

  const nature = scene.querySelector("#bg-nature");
  const paint = () => {
    const cols = Math.ceil(window.innerWidth / LAND_SCALE) + 6;
    nature.innerHTML = renderLandscape(cols, LAND_SCALE);
  };
  paint();

  // Re-render the landscape on resize (debounced) so it always spans the window.
  let timer;
  window.addEventListener("resize", () => {
    clearTimeout(timer);
    timer = setTimeout(paint, 150);
  });

  return scene;
}
