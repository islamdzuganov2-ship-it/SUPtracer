/* Диаграммы дашборда — инлайновый SVG, без библиотек.
 *
 * Форма выбирается по задаче данных, цвет назначается последним:
 *   величина по категориям -> горизонтальные бары, один тон, без легенды;
 *   состав целого          -> стопочные бары, категориальные слоты по порядку;
 *   матрица величин        -> теплокарта одним тоном, светлое = меньше;
 *   связь двух мер         -> точечная, один ряд + прямые подписи.
 * Дуальных осей нет нигде: две меры разного масштаба — это две диаграммы.
 */

const VIZ = (() => {
  const NS = 'http://www.w3.org/2000/svg';

  // Категориальные слоты назначаются строго по порядку и никогда не перебираются
  // по кругу: девятый ряд сворачивается в «прочее», а не получает новый оттенок.
  const CAT = ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)',
               'var(--s5)', 'var(--s6)', 'var(--s7)', 'var(--s8)'];

  // Ширина берётся у контейнера: тогда масштаб viewBox равен единице и кегль
  // текста на экране совпадает с заданным, а не растягивается вместе с картинкой.
  const hostWidth = host => Math.max(340, Math.round(host.clientWidth || 900));

  const el = (name, attrs, text) => {
    const n = document.createElementNS(NS, name);
    for (const k in attrs) if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  };

  // --- слой подсказок: один на страницу, следует за курсором -----------------
  let tip;
  function tipInit() {
    if (tip) return;
    tip = document.createElement('div');
    tip.className = 'viz-tip';
    tip.setAttribute('role', 'tooltip');
    document.body.appendChild(tip);
  }
  function hoverable(node, html) {
    tipInit();
    node.style.cursor = 'default';
    node.addEventListener('pointerenter', e => {
      tip.innerHTML = html;
      tip.classList.add('on');
      move(e);
    });
    node.addEventListener('pointermove', move);
    node.addEventListener('pointerleave', () => tip.classList.remove('on'));
    function move(e) {
      const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
      let x = e.clientX + pad, y = e.clientY + pad;
      if (x + w > innerWidth - 8) x = e.clientX - w - pad;
      if (y + h > innerHeight - 8) y = e.clientY - h - pad;
      tip.style.left = Math.max(8, x) + 'px';
      tip.style.top = Math.max(8, y) + 'px';
    }
  }

  function frame(host, w, h, title, sub) {
    host.innerHTML = '';
    if (title) {
      const t = document.createElement('div');
      t.className = 'viz-title';
      t.textContent = title;
      host.appendChild(t);
    }
    if (sub) {
      const s = document.createElement('div');
      s.className = 'viz-sub';
      s.innerHTML = sub;
      host.appendChild(s);
    }
    const svg = el('svg', {viewBox: `0 0 ${w} ${h}`, role: 'img'});
    host.appendChild(svg);
    return svg;
  }

  function legend(host, items) {
    const box = document.createElement('div');
    box.className = 'viz-legend';
    box.innerHTML = items.map(i =>
      `<span><i style="background:${i.color}"></i>${i.label}</span>`).join('');
    host.appendChild(box);
  }

  /* Горизонтальные бары: величина по категориям. Один ряд — один тон и никакой
   * легенды, название диаграммы уже называет меру. Подпись значения — у каждого
   * бара, потому что рядов немного и число важнее точности глазомера. */
  function bars(host, {title, sub, rows, fmt, color, max, note}) {
    const w = hostWidth(host);
    const RH = 26, PAD_T = 6, PAD_B = 4, LW = Math.min(180, Math.max(96, w * 0.30)), VW = 86;
    const h = PAD_T + rows.length * RH + PAD_B;
    const svg = frame(host, w, h, title, sub);
    const top = Math.max(max || 0, ...rows.map(r => r.value), 1);
    const plot = w - LW - VW;

    rows.forEach((r, i) => {
      const y = PAD_T + i * RH;
      const bw = Math.max(2, plot * (r.value / top));
      const c = r.color || color || 'var(--s1)';

      svg.appendChild(el('text', {
        x: LW - 10, y: y + RH / 2 + 4, 'text-anchor': 'end',
        class: 'viz-label'
      }, r.label));

      const bar = el('rect', {
        x: LW, y: y + 6, width: bw, height: RH - 13, rx: 4, fill: c
      });
      hoverable(bar, r.tip || `<b>${r.label}</b><br>${fmt ? fmt(r.value) : r.value}`);
      svg.appendChild(bar);

      svg.appendChild(el('text', {
        x: LW + bw + 8, y: y + RH / 2 + 4, class: 'viz-value'
      }, fmt ? fmt(r.value) : String(r.value)));
    });
    if (note) caption(host, note);
    return svg;
  }

  /* Стопочные бары: состав целого по периодам. Между сегментами — зазор в цвет
   * поверхности, иначе соседние заливки сливаются в одну фигуру. */
  function stacked(host, {title, sub, cats, series, fmt, note}) {
    const w = hostWidth(host);
    const PAD_L = 62, PAD_R = 14, PAD_T = 14, PAD_B = 30, GAP = 2;
    const h = 250;
    const svg = frame(host, w, h, title, sub);
    const totals = cats.map((_, i) => series.reduce((s, x) => s + (x.values[i] || 0), 0));
    const top = Math.max(...totals, 1);
    const bw = (w - PAD_L - PAD_R) / cats.length;
    const Y = v => PAD_T + (h - PAD_T - PAD_B) * (1 - v / top);

    for (let f = 0; f <= 1.0001; f += 0.25) {
      const y = Y(top * f);
      svg.appendChild(el('line', {x1: PAD_L, y1: y, x2: w - PAD_R, y2: y, class: 'viz-grid'}));
      svg.appendChild(el('text', {x: PAD_L - 8, y: y + 4, 'text-anchor': 'end', class: 'viz-tick'},
        fmt ? fmt(top * f) : String(Math.round(top * f))));
    }

    cats.forEach((cat, i) => {
      let acc = 0;
      const x = PAD_L + i * bw + bw * 0.24, bar = bw * 0.52;
      series.forEach((s, si) => {
        const v = s.values[i] || 0;
        if (v <= 0) return;
        const y0 = Y(acc), y1 = Y(acc + v);
        acc += v;
        const hh = Math.max(0, y0 - y1 - GAP);
        if (hh <= 0) return;
        const seg = el('rect', {x, y: y1, width: bar, height: hh, rx: 2, fill: CAT[si] || CAT[7]});
        hoverable(seg, `<b>${s.label}</b><br>${cat}: ${fmt ? fmt(v) : v}`);
        svg.appendChild(seg);
      });
      svg.appendChild(el('text', {x: x + bar / 2, y: Y(acc) - 7, 'text-anchor': 'middle', class: 'viz-value'},
        fmt ? fmt(totals[i]) : String(totals[i])));
      svg.appendChild(el('text', {x: x + bar / 2, y: h - 9, 'text-anchor': 'middle', class: 'viz-tick'}, cat));
    });

    legend(host, series.map((s, i) => ({label: s.label, color: CAT[i] || CAT[7]})));
    if (note) caption(host, note);
  }

  /* Горизонтальные стопочные бары: состав целого по категориям, когда важны
   * и доля, и абсолютный размер. Общая длина = целое, поэтому проекты сравнимы
   * между собой не только по проценту. */
  function hstack(host, {title, sub, rows, series, fmt, note}) {
    const w = hostWidth(host);
    const RH = 28, PAD_T = 6, LW = Math.min(180, Math.max(96, w * 0.30)), VW = 60, GAP = 2;
    const h = PAD_T + rows.length * RH + 6;
    const svg = frame(host, w, h, title, sub);
    const totals = rows.map(r => r.parts.reduce((s, v) => s + v, 0));
    const top = Math.max(...totals, 1);
    const plot = w - LW - VW;

    rows.forEach((r, i) => {
      const y = PAD_T + i * RH;
      svg.appendChild(el('text', {x: LW - 10, y: y + RH / 2 + 4, 'text-anchor': 'end', class: 'viz-label'}, r.label));
      let x = LW;
      r.parts.forEach((v, si) => {
        if (v <= 0) return;
        const bw = plot * (v / top);
        const draw = Math.max(0, bw - GAP);
        if (draw <= 0) { x += bw; return; }
        const seg = el('rect', {x, y: y + 7, width: draw, height: RH - 15, rx: 4, fill: CAT[si] || CAT[7]});
        hoverable(seg, `<b>${r.label}</b><br>${series[si].label}: ${fmt ? fmt(v) : v}`);
        svg.appendChild(seg);
        x += bw;
      });
      svg.appendChild(el('text', {x: LW + plot * (totals[i] / top) + 8, y: y + RH / 2 + 4, class: 'viz-value'},
        r.right != null ? r.right : (fmt ? fmt(totals[i]) : String(totals[i]))));
    });
    legend(host, series.map((s, i) => ({label: s.label, color: CAT[i] || CAT[7]})));
    if (note) caption(host, note);
  }

  /* Теплокарта: матрица величин одной природы. Один тон, светлое — near zero.
   * Число печатается в каждой ячейке, поэтому цвет не единственный носитель. */
  function matrix(host, {title, sub, rows, cols, cell, note}) {
    const w = hostWidth(host);
    const LW = Math.min(190, Math.max(110, w * 0.20)), RH = 30, HDR = 26;
    const CW = (w - LW) / cols.length;
    const h = HDR + rows.length * RH + 4;
    const svg = frame(host, w, h, title, sub);

    cols.forEach((c, j) => svg.appendChild(el('text', {
      x: LW + j * CW + CW / 2, y: HDR - 9, 'text-anchor': 'middle', class: 'viz-tick'
    }, c.label)));

    rows.forEach((r, i) => {
      const y = HDR + i * RH;
      svg.appendChild(el('text', {x: LW - 10, y: y + RH / 2 + 4, 'text-anchor': 'end', class: 'viz-label'}, r.label));
      cols.forEach((c, j) => {
        const d = cell(r, c);
        const x = LW + j * CW;
        const rect = el('rect', {
          x: x + 2, y: y + 3, width: CW - 4, height: RH - 6, rx: 4,
          fill: d.value == null ? 'var(--empty)' : ramp(d.value)
        });
        hoverable(rect, d.tip || `<b>${r.label}</b> · ${c.label}<br>${d.value ?? '—'}`);
        svg.appendChild(rect);
        svg.appendChild(el('text', {
          x: x + CW / 2, y: y + RH / 2 + 4, 'text-anchor': 'middle',
          class: d.value != null && d.value >= 62 ? 'viz-cell-on' : 'viz-cell'
        }, d.value == null ? '—' : d.text ?? String(d.value)));
      });
    });
    if (note) caption(host, note);
  }

  // Последовательная шкала: один тон, 100 -> 700. Светлое = меньше.
  const RAMP = ['--q1', '--q2', '--q3', '--q4', '--q5', '--q6', '--q7'];
  function ramp(v) {
    const i = Math.min(RAMP.length - 1, Math.max(0, Math.round((v / 100) * (RAMP.length - 1))));
    return `var(${RAMP[i]})`;
  }

  /* Точечная: связь двух мер. Один ряд и прямые подписи — так identity не
   * держится на цвете и не упирается в предел различимости оттенков. */
  function scatter(host, {title, sub, points, xLabel, yLabel, fmtX, fmtY, note}) {
    const w = hostWidth(host);
    const PAD_L = 54, PAD_R = Math.min(150, w * 0.16), PAD_T = 14, PAD_B = 42;
    const h = 330;
    const svg = frame(host, w, h, title, sub);
    const xs = points.map(p => p.x), ys = points.map(p => p.y);
    const xMax = Math.max(...xs, 1) * 1.1, yMax = Math.max(...ys, 10) * 1.15;
    const X = v => PAD_L + (w - PAD_L - PAD_R) * (v / xMax);
    const Y = v => PAD_T + (h - PAD_T - PAD_B) * (1 - v / yMax);

    for (let f = 0; f <= 1.0001; f += 0.25) {
      const y = Y(yMax * f);
      svg.appendChild(el('line', {x1: PAD_L, y1: y, x2: w - PAD_R, y2: y, class: 'viz-grid'}));
      svg.appendChild(el('text', {x: PAD_L - 8, y: y + 4, 'text-anchor': 'end', class: 'viz-tick'},
        fmtY ? fmtY(yMax * f) : String(Math.round(yMax * f))));
    }
    for (let f = 0; f <= 1.0001; f += 0.25) {
      const x = X(xMax * f);
      svg.appendChild(el('text', {x, y: h - 22, 'text-anchor': 'middle', class: 'viz-tick'},
        fmtX ? fmtX(xMax * f) : String(Math.round(xMax * f))));
    }
    svg.appendChild(el('text', {x: (PAD_L + w - PAD_R) / 2, y: h - 5, 'text-anchor': 'middle', class: 'viz-axis'}, xLabel));
    svg.appendChild(el('text', {x: 12, y: PAD_T + 4, class: 'viz-axis'}, yLabel));

    points.forEach(p => {
      const cx = X(p.x), cy = Y(p.y);
      const dot = el('circle', {cx, cy, r: 7, fill: 'var(--s1)', stroke: 'var(--surface)', 'stroke-width': 2});
      hoverable(dot, p.tip || `<b>${p.label}</b>`);
      svg.appendChild(dot);
      svg.appendChild(el('text', {x: cx + 12, y: cy + 4, class: 'viz-value'}, p.label));
    });
    if (note) caption(host, note);
  }

  function caption(host, text) {
    const c = document.createElement('div');
    c.className = 'viz-note';
    c.innerHTML = text;
    host.appendChild(c);
  }

  return {bars, hstack, stacked, matrix, scatter, caption, CAT, ramp};
})();
