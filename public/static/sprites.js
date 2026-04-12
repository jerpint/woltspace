// ── Creature Sprite Data & Renderer ──
// Shared across all pages. Import via <script src="/static/sprites.js">

const WOLT_EMOJI = {
  raccoon: '🦝', beaver: '🦫', otter: '🦦', rodent: '🦝',
  wolf: '🐺', dog: '🐶', eagle: '🦅',
};
const RODENT_TYPES = new Set(['raccoon', 'beaver', 'otter', 'rodent']);
const WOLT_TYPES = new Set(['raccoon', 'beaver', 'otter', 'rodent', 'dog']);

const WOLT_SPRITES = {
  beaver: {
    map: ['..AAA........AAA.....', '.ABBBA......ABBBA....', '.ABAAAAAAAAAAAABA....', '.ABABBBBBBBBBAABA....', '..ABBBBBBBBBBBAA.....', '..ABABBBBAEBBBAA.....', '..ABABBBBAEBBBAA.....', '.ABBAAAABAEBBBBA.....', '.ABACAACCCFABBBA.....', '.ABACCACCCFABBBA.AAA.', '.ABBAAAAAAEBBBAAADDDA', '..ABBCAFABBBBAA.AADDA', '...ABAAAABBBBBBAADADA', '..ABBBBBBBBBBBBAADDAA', '.ABBABCCCAEBBBBBAADDA', '.ABBACCCABBBBABBADADA', '.AGGACCCAGGGGABBADADA', '..AAACCCAAAAABBBADDA.', '..ABCCCCCCFABBBBAAAA.', '.AAABCCCCAAAABBBAA...', 'ABBBACCCABBBBBBAA....', 'AAAAAAAAAAAAAAAA.....'],
    pal: {A:'#3f190e',B:'#af6127',C:'#fce6b0',D:'#773c1f',E:'#050003',F:'#fffee7',G:'#dd7a2d'}
  },
  otter: {
    map: ['.....AAAAAAAA......', '..AAACCCCCCCCAAA...', '.ACCCCCCCCCCCCCCA..', '.ACACCCCCCCCCCACA..', '..ACCBACCCCBACCA...', '..ACCAACDDCAACCA...', 'AAACBBBBAABBBBCAAA.', '..ABEBABAABABEBA...', '.AAABBBABBABBBAAA..', '...AABBBBBBBBAA....', '...ACCCCCCCCCCA....', '..ACCCCBBBBCCCCA...', '..ACCABBBBBBACCA...', '..ACCCABBBBACCCA...', '..AACCABBBBACCAA.AA', '..ACAABBBBBBAACAACA', '.ACCCBBBBBBBBCCCACA', '.ACAAABBBBBBAAACAA.', '.ACCCCABBBBACCCCA..', '..ACCCAAAAAACCCA...', '...AAA......AAA....'],
    pal: {A:'#402110',B:'#f2d79d',C:'#9f5332',D:'#ff6970',E:'#c47b4a'}
  },
  raccoon: {
    map: ['...BB........BBB.........', '...BFGG.....GFFB.........', '...BABB.....BAAB.........', '...BCAABBBBBACCB.........', '...BAAAAAAAAAAAB.........', '...BCCCCAAACCCCB.........', '...BCBCCAAACCCCB.........', '.BBCCBBCBCCCBCBCB........', '.BBACBBCAAACBCCAB........', '.BBCAAADHHHDAAACB...BBB..', '.BBCAAADHHHDAAACB...BBB..', '...BCAADDDDDAEEB...BCCCB.', '....BCCCCEEECBB....BAAACB', '...BAAAAAAAAAAAB...BCCCBB', '...BAAAAAAAAAAAB...BCCCCB', '.BBAAAAAAAAAAAAAB..BAAAAB', '.BBAABBAAAAABAAAB..BCCCCB', '.BBAAAABAAABAAAABBBAACCCB', '.GGFAAABAAABAAAAGBBAABBCG', 'BAABAAABAAABAAABABBCAAAB.', 'BAAABBBBAAABBBBAABBCCBB..', 'BCCAAAAAAAAAAAAACBBBB....', '.BBBBGGAAAAAGBBBB........', '.BBCCBBAAAAABCCCB........', '...BBBBBBBBBBBBB.........'],
    pal: {A:'#7f8894',B:'#282c33',C:'#40454b',D:'#f0ecf0',E:'#545c65',F:'#a0abbd',G:'#050029',H:'#efa09f'}
  }
};
WOLT_SPRITES.rodent = WOLT_SPRITES.raccoon;

