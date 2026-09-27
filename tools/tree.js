/* Древо портфеля — живое дерево на всю страницу (ДРВ-01 … ДРВ-08).

   Не схема, а растение (ДРВ-01). Под землёй корни — фундамент: сделанное, на что опирается
   проект. Ствол — портфель. Скелетные ветви — проекты, от них — запросы и документы;
   задача растёт на ветви запроса, из которого выросла (ДРВ-02). Листья — записи памяти,
   цветы — задачи, плоды — коммиты с номером задачи (ДРВ-05), почки — предложения из входящих.

   Толщина ветви — вес поддерева в токенах (ДРВ-03) пополам с числом узлов: иначе лёгкие
   записи памяти пропали бы рядом с тяжёлыми задачами. Сечения сохраняются, как у живого
   дерева (правило да Винчи): сумма сечений детей равна сечению родителя. Узел с десятками
   детей не расходится метлой из одной точки — ветвь делится развилками по две-три.

   Цвет цветка — статус задачи: реализовано, в реализации, начато не на 100%, в бэклоге,
   предстоит, ждёт решения. Листва знания в режиме «статусы» спокойная, чтобы статусы
   читались; в режиме «виды» — по виду записи.

   Дата узла — когда он появился: «Рост» проигрывает историю по месяцам (ДРВ-04), оценочные
   даты сделанного помечены (ДРВ-06). Широкая ветвь не заслоняет крону: издали листва
   собирается в облака, подписи выводятся по важности и масштабу, ветвь сворачивается (ДРВ-07).
   Управление (ДРВ-08): масштаб колесом и щипком, перетаскивание, поиск с раскрытием пути,
   фильтры статусов, список проектов, миникарта, переход к карточке проекта.

   Дерево природное и занимает всю страницу (ДРВ-09). Canvas 2D без библиотек: дашборд
   открывается и без сети. Мир (ветви, ствол, облака кроны) рисуется в отдельный буфер и при
   движении камеры только сдвигается и масштабируется; листва и подсветка — на верхнем слое.
   При открытии — заставка (ДРВ-10): фигуры перетекают одна в другую, а формулы, из которых
   они получаются, возникают вокруг и питают их. */

