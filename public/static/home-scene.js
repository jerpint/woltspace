// ── Home page: quotes, greeting, background scene, wackiness ──
// Requires sprites.js to be loaded first.

// ── Home quotes ──
const LODGE_QUOTES = [
  'a beaver never ships on Friday.',
  'the forest remembers what the terminal forgets.',
  'every great app starts with a gnaw.',
  'otters move fast. raccoons think twice. beavers build.',
  'if it works in the viewport, it works.',
  'state is temporary. memory is forever.',
  'the best code is the code you didn\'t write.',
  'wolts don\'t sleep — they just lose context.',
  'the river runs whether you push or not.',
  'a good lodge has thick walls and fast livereload.',
  'trees grow from the root, not the branch.',
  'one commit at a time.',
  'the pack runs together.',
  'downstream of every bug is a lesson.',
  'a raccoon\'s taste is a beaver\'s blueprint.',
];

function rotateQuote() {
  const el = document.getElementById('home-quote');
  if (!el) return;
  el.style.opacity = '0';
  setTimeout(() => {
    el.textContent = '\u00BB ' + LODGE_QUOTES[Math.floor(Math.random() * LODGE_QUOTES.length)];
    el.style.opacity = '1';
  }, 400);
}
rotateQuote();
setInterval(rotateQuote, 8000);

// ── Time-based greeting ──
(function() {
  const h = new Date().getHours();
  const el = document.getElementById('home-greeting');
  if (h < 6) el.textContent = 'the lodge never sleeps.';
  else if (h < 12) el.textContent = 'good morning.';
  else if (h < 18) el.textContent = 'the lodge';
  else el.textContent = 'evening in the lodge.';
})();

// ── PIXEL SPRITE RENDERER (background scene) ──
(function() {
  var PX = 8;

  function inject(id, mapKey, palKey, px) {
    var el = document.getElementById(id);
    if (el) el.innerHTML = renderBgSprite(BG_SPRITE_MAPS[mapKey], BG_SPRITE_PAL[palKey], px || PX);
  }

  inject('sp-bv-main-a', 'bvA', 'bv', 4);
  inject('sp-bv-main-b', 'bvB', 'bv', 4);
  inject('sp-bv-side',   'bvA', 'bv', 3);
  inject('sp-rc-a',      'rcA', 'rc', 3);
  inject('sp-rc-b',      'rcB', 'rc', 3);
  inject('sp-log',       'log', 'lg', 4);
  inject('sp-leaf-g',    'leafG', 'lf', 4);
  inject('sp-fish',      'fish', 'fs', 3);
  inject('sp-peek',      'peek', 'pk', 4);

  document.querySelectorAll('.sprite-2frame').forEach(function(el) {
    var svg = el.querySelector('svg');
    if (svg) {
      el.style.width  = svg.getAttribute('width')  + 'px';
      el.style.height = svg.getAttribute('height') + 'px';
    }
  });
})();

