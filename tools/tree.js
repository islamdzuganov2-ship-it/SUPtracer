/* Дерево роста портфеля.

   Корень слева, ветви вправо. Ветвь выходит из конца подписи родителя, а не
   из-под неё, — иначе подпись пересекают собственные ветви и читать нельзя.

   Толщина ветви — вес поддерева в токенах: во что эта ветка обошлась или
   обойдётся. Дата узла — когда он появился; ползунок «рост» отсекает по ней,
   поэтому дерево разрастается, а не рисуется сразу целиком.

   Отрисовка откладывается до открытия вкладки: у скрытого блока нулевой размер,
   и вписать в него дерево нельзя. */

const TREEVIEW = (function () {
  const KCOL = {
    'портфель': '--s7', 'проект': '--s1', 'запрос': '--s3', 'решение': '--s6',
    'требование': '--s5', 'риск': '--s8', 'итог': '--s7', 'факт': '--ink3',
    'заметка': '--ink3', 'источник': '--ink3', 'фундамент': '--s2', 'сделано': '--s2',
    'бэклог': '--s4', 'задача': '--s4', 'почки': '--ink3', 'предложение': '--ink3',
  };
  const kcol = k => `var(${KCOL[k] || '--ink3'})`;
  const SVGNS = 'http://www.w3.org/2000/svg';
  let built = false;

  function build(host, TREE, opts) {
    const esc = opts.esc, tok = opts.tok, onProject = opts.onProject || (() => {});
    const root = TREE && TREE.root;
    if (!root || !(root.children || []).length) {
      host.innerHTML = `<div class="note">Дерево пустое. Оно вырастает из запросов: положите заметку
        командой <code>py tools/pm.py ingest файл.md</code> или подключите хранилище Obsidian
        (<code>py tools/pm.py vault init путь</code>). Корнем станет сам запрос, ветвями — задачи,
        которые из него выросли, и решения, которыми на него ответили.</div>`;
      return;
    }
    const months = ((TREE.stats || {}).months || []).filter(Boolean);
    const projOpts = (root.children || [])
      .map(c => `<option value="${esc(c.id)}">${esc(c.label)}</option>`).join('');

    host.innerHTML = `<div class="treebox">
      <div class="ttools">
        <select class="sel" id="tProj"><option value="">Весь портфель</option>${projOpts}</select>
        <input type="search" id="tFind" placeholder="найти узел" autocomplete="off">
        <label>рост <input type="range" id="tTime" min="0" max="${months.length}" value="${months.length}"></label>
        <button class="btn" id="tPlay" title="Проиграть рост дерева по месяцам">▶ рост</button>
        <label><input type="checkbox" id="tFut" checked> планы</label>
        <span class="grow"></span>
        <button class="btn" id="tFit">По размеру</button>
        <button class="btn" id="tColl">Свернуть</button>
        <button class="btn" id="tExp">Развернуть</button>
      </div>
      <div class="tstage" id="tStage">
        <svg id="tSvg"><g id="tCam"></g></svg>
        <div class="tstat" id="tStat"></div>
        <div class="ttip" id="tTip"></div>
      </div>
      <div class="tlegend2">${Object.entries({
        'запрос': 'корневой запрос', 'решение': 'чем ответили', 'требование': 'условие приёмки',
        'риск': 'угроза', 'сделано': 'фундамент — уже работает', 'задача': 'ветвь — запланировано',
        'предложение': 'почка — ещё не принято',
      }).map(([k, v]) => `<span><i style="background:${kcol(k)}"></i>${esc(v)}</span>`).join('')}
        <span style="color:var(--ink3)">толщина ветви — вес в токенах · клик по узлу сворачивает ветвь</span></div>
      ${TREE.note ? `<div class="tnote">${esc(TREE.note)}</div>` : ''}
    </div>`;

    const svg = document.getElementById('tSvg'), cam = document.getElementById('tCam'),
      stage = document.getElementById('tStage'), tip = document.getElementById('tTip'),
      stat = document.getElementById('tStat');
    const collapsed = new Set(), ROW = 25;
    let cutoff = '9999-12', showFuture = true, onlyProj = '', find = '';
    let view = {x: 40, y: 20, k: 1}, playTimer = null;
    const maxW = Math.max(1, root.subtree_weight || 1);
    const nd = n => (n.first_date || n.date || '');

    const kidsOf = n => (n.children || []).filter(c => {
      if (!showFuture && c.future) return false;
      const d = nd(c);
      return !(d && d.slice(0, 7) > cutoff);
    });

    // Большое дерево не читается развёрнутым — глубокие ветви свёрнуты заранее.
    (function autoCollapse(n, d) {
      if ((TREE.stats || {}).nodes > 70 && d >= 2 && (n.children || []).length) collapsed.add(n.id);
      (n.children || []).forEach(c => autoCollapse(c, d + 1));
    })(root, 0);

    function radius(n) { return Math.max(3.5, Math.min(11, 3.5 + 7 * Math.sqrt((n.subtree_weight || 0) / maxW))); }
    function width(n) { return Math.max(.9, Math.min(7, .9 + 6 * Math.sqrt((n.subtree_weight || 0) / maxW))); }
    function shortLabel(n) { const s = String(n.label || ''); return s.length > 46 ? s.slice(0, 46) + '…' : s; }
    function labelW(n) { return radius(n) + 9 + shortLabel(n).length * 6.35 + (n._hidden > 0 ? 26 : 0); }

    function visibleTree() {
      const nodes = [], links = [];
      let y = 0;
      (function walk(n, depth, parent) {
        if (onlyProj && depth === 1 && n.id !== onlyProj) return null;
        n._d = depth; n._p = parent;
        const kids = collapsed.has(n.id) ? [] : kidsOf(n);
        const laid = kids.map(k => walk(k, depth + 1, n)).filter(Boolean);
        if (laid.length) n._y = (laid[0]._y + laid[laid.length - 1]._y) / 2;
        else { n._y = y; y += ROW; }
        n._hidden = (n.children || []).length - laid.length;
        nodes.push(n);
        laid.forEach(k => links.push([n, k]));
        return n;
      })(root, 0, null);

      const wide = {};
      nodes.forEach(n => { n._lw = labelW(n); wide[n._d] = Math.max(wide[n._d] || 0, n._lw); });
      const colX = {}; let acc = 0;
      Object.keys(wide).map(Number).sort((a, b) => a - b).forEach(d => { colX[d] = acc; acc += wide[d] + 36; });
      nodes.forEach(n => n._x = colX[n._d] || 0);
      return {nodes, links};
    }

    function draw(animate) {
      const {nodes, links} = visibleTree();
      cam.innerHTML = '';
      const gl = document.createElementNS(SVGNS, 'g'), gn = document.createElementNS(SVGNS, 'g');
      cam.append(gl, gn);

      links.forEach(([a, b]) => {
        const p = document.createElementNS(SVGNS, 'path');
        const x0 = a._x + (a._lw || 0), dx = Math.max(14, (b._x - x0) * 0.5);
        p.setAttribute('d', `M${x0},${a._y} C${x0 + dx},${a._y} ${b._x - dx},${b._y} ${b._x},${b._y}`);
        p.setAttribute('class', 'tt-link' + (animate ? ' tt-fade' : ''));
        p.setAttribute('stroke', kcol(b.kind));
        p.setAttribute('stroke-width', width(b));
        if (b.future) p.setAttribute('stroke-dasharray', '4 4');
        gl.appendChild(p);
      });

      const hit = find ? nodes.filter(n => String(n.label || '').toLowerCase().includes(find)) : [];
      nodes.forEach(n => {
        const g = document.createElementNS(SVGNS, 'g');
        g.setAttribute('class', 'tt-n' + (animate ? ' tt-fade' : '')
          + (find && !hit.includes(n) ? ' dim' : '') + (find && hit.includes(n) ? ' hit' : ''));
        g.setAttribute('transform', `translate(${n._x},${n._y})`);
        const c = document.createElementNS(SVGNS, 'circle');
        c.setAttribute('class', 'dot'); c.setAttribute('r', radius(n));
        c.setAttribute('fill', n.future ? 'var(--panel)' : kcol(n.kind));
        c.setAttribute('stroke', kcol(n.kind)); c.setAttribute('stroke-width', n.future ? 1.6 : 0);
        g.appendChild(c);
        const t = document.createElementNS(SVGNS, 'text');
        t.setAttribute('x', radius(n) + 8);
        t.textContent = shortLabel(n) + (n._hidden > 0 ? `  (+${n._hidden})` : '');
        g.appendChild(t);
        g.addEventListener('click', e => {
          e.stopPropagation();
          if ((n.children || []).length) {
            collapsed.has(n.id) ? collapsed.delete(n.id) : collapsed.add(n.id);
            draw(true);
          }
        });
        g.addEventListener('dblclick', () => { if (n.project) onProject(n.project); });
        g.addEventListener('mousemove', e => showTip(e, n));
        g.addEventListener('mouseleave', () => { tip.style.opacity = 0; });
        gn.appendChild(g);
      });

      if (animate) requestAnimationFrame(() =>
        cam.querySelectorAll('.tt-fade').forEach(e => e.classList.remove('tt-fade')));
      const w = nodes.filter(n => n._d === 1).reduce((s, n) => s + (n.subtree_weight || 0), 0);
      stat.innerHTML = `узлов ${nodes.length} из ${(TREE.stats || {}).nodes || nodes.length} · ${tok(w)} токенов`
        + (cutoff < '9999' ? ` · на ${esc(cutoff)}` : '');
      apply();
    }

    function showTip(e, n) {
      const r = stage.getBoundingClientRect();
      const m = [];
      if (n.size) m.push('размер ' + esc(n.size));
      if (n.status) m.push(esc(n.status));
      if (nd(n)) m.push(esc(nd(n)));
      if (n.subtree_weight) m.push(tok(n.subtree_weight) + ' токенов');
      if (n.count > 1) m.push(n.count + ' узлов в ветви');
      if (n.source) m.push('из ' + esc(String(n.source).slice(0, 60)));
      tip.innerHTML = `<div class="k">${esc(n.kind)}</div><b>${esc(n.label)}</b>`
        + (n.detail ? `<div style="margin-top:5px">${esc(String(n.detail).slice(0, 260))}</div>` : '')
        + (m.length ? `<div style="margin-top:6px;color:var(--ink3)">${m.join(' · ')}</div>` : '')
        + ((n.children || []).length
          ? `<div style="margin-top:6px;color:var(--ink3)">клик — ${collapsed.has(n.id) ? 'раскрыть' : 'свернуть'} ветвь</div>` : '');
      tip.style.opacity = 1;
      const x = Math.min(e.clientX - r.left + 14, r.width - tip.offsetWidth - 8);
      const y = Math.min(e.clientY - r.top + 12, r.height - tip.offsetHeight - 8);
      tip.style.left = Math.max(6, x) + 'px';
      tip.style.top = Math.max(6, y) + 'px';
    }

    function apply() { cam.setAttribute('transform', `translate(${view.x},${view.y}) scale(${view.k})`); }

    function fit() {
      const b = cam.getBBox(), r = stage.getBoundingClientRect();
      if (!b.width || !b.height || !r.width) return;
      view.k = Math.max(.18, Math.min((r.width - 70) / b.width, (r.height - 40) / b.height, 1.15));
      view.x = 28 - b.x * view.k;
      view.y = (r.height - b.height * view.k) / 2 - b.y * view.k;
      apply();
    }

    svg.addEventListener('wheel', e => {
      e.preventDefault();
      const r = svg.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
      const k = Math.max(.15, Math.min(3, view.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
      view.x = mx - (mx - view.x) * (k / view.k);
      view.y = my - (my - view.y) * (k / view.k);
      view.k = k; apply();
    }, {passive: false});
    let drag = null;
    svg.addEventListener('pointerdown', e => {
      drag = {x: e.clientX, y: e.clientY, vx: view.x, vy: view.y};
      svg.classList.add('drag'); svg.setPointerCapture(e.pointerId);
    });
    svg.addEventListener('pointermove', e => {
      if (!drag) return;
      view.x = drag.vx + (e.clientX - drag.x);
      view.y = drag.vy + (e.clientY - drag.y);
      apply();
    });
    svg.addEventListener('pointerup', () => { drag = null; svg.classList.remove('drag'); });
    svg.addEventListener('pointerleave', () => { drag = null; svg.classList.remove('drag'); tip.style.opacity = 0; });

    document.getElementById('tProj').onchange = e => { onlyProj = e.target.value; draw(true); fit(); };
    document.getElementById('tFind').oninput = e => {
      find = e.target.value.trim().toLowerCase();
      if (find) expandTo(find);
      draw(false);
    };
    document.getElementById('tFut').onchange = e => { showFuture = e.target.checked; draw(true); };
    document.getElementById('tFit').onclick = () => fit();
    document.getElementById('tColl').onclick = () => {
      (function c(n, d) {
        if (d >= 1 && (n.children || []).length) collapsed.add(n.id);
        (n.children || []).forEach(x => c(x, d + 1));
      })(root, 0);
      draw(true); fit();
    };
    document.getElementById('tExp').onclick = () => { collapsed.clear(); draw(true); fit(); };

    const slider = document.getElementById('tTime');
    slider.oninput = e => {
      const i = +e.target.value;
      cutoff = i >= months.length ? '9999-12' : months[i];
      draw(true);
    };

    document.getElementById('tPlay').onclick = function () {
      if (playTimer) { clearInterval(playTimer); playTimer = null; this.textContent = '▶ рост'; return; }
      if (!months.length) return;
      this.textContent = '❚❚ стоп';
      let i = 0;
      slider.value = 0; cutoff = months[0]; draw(true);
      playTimer = setInterval(() => {
        i++;
        if (i > months.length) {
          clearInterval(playTimer); playTimer = null;
          document.getElementById('tPlay').textContent = '▶ рост';
          return;
        }
        slider.value = i;
        cutoff = i >= months.length ? '9999-12' : months[i];
        draw(true);
      }, 780);
    };

    function expandTo(q) {
      (function walk(n, chain) {
        const c = chain.concat([n]);
        if (String(n.label || '').toLowerCase().includes(q)) c.forEach(x => collapsed.delete(x.id));
        (n.children || []).forEach(k => walk(k, c));
      })(root, []);
    }

    draw(false); fit();
    let rz;
    addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(fit, 180); });
  }

  return {
    /* Вызывается при каждом показе вкладки: первый раз строит, дальше ничего не делает. */
    ensure(host, TREE, opts) {
      if (built) return;
      built = true;
      build(host, TREE, opts);
    },
  };
})();