const TREEVIEW = (function () {
  'use strict';

  /* ================================ утилиты ================================ */
  const TAU = Math.PI * 2;
  const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
  const lerp = (a, b, t) => a + (b - a) * t;
  const easeIO = t => (t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const easeOut = t => 1 - Math.pow(1 - t, 3);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const nf = n => (n == null || isNaN(n)) ? '—' : Math.round(n).toLocaleString('ru-RU').replace(/\s/g, '\u202f');
  const tokf = n => !n ? '0' : n >= 1e9 ? (n / 1e9).toFixed(1).replace('.', ',') + ' млрд'
    : n >= 1e6 ? Math.round(n / 1e6) + ' млн' : n >= 1e4 ? Math.round(n / 1e3) + ' тыс.' : nf(n);
  const plural = (n, one, few, many) => {
    const m10 = n % 10, m100 = n % 100;
    return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
  };
  const short = (s, n) => { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; };

  function hashStr(s) {
    let h = 2166136261 >>> 0;
    s = String(s);
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  // k-е «случайное» число узла. Раскладка детерминирована: дерево не прыгает между открытиями.
  function rnd(seed, k) {
    let t = (seed ^ Math.imul(k + 1, 0x9E3779B1)) >>> 0;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }
  const mix32 = (a, b) => Math.imul((a ^ Math.imul(b + 1, 0x9E3779B1)) >>> 0, 0x85EBCA6B) >>> 0;
  const HEX = new Map();
  function hex3(h) {
    let c = HEX.get(h);
    if (!c) { const n = parseInt(h.slice(1), 16); c = [n >> 16 & 255, n >> 8 & 255, n & 255]; HEX.set(h, c); }
    return c;
  }
  const rgba = (c, a) => 'rgba(' + (c[0] | 0) + ',' + (c[1] | 0) + ',' + (c[2] | 0) + ',' + (a == null ? 1 : +a.toFixed(3)) + ')';
  const mul3 = (c, k) => [clamp(c[0] * k, 0, 255), clamp(c[1] * k, 0, 255), clamp(c[2] * k, 0, 255)];

  /* =========================== статусы и цвета =========================== */
  // Статусы в бэклоге пишут как придётся: «queued», «в реализации 59%», «частично»,
  // «ждёт решения владельца стенда». Один словарь на всё дерево.
  function workStatus(s) {
    s = String(s || '').toLowerCase().trim();
    if (!s) return 'backlog';
    if (/^(done|готов|сделан|выполнен|закрыт)/.test(s)) return 'done';
    if (/(blocked|блок|жд[её]т|ожида)/.test(s)) return 'blocked';
    if (/(частич|partial)/.test(s) || (/готов/.test(s) && /не\s*начат/.test(s))) return 'partial';
    if (/(in-progress|progress|в работе|в реализации|ид[её]т|wip)/.test(s)) return 'progress';
    if (/(idea|иде[яи]|не\s*начат|отлож|later|потом)/.test(s)) return 'later';
    return 'backlog';
  }
  const CATS = {
    done:     {g: 'work', label: 'Реализовано', c: ['#15a04a', '#3ddc84']},
    progress: {g: 'work', label: 'В реализации', c: ['#f08c00', '#ffa94d']},
    partial:  {g: 'work', label: 'Начато, не на 100%', c: ['#d6a300', '#ffd43b']},
    backlog:  {g: 'work', label: 'В бэклоге', c: ['#2f7de1', '#6aa9ff']},
    later:    {g: 'work', label: 'Предстоит · идеи', c: ['#8b79c6', '#b6a7f0']},
    blocked:  {g: 'work', label: 'Ждёт решения · блок', c: ['#e03131', '#ff6b6b']},
    bud:      {g: 'work', label: 'Почки — предложения', c: ['#e64980', '#f783ac']},
    fruit:    {g: 'work', label: 'Плоды — коммиты', c: ['#8d5b24', '#dea45b']},
    'k:запрос':     {g: 'know', label: 'Запросы', c: ['#1f8a6a', '#3cc49a']},
    'k:решение':    {g: 'know', label: 'Решения', c: ['#2b7a96', '#52b6d8']},
    'k:требование': {g: 'know', label: 'Требования', c: ['#6b9a2f', '#9dcb55']},
    'k:риск':       {g: 'know', label: 'Риски', c: ['#b4572f', '#e8865c']},
    'k:итог':       {g: 'know', label: 'Итоги', c: ['#3c7f37', '#67bd5f']},
    'k:факт':       {g: 'know', label: 'Факты', c: ['#8a8e3a', '#bdc160']},
    'k:заметка':    {g: 'know', label: 'Заметки и источники', c: ['#778c68', '#98ad8c']},
    foundation:     {g: 'root', label: 'Корни — сделанное', c: ['#c68b22', '#ecb752']},
  };
  const WORK = ['done', 'progress', 'partial', 'backlog', 'later', 'blocked', 'bud', 'fruit'];
  const KNOW = ['k:запрос', 'k:решение', 'k:требование', 'k:риск', 'k:итог', 'k:факт', 'k:заметка'];
  const FOLIAGE = ['#5c9447', '#4b8447'];     // листва знания в режиме «статусы»
  const STRUCT = {'портфель': 1, 'проект': 1, 'фундамент': 1, 'бэклог': 1, 'почки': 1};
  function catOf(n) {
    switch (n.kind) {
      case 'задача': return workStatus(n.status);
      case 'предложение': return 'bud';
      case 'коммит': return 'fruit';
      case 'сделано': return 'foundation';
      case 'источник': return 'k:заметка';
      default: return CATS['k:' + n.kind] ? 'k:' + n.kind : 'k:заметка';
    }
  }
  const KIND_RU = {
    'портфель': 'Портфель — ствол', 'проект': 'Проект — скелетная ветвь', 'фундамент': 'Фундамент — корни проекта',
    'сделано': 'Сделано — корень', 'бэклог': 'Ветвь задач без запроса', 'почки': 'Почки — предложения',
    'предложение': 'Предложение из заметок', 'задача': 'Задача — цветок', 'коммит': 'Коммит — плод задачи',
    'запрос': 'Запрос', 'решение': 'Решение', 'требование': 'Требование', 'риск': 'Риск', 'итог': 'Итог',
    'факт': 'Факт', 'источник': 'Источник', 'заметка': 'Заметка',
  };
  const SCENE = {
    light: {sky: ['#9fc7e4', '#cfe4ef', '#f3ecda'], lamp: [255, 238, 176], soil: ['#5e4229', '#3e2a18', '#20150b'],
            strata: 'rgba(40,24,12,.16)', grass: '#6c9a39', wood: '#5f4430', woodHi: '#93704f', twig: '#6f5c3d',
            root: '#b68d63', ink: '#132016', ink2: '#3e4b40', halo: 'rgba(250,250,244,.92)', pill: 'rgba(255,255,250,.84)',
            glow: '#ff9f1c', cloud: 'rgba(255,255,255,.55)', dim: .2, flowerHalo: 'rgba(255,255,255,.95)'},
    dark:  {sky: ['#03070e', '#07111d', '#132235'], lamp: [175, 198, 255], soil: ['#2a1c11', '#170e08', '#080503'],
            strata: 'rgba(255,215,170,.05)', grass: '#3d6a30', wood: '#8a6749', woodHi: '#b8906a', twig: '#8a7a55',
            root: '#8c6c4c', ink: '#eef4ec', ink2: '#b9c6bb', halo: 'rgba(5,9,14,.9)', pill: 'rgba(9,14,22,.8)',
            glow: '#ffd35c', cloud: 'rgba(150,180,230,.05)', dim: .17, flowerHalo: 'rgba(6,10,16,.92)'},
  };

  /* ================================ раскладка ================================ */
  function layoutTree(TREE) {
    const root = TREE.root;
    const nodes = [];
    (function walk(n, p, d) {
      n._p = p; n._d = d;
      n._h = hashStr(n.id || ((p && p.id) || '') + '/' + nodes.length);
      n._struct = !!STRUCT[n.kind];
      n._cat = n._struct ? '' : catOf(n);
      n._root = false; n._vis = true; n._gt0 = null; n._chain = null; n._work = false;
      nodes.push(n);
      for (const c of (n.children || [])) walk(c, n, d + 1);
    })(root, null, 0);

    const projects = (root.children || []).filter(p => p.kind === 'проект');
    for (const p of projects) {
      p._found = (p.children || []).find(c => c.kind === 'фундамент') || null;
      p._crown = (p.children || []).filter(c => c.kind !== 'фундамент');
      if (p._found) (function mark(x) { x._root = true; (x.children || []).forEach(mark); })(p._found);
    }
    const crownKids = n => (n === root ? projects : n.kind === 'проект' ? n._crown : (n.children || []));
    const branchKids = n => crownKids(n).filter(c => c.kind !== 'коммит');
    for (const n of nodes) { let x = n; while (x && x.kind !== 'проект') x = x._p; n._proj = x || null; }

    // Доля кроны — подлинейно от числа узлов: большой проект заметен, но не съедает всё.
    (function count(n) {
      let c = 1;
      for (const k of crownKids(n)) { count(k); c += k._cnt; }
      n._cnt = c; n._sw = Math.pow(c, .62);
    })(root);

    const Ry = clamp(300 + 17 * Math.sqrt(root._cnt), 520, 1500);
    const Rx = Ry * 1.3, T = Ry * .52, A = 1.95, Cx = 0, Cy = -T - Ry * .38;
    // Край кроны — эллипс с «комьями»: силуэт дуба неровный.
    const rb = a => {
      const s = Math.sin(a), c = Math.cos(a);
      return (1 / Math.sqrt(s * s / (Rx * Rx) + c * c / (Ry * Ry))) *
        (1 + .07 * Math.sin(3 * a + 1.3) + .045 * Math.sin(7 * a + .4) + .028 * Math.sin(13 * a + 2.1));
    };
    // Самая тяжёлая ветвь — в середине, остальные попеременно по сторонам.
    const arranged = [];
    projects.slice().sort((a, b) => b._sw - a._sw).forEach((p, i) => (i % 2 ? arranged.unshift(p) : arranged.push(p)));

    function alloc(n, a0, a1) {
      n._a0 = a0; n._a1 = a1;
      const ks = branchKids(n), tot = ks.reduce((s, c) => s + c._sw, 0) || 1;
      let a = a0;
      for (const c of ks) { const sp = (a1 - a0) * c._sw / tot; alloc(c, a, a + sp); a += sp; }
    }
    { const tot = arranged.reduce((s, p) => s + p._sw, 0) || 1; let a = -A;
      for (const p of arranged) { const sp = 2 * A * p._sw / tot; alloc(p, a, a + sp); a += sp; } }

    // Листья растут гроздьями по 3–6 на концах побегов, внутри грозди — по золотому углу,
    // как семечки подсолнуха: вблизи крона похожа на крону, а не на щётку из прутьев.
    // Вынос ветви зависит от размера проекта: контур кроны задают крупные, мелкие растут внутри.
    const clusters = [], maxCnt = Math.max(2, ...projects.map(p => p._cnt));
    for (const p of projects) p._reach = .5 + .5 * Math.log(Math.max(2, p._cnt)) / Math.log(maxCnt);
    function place(n, fp) {
      const ks = branchKids(n), mid = (n._a0 + n._a1) / 2, span = n._a1 - n._a0;
      const reach = n._proj ? n._proj._reach : 1;
      const f = Math.min(1.03, n.kind === 'проект' ? .18 + .06 * rnd(n._h, 1) : fp + (reach - fp) * (.14 + .14 * rnd(n._h, 1)));
      const r = f * rb(mid);
      n._f = f; n._ang = mid;
      n._x = Cx + r * Math.sin(mid); n._y = Cy - r * Math.cos(mid);
      n._sp = Math.max(3, Math.abs(span) * r);
      let run = [];
      const flush = () => { if (run.length) cluster(run, f); run = []; };
      for (const c of ks) { if (branchKids(c).length) { flush(); place(c, f); } else run.push(c); }
      flush();
    }
    function cluster(run, fp) {
      const size = Math.ceil(run.length / Math.ceil(run.length / 6));
      for (let i = 0; i < run.length; i += size) {
        const m = run.slice(i, i + size), h = m[0]._h, a0 = m[0]._a0, a1 = m[m.length - 1]._a1;
        const mid = (a0 + a1) / 2 + (rnd(h, 2) - .5) * (a1 - a0) * .3;
        // гроздь — во всей толще кроны, гуще к краю: издали крона сплошной купол, а не кольцо
        const top = Math.max(fp + .08, 1.02 * (m[0]._proj ? m[0]._proj._reach : 1));
        const f = Math.min(1.02, fp + (top - fp) * (.2 + .8 * Math.sqrt(rnd(h, 1)))), r = f * rb(mid);
        const rho = m.length > 1 ? clamp((a1 - a0) * r * .42, 10, 64) : 0;
        const cl = {isCl: true, m, _h: h, _x: Cx + r * Math.sin(mid), _y: Cy - r * Math.cos(mid), rho, _sw: 0};
        const base = rnd(h, 3) * TAU;
        m.forEach((c, j) => {
          const th = base + j * 2.39996323, d = rho * Math.sqrt((j + .5) / m.length);
          c._x = cl._x + Math.cos(th) * d; c._y = cl._y + Math.sin(th) * d;
          c._f = f; c._ang = mid; c._sp = Math.max(6, rho ? rho * 1.3 : (a1 - a0) * r); c._cl = cl;
          cl._sw += c._sw;
        });
        clusters.push(cl);
      }
    }
    arranged.forEach(p => place(p, 0));
    root._x = 0; root._y = -T; root._hd = {x: 0, y: -1};

    // Корни: под землёй, шире и мельче кроны; стороны совпадают с ветвями своих проектов.
    const Rrx = Rx * 1.08, Rry = Ry * .46, Ar = 1.3;
    const rbr = a => {
      const s = Math.sin(a), c = Math.cos(a);
      return (1 / Math.sqrt(s * s / (Rrx * Rrx) + c * c / (Rry * Rry))) * (1 + .1 * Math.sin(5 * a + .7) + .05 * Math.sin(11 * a + 1.9));
    };
    const founds = arranged.map(p => p._found).filter(Boolean);
    for (const fd of founds) {
      const ks = fd.children || [];
      fd._cnt = 1 + ks.length; fd._sw = Math.pow(fd._cnt, .7);
      for (const k of ks) { k._cnt = 1; k._sw = 1; }
    }
    { const tot = founds.reduce((s, f) => s + f._sw, 0) || 1; let a = -Ar;
      for (const fd of founds) { const sp = 2 * Ar * fd._sw / tot; fd._a0 = a; fd._a1 = a + sp; a += sp; } }
    for (const fd of founds) {
      const mid = (fd._a0 + fd._a1) / 2;
      fd._ang = mid; fd._f = .22 + .08 * rnd(fd._h, 1);
      const r = fd._f * rbr(mid);
      fd._x = r * Math.sin(mid); fd._y = r * Math.cos(mid);
      fd._sp = Math.max(3, (fd._a1 - fd._a0) * r);
      const ks = fd.children || [], span = (fd._a1 - fd._a0) / Math.max(1, ks.length);
      ks.forEach((k, i) => {
        const a = fd._a0 + span * (i + .5) + (rnd(k._h, 2) - .5) * span * .6;
        const f = fd._f + (1 - fd._f) * (.3 + .7 * rnd(k._h, 1)), rr = f * rbr(a);
        k._ang = a; k._f = f; k._x = rr * Math.sin(a); k._y = rr * Math.cos(a);
        k._sp = Math.max(3, span * rr);
      });
    }

    // Сечение (да Винчи): вес родителя делится между детьми — пополам по токенам и по числу узлов.
    const W0 = Math.max(1, root.subtree_weight || 0);
    function share(ks, ew) {
      let tw = 0, tc = 0;
      for (const c of ks) { tw += Math.max(0, c.subtree_weight || 0); tc += c._cnt || 1; }
      for (const c of ks) {
        const b = (c._cnt || 1) / (tc || 1);
        c._ew = ew * (tw > 0 ? .5 * Math.max(0, c.subtree_weight || 0) / tw + .5 * b : b);
      }
    }
    root._ew = W0;
    (function down(n) { const ks = branchKids(n); if (ks.length) { share(ks, n._ew); ks.forEach(down); } })(root);
    share(founds, W0 * .7);
    for (const fd of founds) share(fd.children || [], fd._ew);
    const TW = clamp(Ry * .13, 70, 190);
    const wOf = ew => TW * Math.pow(Math.max(0, ew || 0) / W0, .6);
    for (const n of nodes) n._w = wOf(n._ew);
    for (const cl of clusters) cl._ew = cl.m.reduce((s, c) => s + (c._ew || 0), 0);

    // Ветви — кубические кривые: выходят по направлению родителя и изгибаются к цели.
    const edges = [], clumps = [];
    function curve(x0, y0, hx, hy, x3, y3, wig, seed) {
      const dx = x3 - x0, dy = y3 - y0, d = Math.hypot(dx, dy) || 1;
      const ux = dx / d, uy = dy / d, px = -uy, py = ux;
      let bx = hx * .55 + ux * .45, by = hy * .55 + uy * .45;
      const bl = Math.hypot(bx, by) || 1; bx /= bl; by /= bl;
      const n1 = (rnd(seed, 5) - .5) * wig * d, n2 = (rnd(seed, 6) - .5) * wig * d;
      const x1 = x0 + bx * d * .36 + px * n1, y1 = y0 + by * d * .36 + py * n1;
      const x2 = x3 - ux * d * .3 + px * n2, y2 = y3 - uy * d * .3 + py * n2;
      const tx = x3 - x2, ty = y3 - y2, tl = Math.hypot(tx, ty) || 1;
      return {x0, y0, x1, y1, x2, y2, x3, y3, hx: tx / tl, hy: ty / tl};
    }
    // Ребро знает своих «членов» — узлы, к которым через него идёт сок: по ним видимость,
    // рост и подсветка. lvl — место ребра в цепочке от родителя к узлу.
    function edge(e, w0, w1, isRoot, members, lvl) {
      e.w0 = w0; e.w1 = w1; e.root = isRoot; e.m = members; e.lvl = lvl;
      const m = Math.max(w0, w1);
      e.minx = Math.min(e.x0, e.x1, e.x2, e.x3) - m; e.maxx = Math.max(e.x0, e.x1, e.x2, e.x3) + m;
      e.miny = Math.min(e.y0, e.y1, e.y2, e.y3) - m; e.maxy = Math.max(e.y0, e.y1, e.y2, e.y3) + m;
      edges.push(e);
      return e;
    }
    // Гроздь ветвится как одно целое: побег до её центра, от центра — черешки к листьям.
    function unitsOf(ks) {
      const out = [];
      let last = null;
      for (const c of ks) {
        const cl = c._cl && c._cl.m.length > 1 ? c._cl : null;
        if (cl) { if (cl !== last) { out.push(cl); last = cl; } } else { out.push(c); last = null; }
      }
      return out;
    }
    function splitGroups(ks) {
      const parts = ks.length > 12 ? 3 : 2, tot = ks.reduce((s, c) => s + c._sw, 0);
      const out = [];
      let cur = [], acc = 0, k = 1;
      for (const c of ks) {
        cur.push(c); acc += c._sw;
        if (k < parts && acc >= tot * k / parts) { out.push(cur); cur = []; k++; }
      }
      if (cur.length) out.push(cur);
      if (out.length < 2) { const h = Math.ceil(ks.length / 2); return [ks.slice(0, h), ks.slice(h)]; }
      return out;
    }
    function direct(from, c, chain, isRoot) {
      if (c.isCl) {
        const w = Math.max(wOf(c._ew), .45);
        const e = edge(curve(from.x, from.y, from.hx, from.hy, c._x, c._y, .18, c._h), w * 1.03, w * .5, false, c.m, chain.length);
        const ch = chain.concat([e]);
        for (const lf of c.m) {
          const dx = lf._x - c._x, dy = lf._y - c._y, d = Math.hypot(dx, dy);
          const hx = d > .5 ? dx / d : e.hx, hy = d > .5 ? dy / d : e.hy;
          const pe = edge(curve(c._x, c._y, hx, hy, lf._x, lf._y, .1, lf._h), Math.max(w * .4, .3), .3, false, [lf], ch.length);
          lf._chain = ch.concat([pe]); lf._hd = {x: hx, y: hy};
          sprout(lf);
        }
        return;
      }
      const e = edge(curve(from.x, from.y, from.hx, from.hy, c._x, c._y, isRoot ? .34 : .2, c._h),
        Math.max(c._w * 1.04, .45), Math.max(c._w * (branchKids(c).length ? .9 : .55), .35), isRoot, [c], chain.length);
      c._chain = chain.concat([e]);
      c._hd = {x: e.hx, y: e.hy};
      sprout(c);
    }
    // Развилки: дети делятся на 2–3 соседние группы, у группы — общая ветвь до развилки.
    function fan(from, ks, chain, isRoot, seed, spread) {
      const us = unitsOf(ks);
      if (!us.length) return;
      const groups = us.length <= 3 ? us.map(c => [c]) : splitGroups(us);
      const px = -from.hy, py = from.hx;
      groups.forEach((g, i) => {
        let o = from;
        if (spread && groups.length > 1) {
          // у ствола ветви выходят рядом, а не из одной точки
          const u = i / (groups.length - 1) - .5, off = u * spread;
          let hx = from.hx + px * u * .9, hy = from.hy + py * u * .9;
          const hl = Math.hypot(hx, hy) || 1; hx /= hl; hy /= hl;
          o = {x: from.x + px * off, y: from.y + py * off, hx, hy};
        }
        if (g.length === 1) { direct(o, g[0], chain, isRoot); return; }
        let sx = 0, sy = 0, sw = 0, ew = 0;
        for (const c of g) { sx += c._x * c._sw; sy += c._y * c._sw; sw += c._sw; ew += c._ew || 0; }
        // малая группа ветвится ближе к своим листьям: побеги короткие, как у дуба
        const js = mix32(seed, i), t = clamp(.8 - .11 * Math.log2(g.length + 1), .3, .66) + (rnd(js, 1) - .5) * .08;
        const jx = o.x + (sx / sw - o.x) * t, jy = o.y + (sy / sw - o.y) * t;
        const w = Math.max(wOf(ew), .45), members = [];
        for (const u of g) { if (u.isCl) members.push(...u.m); else members.push(u); }
        const e = edge(curve(o.x, o.y, o.hx, o.hy, jx, jy, isRoot ? .3 : .16, js), w * 1.02, w * .9, isRoot, members, chain.length);
        fan({x: jx, y: jy, hx: e.hx, hy: e.hy}, g, chain.concat([e]), isRoot, js, 0);
      });
    }
    function sprout(n) {
      const ks = branchKids(n);
      if (ks.length) fan({x: n._x, y: n._y, hx: n._hd.x, hy: n._hd.y}, ks, [], n._root, n._h, 0);
      // плоды висят на коротких черенках под своей задачей
      const cm = crownKids(n).filter(c => c.kind === 'коммит');
      cm.forEach((c, i) => {
        const a = Math.PI / 2 + (i - (cm.length - 1) / 2) * .55, d = 15 + (i % 2) * 7;
        c._x = n._x + Math.cos(a) * d; c._y = n._y + Math.sin(a) * d + 3;
        const e = edge(curve(n._x, n._y, 0, 1, c._x, c._y, .05, c._h), .9, .55, false, [c], 0);
        c._chain = [e]; c._hd = {x: e.hx, y: e.hy};
      });
    }
    fan({x: 0, y: -T * .97, hx: 0, hy: -1}, arranged, [], false, root._h, TW * .5);
    fan({x: 0, y: -TW * .04, hx: 0, hy: 1}, founds, [], true, mix32(root._h, 99), TW * 1.1);
    // облака кроны: вокруг каждой грозди, отдельного листа и узла кроны — издали купол сплошной
    for (const cl of clusters) clumps.push({x: cl._x, y: cl._y, r: clamp(cl.rho * 1.25 + 46, 50, 150), g: cl.m});
    for (const n of nodes) {
      if (n._root || n._struct || n === root || n.kind === 'коммит' || !branchKids(n).length) continue;
      clumps.push({x: n._x, y: n._y, r: 58, g: [n]});
    }

    // Листья, цветы, плоды, почки, узелки корней.
    const leaves = [], fruits = [], buds = [], tips = [];
    for (const n of nodes) {
      if (n._struct || n === root) continue;
      if (n._root) { tips.push(n); continue; }
      if (n.kind === 'коммит') { fruits.push(n); continue; }
      if (n.kind === 'предложение') { buds.push(n); continue; }
      leaves.push(n);
    }
    for (const n of leaves) {
      const kids = branchKids(n).length;
      n._work = n.kind === 'задача';
      const base = n._cl && n._cl.rho ? clamp(n._cl.rho * .5, 6, 14) : clamp(n._sp * .9, 5, 15);
      n._ls = n._work ? clamp(base * 1.4, 10, 18) : base * (kids ? 1.15 : 1);
      const hd = n._hd || {x: 0, y: -1};
      n._lr = Math.atan2(hd.y, hd.x) + (rnd(n._h, 7) - .5) * .9;
      const off = kids && !n._work ? n._ls * .35 : 0;
      n._lx = n._x + hd.x * off; n._ly = n._y + hd.y * off;
      n._var = n._h % 3; n._ph = rnd(n._h, 8) * TAU;
    }
    for (const n of buds) { n._ls = 6.5; n._lx = n._x; n._ly = n._y; n._ph = rnd(n._h, 8) * TAU; }
    for (const n of fruits) { n._ls = 6; n._lx = n._x; n._ly = n._y; n._ph = rnd(n._h, 8) * TAU; }
    for (const n of tips) { n._ls = clamp(2.2 + n._w * 1.4, 3, 11); n._lx = n._x; n._ly = n._y; }
    // подпись проекта — в его части кроны, а не у развилки, где их было бы не прочесть
    for (const p of projects) {
      const mid = (p._a0 + p._a1) / 2, r = .58 * rb(mid);
      p._tx = Cx + r * Math.sin(mid); p._ty = Cy - r * Math.cos(mid);
    }

    // Сетка для попадания курсором.
    const CELL = 56, grid = new Map();
    for (const n of nodes) {
      if (n === root || n._x == null) continue;
      n._hx = n._lx != null ? n._lx : n._x; n._hy = n._ly != null ? n._ly : n._y;
      const k = Math.floor(n._hx / CELL) + ':' + Math.floor(n._hy / CELL);
      let a = grid.get(k); if (!a) grid.set(k, a = []); a.push(n);
    }

    let minx = -TW * 1.4, maxx = TW * 1.4, miny = -T, maxy = 0;
    for (const n of nodes) {
      if (n._x == null) continue;
      minx = Math.min(minx, n._x); maxx = Math.max(maxx, n._x);
      miny = Math.min(miny, n._y); maxy = Math.max(maxy, n._y);
    }
    const pad = 60, bounds = {minx: minx - pad, maxx: maxx + pad, miny: miny - pad, maxy: maxy + pad};
    bounds.w = bounds.maxx - bounds.minx; bounds.h = bounds.maxy - bounds.miny;
    bounds.cx = (bounds.minx + bounds.maxx) / 2; bounds.cy = (bounds.miny + bounds.maxy) / 2;

    // Месяц появления: ветвь — по самому раннему узлу, лист — по своей дате (ДРВ-04).
    for (const n of nodes) {
      const own = String(n.date || '').slice(0, 7), first = String(n.first_date || '').slice(0, 7);
      n._mk = (n.children || []).length ? (first || own) : (own || first);
    }
    const maxD = nodes.reduce((m, n) => Math.max(m, n._d), 0);
    return {root, nodes, projects, arranged, founds, edges, leaves, fruits, buds, tips, clumps, grid, CELL,
            dims: {Rx, Ry, T, TW, Cx, Cy, Rrx, Rry, A}, bounds, maxD, growUntil: 0};
  }

  /* ============================== состояние ============================== */
  const GROW_MS = 900;
  let M = null, DATA = null, OPTS = {};
  let layer = null, bg = null, fg = null, off = null, bctx = null, fctx = null, octx = null, mini = null, mctx = null;
  let W = 0, H = 0, DPR = 1;
  const cam = {x: 0, y: 0, s: .3};
  let offCam = null, staticMs = 0, lastInput = 0, idleT = 0;
  let fly = null, raf = 0, dirtyWorld = true, dirtyDyn = true, camMoved = true, lastFg = 0;
  let hover = null, sel = null, hiSet = null, catFocus = null;
  const hidden = new Set(), collapsed = new Set();
  let colorMode = 'status', windOn = true, cutoff = '', months = [], tIdx = 0;
  let playing = null, isOpenFlag = false, introDone = false, reduced = false;
  let SC = SCENE.light, dark = false, miniCache = null, els = {};

  function isDark() {
    const t = document.documentElement.getAttribute('data-theme');
    if (t === 'dark') return true;
    if (t === 'light') return false;
    return matchMedia('(prefers-color-scheme: dark)').matches;
  }
  function applyTheme() {
    dark = isDark(); SC = dark ? SCENE.dark : SCENE.light;
    if (layer) layer.classList.toggle('dark', dark);
    miniCache = null; redrawAll();
  }
  const catColor = cat => CATS[cat] ? CATS[cat].c[dark ? 1 : 0] : FOLIAGE[dark ? 1 : 0];
  function leafKey(n) {
    const c = n._cat;
    if (CATS[c] && CATS[c].g === 'know' && colorMode === 'status') return 'foliage';
    return c;
  }
  const keyColor = k => (k === 'foliage' ? FOLIAGE[dark ? 1 : 0] : catColor(k));
  const keyLabel = k => (k === 'foliage' ? 'Листва — записи памяти' : CATS[k] ? CATS[k].label : k);
  const leafColor = n => keyColor(leafKey(n));
  const shadeCache = new Map();
  function shade(hex, v) {
    const k = hex + v;
    let s = shadeCache.get(k);
    if (!s) { s = rgba(mul3(hex3(hex), [.84, 1, 1.13][v]), 1); shadeCache.set(k, s); }
    return s;
  }

  /* ============================== камера ============================== */
  const toSX = x => (x - cam.x) * cam.s + W / 2;
  const toSY = y => (y - cam.y) * cam.s + H / 2;
  const toWX = X => (X - W / 2) / cam.s + cam.x;
  const toWY = Y => (Y - H / 2) / cam.s + cam.y;
  const S_MIN = .03, S_MAX = 10;

  function schedule() { if (!raf && isOpenFlag) raf = requestAnimationFrame(frame); }
  function redrawAll() { dirtyWorld = true; dirtyDyn = true; schedule(); }
  function redrawDyn() { dirtyDyn = true; schedule(); }
  function moved() {
    camMoved = true; dirtyDyn = true; lastInput = performance.now();
    clearTimeout(idleT); idleT = setTimeout(schedule, 190);
    schedule();
  }
  function fitRect(minx, miny, maxx, maxy, dur, maxS) {
    // панели по краям закрывают часть сцены — дерево вписывается в то, что осталось
    const wide = W > 900, top = 84, bot = 74;
    const left = wide && els.proj && !els.proj.classList.contains('min') ? 262 : 24;
    const right = sel && wide ? 392 : wide && els.legend && !els.legend.classList.contains('min') ? 262 : 24;
    const s = clamp(Math.min((W - left - right) / Math.max(40, maxx - minx), (H - top - bot) / Math.max(40, maxy - miny)), S_MIN, maxS || S_MAX);
    const cx = (minx + maxx) / 2 - (left + (W - right) - W) / 2 / s;
    const cy = (miny + maxy) / 2 - (top - bot) / 2 / s;
    flyTo(cx, cy, s, dur);
  }
  function fitAll(dur) { const b = M.bounds; fitRect(b.minx, b.miny, b.maxx, b.maxy, dur == null ? 650 : dur); }
  function flyTo(x, y, s, dur) {
    if (!dur || reduced) { cam.x = x; cam.y = y; cam.s = s; fly = null; moved(); return; }
    fly = {x0: cam.x, y0: cam.y, s0: cam.s, x1: x, y1: y, s1: s, t0: performance.now(), dur};
    schedule();
  }
  function zoomAt(X, Y, k) {
    const wx = toWX(X), wy = toWY(Y), s = clamp(cam.s * k, S_MIN, S_MAX);
    cam.x = wx - (X - W / 2) / s; cam.y = wy - (Y - H / 2) / s; cam.s = s;
    fly = null; moved();
  }
  function subtreeBox(n) {
    let a = Infinity, b = Infinity, c = -Infinity, d = -Infinity;
    const add = (x, y) => { if (x < a) a = x; if (y < b) b = y; if (x > c) c = x; if (y > d) d = y; };
    (function w(x) {
      if (x._x != null) add(x._x, x._y);
      for (const e of (x._chain || [])) add(e.x0, e.y0);
      if (!collapsed.has(x.id)) for (const k of (x.children || [])) w(k);
    })(n);
    return [a - 40, b - 40, c + 40, d + 40];
  }
  function focusNode(n, dur) {
    if (!n || n === M.root) { fitAll(dur); return; }
    const [a, b, c, d] = subtreeBox(n);
    fitRect(a, b, c, d, dur == null ? 750 : dur, (n.children || []).length ? 3.2 : 4.5);
  }

  /* ============================== видимость ============================== */
  function recompute(now, animate) {
    let until = M.growUntil;
    for (const n of M.nodes) {
      const p = n._p;
      const v = (!p || (p._vis && !collapsed.has(p.id))) && (!cutoff || !n._mk || n._mk <= cutoff);
      if (animate && v && !n._vis) { n._gt0 = now + rnd(n._h, 9) * 260; until = Math.max(until, n._gt0 + GROW_MS); }
      n._vis = v;
    }
    M.growUntil = until;
    miniCache = null; redrawAll();
  }
  const growth = (n, now) => (n._gt0 == null ? 1 : clamp((now - n._gt0) / GROW_MS, 0, 1));
  const leafGrowth = (n, now) => easeOut(clamp((growth(n, now) - .6) / .4, 0, 1));
  const leafShown = n => n._vis && !hidden.has(leafKey(n));
  const dimmed = n => (hiSet && !hiSet.has(n)) || (catFocus && leafKey(n) !== catFocus);

  /* ============================== отрисовка ============================== */
  function partial(e, t) {
    const x01 = lerp(e.x0, e.x1, t), y01 = lerp(e.y0, e.y1, t), x12 = lerp(e.x1, e.x2, t), y12 = lerp(e.y1, e.y2, t);
    const x23 = lerp(e.x2, e.x3, t), y23 = lerp(e.y2, e.y3, t);
    const x012 = lerp(x01, x12, t), y012 = lerp(y01, y12, t), x123 = lerp(x12, x23, t), y123 = lerp(y12, y23, t);
    return [e.x0, e.y0, x01, y01, x012, y012, lerp(x012, x123, t), lerp(y012, y123, t)];
  }
  const full = e => [e.x0, e.y0, e.x1, e.y1, e.x2, e.y2, e.x3, e.y3];
  function taper(g, p, w0, w1) {
    const N = 12, L = [], R = [];
    for (let i = 0; i <= N; i++) {
      const t = i / N, m = 1 - t;
      const x = m * m * m * p[0] + 3 * m * m * t * p[2] + 3 * m * t * t * p[4] + t * t * t * p[6];
      const y = m * m * m * p[1] + 3 * m * m * t * p[3] + 3 * m * t * t * p[5] + t * t * t * p[7];
      const dx = 3 * m * m * (p[2] - p[0]) + 6 * m * t * (p[4] - p[2]) + 3 * t * t * (p[6] - p[4]);
      const dy = 3 * m * m * (p[3] - p[1]) + 6 * m * t * (p[5] - p[3]) + 3 * t * t * (p[7] - p[5]);
      const l = Math.hypot(dx, dy) || 1, nx = -dy / l, ny = dx / l, w = lerp(w0, w1, t) / 2;
      L.push(x + nx * w, y + ny * w); R.push(x - nx * w, y - ny * w);
    }
    // левый край — до конца, скруглённый кончик вперёд, правый край — обратно
    const e = L.length - 2;
    g.beginPath(); g.moveTo(L[0], L[1]);
    for (let i = 2; i < L.length; i += 2) g.lineTo(L[i], L[i + 1]);
    g.arc(p[6], p[7], w1 / 2, Math.atan2(L[e + 1] - p[7], L[e] - p[6]), Math.atan2(R[e + 1] - p[7], R[e] - p[6]), true);
    for (let i = e; i >= 0; i -= 2) g.lineTo(R[i], R[i + 1]);
    g.closePath(); g.fill();
  }
  function worldTransform(g) {
    g.setTransform(DPR * cam.s, 0, 0, DPR * cam.s, DPR * (W / 2 - cam.x * cam.s), DPR * (H / 2 - cam.y * cam.s));
  }
  function viewRect(margin) {
    const m = (margin || 40) / cam.s;
    return {x0: toWX(0) - m, x1: toWX(W) + m, y0: toWY(0) - m, y1: toWY(H) + m};
  }
  function roundRect(g, x, y, w, h, r) {
    if (g.roundRect) { g.roundRect(x, y, w, h, r); return; }
    g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
    g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
  }

  // Небо и земля — в экранных координатах: их не нужно кешировать вместе с миром.
  function drawSky(g) {
    g.setTransform(DPR, 0, 0, DPR, 0, 0);
    const gy = clamp(toSY(0), -10, H + 10), top = Math.max(0, gy);
    const sky = g.createLinearGradient(0, 0, 0, Math.max(40, gy));
    sky.addColorStop(0, SC.sky[0]); sky.addColorStop(.62, SC.sky[1]); sky.addColorStop(1, SC.sky[2]);
    g.fillStyle = sky; g.fillRect(0, 0, W, top);
    const lx = W * .83, ly = H * .13, lr = Math.max(W, H) * .42;
    const lamp = g.createRadialGradient(lx, ly, 0, lx, ly, lr);
    lamp.addColorStop(0, rgba(SC.lamp, dark ? .22 : .55)); lamp.addColorStop(.18, rgba(SC.lamp, dark ? .08 : .22));
    lamp.addColorStop(1, rgba(SC.lamp, 0));
    g.fillStyle = lamp; g.fillRect(0, 0, W, top);
    if (dark) {
      g.fillStyle = '#fff';
      for (let i = 0; i < 170; i++) {
        const x = rnd(7, i) * W, y = rnd(11, i) * top * .92;
        g.globalAlpha = .15 + rnd(13, i) * .55;
        const r = rnd(17, i) < .08 ? 1.3 : .7;
        g.fillRect(x, y, r, r);
      }
      g.globalAlpha = 1;
      if (ly < top) { g.fillStyle = 'rgba(235,240,255,.9)'; g.beginPath(); g.arc(lx, ly, 13, 0, TAU); g.fill(); }
    }
    if (gy < H) {
      const soil = g.createLinearGradient(0, gy, 0, H + Math.max(0, -gy));
      soil.addColorStop(0, SC.soil[0]); soil.addColorStop(.35, SC.soil[1]); soil.addColorStop(1, SC.soil[2]);
      g.fillStyle = soil; g.fillRect(0, top, W, H - top);
    }
  }
  function drawGround(g) {
    const v = viewRect(60), Rx = M.dims.Rx * 1.6;
    g.strokeStyle = SC.strata; g.lineWidth = 2.2 / cam.s;
    for (const [depth, amp, ph] of [[60, 9, .3], [170, 14, 1.4], [330, 18, 2.2], [520, 24, .9]]) {
      if (depth + 40 < v.y0 || depth - 40 > v.y1) continue;
      g.beginPath();
      const x0 = Math.max(v.x0, -Rx * 1.4), x1 = Math.min(v.x1, Rx * 1.4);
      for (let x = x0; x <= x1; x += 40) {
        const y = depth + Math.sin(x * .004 + ph) * amp + Math.sin(x * .013 + ph * 2) * amp * .3;
        x === x0 ? g.moveTo(x, y) : g.lineTo(x, y);
      }
      g.stroke();
    }
    if (cam.s > .35 && v.y0 < 20 && v.y1 > -30) {
      g.strokeStyle = SC.grass; g.lineWidth = 1.5; g.lineCap = 'round';
      g.beginPath();
      const x0 = Math.max(v.x0, -Rx), x1 = Math.min(v.x1, Rx), step = cam.s > .6 ? 3.5 : 7;
      for (let x = Math.floor(x0 / step) * step; x <= x1; x += step) {
        const h = hashStr('g' + Math.round(x)), len = 5 + rnd(h, 1) * 13, lean = (rnd(h, 2) - .5) * 7;
        g.moveTo(x, 1.5); g.quadraticCurveTo(x + lean * .3, -len * .6, x + lean, -len);
      }
      g.stroke();
    }
    g.fillStyle = dark ? 'rgba(0,0,0,.35)' : 'rgba(40,28,14,.25)';
    const gx0 = Math.max(v.x0, -Rx * 2);
    g.fillRect(gx0, -.8, Math.min(v.x1, Rx * 2) - gx0, 3.2);
  }
  function drawClouds(g) {
    if (dark) return;
    const {Rx, Ry, Cy} = M.dims;
    g.fillStyle = SC.cloud;
    for (let i = 0; i < 5; i++) {
      const cx = (rnd(31, i) - .5) * Rx * 3.2, cy = Cy - Ry * (1.25 + rnd(37, i) * .6), w = Ry * (.25 + rnd(41, i) * .25);
      g.beginPath();
      for (let k = 0; k < 5; k++) {
        const ex = cx + (k - 2) * w * .32, ey = cy + Math.sin(k * 1.7) * w * .06;
        g.moveTo(ex + w * .32, ey); g.ellipse(ex, ey, w * .32, w * .16, 0, 0, TAU);
      }
      g.fill();
    }
  }
  function drawTrunk(g, now) {
    const {T, TW} = M.dims;
    const gr = M.root._gt0 == null ? 1 : clamp((now - M.root._gt0) / (GROW_MS * 1.1), 0, 1);
    if (gr <= 0) return;
    const N = 22, L = [], R = [];
    const sway = u => TW * .07 * Math.sin(u * 2.4 + .6);
    const half = u => lerp(TW * .72, TW * .5, easeOut(u)) + TW * .55 * Math.exp(-u * 11);
    for (let i = 0; i <= N; i++) {
      const u = i / N * gr, y = -u * T, cx = sway(u), w = half(u);
      L.push(cx - w, y); R.push(cx + w, y);
    }
    const grad = g.createLinearGradient(-TW, 0, TW, 0);
    grad.addColorStop(0, rgba(mul3(hex3(SC.wood), .62)));
    grad.addColorStop(.38, SC.woodHi);
    grad.addColorStop(.6, SC.wood);
    grad.addColorStop(1, rgba(mul3(hex3(SC.wood), .5)));
    g.fillStyle = grad;
    g.beginPath(); g.moveTo(L[0], L[1]);
    for (let i = 2; i < L.length; i += 2) g.lineTo(L[i], L[i + 1]);
    for (let i = R.length - 2; i >= 0; i -= 2) g.lineTo(R[i], R[i + 1]);
    g.closePath(); g.fill();
    if (cam.s > .16) {                       // кора: продольные трещины
      g.strokeStyle = rgba(mul3(hex3(SC.wood), .5), .42); g.lineWidth = Math.max(1.2, 1 / cam.s);
      for (let k = -4; k <= 4; k++) {
        g.beginPath();
        for (let i = 0; i <= 24; i++) {
          const u = i / 24 * gr, y = -u * T;
          const x = sway(u) + (k / 5) * half(u) * .9 + Math.sin(u * 30 + k * 1.7) * TW * .018;
          i ? g.lineTo(x, y) : g.moveTo(x, y);
        }
        g.stroke();
      }
    }
  }
  function edgeLive(e, now) {
    let vis = false, gt = 0, hi = !hiSet;
    for (const m of e.m) {
      if (!m._vis) continue;
      vis = true;
      if (gt < 1) {
        const g = growth(m, now) * (m._chain ? m._chain.length : 1) - e.lvl;
        if (g > gt) gt = g > 1 ? 1 : g;
      }
      if (!hi && hiSet.has(m)) hi = true;
      if (hi && gt >= 1) break;
    }
    e._gt = gt; e._hi = hi;
    return vis && gt > 0;
  }
  function drawEdges(g, now) {
    const v = viewRect(30), s = cam.s, thin = new Map(), thick = [];
    for (const e of M.edges) {
      if (e.maxx < v.x0 || e.minx > v.x1 || e.maxy < v.y0 || e.miny > v.y1) continue;
      if (!edgeLive(e, now)) continue;
      const wpx = (e.w0 + e.w1) / 2 * s;
      if (wpx >= 2.6) { thick.push(e); continue; }
      const col = e.root ? SC.root : wpx > 1.2 ? SC.wood : SC.twig;
      const key = (e._hi ? 1 : 0) + '|' + col + '|' + Math.max(.6, Math.round(wpx * 2) / 2);
      let a = thin.get(key); if (!a) thin.set(key, a = []);
      a.push(e);
    }
    g.lineCap = 'round';
    for (const [key, arr] of thin) {
      const [hi, col, wk] = key.split('|');
      g.globalAlpha = (hi === '1' ? 1 : SC.dim) * (+wk < 1 && s < .5 ? .55 : 1);
      g.strokeStyle = col; g.lineWidth = +wk / s;
      g.beginPath();
      for (const e of arr) {
        const p = e._gt < 1 ? partial(e, easeOut(e._gt)) : full(e);
        g.moveTo(p[0], p[1]); g.bezierCurveTo(p[2], p[3], p[4], p[5], p[6], p[7]);
      }
      g.stroke();
    }
    thick.sort((a, b) => a.w0 - b.w0);
    const hiC = hex3(SC.woodHi);
    for (const e of thick) {
      g.globalAlpha = e._hi ? 1 : SC.dim;
      g.fillStyle = e.root ? SC.root : SC.wood;
      const gt = e._gt, p = gt < 1 ? partial(e, easeOut(gt)) : full(e);
      taper(g, p, e.w0, lerp(e.w0, e.w1, gt));
      if (s * e.w0 > 7 && gt >= 1) {         // свет слева сверху, тень справа: объём ветви
        let nx = -(e.y1 - e.y0), ny = e.x1 - e.x0;
        const nl = Math.hypot(nx, ny) || 1; nx /= nl; ny /= nl;
        if (nx > 0) { nx = -nx; ny = -ny; }
        g.lineWidth = e.w0 * .2;
        g.strokeStyle = rgba(hiC, e.root ? .16 : .32);
        const o0 = e.w0 * .2, o1 = e.w1 * .2;
        g.beginPath(); g.moveTo(p[0] + nx * o0, p[1] + ny * o0);
        g.bezierCurveTo(p[2] + nx * o0, p[3] + ny * o0, p[4] + nx * o1, p[5] + ny * o1, p[6] + nx * o1, p[7] + ny * o1);
        g.stroke();
        g.strokeStyle = 'rgba(0,0,0,.16)';
        g.beginPath(); g.moveTo(p[0] - nx * o0 * 1.2, p[1] - ny * o0 * 1.2);
        g.bezierCurveTo(p[2] - nx * o0 * 1.2, p[3] - ny * o0 * 1.2, p[4] - nx * o1 * 1.2, p[5] - ny * o1 * 1.2, p[6] - nx * o1 * 1.2, p[7] - ny * o1 * 1.2);
        g.stroke();
      }
    }
    g.globalAlpha = 1;
  }
  // Облака кроны: издали листья собираются в массы. Тень — позади ветвей, свет — поверх.
  function drawClumps(g, now, pass) {
    const s = cam.s, base = clamp(.66 - s * .8, 0, .56);
    if (base <= .01) return;
    const v = viewRect(260);
    for (const c of M.clumps) {
      if (c.x + c.r < v.x0 || c.x - c.r > v.x1 || c.y + c.r < v.y0 || c.y - c.r > v.y1) continue;
      let r = 0, gg = 0, b = 0, n = 0, grown = 0, hi = !hiSet;
      for (const k of c.g) {
        if (!k._vis) continue;
        const gr = leafGrowth(k, now); if (gr > grown) grown = gr;
        if (!hi && hiSet.has(k)) hi = true;
        if (k._work || !leafShown(k)) continue;
        const rgb = hex3(leafColor(k));
        r += rgb[0]; gg += rgb[1]; b += rgb[2]; n++;
      }
      if (grown <= 0) continue;
      const col = n ? [r / n, gg / n, b / n] : hex3(FOLIAGE[dark ? 1 : 0]);
      const a = base * (hi ? 1 : .28) * grown * (n ? 1 : .5);
      let grd, rad;
      if (pass === 0) {
        rad = c.r * 1.6 * (.5 + .5 * grown);
        const dc = mul3(col, dark ? .48 : .58);
        grd = g.createRadialGradient(c.x + rad * .1, c.y + rad * .14, 0, c.x, c.y, rad);
        grd.addColorStop(0, rgba(dc, a * .95)); grd.addColorStop(.6, rgba(dc, a * .7)); grd.addColorStop(1, rgba(dc, 0));
      } else {
        rad = c.r * .92 * (.5 + .5 * grown);
        const lc = mul3(col, dark ? 1.05 : 1.14);
        grd = g.createRadialGradient(c.x - rad * .28, c.y - rad * .32, 0, c.x, c.y, rad * 1.05);
        grd.addColorStop(0, rgba(lc, a * .72)); grd.addColorStop(.55, rgba(col, a * .42)); grd.addColorStop(1, rgba(col, 0));
      }
      g.fillStyle = grd;
      g.beginPath(); g.arc(c.x, c.y, rad, 0, TAU); g.fill();
    }
  }
  function drawTips(g, now) {
    if (hidden.has('foundation')) return;
    const v = viewRect(20), s = cam.s, col = catColor('foundation'), soft = rgba(hex3(col), .22);
    for (const n of M.tips) {
      if (!n._vis || n.kind !== 'сделано') continue;
      if (n._x < v.x0 || n._x > v.x1 || n._y < v.y0 || n._y > v.y1) continue;
      const gt = leafGrowth(n, now); if (gt <= 0) continue;
      const r = Math.max(n._ls, 1.8 / s) * gt;
      g.globalAlpha = hiSet && !hiSet.has(n) ? SC.dim : 1;
      g.fillStyle = soft; g.beginPath(); g.arc(n._x, n._y, r * 2.1, 0, TAU); g.fill();
      g.fillStyle = col; g.beginPath(); g.arc(n._x, n._y, r, 0, TAU); g.fill();
      if (r * s > 5) { g.fillStyle = 'rgba(255,255,255,.55)'; g.beginPath(); g.arc(n._x - r * .3, n._y - r * .3, r * .35, 0, TAU); g.fill(); }
    }
    g.globalAlpha = 1;
  }
  function renderWorld(now) {
    const t0 = performance.now(), g = octx;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, off.width, off.height);
    worldTransform(g);
    drawClouds(g);
    drawGround(g);
    drawClumps(g, now, 0);
    drawEdges(g, now);
    drawTrunk(g, now);
    drawTips(g, now);
    drawClumps(g, now, 1);
    offCam = {x: cam.x, y: cam.y, s: cam.s};
    staticMs = staticMs * .6 + (performance.now() - t0) * .4;
  }
  // Кадр фона: небо по текущей камере и мир из буфера — сдвинутый и масштабированный,
  // если камера ушла с тех пор, как он был нарисован.
  function compose() {
    const g = bctx;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, bg.width, bg.height);
    drawSky(g);
    if (!offCam) return;
    const k = cam.s / offCam.s;
    const dx = W / 2 * (1 - k) + (offCam.x - cam.x) * cam.s, dy = H / 2 * (1 - k) + (offCam.y - cam.y) * cam.s;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.drawImage(off, dx * DPR, dy * DPR, off.width * k, off.height * k);
  }

  // Лист дуба: лопасти по краю, черешок в начале координат, кончик в (1, 0).
  const LEAF = (function () {
    const p = new Path2D(), up = [], lo = [], N = 36;
    for (let i = 0; i <= N; i++) {
      const u = i / N;
      const w = Math.pow(Math.sin(Math.PI * Math.min(1, u * 1.04)), .85) * .31 * (.74 + .26 * Math.cos(u * Math.PI * 10 - .6));
      up.push([u, -w]); lo.push([u, w]);
    }
    p.moveTo(0, 0);
    up.forEach(([x, y]) => p.lineTo(x, y));
    for (let i = lo.length - 1; i >= 0; i--) p.lineTo(lo[i][0], lo[i][1]);
    p.closePath();
    return p;
  })();
  const VEIN = (function () {
    const p = new Path2D();
    p.moveTo(-.08, 0); p.lineTo(.96, 0);
    for (const u of [.25, .45, .65, .82]) { p.moveTo(u, 0); p.lineTo(u + .1, -.17); p.moveTo(u, 0); p.lineTo(u + .1, .17); }
    return p;
  })();
  // Цветок задачи: пять лепестков вокруг центра радиусом 1.
  const FLOWER = (function () {
    const p = new Path2D();
    for (let k = 0; k < 5; k++) {
      const a = k / 5 * TAU, cx = Math.cos(a) * .52, cy = Math.sin(a) * .52;
      p.moveTo(cx + Math.cos(a) * .48, cy + Math.sin(a) * .48);
      p.ellipse(cx, cy, .48, .32, a, 0, TAU);
    }
    return p;
  })();

  function drawLeaves(g, now) {
    const v = viewRect(24), s = cam.s, t = now * .0016, amp = windOn && !reduced ? .15 : 0;
    const buckets = new Map();
    for (const n of M.leaves) {
      if (n._work || !leafShown(n)) continue;
      if (n._lx < v.x0 || n._lx > v.x1 || n._ly < v.y0 || n._ly > v.y1) continue;
      if (leafGrowth(n, now) <= 0) continue;
      const key = (dimmed(n) ? 0 : 1) + '|' + leafColor(n) + '|' + n._var;
      let a = buckets.get(key); if (!a) buckets.set(key, a = []);
      a.push(n);
    }
    const detail = s > .62;
    for (const [key, arr] of buckets) {
      const [hi, col, vv] = key.split('|');
      g.globalAlpha = hi === '1' ? 1 : SC.dim;
      g.fillStyle = shade(col, +vv);
      if (!detail) {
        g.beginPath();
        for (const n of arr) {
          const r = Math.max(n._ls * .42, 2.3 / s) * leafGrowth(n, now);
          const rot = n._lr + (amp ? Math.sin(t + n._ph) * amp : 0);
          g.moveTo(n._lx + r * Math.cos(rot), n._ly + r * Math.sin(rot));
          g.ellipse(n._lx, n._ly, r, r * .55, rot, 0, TAU);
        }
        g.fill();
        continue;
      }
      for (const n of arr) {
        const sz = n._ls * leafGrowth(n, now), rot = n._lr + (amp ? Math.sin(t + n._ph) * amp : 0);
        const c = Math.cos(rot) * sz * s * DPR, sn = Math.sin(rot) * sz * s * DPR;
        g.setTransform(c, sn, -sn, c, DPR * ((n._lx - cam.x) * s + W / 2), DPR * ((n._ly - cam.y) * s + H / 2));
        g.fill(LEAF);
        if (sz * s > 14) { g.strokeStyle = 'rgba(0,0,0,.18)'; g.lineWidth = 1.1 / (sz * s); g.stroke(VEIN); }
      }
      worldTransform(g);
    }
    g.globalAlpha = 1;
  }
  // Задачи — цветы: издали яркие точки с ореолом, вблизи пять лепестков. Цвет — статус.
  function drawBlossoms(g, now) {
    const v = viewRect(30), s = cam.s, t = now * .0016, amp = windOn && !reduced ? .12 : 0;
    const by = new Map();
    for (const n of M.leaves) {
      if (!n._work || !leafShown(n)) continue;
      if (n._lx < v.x0 || n._lx > v.x1 || n._ly < v.y0 || n._ly > v.y1) continue;
      if (leafGrowth(n, now) <= 0) continue;
      const key = (dimmed(n) ? 0 : 1) + '|' + catColor(n._cat);
      let a = by.get(key); if (!a) by.set(key, a = []);
      a.push(n);
    }
    const detail = s > .55, keys = [...by.keys()].sort();   // приглушённые — первыми, под яркими
    for (const key of keys) {
      const arr = by.get(key), [hi, col] = key.split('|');
      g.globalAlpha = hi === '1' ? 1 : SC.dim;
      if (!detail) {
        const rad = n => Math.max(n._ls * .5, 4.4 / s) * leafGrowth(n, now);
        g.fillStyle = SC.flowerHalo; g.beginPath();
        for (const n of arr) { const r = rad(n) + 1.7 / s; g.moveTo(n._lx + r, n._ly); g.arc(n._lx, n._ly, r, 0, TAU); }
        g.fill();
        g.fillStyle = col; g.beginPath();
        for (const n of arr) { const r = rad(n); g.moveTo(n._lx + r, n._ly); g.arc(n._lx, n._ly, r, 0, TAU); }
        g.fill();
        continue;
      }
      for (const n of arr) {
        const sz = n._ls * .62 * leafGrowth(n, now), rot = n._ph + (amp ? Math.sin(t + n._ph) * amp : 0);
        const c = Math.cos(rot) * sz * s * DPR, sn = Math.sin(rot) * sz * s * DPR;
        g.setTransform(c, sn, -sn, c, DPR * ((n._lx - cam.x) * s + W / 2), DPR * ((n._ly - cam.y) * s + H / 2));
        g.strokeStyle = SC.flowerHalo; g.lineWidth = 3 / (sz * s); g.stroke(FLOWER);
        g.fillStyle = col; g.fill(FLOWER);
        g.fillStyle = '#ffe8a3'; g.beginPath(); g.arc(0, 0, .26, 0, TAU); g.fill();
      }
      worldTransform(g);
    }
    g.globalAlpha = 1;
  }
  function drawFruitsBuds(g, now) {
    const v = viewRect(20), s = cam.s, t = now * .0016, amp = windOn && !reduced ? 1.8 : 0;
    if (!hidden.has('fruit')) {
      const body = catColor('fruit'), cap = rgba(mul3(hex3(body), .55));
      for (const n of M.fruits) {
        if (!n._vis || n._lx < v.x0 || n._lx > v.x1 || n._ly < v.y0 || n._ly > v.y1) continue;
        const gt = leafGrowth(n, now); if (gt <= 0) continue;
        const r = Math.max(n._ls, 2.4 / s) * gt, dx = amp ? Math.sin(t * 1.3 + n._ph) * amp * .4 : 0;
        g.globalAlpha = dimmed(n) ? SC.dim : 1;
        g.fillStyle = body; g.beginPath(); g.ellipse(n._lx + dx, n._ly + r * .35, r * .62, r * .82, 0, 0, TAU); g.fill();
        g.fillStyle = cap; g.beginPath(); g.ellipse(n._lx + dx, n._ly - r * .12, r * .7, r * .38, 0, Math.PI, TAU); g.fill();
      }
    }
    if (!hidden.has('bud')) {
      const col = catColor('bud');
      for (const n of M.buds) {
        if (!n._vis || n._lx < v.x0 || n._lx > v.x1 || n._ly < v.y0 || n._ly > v.y1) continue;
        const gt = leafGrowth(n, now); if (gt <= 0) continue;
        const r = Math.max(n._ls, 2.6 / s) * gt;
        g.globalAlpha = dimmed(n) ? SC.dim : 1;
        g.fillStyle = col;
        for (let k = 0; k < 5; k++) {
          const a = k / 5 * TAU + n._ph;
          g.beginPath(); g.arc(n._lx + Math.cos(a) * r * .55, n._ly + Math.sin(a) * r * .55, r * .5, 0, TAU); g.fill();
        }
        g.fillStyle = '#fff4c7'; g.beginPath(); g.arc(n._lx, n._ly, r * .32, 0, TAU); g.fill();
      }
    }
    g.globalAlpha = 1;
  }
  // Наведение на легенду: всё этой категории пульсирует кольцами — видно и издали.
  function drawFocusRings(g, now) {
    if (!catFocus) return;
    const s = cam.s, v = viewRect(30), pulse = .5 + .5 * Math.sin(now * .007);
    const list = catFocus === 'foundation' ? M.tips : catFocus === 'fruit' ? M.fruits : catFocus === 'bud' ? M.buds : M.leaves;
    g.strokeStyle = SC.glow; g.lineWidth = 2 / s; g.globalAlpha = .45 + .45 * pulse;
    g.beginPath();
    for (const n of list) {
      if (!n._vis || leafKey(n) !== catFocus) continue;
      if (n._lx < v.x0 || n._lx > v.x1 || n._ly < v.y0 || n._ly > v.y1) continue;
      const r = (9 + 5 * pulse) / s + (n._ls || 0) * .4;
      g.moveTo(n._lx + r, n._ly); g.arc(n._lx, n._ly, r, 0, TAU);
    }
    g.stroke();
    g.globalAlpha = 1;
  }
  function strokeEdge(g, e) {
    g.beginPath(); g.moveTo(e.x0, e.y0); g.bezierCurveTo(e.x1, e.y1, e.x2, e.y2, e.x3, e.y3); g.stroke();
  }
  // Путь узла до ствола — светящейся линией; для кроны — и корни её проекта.
  function drawGlow(g, n, strong) {
    if (!n) return;
    const s = cam.s;
    g.save();
    g.lineCap = 'round'; g.shadowColor = SC.glow; g.shadowBlur = strong ? 16 : 10; g.strokeStyle = SC.glow;
    g.globalAlpha = strong ? .95 : .8;
    for (let x = n; x && x !== M.root; x = x._p) {
      for (const e of (x._chain || [])) { g.lineWidth = Math.max(2.4 / s, (e.w0 + e.w1) * .16); strokeEdge(g, e); }
    }
    g.lineWidth = Math.max(3 / s, M.dims.TW * .1);
    g.beginPath(); g.moveTo(0, 0); g.lineTo(0, -M.dims.T); g.stroke();
    const pr = n._proj;
    if (pr && pr._found && n !== M.root && !n._root) {
      g.globalAlpha = .5; g.shadowBlur = 8;
      const f = pr._found;
      for (const e of (f._chain || [])) { g.lineWidth = Math.max(2 / s, (e.w0 + e.w1) * .12); strokeEdge(g, e); }
      for (const k of (f.children || [])) for (const e of (k._chain || [])) { g.lineWidth = Math.max(1.3 / s, (e.w0 + e.w1) * .12); strokeEdge(g, e); }
    }
    g.restore();
    const x = n._hx != null ? n._hx : n._x, y = n._hy != null ? n._hy : n._y;
    if (x != null && n !== M.root) {
      g.save();
      g.strokeStyle = SC.glow; g.lineWidth = 2.4 / s; g.shadowColor = SC.glow; g.shadowBlur = 14;
      g.beginPath(); g.arc(x, y, Math.max(10 / s, (n._ls || 8) * .8), 0, TAU); g.stroke();
      g.restore();
    }
  }
  const FONT = 'px system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif';
  function drawLabels(g, now) {
    g.setTransform(DPR, 0, 0, DPR, 0, 0);
    g.textBaseline = 'alphabetic'; g.textAlign = 'left';
    const placed = [], s = cam.s;
    const fits = r => {
      for (const p of placed) if (!(r[2] < p[0] || r[0] > p[2] || r[3] < p[1] || r[1] > p[3])) return false;
      return true;
    };
    const put = (text, X, Y, opt) => {
      const size = opt.size || 12;
      g.font = (opt.weight || 500) + ' ' + size + FONT;
      const w = g.measureText(text).width, x = opt.align === 'center' ? X - w / 2 : X;
      if (x + w < -40 || x > W + 40 || Y < -10 || Y > H + 20) return false;
      const r = [x - 5, Y - size - 2, x + w + 5, Y + 5];
      if (!opt.force && !fits(r)) return false;
      if (opt.pill) {
        g.fillStyle = SC.pill; g.beginPath(); roundRect(g, r[0], r[1], r[2] - r[0], r[3] - r[1], 9); g.fill();
      } else { g.lineJoin = 'round'; g.lineWidth = 4; g.strokeStyle = SC.halo; g.strokeText(text, x, Y); }
      g.fillStyle = opt.color || SC.ink; g.fillText(text, x, Y);
      placed.push(r);
      return true;
    };
    // путь выделенного и наведённого узла — всегда
    for (const n of [hover, sel]) {
      if (!n || n === M.root) continue;
      for (let x = n; x && x !== M.root; x = x._p) {
        if (!x._vis || x._x == null) continue;
        const X = toSX(x._hx != null ? x._hx : x._x), Y = toSY(x._hy != null ? x._hy : x._y);
        put(short(x.label, 46), X + 12, Y - 9, {weight: x === n ? 700 : 600, size: x === n ? 13 : 12, force: x === n});
      }
    }
    if (s < 1.3) {                            // проекты — в своей части кроны
      for (const p of M.projects.slice().sort((a, b) => b._cnt - a._cnt)) {
        if (!p._vis || growth(p, now) < 1) continue;
        put(p.label, toSX(p._tx), toSY(p._ty), {weight: 700, size: s > .5 ? 14.5 : 13, align: 'center', pill: true});
      }
    }
    if (s > .42) {
      for (const f of M.founds) {
        if (!f._vis || growth(f, now) < 1) continue;
        put('корни · ' + short(f._proj ? f._proj.label : '', 22), toSX(f._x), toSY(f._y) + 18, {weight: 600, size: 11, align: 'center', color: SC.ink2});
      }
    }
    if (s > .55) {                            // остальные — по важности, пока есть место
      const v = viewRect(0), cands = [];
      for (const n of M.nodes) {
        if (!n._vis || n._struct || n._hx == null || n.kind === 'коммит') continue;
        if (n._hx < v.x0 || n._hx > v.x1 || n._hy < v.y0 || n._hy > v.y1) continue;
        if (!leafShown(n) && !(n.children || []).length) continue;
        cands.push([(n._work ? 80 : 0) + (n._cnt || 1) + (n.kind === 'запрос' ? 30 : 0), n]);
      }
      cands.sort((a, b) => b[0] - a[0]);
      const cap = s > 2.2 ? 170 : s > 1.2 ? 95 : 48;
      let shown = 0;
      for (const [, n] of cands) {
        if (shown >= cap) break;
        if (put(short(n.label, s > 2 ? 64 : 40), toSX(n._hx) + 10, toSY(n._hy) + 4, {weight: n._work ? 650 : 500, size: 11.5})) shown++;
      }
    }
    for (const id of collapsed) {             // свёрнутые ветви — число спрятанного
      const n = M.byId.get(id);
      if (!n || !n._vis) continue;
      const X = toSX(n._hx != null ? n._hx : n._x), Y = toSY(n._hy != null ? n._hy : n._y);
      g.fillStyle = SC.glow; g.beginPath(); g.arc(X, Y, 12, 0, TAU); g.fill();
      g.fillStyle = '#231a0b'; g.font = '700 10' + FONT; g.textAlign = 'center';
      g.fillText('+' + ((n._cnt || 1) - 1), X, Y + 3.5); g.textAlign = 'left';
    }
  }
  function drawDynamic(now) {
    const g = fctx;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, fg.width, fg.height);
    worldTransform(g);
    drawLeaves(g, now);
    drawFruitsBuds(g, now);
    drawBlossoms(g, now);
    drawFocusRings(g, now);
    if (sel) drawGlow(g, sel, true);
    if (hover && hover !== sel) drawGlow(g, hover, false);
    drawLabels(g, now);
    drawMini();
  }

  /* ============================== цикл кадров ============================== */
  function frame(now) {
    raf = 0;
    if (!M || !isOpenFlag) return;
    let anim = false;
    if (fly) {
      const t = clamp((now - fly.t0) / fly.dur, 0, 1), k = easeIO(t);
      cam.x = lerp(fly.x0, fly.x1, k); cam.y = lerp(fly.y0, fly.y1, k);
      cam.s = Math.exp(lerp(Math.log(fly.s0), Math.log(fly.s1), k));
      camMoved = true; dirtyDyn = true; lastInput = now;
      if (t >= 1) { fly = null; clearTimeout(idleT); idleT = setTimeout(schedule, 190); } else anim = true;
    }
    if (M.growUntil > now) { dirtyWorld = true; dirtyDyn = true; anim = true; }
    // Пока камера движется, тяжёлый мир не перерисовывается, а сдвигается готовым кадром.
    const moving = now - lastInput < 180;
    if (dirtyWorld || (camMoved && (!moving || staticMs < 9))) {
      renderWorld(now); dirtyWorld = false; camMoved = false; compose(); dirtyDyn = true;
    } else if (camMoved) compose();
    const live = (windOn && !reduced) || !!catFocus;
    if (dirtyDyn || (live && now - lastFg > 32)) { drawDynamic(now); lastFg = now; dirtyDyn = false; }
    if (anim || live || playing) raf = requestAnimationFrame(frame);
  }

  /* ============================== попадание ============================== */
  function pick(X, Y) {
    const wx = toWX(X), wy = toWY(Y), rad = 14 / cam.s;
    const c0 = Math.floor((wx - rad) / M.CELL), c1 = Math.floor((wx + rad) / M.CELL);
    const r0 = Math.floor((wy - rad) / M.CELL), r1 = Math.floor((wy + rad) / M.CELL);
    let best = null, bd = Infinity;
    for (let cx = c0; cx <= c1; cx++) for (let cy = r0; cy <= r1; cy++) {
      const a = M.grid.get(cx + ':' + cy); if (!a) continue;
      for (const n of a) {
        if (!n._vis) continue;
        if (!n._struct && n._cat !== 'foundation' && !leafShown(n) && !(n.children || []).length) continue;
        const d = Math.hypot(n._hx - wx, n._hy - wy) - (n._ls || 0) * .4 - (n._work ? 5 / cam.s : 0);
        if (d < rad && d < bd) { bd = d; best = n; }
      }
    }
    if (!best && Math.abs(wx) < M.dims.TW * .7 && wy < 0 && wy > -M.dims.T) best = M.root;
    return best;
  }

  /* ============================== интерфейс ============================== */
  const CSS = `
.forest{position:fixed;inset:0;z-index:1000;overflow:hidden;font:13px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
  --fp:rgba(252,252,247,.82);--fl:rgba(40,60,40,.16);--fi:#152018;--fi2:#48574b;--fi3:#6f7c73;--fa:#2e7d3b;--fsh:0 10px 34px rgba(24,40,24,.2);
  color:var(--fi);background:#cfe4ef;user-select:none;-webkit-user-select:none}
.forest.dark{--fp:rgba(12,18,26,.76);--fl:rgba(170,210,180,.14);--fi:#eef4ec;--fi2:#b8c5ba;--fi3:#84918a;--fa:#7fdc8e;--fsh:0 10px 34px rgba(0,0,0,.5);background:#07111d}
.forest[hidden]{display:none}
.forest canvas.fl{position:absolute;inset:0;width:100%;height:100%;display:block}
.forest canvas.ff{cursor:grab;touch-action:none}
.forest canvas.ff.drag{cursor:grabbing}
.forest canvas.ff.pt{cursor:pointer}
.forest .gl{background:var(--fp);border:1px solid var(--fl);border-radius:14px;box-shadow:var(--fsh);
  backdrop-filter:blur(14px) saturate(1.25);-webkit-backdrop-filter:blur(14px) saturate(1.25)}
.forest button{font:inherit;color:inherit}
.f-top{position:absolute;top:12px;left:12px;right:12px;display:flex;gap:10px;align-items:flex-start;pointer-events:none;z-index:3}
.f-top>*{pointer-events:auto}
.f-title{display:flex;align-items:center;gap:10px;padding:7px 12px 7px 7px;flex:none}
.f-title b{font-size:15px;letter-spacing:.2px}
.f-title small{color:var(--fi3);font-size:12px;font-variant-numeric:tabular-nums}
.f-btn{border:1px solid var(--fl);background:var(--fp);border-radius:10px;padding:6px 11px;cursor:pointer;
  display:inline-flex;align-items:center;gap:6px;white-space:nowrap;transition:background .15s,border-color .15s}
.f-btn:hover{border-color:var(--fa)}
.f-btn:focus-visible,.f-li:focus-visible,.f-pi:focus-visible{outline:2px solid var(--fa);outline-offset:1px}
.f-btn.on{background:var(--fa);color:#fff;border-color:var(--fa)}
.forest.dark .f-btn.on{color:#0b1a10}
.f-ic{width:34px;height:34px;padding:0;justify-content:center;font-size:16px}
.f-search{position:relative;flex:1;min-width:150px;max-width:520px;margin:0 auto}
.f-search input{width:100%;box-sizing:border-box;border:1px solid var(--fl);background:var(--fp);color:var(--fi);border-radius:12px;
  padding:9px 14px 9px 34px;font:inherit;font-size:13.5px;outline:none;box-shadow:var(--fsh);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.f-search input:focus{border-color:var(--fa)}
.f-search .mg{position:absolute;left:12px;top:9px;color:var(--fi3);pointer-events:none}
.f-res{position:absolute;top:44px;left:0;right:0;max-height:58vh;overflow:auto;padding:6px;display:none}
.f-res.open{display:block}
.f-ri{display:flex;gap:9px;align-items:center;padding:7px 9px;border-radius:9px;cursor:pointer}
.f-ri:hover,.f-ri.act{background:rgba(127,200,140,.18)}
.f-ri i{width:10px;height:10px;border-radius:50%;flex:none}
.f-ri span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.f-ri em{font-style:normal;color:var(--fi3);font-size:11.5px;white-space:nowrap}
.f-tools{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;flex:none;padding:6px}
.f-time{position:absolute;left:12px;bottom:12px;z-index:3;display:flex;gap:10px;align-items:center;padding:7px 10px;
  width:min(440px,calc(100vw - 250px));box-sizing:border-box}
.f-time input{flex:1;min-width:70px;accent-color:var(--fa)}
.f-time .lab{min-width:86px;font-variant-numeric:tabular-nums;color:var(--fi2);font-size:12.5px}
.f-legend{position:absolute;top:66px;right:12px;width:238px;max-height:calc(100vh - 262px);overflow:auto;padding:8px 10px 10px;z-index:3;box-sizing:border-box}
.f-legend.min{width:auto}
.f-legend.min .f-lgb{display:none}
.forest.has-sel .f-legend{display:none}
.f-lgh{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:2px 4px 4px;cursor:pointer}
.f-lgh b,.f-lg-h{font-size:10.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--fi3);font-weight:700}
.f-lg-h{margin:8px 4px 3px;display:flex;justify-content:space-between;gap:8px}
.f-lg-h a{text-transform:none;letter-spacing:0;color:var(--fa);cursor:pointer;font-weight:500}
.f-lg{display:flex;flex-direction:column;gap:1px}
.f-li{display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:8px;cursor:pointer;font-size:12.5px}
.f-li:hover{background:rgba(127,200,140,.16)}
.f-li.off{opacity:.38;text-decoration:line-through}
.f-li i{width:11px;height:11px;border-radius:50%;flex:none;box-shadow:0 0 0 2px var(--fp)}
.f-li.lf i{border-radius:50% 0 50% 0;transform:rotate(45deg);width:10px;height:10px}
.f-li em{font-style:normal;color:var(--fi3);font-variant-numeric:tabular-nums;margin-left:auto}
.f-note{font-size:11px;color:var(--fi3);margin:8px 4px 0;line-height:1.35}
.f-mini{position:absolute;right:12px;bottom:12px;width:210px;height:150px;z-index:3;padding:0;overflow:hidden;cursor:crosshair}
.f-mini canvas{width:100%;height:100%;display:block}
.f-proj{position:absolute;left:12px;top:66px;width:236px;max-height:calc(100vh - 330px);overflow:auto;padding:8px;z-index:3;box-sizing:border-box}
.f-proj.min{width:auto}
.f-proj.min .f-pl{display:none}
.f-ph{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:2px 4px 6px;cursor:pointer}
.f-proj.min .f-ph{padding:2px 4px}
.f-ph b{font-size:10.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--fi3)}
.f-pi{display:flex;align-items:center;gap:8px;padding:6px 7px;border-radius:9px;cursor:pointer}
.f-pi:hover{background:rgba(127,200,140,.16)}
.f-pi.act{background:rgba(127,200,140,.28)}
.f-pi .bar{flex:none;width:36px;height:6px;border-radius:4px;background:var(--fl);overflow:hidden;display:flex}
.f-pi .bar i{display:block;height:100%}
.f-pi span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.f-pi em{font-style:normal;color:var(--fi3);font-size:11px;font-variant-numeric:tabular-nums}
.f-tip{position:absolute;pointer-events:none;padding:9px 11px;max-width:340px;font-size:12.5px;opacity:0;transition:opacity .1s;z-index:6}
.f-tip .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.6px;color:var(--fi3);margin-bottom:2px;display:flex;gap:6px;align-items:center}
.f-tip .k i{width:9px;height:9px;border-radius:50%}
.f-tip b{font-weight:650}
.f-tip .m{color:var(--fi2);margin-top:4px}
.f-tip .p{color:var(--fi3);margin-top:4px;font-size:11.5px}
.f-panel{position:absolute;top:66px;right:12px;bottom:176px;width:360px;overflow:auto;padding:14px 16px;z-index:4;box-sizing:border-box;user-select:text;-webkit-user-select:text}
.f-panel[hidden]{display:none}
.f-panel .k{display:flex;align-items:center;gap:7px;font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--fi3)}
.f-panel .k i{width:10px;height:10px;border-radius:50%}
.f-panel h3{font-size:16px;line-height:1.3;margin:6px 0 8px}
.f-crumbs{display:flex;flex-wrap:wrap;gap:3px;font-size:11.5px;color:var(--fi3);margin-bottom:10px}
.f-crumbs a{color:var(--fi2);cursor:pointer;text-decoration:none;border-bottom:1px dotted var(--fi3)}
.f-crumbs a:hover{color:var(--fa)}
.f-chip{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600;color:#fff}
.f-facts{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:12.5px;margin:10px 0}
.f-facts dt{color:var(--fi3)}
.f-facts dd{margin:0;word-break:break-word}
.f-facts a{color:var(--fa);cursor:pointer}
.f-detail{font-size:12.5px;line-height:1.5;color:var(--fi2);background:rgba(127,160,130,.1);border-radius:10px;padding:9px 11px;margin:8px 0;white-space:pre-wrap}
.f-kids{display:flex;flex-direction:column;gap:2px;margin-top:6px}
.f-kid{display:flex;gap:8px;align-items:center;padding:4px 6px;border-radius:8px;cursor:pointer;font-size:12.5px}
.f-kid:hover{background:rgba(127,200,140,.16)}
.f-kid i{width:9px;height:9px;border-radius:50%;flex:none}
.f-kid span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.f-mix{display:flex;height:8px;border-radius:5px;overflow:hidden;margin:8px 0 4px;background:var(--fl)}
.f-mix i{display:block;height:100%}
.f-acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.f-hint{position:absolute;left:50%;top:66px;transform:translateX(-50%);max-width:calc(100vw - 540px);text-align:center;padding:6px 12px;font-size:12px;color:var(--fi2);z-index:2;transition:opacity .6s;pointer-events:none}
.f-intro{position:absolute;inset:0;z-index:20;transition:opacity .55s;cursor:pointer}
.f-intro canvas{position:absolute;inset:0;width:100%;height:100%}
.f-intro .msg{position:absolute;left:0;right:0;bottom:11%;text-align:center;font-size:13px;letter-spacing:.3px;color:#cfe0f5;font-variant-numeric:tabular-nums}
.f-intro .bar{position:absolute;left:50%;bottom:calc(11% - 16px);width:220px;height:2px;margin-left:-110px;background:rgba(160,200,255,.18);border-radius:2px;overflow:hidden}
.f-intro .bar i{display:block;height:100%;width:0;background:#8fd3ff}
.f-intro .skip{position:absolute;right:18px;bottom:16px;font-size:11.5px;color:rgba(200,220,255,.55)}
.forest:not(.dark) .f-intro .msg{color:#2b3a55}
.forest:not(.dark) .f-intro .bar{background:rgba(40,60,110,.15)}
.forest:not(.dark) .f-intro .bar i{background:#1f4e79}
.forest:not(.dark) .f-intro .skip{color:rgba(40,60,110,.5)}
@media(max-width:1100px){.f-hint{display:none}}
@media(max-width:900px){.f-proj,.f-mini{display:none}.f-time{width:auto;right:12px}
  .f-panel{left:12px;right:12px;width:auto;top:auto;bottom:12px;max-height:50vh}
  .f-title small,.f-tools [data-a="wind"],.f-tools [data-a="full"]{display:none}}
@media(max-width:600px){.f-top{flex-wrap:wrap}.f-search{order:3;flex-basis:100%;max-width:none}.f-tools [data-a="color"]{display:none}}
`;

  function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }

  function buildDOM() {
    const st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
    layer = el('div', 'forest'); layer.hidden = true;
    layer.setAttribute('role', 'application'); layer.setAttribute('aria-label', 'Древо портфеля');
    bg = el('canvas', 'fl'); fg = el('canvas', 'fl ff');
    off = document.createElement('canvas');
    layer.append(bg, fg);
    const top = el('div', 'f-top');
    top.innerHTML = `
      <div class="f-title gl"><button class="f-btn f-back" title="Вернуться к дашборду (Esc)">← Дашборд</button>
        <div><b>Древо портфеля</b><br><small class="f-stats"></small></div></div>
      <div class="f-search"><span class="mg">⌕</span><input type="search" placeholder="Найти задачу, запись, проект…  ( / )" aria-label="Поиск по дереву">
        <div class="f-res gl" role="listbox"></div></div>
      <div class="f-tools gl">
        <button class="f-btn f-ic" data-a="out" title="Отдалить (−)" aria-label="Отдалить">−</button>
        <button class="f-btn f-ic" data-a="in" title="Приблизить (+)" aria-label="Приблизить">+</button>
        <button class="f-btn" data-a="fit" title="Всё дерево (0)">⤢ Всё дерево</button>
        <button class="f-btn" data-a="color" title="Цвет листвы: спокойный, чтобы читались статусы задач, или по виду записи">Листва: спокойная</button>
        <button class="f-btn on" data-a="wind" title="Ветер — листья качаются">≋ Ветер</button>
        <button class="f-btn f-ic" data-a="theme" title="День или ночь" aria-label="Сменить тему">◐</button>
        <button class="f-btn f-ic" data-a="full" title="Во весь экран" aria-label="Во весь экран">⛶</button>
      </div>`;
    const proj = el('div', 'f-proj gl'), legend = el('div', 'f-legend gl'), time = el('div', 'f-time gl');
    const miniBox = el('div', 'f-mini gl'); mini = el('canvas'); miniBox.appendChild(mini);
    miniBox.title = 'Миникарта: клик или перетаскивание — перейти';
    const tip = el('div', 'f-tip gl'), panel = el('aside', 'f-panel gl'); panel.hidden = true;
    const hint = el('div', 'f-hint gl', 'Колесо — масштаб · перетаскивание — обзор · клик — связи и подробности · двойной клик — приблизить ветвь');
    layer.append(top, proj, legend, time, miniBox, tip, panel, hint);
    document.body.appendChild(layer);
    bctx = bg.getContext('2d'); fctx = fg.getContext('2d'); octx = off.getContext('2d'); mctx = mini.getContext('2d');
    els = {top, proj, legend, time, miniBox, tip, panel, hint,
      search: top.querySelector('input'), res: top.querySelector('.f-res'), stats: top.querySelector('.f-stats')};
    bindUI();
  }

  function resize() {
    if (!layer) return;
    DPR = Math.min(2, window.devicePixelRatio || 1);
    W = layer.clientWidth; H = layer.clientHeight;
    for (const c of [bg, fg, off]) { c.width = Math.round(W * DPR); c.height = Math.round(H * DPR); }
    const r = els.miniBox.getBoundingClientRect();
    mini.width = Math.round((r.width || 210) * DPR); mini.height = Math.round((r.height || 150) * DPR);
    offCam = null; miniCache = null; camMoved = true; redrawAll();
  }

  /* ---------- подсказка ---------- */
  function pathText(n) {
    const out = [];
    for (let x = n._p; x && x !== M.root; x = x._p) out.unshift(short(x.label, 26));
    return out.join(' › ');
  }
  function nodeColor(n) { return n._struct ? SC.wood : n._work ? catColor(n._cat) : leafColor(n); }
  function showTip(n, X, Y) {
    const t = els.tip;
    if (!n) { t.style.opacity = 0; return; }
    const meta = [];
    if (n.kind === 'задача') meta.push('<b>' + esc(CATS[n._cat].label) + '</b>' + (n.status ? ' · ' + esc(n.status) : ''));
    if (n.task) meta.push(esc(n.task));
    if (n.date || n.first_date) meta.push(esc(String(n.date || n.first_date).slice(0, 10)) + (n.date_estimated ? ' (оценка)' : ''));
    if (n.subtree_weight) meta.push(tokf(n.subtree_weight) + ' ток.');
    if ((n._cnt || 1) > 1) meta.push(nf(n._cnt - 1) + ' ' + plural(n._cnt - 1, 'узел', 'узла', 'узлов') + ' в ветви');
    const path = n === M.root ? '' : pathText(n);
    t.innerHTML = `<div class="k"><i style="background:${nodeColor(n)}"></i>${esc(KIND_RU[n.kind] || n.kind)}</div><b>${esc(short(n.label, 130))}</b>` +
      (meta.length ? `<div class="m">${meta.join(' · ')}</div>` : '') + (path ? `<div class="p">${esc(path)}</div>` : '');
    t.style.opacity = 1;
    const w = t.offsetWidth, h = t.offsetHeight;
    t.style.left = clamp(X + 16, 6, W - w - 6) + 'px';
    t.style.top = clamp(Y + 14, 6, H - h - 6) + 'px';
  }

  /* ---------- панель узла ---------- */
  function mixOf(n) {
    const c = {};
    (function w(x) { for (const k of (x.children || [])) { if (!k._struct && k._vis) { const kk = leafKey(k); c[kk] = (c[kk] || 0) + 1; } w(k); } })(n);
    return c;
  }
  function renderPanel() {
    const p = els.panel;
    if (!sel) { p.hidden = true; return; }
    const n = sel, crumbs = [];
    for (let x = n._p; x; x = x._p) crumbs.unshift(x);
    const facts = [];
    const add = (k, v) => { if (v != null && v !== '') facts.push(`<dt>${k}</dt><dd>${v}</dd>`); };
    if (n.kind === 'задача') {
      add('Статус', `<span class="f-chip" style="background:${catColor(n._cat)}">${esc(CATS[n._cat].label)}</span>` +
        (n.status ? ` <span style="color:var(--fi3)">${esc(n.status)}</span>` : ''));
      add('Задача', esc(n.task || ''));
      add('Размер', esc(n.size || ''));
      add('Приоритет', esc(n.priority || ''));
      if (n.usd) add('Оценка', '$' + nf(n.usd));
    }
    if (n.kind === 'проект') {
      add('Статус', esc(n.status || ''));
      add('Цель', esc(n.detail || ''));
      if (n.spent_usd) add('Потрачено', '$' + nf(n.spent_usd) + (n.spent_tokens ? ' · ' + tokf(n.spent_tokens) + ' токенов' : ''));
      if (n.commits) add('Коммитов', nf(n.commits));
    }
    if (n._proj && n.kind !== 'проект') add('Проект', `<a data-id="${esc(n._proj.id)}">${esc(n._proj.label)}</a>`);
    if (n.date || n.first_date) add('Дата', esc(String(n.date || n.first_date).slice(0, 10)) + (n.date_estimated ? ' · оценка по месту в списке' : ''));
    if (n.subtree_weight) add('Вес ветви', tokf(n.subtree_weight) + ' токенов');
    if ((n._cnt || 1) > 1) add('В ветви', nf(n._cnt - 1) + ' ' + plural(n._cnt - 1, 'узел', 'узла', 'узлов'));
    if (n.memory) add('Запись', `<code>${esc(n.memory)}</code>`);
    if (n.sha) add('Коммит', `<code>${esc(n.sha)}</code>`);
    if (n.source) add('Источник', esc(String(n.source).replace(/^obsidian:/, 'Obsidian · ')));
    const mix = Object.entries(mixOf(n)).sort((a, b) => b[1] - a[1]), total = mix.reduce((s, e) => s + e[1], 0);
    const mixBar = total ? `<div class="f-mix">${mix.map(([k, v]) =>
      `<i style="width:${(100 * v / total).toFixed(2)}%;background:${keyColor(k)}" title="${esc(keyLabel(k))}: ${v}"></i>`).join('')}</div>
      <div style="display:flex;flex-wrap:wrap;gap:4px 12px;font-size:11.5px;color:var(--fi3)">${mix.slice(0, 9).map(([k, v]) =>
      `<span><i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${keyColor(k)};margin-right:4px"></i>${esc(keyLabel(k))} ${v}</span>`).join('')}</div>` : '';
    const kids = (n.children || []).filter(k => k._vis).slice(0, 14);
    const more = (n.children || []).length - kids.length;
    p.innerHTML = `
      <div class="k"><i style="background:${nodeColor(n)}"></i>${esc(KIND_RU[n.kind] || n.kind)}</div>
      <h3>${esc(n.label || '')}</h3>
      ${crumbs.length ? `<div class="f-crumbs">${crumbs.map(c => `<a data-id="${esc(c.id)}">${esc(short(c.label, 28))}</a>`).join(' › ')}</div>` : ''}
      <dl class="f-facts">${facts.join('')}</dl>
      ${n.detail && n.kind !== 'проект' ? `<div class="f-detail">${esc(String(n.detail).slice(0, 700))}</div>` : ''}
      ${mixBar}
      ${kids.length ? `<div class="f-kids">${kids.map(k => `<div class="f-kid" data-id="${esc(k.id)}"><i style="background:${nodeColor(k)}"></i><span>${esc(k.label || '')}</span></div>`).join('')}${more > 0 ? `<div style="color:var(--fi3);font-size:12px;padding:4px 6px">…и ещё ${more}</div>` : ''}</div>` : ''}
      <div class="f-acts">
        <button class="f-btn" data-p="focus">⌖ Показать ветвь</button>
        ${(n.children || []).length && n !== M.root ? `<button class="f-btn" data-p="fold">${collapsed.has(n.id) ? '▾ Развернуть' : '▸ Свернуть'}</button>` : ''}
        ${n._proj && OPTS.onProject ? `<button class="f-btn" data-p="card">Карточка проекта</button>` : ''}
        <button class="f-btn" data-p="close" aria-label="Закрыть">✕</button>
      </div>`;
    p.hidden = false;
  }
  function select(n, focus) {
    sel = n || null;
    hiSet = null;
    if (sel && sel !== M.root) {
      hiSet = new Set();
      for (let x = sel; x; x = x._p) hiSet.add(x);
      (function d(x) { for (const c of (x.children || [])) { hiSet.add(c); d(c); } })(sel);
      const pr = sel._proj;
      if (pr && pr._found) { hiSet.add(pr._found); (pr._found.children || []).forEach(c => hiSet.add(c)); }
    }
    layer.classList.toggle('has-sel', !!sel);
    redrawAll(); renderPanel(); renderProjects();
    if (sel && focus) focusNode(sel);
  }

  /* ---------- поиск ---------- */
  let results = [], resIdx = 0;
  function runSearch(q) {
    q = q.trim().toLowerCase();
    const box = els.res;
    if (!q) { box.classList.remove('open'); results = []; return; }
    const found = [];
    for (const n of M.nodes) {
      if (n === M.root) continue;
      const hay = ((n.label || '') + ' ' + (n.task || '') + ' ' + (n.memory || '') + ' ' + (n.sha || '')).toLowerCase();
      const i = hay.indexOf(q);
      if (i < 0) continue;
      found.push([(n.kind === 'проект' ? 0 : n._work ? 1 : 2) * 1000 + (i === 0 ? 0 : 100) + Math.min(99, (n.label || '').length / 4), n]);
    }
    found.sort((a, b) => a[0] - b[0]);
    results = found.slice(0, 40).map(r => r[1]);
    resIdx = 0;
    box.innerHTML = results.length ? results.map((n, i) =>
      `<div class="f-ri${i === 0 ? ' act' : ''}" data-i="${i}" role="option"><i style="background:${nodeColor(n)}"></i>` +
      `<span>${esc(n.label || '')}</span><em>${esc(n.kind === 'проект' ? 'проект' : (n._proj ? n._proj.label : ''))}</em></div>`).join('')
      : '<div style="padding:8px 10px;color:var(--fi3)">Ничего не нашлось</div>';
    box.classList.add('open');
  }
  function chooseResult(i) {
    const n = results[i]; if (!n) return;
    els.res.classList.remove('open');
    let opened = false;
    for (let x = n._p; x; x = x._p) if (collapsed.delete(x.id)) opened = true;
    if (cutoff && n._mk && n._mk > cutoff) { setMonth(months.length, false); opened = true; }
    if (opened) recompute(performance.now(), false);
    hidden.delete(leafKey(n));
    renderLegend();
    select(n, true);
  }

  /* ---------- легенда и рост (ДРВ-04) ---------- */
  function counts() {
    const c = {};
    for (const n of M.nodes) {
      if (n._struct || n === M.root) continue;
      const k = leafKey(n);
      c[k] = (c[k] || 0) + 1;
    }
    return c;
  }
  function renderTime() {
    months = ((DATA.stats || {}).months || []).filter(Boolean).slice().sort();
    tIdx = months.length;
    els.time.innerHTML = `<button class="f-btn" data-t="play" title="Проиграть рост дерева по месяцам">▶ Рост</button>
      <input type="range" min="0" max="${months.length}" value="${months.length}" aria-label="Рост дерева по месяцам">
      <span class="lab">всё время</span>`;
    els.range = els.time.querySelector('input'); els.tlab = els.time.querySelector('.lab');
    els.play = els.time.querySelector('[data-t="play"]');
    els.range.addEventListener('input', () => { stopPlay(); setMonth(+els.range.value, true); });
  }
  function renderLegend() {
    const c = counts();
    const item = (k, leaf) => `<span class="f-li${hidden.has(k) ? ' off' : ''}${leaf ? ' lf' : ''}" data-k="${k}" tabindex="0" role="button" aria-pressed="${!hidden.has(k)}"
      title="Наведение — показать все · клик — скрыть или вернуть · двойной клик — только это"><i style="background:${keyColor(k)}"></i>${esc(keyLabel(k))} <em>${nf(c[k] || 0)}</em></span>`;
    const work = WORK.filter(k => c[k]);
    const know = colorMode === 'status' ? ['foliage'] : KNOW.filter(k => c[k]);
    const min = els.legend.classList.contains('min');
    els.legend.innerHTML = `<div class="f-lgh" data-t="leg" title="Свернуть или развернуть легенду"><b>Легенда</b><span style="color:var(--fi3)">${min ? '▸' : '▾'}</span></div>
      <div class="f-lgb">
      <div class="f-lg-h"><span>Задачи — цветы по статусу</span>${hidden.size ? '<a data-t="all">показать всё</a>' : ''}</div>
      <div class="f-lg">${work.map(k => item(k)).join('') || '<span class="f-note">задач нет</span>'}</div>
      <div class="f-lg-h"><span>Знание и корни</span></div>
      <div class="f-lg">${know.map(k => item(k, true)).join('')}${c.foundation ? item('foundation') : ''}</div>
      <div class="f-note">Толщина ветви — вес в токенах и число узлов. Корни — сделанное, на что проект опирается.</div></div>`;
  }
  const MRU = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
  const monthRu = m => { const [y, mm] = m.split('-'); return (MRU[+mm - 1] || mm) + ' ' + y; };
  function setMonth(i, animate) {
    tIdx = clamp(i, 0, months.length);
    cutoff = tIdx >= months.length ? '' : months[tIdx];
    els.tlab.textContent = cutoff ? 'по ' + monthRu(cutoff) : 'всё время';
    els.range.value = tIdx;
    recompute(performance.now(), animate);
  }
  function startPlay() {
    els.play.textContent = '❚❚ Стоп';
    let i = 0; setMonth(0, true);
    playing = setInterval(() => { if (++i > months.length) { stopPlay(); return; } setMonth(i, true); }, 1300);
    schedule();
  }
  function stopPlay() {
    if (playing) { clearInterval(playing); playing = null; }
    if (els.play) els.play.textContent = '▶ Рост';
  }

  /* ---------- проекты ---------- */
  function renderProjects() {
    const ps = M.projects.slice().sort((a, b) => (b._cnt || 0) - (a._cnt || 0));
    const bar = p => {
      const m = mixOf(p), tot = WORK.reduce((s, k) => s + (m[k] || 0), 0);
      if (!tot) return '<span class="bar"></span>';
      return `<span class="bar">${WORK.filter(k => m[k]).map(k => `<i style="width:${(100 * m[k] / tot).toFixed(1)}%;background:${catColor(k)}"></i>`).join('')}</span>`;
    };
    const min = els.proj.classList.contains('min');
    els.proj.innerHTML = `<div class="f-ph" data-t="1" title="Свернуть или развернуть список"><b>Проекты · ${ps.length}</b><span style="color:var(--fi3)">${min ? '▸' : '▾'}</span></div>
      <div class="f-pl">${ps.map(p => `<div class="f-pi${sel === p ? ' act' : ''}" data-id="${esc(p.id)}" tabindex="0" title="${esc(p.label)} — полоска: статусы задач">${bar(p)}<span>${esc(p.label)}</span><em>${nf(p._cnt - 1)}</em></div>`).join('')}</div>`;
  }

  /* ---------- миникарта ---------- */
  function miniMap() {
    const b = M.bounds, w = mini.width, h = mini.height, k = Math.min(w / b.w, h / b.h) * .92;
    return {k, ox: w / 2 - b.cx * k, oy: h / 2 - b.cy * k};
  }
  function drawMini() {
    if (!mctx || !M || !mini.width || !els.miniBox.offsetWidth) return;
    const {k, ox, oy} = miniMap(), g = mctx, w = mini.width, h = mini.height;
    if (!miniCache) {
      miniCache = document.createElement('canvas'); miniCache.width = w; miniCache.height = h;
      const c = miniCache.getContext('2d');
      c.fillStyle = dark ? '#0b1726' : '#d9e9f0'; c.fillRect(0, 0, w, oy);
      c.fillStyle = dark ? '#1b120b' : '#6f5237'; c.fillRect(0, oy, w, h - oy);
      c.lineCap = 'round';
      for (const e of M.edges) {
        const lw = (e.w0 + e.w1) / 2 * k;
        if (lw < .3 || !e.m.some(m => m._vis)) continue;
        c.strokeStyle = e.root ? SC.root : SC.wood; c.lineWidth = Math.max(.6, lw);
        c.beginPath(); c.moveTo(ox + e.x0 * k, oy + e.y0 * k);
        c.bezierCurveTo(ox + e.x1 * k, oy + e.y1 * k, ox + e.x2 * k, oy + e.y2 * k, ox + e.x3 * k, oy + e.y3 * k); c.stroke();
      }
      const {T, TW} = M.dims;
      c.fillStyle = SC.wood; c.fillRect(ox - TW * k * .55, oy - T * k, TW * k * 1.1, T * k);
      for (const pass of [0, 1]) for (const n of M.leaves) {
        if (!leafShown(n) || (pass === 0) === n._work) continue;
        c.fillStyle = n._work ? catColor(n._cat) : leafColor(n);
        const sz = (n._work ? 2.4 : 1.3) * DPR;
        c.fillRect(ox + n._lx * k - sz / 2, oy + n._ly * k - sz / 2, sz, sz);
      }
    }
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, w, h);
    g.drawImage(miniCache, 0, 0);
    const x0 = Math.max(1, ox + toWX(0) * k), y0 = Math.max(1, oy + toWY(0) * k);
    const x1 = Math.min(w - 1, ox + toWX(W) * k), y1 = Math.min(h - 1, oy + toWY(H) * k);
    const b = M.bounds;
    if (toWX(0) > b.minx || toWX(W) < b.maxx || toWY(0) > b.miny || toWY(H) < b.maxy) {
      g.fillStyle = 'rgba(255,200,80,.1)'; g.fillRect(x0, y0, x1 - x0, y1 - y0);
      g.strokeStyle = SC.glow; g.lineWidth = 1.5 * DPR; g.strokeRect(x0, y0, x1 - x0, y1 - y0);
    }
  }

  /* ---------- события ---------- */
  function bindUI() {
    els.top.addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      if (b.classList.contains('f-back')) { if (OPTS.onClose) OPTS.onClose(); return; }
      const a = b.dataset.a;
      if (a === 'in') zoomAt(W / 2, H / 2, 1.45);
      else if (a === 'out') zoomAt(W / 2, H / 2, 1 / 1.45);
      else if (a === 'fit') fitAll();
      else if (a === 'wind') { windOn = !windOn; b.classList.toggle('on', windOn); redrawDyn(); }
      else if (a === 'color') {
        colorMode = colorMode === 'status' ? 'kind' : 'status';
        b.textContent = colorMode === 'status' ? 'Листва: спокойная' : 'Листва: по видам';
        b.classList.toggle('on', colorMode === 'kind');
        hidden.delete('foliage'); KNOW.forEach(k => hidden.delete(k));
        miniCache = null; renderLegend(); renderPanel(); redrawAll();
      } else if (a === 'theme') {
        const next = dark ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        try { localStorage.setItem('tt-theme', next); } catch (err) { /* без хранилища — только на сеанс */ }
        applyTheme(); renderLegend(); renderProjects(); renderPanel();
      } else if (a === 'full') {
        if (document.fullscreenElement) document.exitFullscreen(); else if (layer.requestFullscreen) layer.requestFullscreen();
      }
    });
    els.search.addEventListener('input', () => runSearch(els.search.value));
    els.search.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        resIdx = clamp(resIdx + (e.key === 'ArrowDown' ? 1 : -1), 0, Math.max(0, results.length - 1));
        els.res.querySelectorAll('.f-ri').forEach((r, i) => r.classList.toggle('act', i === resIdx));
        const act = els.res.querySelector('.f-ri.act'); if (act) act.scrollIntoView({block: 'nearest'});
      } else if (e.key === 'Enter') { e.preventDefault(); chooseResult(resIdx); }
      else if (e.key === 'Escape') { els.search.value = ''; runSearch(''); els.search.blur(); }
    });
    els.res.addEventListener('click', e => { const r = e.target.closest('.f-ri'); if (r) chooseResult(+r.dataset.i); });
    // рост и легенда
    els.time.addEventListener('click', e => { if (e.target.closest('[data-t="play"]')) playing ? stopPlay() : startPlay(); });
    let lgTimer = null;
    const toggleCat = k => { hidden.has(k) ? hidden.delete(k) : hidden.add(k); miniCache = null; renderLegend(); redrawAll(); };
    els.legend.addEventListener('click', e => {
      const t = e.target.closest('[data-t]');
      if (t && t.dataset.t === 'leg') { els.legend.classList.toggle('min'); renderLegend(); return; }
      if (t && t.dataset.t === 'all') { hidden.clear(); miniCache = null; renderLegend(); redrawAll(); return; }
      const it = e.target.closest('.f-li'); if (!it) return;
      clearTimeout(lgTimer);
      lgTimer = setTimeout(() => toggleCat(it.dataset.k), 230);
    });
    els.legend.addEventListener('dblclick', e => {
      const it = e.target.closest('.f-li'); if (!it) return;
      clearTimeout(lgTimer);
      const k = it.dataset.k, all = Array.from(els.legend.querySelectorAll('.f-li')).map(x => x.dataset.k);
      const solo = !hidden.has(k) && all.every(x => x === k || hidden.has(x));
      all.forEach(x => (solo || x === k ? hidden.delete(x) : hidden.add(x)));
      miniCache = null; renderLegend(); redrawAll();
    });
    els.legend.addEventListener('keydown', e => {
      const it = e.target.closest('.f-li');
      if (it && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); toggleCat(it.dataset.k); }
    });
    els.legend.addEventListener('mouseover', e => {
      const it = e.target.closest('.f-li'), k = it ? it.dataset.k : null;
      if (k !== catFocus) { catFocus = k && !hidden.has(k) ? k : null; redrawDyn(); }
    });
    els.legend.addEventListener('mouseleave', () => { if (catFocus) { catFocus = null; redrawDyn(); } });
    // проекты
    els.proj.addEventListener('click', e => {
      if (e.target.closest('[data-t]')) { els.proj.classList.toggle('min'); renderProjects(); return; }
      const it = e.target.closest('.f-pi'); if (!it) return;
      const n = M.byId.get(it.dataset.id); if (n) select(n, true);
    });
    els.proj.addEventListener('keydown', e => {
      const it = e.target.closest('.f-pi');
      if (it && e.key === 'Enter') { const n = M.byId.get(it.dataset.id); if (n) select(n, true); }
    });
    els.proj.addEventListener('mouseover', e => {
      const it = e.target.closest('.f-pi'), n = it ? M.byId.get(it.dataset.id) : null;
      if (n !== hover) { hover = n || null; redrawDyn(); }
    });
    els.proj.addEventListener('mouseleave', () => { if (hover) { hover = null; redrawDyn(); } });
    // панель
    els.panel.addEventListener('click', e => {
      const a = e.target.closest('[data-id]');
      if (a) { const n = M.byId.get(a.dataset.id); if (n) select(n, true); return; }
      const b = e.target.closest('[data-p]'); if (!b || !sel) return;
      const act = b.dataset.p;
      if (act === 'focus') focusNode(sel);
      else if (act === 'close') select(null);
      else if (act === 'card' && sel._proj && OPTS.onProject) OPTS.onProject(sel._proj.project || sel._proj.id.replace(/^p:/, ''));
      else if (act === 'fold') {
        collapsed.has(sel.id) ? collapsed.delete(sel.id) : collapsed.add(sel.id);
        recompute(performance.now(), true); renderPanel();
      }
    });
    // миникарта
    const miniGo = e => {
      const r = mini.getBoundingClientRect(), {k, ox, oy} = miniMap();
      cam.x = ((e.clientX - r.left) * DPR - ox) / k; cam.y = ((e.clientY - r.top) * DPR - oy) / k;
      fly = null; moved();
    };
    let miniDrag = false;
    els.miniBox.addEventListener('pointerdown', e => { miniDrag = true; els.miniBox.setPointerCapture(e.pointerId); miniGo(e); });
    els.miniBox.addEventListener('pointermove', e => { if (miniDrag) miniGo(e); });
    els.miniBox.addEventListener('pointerup', () => { miniDrag = false; });

    // холст: колесо, перетаскивание, щипок, наведение, клик
    fg.addEventListener('wheel', e => {
      e.preventDefault();
      zoomAt(e.offsetX, e.offsetY, Math.exp(-(e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY) * .0016));
    }, {passive: false});
    const ptrs = new Map();
    let drag = null, pinch = null;
    fg.addEventListener('pointerdown', e => {
      fg.setPointerCapture(e.pointerId);
      ptrs.set(e.pointerId, {x: e.offsetX, y: e.offsetY});
      if (ptrs.size === 2) {
        const [a, b] = [...ptrs.values()];
        pinch = {d: Math.hypot(a.x - b.x, a.y - b.y) || 1, s: cam.s, cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2};
        drag = null;
      } else drag = {x: e.offsetX, y: e.offsetY, cx: cam.x, cy: cam.y, moved: false};
      fly = null;
      els.res.classList.remove('open');
    });
    fg.addEventListener('pointermove', e => {
      if (ptrs.has(e.pointerId)) ptrs.set(e.pointerId, {x: e.offsetX, y: e.offsetY});
      if (pinch && ptrs.size === 2) {
        const [a, b] = [...ptrs.values()];
        zoomAt(pinch.cx, pinch.cy, (pinch.s * Math.hypot(a.x - b.x, a.y - b.y) / pinch.d) / cam.s);
        return;
      }
      if (drag) {
        const dx = e.offsetX - drag.x, dy = e.offsetY - drag.y;
        if (!drag.moved && Math.hypot(dx, dy) > 4) { drag.moved = true; fg.classList.add('drag'); showTip(null); }
        if (drag.moved) { cam.x = drag.cx - dx / cam.s; cam.y = drag.cy - dy / cam.s; moved(); }
        return;
      }
      const n = pick(e.offsetX, e.offsetY);
      if (n !== hover) { hover = n; fg.classList.toggle('pt', !!n); redrawDyn(); }
      showTip(n, e.offsetX, e.offsetY);
    });
    const up = e => {
      ptrs.delete(e.pointerId);
      if (ptrs.size < 2) pinch = null;
      if (drag && !drag.moved && e.type === 'pointerup') select(pick(e.offsetX, e.offsetY), false);
      drag = null; fg.classList.remove('drag');
    };
    fg.addEventListener('pointerup', up);
    fg.addEventListener('pointercancel', up);
    fg.addEventListener('pointerleave', () => { if (!drag && hover) { hover = null; showTip(null); redrawDyn(); } });
    fg.addEventListener('dblclick', e => {
      const n = pick(e.offsetX, e.offsetY);
      if (n) { select(n, true); return; }
      zoomAt(e.offsetX, e.offsetY, 2);
    });
    window.addEventListener('resize', () => { if (isOpenFlag) resize(); });
    document.addEventListener('keydown', e => {
      if (!isOpenFlag || !introDone || e.target === els.search || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === '/') { e.preventDefault(); els.search.focus(); }
      else if (e.key === '+' || e.key === '=') zoomAt(W / 2, H / 2, 1.3);
      else if (e.key === '-' || e.key === '_') zoomAt(W / 2, H / 2, 1 / 1.3);
      else if (e.key === '0') fitAll();
      else if (e.key === 'Escape') { if (sel) select(null); else if (OPTS.onClose) OPTS.onClose(); }
      else if (e.key.startsWith('Arrow') && !e.target.closest('input')) {
        e.preventDefault();
        const d = 90 / cam.s;
        if (e.key === 'ArrowLeft') cam.x -= d; else if (e.key === 'ArrowRight') cam.x += d;
        else if (e.key === 'ArrowUp') cam.y -= d; else cam.y += d;
        moved();
      }
    });
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (isOpenFlag) { applyTheme(); renderLegend(); } });
  }

  /* ============================ заставка ============================ */
  // Фигура появляется из формул: формулы печатаются вокруг, от них к кривой бегут частицы,
  // фигура перетекает в следующую. В конце L-система выращивает семя — и из него дерево.
  function intro(info) {
    return new Promise(resolve => {
      const box = el('div', 'f-intro'), cv = el('canvas'), msg = el('div', 'msg');
      const bar = el('div', 'bar', '<i></i>'), skip = el('div', 'skip', 'клик или Esc — пропустить');
      box.append(cv, msg, bar, skip);
      layer.appendChild(box);
      const g = cv.getContext('2d');
      let w = 0, h = 0;
      const fit = () => { w = box.clientWidth; h = box.clientHeight; cv.width = Math.round(w * DPR); cv.height = Math.round(h * DPR); };
      fit();
      const N = 420, PHI = (1 + Math.sqrt(5)) / 2;
      const make = f => { const o = new Float32Array(N * 2); for (let i = 0; i < N; i++) { const [x, y] = f(i); o[2 * i] = x; o[2 * i + 1] = y; } return o; };
      const poly = (k, rot) => {
        const V = []; for (let i = 0; i < k; i++) { const a = rot + i * TAU / k; V.push([Math.cos(a), Math.sin(a)]); }
        return make(i => { const u = i / N * k, j = Math.floor(u), f = u - j, P = V[j % k], Q = V[(j + 1) % k]; return [P[0] + (Q[0] - P[0]) * f, P[1] + (Q[1] - P[1]) * f]; });
      };
      const T1 = 3.3 * Math.PI, RMAX = Math.pow(PHI, 2 * T1 / Math.PI);
      const seed = make(() => [0, 0]);
      const steps = [
        {p: make(i => { const a = i / N * TAU - Math.PI / 2; return [Math.cos(a), Math.sin(a)]; }), f: ['x² + y² = r²', 'x = r·cos t', 'y = r·sin t'], name: 'окружность'},
        {p: poly(3, -Math.PI / 2), f: ['α + β + γ = π', 'S = ½·a·h'], name: 'треугольник'},
        {p: poly(4, Math.PI / 4), f: ['S = a²', 'd = a·√2'], name: 'квадрат'},
        {p: poly(6, 0), f: ['S = (3√3 ⁄ 2)·a²', 'φ = 2π ⁄ 6'], name: 'шестиугольник'},
        {p: make(i => { const t = i / N * Math.PI, r = Math.cos(5 * t); return [r * Math.cos(t - Math.PI / 2), r * Math.sin(t - Math.PI / 2)]; }), f: ['r = cos 5θ', 'θ ∈ [0, π]'], name: 'роза Гранди'},
        {p: make(i => { const t = i / (N - 1) * T1, r = Math.pow(PHI, 2 * t / Math.PI) / RMAX; return [r * Math.cos(t + .4), r * Math.sin(t + .4)]; }), f: ['φ = (1 + √5) ⁄ 2', 'r = a·φ^(2θ⁄π)', 'φ² = φ + 1'], name: 'золотая спираль', open: true},
        {p: make(i => { const t = (i + 1) * 2.39996323, r = Math.sqrt((i + 1) / N); return [r * Math.cos(t), r * Math.sin(t)]; }), f: ['θₙ = n · 137,5°', 'rₙ = c·√n', 'Fₙ₊₁ = Fₙ + Fₙ₋₁'], name: 'филлотаксис', dots: true},
      ];
      const LSF = ['F → F[+F]F[−F]F', 'δ = 25,7°', 'n = 4'];
      const msgs = [
        'Считываю портфель: ' + nf(info.nodes) + ' ' + plural(info.nodes, 'узел', 'узла', 'узлов'),
        'Скелетные ветви: ' + info.projects + ' ' + plural(info.projects, 'проект', 'проекта', 'проектов'),
        'Корни: ' + nf(info.roots) + ' ' + plural(info.roots, 'опора', 'опоры', 'опор') + ' фундамента',
        'Листва: ' + nf(info.leaves) + ' ' + plural(info.leaves, 'запись', 'записи', 'записей') + ' памяти',
        'Цветы: ' + nf(info.tasks) + ' ' + plural(info.tasks, 'задача', 'задачи', 'задач') + ' · плоды: ' + nf(info.fruits),
        'Раскладываю крону по золотому углу…',
        'Филлотаксис: листья не заслоняют друг друга',
        'Семя готово — растём'];
      // L-система: F → F[+F]F[−F]F, угол 25,7°, четыре поколения
      let s = 'F'; for (let i = 0; i < 4; i++) s = s.replace(/F/g, 'F[+F]F[-F]F');
      const segs = [];
      { let x = 0, y = 0, a = -Math.PI / 2; const st = [], ang = 25.7 * Math.PI / 180;
        for (const ch of s) {
          if (ch === 'F') { const nx = x + Math.cos(a), ny = y + Math.sin(a); segs.push([x, y, nx, ny, st.length]); x = nx; y = ny; }
          else if (ch === '+') a += ang; else if (ch === '-') a -= ang;
          else if (ch === '[') st.push([x, y, a]); else if (ch === ']') [x, y, a] = st.pop();
        } }
      let lsTop = 0; for (const q of segs) lsTop = Math.min(lsTop, q[1], q[3]);
      const STEP = reduced ? 150 : 820, MORPH = reduced ? 80 : 430, LS = reduced ? 300 : 1600, CPS = 34;
      const total = steps.length * STEP + LS;
      const col = dark ? {bg0: '#0c1b30', bg1: '#02060c', grid: 'rgba(120,170,255,.07)', axis: 'rgba(120,170,255,.2)', line: '#8fd3ff', txt: [214, 232, 255], part: '#c6ecff'}
        : {bg0: '#fbf8f0', bg1: '#e8e0cc', grid: 'rgba(40,70,120,.08)', axis: 'rgba(40,70,120,.22)', line: '#1f4e79', txt: [31, 52, 85], part: '#2f6aa3'};
      const FF = '"Cambria Math","STIX Two Math","Latin Modern Math","Times New Roman",serif';
      const t0 = performance.now();
      let done = false, rafI = 0;
      const keySkip = e => { if (isOpenFlag && (e.key === 'Escape' || e.key === ' ' || e.key === 'Enter')) { e.preventDefault(); finish(); } };
      const finish = () => {
        if (done) return; done = true;
        cancelAnimationFrame(rafI);
        document.removeEventListener('keydown', keySkip);
        box.style.opacity = 0;
        setTimeout(() => box.remove(), 600);
        resolve();
      };
      box.addEventListener('click', finish);
      document.addEventListener('keydown', keySkip);
      const pts = new Float32Array(N * 2);
      // где формула i шага k: по кругу вокруг фигуры, соседние шаги — со сдвигом
      const fAngle = (i, k) => [-2.5, -.62, .98, 2.55][i % 4] + (k % 2 ? .3 : -.16);
      function anchorOf(i, k, cx, cy, R) {
        const a = fAngle(i, k);
        return [cx + Math.cos(a) * R * 2.02, cy + Math.sin(a) * R * 1.38, Math.cos(a) < 0];
      }
      function drawFormula(f, i, k, cx, cy, R, typed, alpha, target, flow, now) {
        const [fx, fy, left] = anchorOf(i, k, cx, cy, R);
        const txt = f.slice(0, Math.max(0, Math.ceil(f.length * typed)));
        g.textAlign = left ? 'right' : 'left';
        g.fillStyle = rgba(col.txt, .95 * alpha);
        g.fillText(txt + (typed < 1 && Math.floor(now / 110) % 2 ? '▏' : ''), fx, fy);
        if (!flow || typed <= 0) return;
        // выноска и частицы: формула питает кривую
        const ax = fx + (left ? 10 : -10), ay = fy - 7, [px, py] = target;
        const mx = (ax + px) / 2 + (py - ay) * .18, my = (ay + py) / 2 - (px - ax) * .18;
        g.strokeStyle = rgba(col.txt, .34 * alpha * typed); g.setLineDash([3, 5]); g.lineWidth = 1;
        g.beginPath(); g.moveTo(ax, ay); g.quadraticCurveTo(mx, my, px, py); g.stroke(); g.setLineDash([]);
        g.fillStyle = col.part;
        for (let q = 0; q < 7; q++) {
          const u = (now * .00085 + q / 7 + i * .17) % 1, m = 1 - u;
          g.globalAlpha = typed * alpha * Math.sin(u * Math.PI) * .95;
          g.beginPath(); g.arc(m * m * ax + 2 * m * u * mx + u * u * px, m * m * ay + 2 * m * u * my + u * u * py, 2.2, 0, TAU); g.fill();
        }
        g.globalAlpha = 1;
      }
      function drawFrame(now) {
        if (done) return;
        const t = now - t0;
        if (box.clientWidth !== w || box.clientHeight !== h) fit();
        g.setTransform(DPR, 0, 0, DPR, 0, 0);
        const cx = w / 2, cy = h * .45, R = Math.min(w, h) * .19;
        const bgG = g.createRadialGradient(cx, cy, 0, cx, cy, Math.max(w, h) * .75);
        bgG.addColorStop(0, col.bg0); bgG.addColorStop(1, col.bg1);
        g.fillStyle = bgG; g.fillRect(0, 0, w, h);
        // миллиметровка и оси
        g.strokeStyle = col.grid; g.lineWidth = 1;
        const gs = 32;
        g.beginPath();
        for (let x = cx % gs; x < w; x += gs) { g.moveTo(x, 0); g.lineTo(x, h); }
        for (let y = cy % gs; y < h; y += gs) { g.moveTo(0, y); g.lineTo(w, y); }
        g.stroke();
        g.strokeStyle = col.axis; g.beginPath(); g.moveTo(0, cy); g.lineTo(w, cy); g.moveTo(cx, 0); g.lineTo(cx, h); g.stroke();
        // фон: блуждающие формулы
        g.font = 'italic 13px ' + FF; g.textAlign = 'left';
        ['∑ wᵢ = W', '∂f ⁄ ∂x', 'e^(iπ) + 1 = 0', '∫ r dθ', 'y = kx + b', 'n! ≈ √(2πn)·(n ⁄ e)ⁿ', 'sin²α + cos²α = 1', 'lim 1 ⁄ n = 0', 'Δ = b² − 4ac']
          .forEach((f, i) => {
            g.fillStyle = rgba(col.txt, .07 + .05 * Math.sin(t * .0007 + i));
            g.fillText(f, (rnd(91, i) * .86 + .04) * w + Math.sin(t * .00025 + i) * 12, (rnd(97, i) * .86 + .07) * h + Math.cos(t * .0003 + i) * 10);
          });
        const inLs = t >= steps.length * STEP;
        const si = Math.min(steps.length - 1, Math.floor(t / STEP));
        const st = steps[si], prev = si ? steps[si - 1].p : seed;
        const pm = inLs ? 1 : easeIO(clamp((t - si * STEP) / MORPH, 0, 1));
        for (let i = 0; i < N * 2; i++) pts[i] = lerp(prev[i], st.p[i], pm);
        const lsP = inLs ? clamp((t - steps.length * STEP) / LS, 0, 1) : 0, fade = 1 - easeOut(lsP);
        // фигура
        if (fade > .01) {
          const dotsA = st.dots ? pm : 0, lineA = (1 - dotsA) * fade;
          if (lineA > .01) {
            g.lineJoin = 'round';
            for (const [lw, a] of [[8, .08], [3.2, .2], [1.5, .95]]) {
              g.strokeStyle = col.line; g.globalAlpha = a * lineA; g.lineWidth = lw;
              g.beginPath();
              for (let i = 0; i < N; i++) { const X = cx + pts[2 * i] * R, Y = cy + pts[2 * i + 1] * R; i ? g.lineTo(X, Y) : g.moveTo(X, Y); }
              if (!(st.open && pm >= 1)) g.closePath();
              g.stroke();
            }
          }
          if (dotsA > .01) {
            g.fillStyle = col.line;
            for (let i = 0; i < N; i++) {
              g.globalAlpha = dotsA * fade * (.35 + .65 * i / N);
              g.beginPath(); g.arc(cx + pts[2 * i] * R, cy + pts[2 * i + 1] * R, .8 + 2.4 * i / N, 0, TAU); g.fill();
            }
          }
          g.globalAlpha = 1;
          // подпись фигуры
          g.font = '600 11px system-ui,-apple-system,"Segoe UI",sans-serif'; g.textAlign = 'center';
          g.fillStyle = rgba(col.txt, .5 * fade * pm);
          g.fillText(st.name.toUpperCase().split('').join('\u2009'), cx, cy + R * 1.34);
        }
        // формулы: текущего шага печатаются, прошлого — гаснут
        g.font = 'italic ' + Math.round(clamp(R * .15, 15, 24)) + 'px ' + FF;
        const tf = inLs ? t - steps.length * STEP : t - si * STEP;
        const target = i => {
          if (inLs) return [cx, cy + R * .6];
          const a = fAngle(i, si), idx = Math.floor((((a + Math.PI / 2) % TAU + TAU) % TAU) / TAU * N) % N;
          return [cx + pts[2 * idx] * R, cy + pts[2 * idx + 1] * R];
        };
        if (!inLs && si > 0 && tf < 420) steps[si - 1].f.forEach((f, i) => drawFormula(f, i, si - 1, cx, cy, R, 1, 1 - tf / 420, null, false, now));
        if (inLs && tf < 420) st.f.forEach((f, i) => drawFormula(f, i, si, cx, cy, R, 1, 1 - tf / 420, null, false, now));
        const list = inLs ? LSF : st.f, k = inLs ? si + 1 : si;
        list.forEach((f, i) => {
          const typed = clamp((tf - 120 - i * 150) / (f.length * CPS), 0, 1);
          drawFormula(f, i, k, cx, cy, R, typed, inLs ? 1 - lsP * .6 : 1, target(i), true, now);
        });
        g.textAlign = 'left';
        // L-система: из формулы вырастает семя дерева
        if (inLs) {
          const kk = Math.floor(segs.length * easeOut(clamp(lsP * 1.2, 0, 1)));
          const sc = (R * 2.2) / -lsTop, bx = cx, by = cy + R * 1.1;
          g.lineCap = 'round'; g.strokeStyle = col.line; g.globalAlpha = .92;
          for (let i = 0; i < kk; i++) {
            const q = segs[i];
            g.lineWidth = Math.max(.6, 3 - q[4] * .6);
            g.beginPath(); g.moveTo(bx + q[0] * sc, by + q[1] * sc); g.lineTo(bx + q[2] * sc, by + q[3] * sc); g.stroke();
          }
          g.globalAlpha = 1;
        }
        const mi = inLs ? msgs.length - 1 : Math.min(msgs.length - 2, si);
        if (msg.textContent !== msgs[mi]) msg.textContent = msgs[mi];
        bar.firstChild.style.width = (100 * clamp(t / total, 0, 1)).toFixed(1) + '%';
        if (t >= total + (reduced ? 0 : 250)) { finish(); return; }
        rafI = requestAnimationFrame(drawFrame);
      }
      rafI = requestAnimationFrame(drawFrame);
    });
  }

  /* ============================ открытие ============================ */
  function prepare(TREE) {
    DATA = TREE;
    M = layoutTree(TREE);
    M.byId = new Map(M.nodes.map(n => [n.id, n]));
    hidden.clear(); collapsed.clear(); sel = null; hiSet = null; hover = null; cutoff = '';
    const tasks = M.leaves.filter(n => n._work).length;
    els.stats.textContent = nf(M.nodes.length) + ' ' + plural(M.nodes.length, 'узел', 'узла', 'узлов') + ' · ' +
      M.projects.length + ' ' + plural(M.projects.length, 'проект', 'проекта', 'проектов') + ' · ' + tasks + ' ' + plural(tasks, 'задача', 'задачи', 'задач');
    if (W < 1180) els.proj.classList.add('min');
    if (W < 900 || H < 620) els.legend.classList.add('min');
    renderTime(); renderLegend(); renderProjects(); renderPanel();
  }
  function startGrowth(now) {
    let until = now;
    for (const n of M.nodes) {
      n._gt0 = n._root ? now + 250 + Math.max(0, n._d - 2) * 450 + rnd(n._h, 10) * 200
        : now + 350 + Math.max(0, n._d - 1) * 600 + rnd(n._h, 10) * 300;
      until = Math.max(until, n._gt0 + GROW_MS);
    }
    M.root._gt0 = now;
    M.growUntil = until;
    redrawAll();
    setTimeout(() => { if (els.hint) els.hint.style.opacity = 0; }, 9000);
  }

  return {
    open(TREE, opts) {
      OPTS = opts || {};
      if (!TREE || !TREE.root || !(TREE.root.children || []).length) {
        if (OPTS.onEmpty) OPTS.onEmpty();
        return;
      }
      reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
      if (!layer) buildDOM();
      layer.hidden = false; isOpenFlag = true;
      document.documentElement.style.overflow = 'hidden';
      applyTheme();
      resize();
      if (!M || DATA !== TREE) {
        prepare(TREE);
        fitAll(0);
        for (const n of M.nodes) n._gt0 = Infinity;   // до конца заставки дерево ещё не проросло
        const info = {nodes: M.nodes.length, projects: M.projects.length, roots: M.tips.filter(n => n.kind === 'сделано').length,
          leaves: M.leaves.filter(n => !n._work).length, tasks: M.leaves.filter(n => n._work).length, fruits: M.fruits.length};
        introDone = false;
        intro(info).then(() => { introDone = true; startGrowth(performance.now()); });
      } else { introDone = true; redrawAll(); }
    },
    close() {
      if (!layer) return;
      isOpenFlag = false; layer.hidden = true; stopPlay();
      document.documentElement.style.overflow = '';
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
    },
    isOpen: () => isOpenFlag,
    debug: () => M && {nodes: M.nodes.length, edges: M.edges.length, clumps: M.clumps.length,
      staticMs: +staticMs.toFixed(1), scale: +cam.s.toFixed(3), selected: sel ? sel.id : null},
    _test: {workStatus, layoutTree},          // для юнит-тестов раскладки (tests/test_tree_view.py)
  };
})();
