// ── Pixel landscape + clouds ──
// VENDORED from public/static/home-scene.js (the `bg-nature` landscape generator
// and cloud renderer) so the desktop boot screen shows the same colony scene
// offline. Keep in sync with the source.

// Procedural riverbank landscape: frost bank, snow caps, pine ridge, grass,
// earth, and a shimmering river. Returns an <svg> string sized to `cols` grid
// columns. `S` is the pixel scale.
export function renderLandscape(cols, S = 5) {
  const W = cols;
  const H = 48;
  const BANK = 34;
  const RIVER = 40;

  const C = {
    fa: "#8AA6BA", fb: "#6A8698",
    sn: "#F0F4F8", sb: "#C8D8E8",
    na: "#3E5A32", nb: "#293E20",
    ta: "#3A7030", tb: "#2A5225", tc: "#1A3A18", tt: "#4A2810",
    ga: "#72B855", gb: "#4A8838", gc: "#2A5A1A",
    ea: "#5A4020", eb: "#3A2810",
    ra: "#90D4E8", rb: "#4AAAC8", rc: "#2A7AAA", rd: "#1A5888",
    ba: "#3A7030", bb: "#224A1A",
  };

  const dot = (x, y, c) =>
    `<rect x="${x * S}" y="${y * S}" width="${S}" height="${S}" fill="${c}"/>`;

  const fp = [], np = [];
  for (let x = 0; x < W; x++) {
    fp[x] = Math.round(7 + 5 * Math.sin(x * 0.05 + 0.3) + 3 * Math.sin(x * 0.12 + 1.7) + 1.5 * Math.sin(x * 0.04 + 0.9));
    np[x] = Math.round(6 + 6 * Math.sin(x * 0.08 + 1.0) + 3.5 * Math.sin(x * 0.16 + 0.5) + 1.5 * Math.sin(x * 0.22 + 2.4));
  }

  const out = [];

  // Frost bank + snow caps
  for (let x = 0; x < W; x++) {
    const ft = BANK - 16 - fp[x];
    const snowLine = ft + 3;
    for (let y = Math.max(ft, 0); y < BANK; y++) {
      let col;
      if (y <= snowLine) col = y === ft ? C.sn : (y === snowLine ? C.sb : C.sn);
      else col = y === ft ? C.fb : C.fa;
      out.push(dot(x, y, col));
    }
  }

  // Pine ridge silhouette
  for (let x = 0; x < W; x++) {
    const nt = BANK - 2 - np[x];
    for (let y = Math.max(nt, 1); y < BANK; y++) out.push(dot(x, y, y === nt ? C.nb : C.na));
  }

  // Grass + earth strip
  for (let x = 0; x < W; x++) {
    out.push(dot(x, BANK, C.ga));
    out.push(dot(x, BANK + 1, C.gb));
    out.push(dot(x, BANK + 2, C.gc));
    out.push(dot(x, BANK + 3, C.ea));
    for (let y = BANK + 4; y < RIVER; y++) out.push(dot(x, y, C.eb));
  }

  // River
  for (let x = 0; x < W; x++) {
    out.push(dot(x, RIVER, C.ba));
    const sh = ((x * 3 + 2) % 9 < 2) ? C.ra : (x % 5 === 0 ? C.rb : C.rc);
    out.push(dot(x, RIVER + 1, sh));
    out.push(dot(x, RIVER + 2, C.rc));
    out.push(dot(x, RIVER + 3, C.rd));
    for (let y = RIVER + 4; y < H; y++) out.push(dot(x, y, C.rd));
  }

  // Standing pines on the ridge
  function tree(cx, baseY, h) {
    for (let dy = 0; dy < h; dy++) {
      const hw = Math.floor((dy + 1) * 0.55);
      const tc = dy < 2 ? C.ta : (dy < h - 2 ? C.tb : C.tc);
      for (let dx = -hw; dx <= hw; dx++) {
        const tx = cx + dx;
        if (tx >= 0 && tx < W) out.push(dot(tx, baseY - h + 1 + dy, tc));
      }
    }
    if (cx >= 0 && cx < W) { out.push(dot(cx, baseY + 1, C.tt)); out.push(dot(cx, baseY + 2, C.tt)); }
  }

  let tx = 3;
  while (tx < W - 3) {
    const nt2 = BANK - 2 - np[tx];
    if (nt2 < BANK - 7) tree(tx, nt2 + Math.floor(np[tx] * 0.55), 5 + ((tx * 7) % 4));
    tx += 7 + ((tx * 13 + 5) % 5);
  }

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W * S}" height="${H * S}" style="image-rendering:pixelated;display:block" shape-rendering="crispEdges">${out.join("")}</svg>`;
}

// Drifting pixel clouds (three sizes).
const CLOUD_PAL = { O: "#8898A8", w: "#E0E8EE", W: "#F8FCFF", s: "#BCC8D0" };
const CLOUD_SHAPES = {
  lg: ['......OOOO...OOOO.......', '.....OwwWwOOOwwWwO......', '....OwWWWWwOwWWWWwO.....', '...OOwWWWWWwwWWWWWwOO...', '..OwwWWWWWWWWWWWWWWwO...', '..OwWWWWWWWWWWWWWWWWwO..', '..OwWWWWWWWWWWWWWWWWwO..', '..OwWWWWWWWWWWWWWWWWwO..', '..OwwWWWWWWWWWWWWWWwwO..', '..OssssssssssssssssssO..', '...OOOOOOOOOOOOOOOOOO...'],
  md: ['....OOO...OOO.......', '...OwWwOOOwWwO......', '..OwWWWwOwWWWwO.....', '.OOwWWWWwwWWWWwOO...', '.OwwWWWWWWWWWWWwO...', '.OwWWWWWWWWWWWWwO...', '.OwWWWWWWWWWWWWwO...', '.OwwWWWWWWWWWWwwO...', '.OssssssssssssssO...', '..OOOOOOOOOOOOOO....'],
  sm: ['...OOO..OOO....', '..OwWwOOwWwO...', '.OwWWWwwWWWwO..', '.OwwWWWWWWwwO..', '.OwWWWWWWWWwO..', '.OwwWWWWWWwwO..', '.OssssssssssO..', '..OOOOOOOOOO...'],
};

export function renderCloud(shape, px = 6) {
  const map = CLOUD_SHAPES[shape];
  const cols = Math.max(...map.map((r) => r.length));
  const rects = [];
  for (let r = 0; r < map.length; r++)
    for (let c = 0; c < map[r].length; c++) {
      const fill = CLOUD_PAL[map[r][c]];
      if (fill) rects.push(`<rect x="${c * px}" y="${r * px}" width="${px}" height="${px}" fill="${fill}"/>`);
    }
  const w = cols * px, h = map.length * px;
  return `<svg width="${w}" height="${h}" xmlns="http://www.w3.org/2000/svg" style="image-rendering:pixelated;display:block" shape-rendering="crispEdges">${rects.join("")}</svg>`;
}