// ── LANDSCAPE SCENE GENERATOR ──
(function() {
  var S = 5;
  var W = Math.ceil(window.innerWidth / S) + 6;
  var H  = 48;
  var BANK  = 34;
  var RIVER = 40;

  var C = {
    fa:'#8AA6BA', fb:'#6A8698',
    sn:'#F0F4F8', sb:'#C8D8E8',
    na:'#3E5A32', nb:'#293E20',
    ta:'#3A7030', tb:'#2A5225', tc:'#1A3A18', tt:'#4A2810',
    ga:'#72B855', gb:'#4A8838', gc:'#2A5A1A',
    ea:'#5A4020', eb:'#3A2810',
    ra:'#90D4E8', rb:'#4AAAC8', rc:'#2A7AAA', rd:'#1A5888',
    ba:'#3A7030', bb:'#224A1A',
  };

  function dot(x, y, c) {
    return '<rect x="'+(x*S)+'" y="'+(y*S)+'" width="'+S+'" height="'+S+'" fill="'+c+'"/>';
  }

  var fp = [], np = [];
  for (var x = 0; x < W; x++) {
    fp[x] = Math.round(7 + 5*Math.sin(x*0.05+0.3) + 3*Math.sin(x*0.12+1.7) + 1.5*Math.sin(x*0.04+0.9));
    np[x] = Math.round(6 + 6*Math.sin(x*0.08+1.0) + 3.5*Math.sin(x*0.16+0.5) + 1.5*Math.sin(x*0.22+2.4));
  }

  var out = [];

  for (var x = 0; x < W; x++) {
    var ft = BANK - 16 - fp[x];
    var snowLine = ft + 3;
    for (var y = Math.max(ft, 0); y < BANK; y++) {
      var col;
      if (y <= snowLine) {
        col = y === ft ? C.sn : (y === snowLine ? C.sb : C.sn);
      } else {
        col = y === ft ? C.fb : C.fa;
      }
      out.push(dot(x, y, col));
    }
  }

  for (var x = 0; x < W; x++) {
    var nt = BANK - 2 - np[x];
    for (var y = Math.max(nt, 1); y < BANK; y++)
      out.push(dot(x, y, y === nt ? C.nb : C.na));
  }

  for (var x = 0; x < W; x++) {
    out.push(dot(x, BANK,   C.ga));
    out.push(dot(x, BANK+1, C.gb));
    out.push(dot(x, BANK+2, C.gc));
    out.push(dot(x, BANK+3, C.ea));
    for (var y = BANK+4; y < RIVER; y++) out.push(dot(x, y, C.eb));
  }

  for (var x = 0; x < W; x++) {
    out.push(dot(x, RIVER, C.ba));
    var sh = ((x*3+2) % 9 < 2) ? C.ra : (x % 5 === 0 ? C.rb : C.rc);
    out.push(dot(x, RIVER+1, sh));
    out.push(dot(x, RIVER+2, C.rc));
    out.push(dot(x, RIVER+3, C.rd));
    for (var y = RIVER+4; y < H; y++) out.push(dot(x, y, C.rd));
  }

  function tree(cx, baseY, h) {
    for (var dy = 0; dy < h; dy++) {
      var hw = Math.floor((dy + 1) * 0.55);
      var tc = dy < 2 ? C.ta : (dy < h - 2 ? C.tb : C.tc);
      for (var dx = -hw; dx <= hw; dx++) {
        var tx = cx + dx;
        if (tx >= 0 && tx < W) out.push(dot(tx, baseY - h + 1 + dy, tc));
      }
    }
    if (cx >= 0 && cx < W) { out.push(dot(cx, baseY+1, C.tt)); out.push(dot(cx, baseY+2, C.tt)); }
  }

  var tx = 3;
  while (tx < W - 3) {
    var nt2 = BANK - 2 - np[tx];
    if (nt2 < BANK - 7) {
      tree(tx, nt2 + Math.floor(np[tx] * 0.55), 5 + ((tx*7) % 4));
    }
    tx += 7 + ((tx*13 + 5) % 5);
  }

  var el = document.getElementById('bg-nature');
  if (el) el.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="'+(W*S)+'" height="'+(H*S)+'" style="image-rendering:pixelated;display:block" shape-rendering="crispEdges">'+out.join('')+'</svg>';
})();