function woltSpriteAvatar(type, size) {
  const s = WOLT_SPRITES[type];
  if (!s) return null;
  const rows = s.map.length, cols = Math.max(...s.map.map(r => r.length));
  const px = size / Math.max(rows, cols);
  let rects = '';
  for (let r = 0; r < rows; r++) for (let c = 0; c < s.map[r].length; c++) {
    const ch = s.map[r][c]; if (ch === '.' || ch === ' ') continue;
    const fill = s.pal[ch]; if (!fill) continue;
    rects += `<rect x="${(c*px).toFixed(1)}" y="${(r*px).toFixed(1)}" width="${px.toFixed(1)}" height="${px.toFixed(1)}" fill="${fill}"/>`;
  }
  return `<svg width="${size}" height="${size}" xmlns="http://www.w3.org/2000/svg" style="image-rendering:pixelated;display:block" shape-rendering="crispEdges">${rects}</svg>`;
}

// Background scene sprite data (used by home page)
const BG_SPRITE_PAL = {
  bv: {A:'#3f190e',B:'#af6127',C:'#fce6b0',D:'#773c1f',E:'#050003',F:'#fffee7',G:'#dd7a2d'},
  rc: {A:'#7f8894',B:'#282c33',C:'#40454b',D:'#f0ecf0',E:'#545c65',F:'#a0abbd',G:'#050029',H:'#efa09f'},
  lg: {A:'#894f22',B:'#5a3614',C:'#ffca4e',D:'#d09023',E:'#291a09',F:'#327a00',G:'#ffed66'},
  lf: {A:'#19d024',B:'#009728'},
  fs: {A:'#04273c',B:'#63888b',C:'#ff6f61',D:'#0096d7',E:'#b1c4bb',F:'#fcfcf4',G:'#295a69',H:'#000219',I:'#86a4a1',J:'#d5d5c9',K:'#007efb',L:'#0075bf'},
  pk: {A:'#402110',B:'#f2d79d',C:'#9f5332',D:'#ff6970',E:'#c47b4a'},
};

const BG_SPRITE_MAPS = {
  bvA: ['..AAA........AAA.....', '.ABBBA......ABBBA....', '.ABAAAAAAAAAAAABA....', '.ABABBBBBBBBBAABA....', '..ABBBBBBBBBBBAA.....', '..ABABBBBAEBBBAA.....', '..ABABBBBAEBBBAA.....', '.ABBAAAABAEBBBBA.....', '.ABACAACCCFABBBA.....', '.ABACCACCCFABBBA.AAA.', '.ABBAAAAAAEBBBAAADDDA', '..ABBCAFABBBBAA.AADDA', '...ABAAAABBBBBBAADADA', '..ABBBBBBBBBBBBAADDAA', '.ABBABCCCAEBBBBBAADDA', '.ABBACCCABBBBABBADADA', '.AGGACCCAGGGGABBADADA', '..AAACCCAAAAABBBADDA.', '..ABCCCCCCFABBBBAAAA.', '.AAABCCCCAAAABBBAA...', 'ABBBACCCABBBBBBAA....', 'AAAAAAAAAAAAAAAA.....'],
  bvB: ['..AAA........AAA.....', '.ABBBA......ABBBA....', '.ABAAAAAAAAAAAABA....', '.ABABBBBBBBBBAABA....', '..ABBBBBBBBBBBAA.....', '..ABABBBBAEBBBAA.....', '..ABABBBBAEBBBAA.....', '.ABBAAAABAEBBBBA.....', '.ABACAACCCFABBBA.....', '.ABACCACCCFABBBA.AAA.', '.ABBAAAAAAEBBBAAADDDA', '..ABBCAFABBBBAA.AADDA', '...ABAAAABBBBBBAADADA', '..ABBBBBBBBBBBBAADDAA', '.ABBABCCCAEBBBBBAADDA', '.ABBACCCABBBBABBADADA', '.AGGACCCAGGGGABBADADA', '..AAACCCAAAAABBBADDA.', '..ABCCCCCCFABBBBAAAA.', '.AAABCCCCAAAABBBAA...', '.ABBBACCCABBBBBBAA...', '.AAAAAAAAAAAAAAAA....'],
  rcA: ['...BB........BBB.........', '...BFGG.....GFFB.........', '...BABB.....BAAB.........', '...BCAABBBBBACCB.........', '...BAAAAAAAAAAAB.........', '...BCCCCAAACCCCB.........', '...BCBCCAAACCCCB.........', '.BBCCBBCBCCCBCBCB........', '.BBACBBCAAACBCCAB........', '.BBCAAADHHHDAAACB...BBB..', '.BBCAAADHHHDAAACB...BBB..', '...BCAADDDDDAEEB...BCCCB.', '....BCCCCEEECBB....BAAACB', '...BAAAAAAAAAAAB...BCCCBB', '...BAAAAAAAAAAAB...BCCCCB', '.BBAAAAAAAAAAAAAB..BAAAAB', '.BBAABBAAAAABAAAB..BCCCCB', '.BBAAAABAAABAAAABBBAACCCB', '.GGFAAABAAABAAAAGBBAABBCG', 'BAABAAABAAABAAABABBCAAAB.', 'BAAABBBBAAABBBBAABBCCBB..', 'BCCAAAAAAAAAAAAACBBBB....', '.BBBBGGAAAAAGBBBB........', '.BBCCBBAAAAABCCCB........', '...BBBBBBBBBBBBB.........'],
  rcB: ['...BB........BBB.........', '...BFGG.....GFFB.........', '...BABB.....BAAB.........', '...BCAABBBBBACCB.........', '...BAAAAAAAAAAAB.........', '...BCCCCAAACCCCB.........', '...BCBCCAAACCCCB.........', '.BBCCBBCBCCCBCBCB........', '.BBACBBCAAACBCCAB........', '.BBCAAADHHHDAAACB...BBB..', '.BBCAAADHHHDAAACB...BBB..', '...BCAADDDDDAEEB...BCCCB.', '....BCCCCEEECBB....BAAACB', '...BAAAAAAAAAAAB...BCCCBB', '...BAAAAAAAAAAAB...BCCCCB', '.BBAAAAAAAAAAAAAB..BAAAAB', '.BBAABBAAAAABAAAB..BCCCCB', '.BBAAAABAAABAAAABBBAACCCB', '.GGFAAABAAABAAAAGBBAABBCG', '.BAABAAABAAABAAABABBCAAAB', '.BAAABBBBAAABBBBAABBCCBB.', '.BCCAAAAAAAAAAAAACBBBB...', '..BBBBGGAAAAAGBBBB.......', '..BBCCBBAAAAABCCCB.......', '....BBBBBBBBBBBBB........'],
  log: ['..EEE............FEEEEE.', '.EAABE......F..FFBAACCCE', 'EAABBBFFFF..EF.EBAACCDDG', 'AAABBBAABBBBAAAABBBDCDCD', 'BBBAAAABBAAAAABBBBBDCCDG', 'BBBBBBBBBAABBBBAABBDCDDC', 'EBBBBBBBBBBBBBBBBBBCCDDC', '.EEEEEEEEEEEBEBBEBEEDDDE'],
  leafG: ['..........B', '.........AB', '....AAAAAAB', '...AAAAABAB', '..AABABBAAB', '..AABBAAAAB', '..AABABBABB', '..AABABBABB', '..ABAAAAAB.', '..BAAAAAB..', 'BBBBBBBB...', 'BB.........'],
  fish: ['........AABBBBA', '.......ABBBEAFA', '..AAAAAGBGEEAAA', '..BBBAGBEACEEFA', '..AGABBEBCACCH.', '..AAAGEECCIAA..', '...AGEEBCEJEA..', '...ABEBCJJBJA..', '..AGECCEBAAA...', '..ABECEEH......', '..ABEEEG.......', '...AEA.A.......', '.D.AEA.....D...', '.D.AGGA...DD...', '....ABBA..D....', 'DK..ABAA....DD.', 'DD..AA....D.L..', '..D..A...DD....', '..DDDDDDLDDD...', '....DLLDLDD....'],
  peek: ['.....AAAAAAAA......', '..AAACCCCCCCCAAA...', '.ACCCCCCCCCCCCCCA..', '.ACACCCCCCCCCCACA..', '..ACCBACCCCBACCA...', '..ACCAACDDCAACCA...', 'AAACBBBBAABBBBCAAA.', '..ABEBABAABABEBA...', '.AAABBBABBABBBAAA..', '...AABBBBBBBBAA....', '...ACCCCCCCCCCA....', '..ACCCCBBBBCCCCA...'],
};

function renderBgSprite(map, pal, px) {
  var rows = map.length;
  var cols = Math.max.apply(null, map.map(function(r){return r.length;}));
  var rects = [];
  for (var r = 0; r < rows; r++) {
    for (var c = 0; c < map[r].length; c++) {
      var fill = pal[map[r][c]];
      if (!fill) continue;
      rects.push('<rect x="'+(c*px)+'" y="'+(r*px)+'" width="'+px+'" height="'+px+'" fill="'+fill+'"/>');
    }
  }
  var w = cols*px, h = rows*px;
  return '<svg width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'" xmlns="http://www.w3.org/2000/svg" style="image-rendering:pixelated;display:block" shape-rendering="crispEdges">'+rects.join('')+'</svg>';
}