// ── CLOUDS ──
(function() {
  var CP = { O:'#8898A8', w:'#E0E8EE', W:'#F8FCFF', s:'#BCC8D0' };
  var SHAPES = {
    lg: ['......OOOO...OOOO.......', '.....OwwWwOOOwwWwO......', '....OwWWWWwOwWWWWwO.....', '...OOwWWWWWwwWWWWWwOO...', '..OwwWWWWWWWWWWWWWWwO...', '..OwWWWWWWWWWWWWWWWWwO..', '..OwWWWWWWWWWWWWWWWWwO..', '..OwWWWWWWWWWWWWWWWWwO..', '..OwwWWWWWWWWWWWWWWwwO..', '..OssssssssssssssssssO..', '...OOOOOOOOOOOOOOOOOO...'],
    md: ['....OOO...OOO.......', '...OwWwOOOwWwO......', '..OwWWWwOwWWWwO.....', '.OOwWWWWwwWWWWwOO...', '.OwwWWWWWWWWWWWwO...', '.OwWWWWWWWWWWWWwO...', '.OwWWWWWWWWWWWWwO...', '.OwwWWWWWWWWWWwwO...', '.OssssssssssssssO...', '..OOOOOOOOOOOOOO....'],
    sm: ['...OOO..OOO....', '..OwWwOOwWwO...', '.OwWWWwwWWWwO..', '.OwwWWWWWWwwO..', '.OwWWWWWWWWwO..', '.OwwWWWWWWwwO..', '.OssssssssssO..', '..OOOOOOOOOO...'],
  };

  function renderCloud(map, px) {
    var cols = Math.max.apply(null, map.map(function(r){return r.length;}));
    var rects = [];
    for (var r = 0; r < map.length; r++) {
      for (var c = 0; c < map[r].length; c++) {
        var fill = CP[map[r][c]];
        if (!fill) continue;
        rects.push('<rect x="'+(c*px)+'" y="'+(r*px)+'" width="'+px+'" height="'+px+'" fill="'+fill+'"/>');
      }
    }
    var w = cols*px, h = map.length*px;
    return '<svg width="'+w+'" height="'+h+'" xmlns="http://www.w3.org/2000/svg" style="image-rendering:pixelated;display:block" shape-rendering="crispEdges">'+rects.join('')+'</svg>';
  }

  [
    { id:'cl-1', shape:'lg', px:7 },
    { id:'cl-2', shape:'md', px:6 },
    { id:'cl-3', shape:'sm', px:6 },
    { id:'cl-4', shape:'lg', px:6 },
    { id:'cl-5', shape:'md', px:5 },
    { id:'cl-6', shape:'sm', px:5 },
  ].forEach(function(c) {
    var el = document.getElementById(c.id);
    if (el) el.innerHTML = renderCloud(SHAPES[c.shape], c.px);
  });
})();

// ── BACKGROUND WACKINESS ENGINE ──
(function() {
  var WACKY_EMOJIS = ['🐟','🍂','💦','🌿','🪵','🦆','🐸','🍄','🌊','🪨','🌰','🐛'];

  function spawnFloatingEmoji() {
    var scene = document.getElementById('bg-scene');
    if (!scene || scene.style.display === 'none') return;
    var el = document.createElement('div');
    el.className = 'bg-emoji-float';
    el.textContent = WACKY_EMOJIS[Math.floor(Math.random() * WACKY_EMOJIS.length)];
    el.style.left   = (8 + Math.random() * 84) + '%';
    el.style.bottom = (8 + Math.random() * 35) + '%';
    el.style.fontSize = (14 + Math.random() * 18) + 'px';
    el.style.opacity  = '0.7';
    scene.appendChild(el);
    setTimeout(function(){el.remove();}, 3500);
  }

  function triggerPeek() {
    var peek = document.getElementById('bg-peek');
    if (!peek || peek.classList.contains('active')) return;
    peek.style.left = (15 + Math.random() * 55) + '%';
    peek.classList.add('active');
    setTimeout(function(){peek.classList.remove('active');}, 5500);
  }

  function triggerFishJump() {
    var fish = document.getElementById('bg-fish');
    if (!fish || fish.classList.contains('active')) return;
    fish.style.right = (80 + Math.random() * 200) + 'px';
    fish.classList.add('active');
    setTimeout(function(){fish.classList.remove('active');}, 2800);
  }

  function triggerLogSpin() {
    var log = document.getElementById('bg-log');
    if (!log) return;
    log.style.transition = 'transform 0.7s cubic-bezier(.68,-0.55,.27,1.55)';
    log.style.transform  = 'rotate(180deg) translateY(-10px)';
    setTimeout(function(){log.style.transform = '';}, 800);
  }

  function triggerBeaverFrenzy() {
    var b2 = document.getElementById('bg-beaver-2');
    if (!b2) return;
    b2.style.animation = 'none';
    void b2.offsetWidth;
    b2.style.animation = 'beaver-side-bob 0.45s ease-in-out 8, beaver-side-bob 3.6s ease-in-out infinite';
  }

  var events = [triggerPeek, triggerPeek, triggerPeek, triggerFishJump, spawnFloatingEmoji, spawnFloatingEmoji, triggerLogSpin];

  function scheduleNext() {
    var delay = 7000 + Math.random() * 18000;
    setTimeout(function() {
      events[Math.floor(Math.random() * events.length)]();
      scheduleNext();
    }, delay);
  }

  setTimeout(triggerPeek, 3000);
  setTimeout(scheduleNext, 8000);
})();
