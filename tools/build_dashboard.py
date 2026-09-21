# -*- coding: utf-8 -*-
"""Сборка дашборда: data/*.json -> dashboard.html (один файл, данные внутри).

Данные вшиваются в HTML, потому что file:// запрещает fetch соседних файлов.
Открывается двойным кликом, работает без сервера и без сети.
"""
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def load(name, default=None):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return default if default is not None else {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_snapshots():
    """Снимки по дням, по возрастанию даты. Пустой список — если история ещё не набралась."""
    d = os.path.join(DATA, "snapshots")
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            continue
    return out


HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Портфель проектов</title>
<style>
:root{
  --bg:#f6f7f9; --panel:#ffffff; --panel2:#fbfcfd; --ink:#16191d; --ink2:#5b6470;
  --ink3:#8a929c; --line:#e3e7ec; --line2:#eef1f4;
  --accent:#2f6df6; --ok:#12855b; --warn:#b26a00; --bad:#c62d42; --idle:#8a929c;
  --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.04);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
:root:not([data-theme="light"]) { }
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0e1116; --panel:#161a21; --panel2:#1b2029; --ink:#e8ecf1; --ink2:#a4aeba;
    --ink3:#78828f; --line:#262d38; --line2:#1f2630;
    --accent:#6b9bff; --ok:#3fbd8a; --warn:#e0a44a; --bad:#f2647a; --idle:#78828f;
    --shadow:0 1px 2px rgba(0,0,0,.4);
  }
}
:root[data-theme="dark"]{
  --bg:#0e1116; --panel:#161a21; --panel2:#1b2029; --ink:#e8ecf1; --ink2:#a4aeba;
  --ink3:#78828f; --line:#262d38; --line2:#1f2630;
  --accent:#6b9bff; --ok:#3fbd8a; --warn:#e0a44a; --bad:#f2647a; --idle:#78828f;
  --shadow:0 1px 2px rgba(0,0,0,.4);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1280px;margin:0 auto;padding:28px 16px 80px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

header.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:22px}
h1{font-size:25px;margin:0 0 4px;letter-spacing:-.4px;font-weight:650}
.sub{color:var(--ink2);font-size:13.5px}
.btn{background:var(--panel);border:1px solid var(--line);color:var(--ink2);border-radius:8px;
  padding:7px 12px;font-size:13px;cursor:pointer;font-family:inherit}
.btn:hover{border-color:var(--accent);color:var(--accent)}

.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:22px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:13px 15px;box-shadow:var(--shadow)}
.kpi .v{font-size:23px;font-weight:660;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.kpi .l{font-size:11.5px;color:var(--ink3);text-transform:uppercase;letter-spacing:.5px;margin-top:3px}
.kpi .n{font-size:11.5px;color:var(--ink2);margin-top:5px;line-height:1.35}

.section-h{font-size:12px;text-transform:uppercase;letter-spacing:.9px;color:var(--ink3);
  margin:30px 0 11px;font-weight:620}

.trend{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:15px 17px;
  box-shadow:var(--shadow)}
.trend svg{display:block;width:100%;height:auto;overflow:visible}
.tlegend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--ink2);margin-bottom:10px}
.tlegend i{display:inline-block;width:18px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
.deltas{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:8px;margin-top:14px}
.delta{border:1px solid var(--line2);border-radius:8px;padding:8px 11px;background:var(--panel2);font-size:13px}
.delta .d{font-variant-numeric:tabular-nums;font-weight:620}
.up{color:var(--ok)} .down{color:var(--bad)} .flat{color:var(--ink3)}

.alerts{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--bad);
  border-radius:10px;padding:4px 0;box-shadow:var(--shadow)}
.alert{display:flex;gap:11px;padding:11px 16px;border-bottom:1px solid var(--line2);align-items:flex-start}
.alert:last-child{border-bottom:none}
.alert .sev{font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:5px;white-space:nowrap;margin-top:1px;
  letter-spacing:.3px}
.sev-crit{background:rgba(198,45,66,.13);color:var(--bad)}
.sev-warn{background:rgba(178,106,0,.14);color:var(--warn)}
.alert .txt{font-size:13.5px;line-height:1.5}
.alert .txt b{font-weight:620}
.alert .who{color:var(--ink3);font-size:12px}

table.pf{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);
  border-radius:11px;overflow:hidden;box-shadow:var(--shadow);font-size:13.5px}
table.pf th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--ink3);
  font-weight:600;padding:10px 12px;border-bottom:1px solid var(--line);background:var(--panel2);white-space:nowrap}
table.pf td{padding:11px 12px;border-bottom:1px solid var(--line2);vertical-align:middle}
table.pf tr:last-child td{border-bottom:none}
table.pf tbody tr{cursor:pointer}
table.pf tbody tr:hover{background:var(--panel2)}
td.num{text-align:right;font-variant-numeric:tabular-nums;font-family:var(--mono);font-size:12.5px;white-space:nowrap}
.pname{font-weight:600}
.ptag{color:var(--ink3);font-size:11.5px;font-weight:400;margin-top:1px}

.bar{height:5px;background:var(--line);border-radius:3px;overflow:hidden;min-width:74px;margin-top:4px}
.bar i{display:block;height:100%;border-radius:3px}
.pct{font-variant-numeric:tabular-nums;font-size:12.5px;font-family:var(--mono)}

.chip{display:inline-block;font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid var(--line);
  color:var(--ink2);white-space:nowrap}
.chip.ok{color:var(--ok);border-color:rgba(18,133,91,.35);background:rgba(18,133,91,.08)}
.chip.warn{color:var(--warn);border-color:rgba(178,106,0,.35);background:rgba(178,106,0,.08)}
.chip.bad{color:var(--bad);border-color:rgba(198,45,66,.35);background:rgba(198,45,66,.08)}
.chip.idle{color:var(--idle)}

.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin-bottom:12px;
  box-shadow:var(--shadow);overflow:hidden}
.card>summary{padding:15px 18px;cursor:pointer;list-style:none;display:flex;gap:13px;align-items:center;
  flex-wrap:wrap}
.card>summary::-webkit-details-marker{display:none}
.card>summary:hover{background:var(--panel2)}
.card>summary .arrow{color:var(--ink3);font-size:11px;transition:transform .15s;width:10px}
.card[open]>summary .arrow{transform:rotate(90deg)}
.card>summary h2{font-size:16.5px;margin:0;font-weight:640;letter-spacing:-.2px}
.card>summary .tag{color:var(--ink2);font-size:13px;flex:1;min-width:160px}
.body{padding:2px 18px 18px;border-top:1px solid var(--line2)}

.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:15px}
.block{margin-top:17px}
.block > h3{font-size:11.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--ink3);
  margin:0 0 8px;font-weight:620}
.block p{margin:0 0 8px}
ul.tight{margin:0;padding-left:17px}
ul.tight li{margin-bottom:5px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:5px 13px;font-size:13.5px}
.kv dt{color:var(--ink3);white-space:nowrap}
.kv dd{margin:0}
.mono{font-family:var(--mono);font-size:12.5px}
.note{background:var(--panel2);border:1px solid var(--line2);border-radius:9px;padding:11px 13px;font-size:13.5px}
.verdict{background:var(--panel2);border-left:3px solid var(--accent);border-radius:0 9px 9px 0;
  padding:11px 14px;font-size:14px;line-height:1.55}

table.bl{width:100%;border-collapse:collapse;font-size:13px}
table.bl th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--ink3);
  font-weight:600;padding:6px 9px;border-bottom:1px solid var(--line)}
table.bl td{padding:8px 9px;border-bottom:1px solid var(--line2);vertical-align:top}
table.bl tr:last-child td{border-bottom:none}
.size{display:inline-block;min-width:24px;text-align:center;font-size:10.5px;font-weight:700;padding:2px 5px;
  border-radius:5px;background:var(--line);color:var(--ink2)}
.size.L,.size.XL{background:rgba(178,106,0,.16);color:var(--warn)}
.size.S{background:rgba(18,133,91,.13);color:var(--ok)}

.sec-item{padding:8px 0;border-bottom:1px solid var(--line2);font-size:13.5px}
.sec-item:last-child{border-bottom:none}
.comp{border:1px solid var(--line);border-radius:9px;padding:11px 13px;margin-bottom:8px;background:var(--panel2)}
.comp .nm{font-weight:620;font-size:13.5px}
.comp .ln{font-size:13px;margin-top:4px}
.comp .ln b{color:var(--ink3);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px}

.foot{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);color:var(--ink3);font-size:12.5px}
.foot code{background:var(--panel);border:1px solid var(--line);border-radius:5px;padding:1px 6px;
  font-family:var(--mono);font-size:12px}
@media(max-width:720px){
  .wrap{padding:18px 16px 60px}
  table.pf{display:block;overflow-x:auto}
  h1{font-size:21px}
}
</style>
</head>
<body>
<div class="wrap">
<header class="top">
  <div>
    <h1>Портфель проектов</h1>
    <div class="sub" id="sub"></div>
  </div>
  <button class="btn" id="theme">Тема</button>
</header>

<div class="kpis" id="kpis"></div>
<div id="trendWrap"></div>
<div id="alertsWrap"></div>
<div class="section-h">Сводка</div>
<div id="tableWrap"></div>
<div class="section-h">Проекты подробно</div>
<div id="cards"></div>
<div id="commentsWrap"></div>

<div class="foot" id="foot"></div>
</div>

<script id="DATA" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('DATA').textContent);
const P = D.projects.projects, U = D.usage.projects, R = D.repos.repos, E = D.estimates.projects, C = D.competitors.projects;

const nf = n => (n==null||isNaN(n)) ? '—' : Math.round(n).toLocaleString('ru-RU').replace(/ /g,' ');
const money = n => (n==null||isNaN(n)) ? '—' : '$' + Math.round(n).toLocaleString('ru-RU').replace(/ /g,' ');
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const tok = n => n>=1e9 ? (n/1e9).toFixed(2).replace('.',',')+' млрд' : n>=1e6 ? (n/1e6).toFixed(0)+' млн' : nf(n);

// usage.json строится по имени каталога — сопоставляем
function usageOf(key){
  const path = (P[key].path||'').toLowerCase();
  const base = path.replace(/\/+$/,'').split('/').pop();
  for (const c of [key, key.toLowerCase(), base, 'doc:'+base, 'doc:'+base.replace(/_/g,'-')])
    if (U[c]) return U[c];
  return null;
}
function color(p){ return p>=70?'var(--ok)': p>=40?'var(--warn)':'var(--bad)'; }
function bar(p){ return `<div class="bar"><i style="width:${Math.max(2,p)}%;background:${color(p)}"></i></div>`; }

/* ---------- KPI ---------- */
const keys = Object.keys(P);
const own = keys.filter(k => P[k].tier < 4);
let spent=0, spentTok=0, rem=0, remTok=0, disk=0, loc=0, tasks=0, sess=0;
keys.forEach(k=>{ const u=usageOf(k); if(u){spent+=u.usd; spentTok+=u.billable; sess+=u.sessions;}
  const e=E[k]; if(e){rem+=e.remaining_usd; remTok+=e.remaining_tokens; tasks+=e.items.length;}
  const r=R[k]; if(r&&r.exists){disk+=r.disk_bytes||0; loc+=(r.code&&r.code.loc_total)||0;} });

document.getElementById('sub').textContent =
  `${own.length} собственных проектов · данные собраны ${D.generated_at.slice(0,16).replace('T',' ')} UTC`;

const months = new Set();
keys.forEach(k=>{const u=usageOf(k); if(u) Object.keys(u.by_month||{}).forEach(m=>months.add(m));});
const perMonth = months.size ? spent/months.size : 0;

document.getElementById('kpis').innerHTML = [
  ['Потрачено', money(spent), tok(spentTok)+' токенов · '+sess+' сессий'],
  ['Осталось', money(rem), tok(remTok)+' токенов · '+tasks+' задач в бэклоге'],
  ['В месяц', money(perMonth), months.size+' мес. активности · ориентир отрасли $150–250'],
  ['Кода', nf(loc), 'строк в '+own.length+' проектах'],
  ['На диске', (disk/1e9).toFixed(1).replace('.',',')+' ГБ', 'свободно на C: — 27 ГБ'],
].map(([l,v,n])=>`<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div><div class="n">${n}</div></div>`).join('');

/* ---------- Динамика: строится из снимков, а не из текущего состояния ---------- */
const SN = D.snapshots || [];
(function trends(){
  const el = document.getElementById('trendWrap');

  // Расход по месяцам — реальная история из журналов сессий, доступна сразу.
  const months = [...new Set(keys.flatMap(k=>Object.keys(usageOf(k)?.by_month||{})))].filter(m=>m!=='unknown').sort();
  if(!months.length){ el.innerHTML=''; return; }

  // проекты, на которые пришлось заметно денег — остальные в «прочее»
  const tot = k => usageOf(k)?.usd || 0;
  const top = keys.filter(k=>tot(k)>0).sort((a,b)=>tot(b)-tot(a));
  const named = top.slice(0,4), rest = top.slice(4);
  const palette = ['var(--accent)','var(--ok)','var(--warn)','var(--bad)','var(--ink3)'];
  const rows = named.map((k,i)=>({name:P[k].name, c:palette[i],
      v:months.map(m=>(usageOf(k).by_month[m]||{}).usd||0)}));
  if(rest.length) rows.push({name:'прочее', c:palette[4],
      v:months.map(m=>rest.reduce((s,k)=>s+((usageOf(k).by_month[m]||{}).usd||0),0))});

  const W=1000, H=210, PL=52, PR=14, PT=12, PB=28;
  const colTot = months.map((_,i)=>rows.reduce((s,r)=>s+r.v[i],0));
  const max = Math.max(...colTot, 1);
  const bw = (W-PL-PR)/months.length;
  const Y = v => PT + (H-PT-PB) * (1 - v/max);

  const grid = [0,.25,.5,.75,1].map(f=>{
    const y = Y(max*f);
    return `<line x1="${PL}" y1="${y.toFixed(1)}" x2="${W-PR}" y2="${y.toFixed(1)}" stroke="var(--line)" stroke-width="1"/>
      <text x="${PL-8}" y="${(y+4).toFixed(1)}" text-anchor="end" font-size="11" fill="var(--ink3)">${money(max*f)}</text>`;
  }).join('');

  const bars = months.map((m,i)=>{
    let acc=0;
    const x = PL + i*bw + bw*0.22, w = bw*0.56;
    const seg = rows.map(r=>{
      const v=r.v[i]; if(v<=0) return '';
      const y0=Y(acc), y1=Y(acc+v); acc+=v;
      return `<rect x="${x.toFixed(1)}" y="${y1.toFixed(1)}" width="${w.toFixed(1)}" height="${Math.max(0,y0-y1).toFixed(1)}" fill="${r.c}" rx="1"><title>${esc(r.name)} — ${m}: ${money(v)}</title></rect>`;
    }).join('');
    return seg + `<text x="${(x+w/2).toFixed(1)}" y="${Y(acc)-6}" text-anchor="middle" font-size="11" fill="var(--ink2)">${money(colTot[i])}</text>` +
      `<text x="${(x+w/2).toFixed(1)}" y="${H-8}" text-anchor="middle" font-size="11" fill="var(--ink3)">${m}</text>`;
  }).join('');

  // дельты между снимками — появляются со второго
  let deltaBlock = '';
  if(SN.length >= 2){
    const a=SN[0], b=SN[SN.length-1], ds=[];
    Object.keys(b.projects||{}).forEach(k=>{
      const pa=(a.projects||{})[k], pb=b.projects[k]; if(!pa||!pb||!P[k]) return;
      const df=(pb.functional||0)-(pa.functional||0), dn=(pb.backlog_items||0)-(pa.backlog_items||0);
      if(df||dn) ds.push({name:P[k].name, df, dn});
    });
    deltaBlock = ds.length
      ? `<div class="deltas">${ds.map(d=>`<div class="delta">${esc(d.name)}<br>
          ${d.df?`<span class="d ${d.df>0?'up':'down'}">${d.df>0?'+':''}${d.df} п.п. готовности</span>`:''}
          ${d.df&&d.dn?' · ':''}
          ${d.dn?`<span class="d ${d.dn>0?'down':'up'}">${d.dn>0?'+':''}${d.dn} задач в бэклоге</span>`:''}
        </div>`).join('')}</div>`
      : `<div class="note" style="margin-top:13px">Между снимками ${esc(a.date)} и ${esc(b.date)} готовность и бэклог не менялись.</div>`;
  } else {
    deltaBlock = `<div class="note" style="margin-top:13px">Снимков пока ${SN.length} — изменения готовности и бэклога
      появятся здесь со второго. Снимок сохраняется при каждом
      <code style="font-family:var(--mono)">pm.py refresh</code> в <code style="font-family:var(--mono)">data/snapshots/</code>.</div>`;
  }

  el.innerHTML = `<div class="section-h">Динамика — расход по месяцам</div>
    <div class="trend">
      <div class="tlegend">${rows.map(r=>`<span><i style="background:${r.c}"></i>${esc(r.name)}</span>`).join('')}</div>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Расход по месяцам с разбивкой по проектам">${grid}${bars}</svg>
      ${deltaBlock}
    </div>`;
})();

/* ---------- Тревоги: считаются из данных, не пишутся руками ---------- */
const alerts=[];
keys.forEach(k=>{
  const p=P[k], r=R[k]||{}, u=usageOf(k);
  if(p.tier>=4) return;
  if(r.exists && !r.git && (r.code?.loc_total||0)>500){
    const gb = r.disk_bytes/1e9;
    const size = gb>=0.5 ? ` и ${gb.toFixed(1).replace('.',',')} ГБ данных` : '';
    alerts.push(['crit',`<b>${esc(p.name)}</b> — нет git. ${nf(r.code.loc_total)} строк кода${size} существуют в одном экземпляре: истории нет, откатиться некуда.`,k]);
  }
  (p.security?.open_critical||[]).forEach(s=>alerts.push(['crit',`<b>${esc(p.name)}</b> — ${esc(s.split('—')[0].trim())}: ${esc(s.split('—').slice(1).join('—').trim())}`,k]));
  (r.secrets_suspect||[]).filter(s=>!s.file.includes('.claude/worktrees')).forEach(s=>
    alerts.push(['warn',`<b>${esc(p.name)}</b> — возможный секрет в коде: <span class="mono">${esc(s.file)}</span> (${esc(s.kind)})`,k]));
  (r.weird_paths||[]).forEach(w=>
    alerts.push(['warn',`<b>${esc(p.name)}</b> — мусорный файл с зарезервированным именем Windows: <span class="mono">${esc(w)}</span>`,k]));
  if(r.exists && r.hygiene && !r.hygiene.ci && (r.code?.loc_total||0)>20000)
    alerts.push(['warn',`<b>${esc(p.name)}</b> — ${nf(r.code.loc_total)} строк кода без CI.`,k]);
  if(r.git && r.git.dirty_files>0)
    alerts.push(['warn',`<b>${esc(p.name)}</b> — ${r.git.dirty_files} незакоммиченных файлов в рабочем дереве.`,k]);
});
if(alerts.length) document.getElementById('alertsWrap').innerHTML =
  `<div class="section-h">Требует внимания — ${alerts.length}</div><div class="alerts">` +
  alerts.sort((a,b)=>a[0]==='crit'?-1:1).map(([s,t])=>
    `<div class="alert"><span class="sev sev-${s}">${s==='crit'?'КРИТ':'ВНИМ'}</span><div class="txt">${t}</div></div>`).join('') +
  `</div>`;

/* ---------- Таблица ---------- */
const order = keys.slice().sort((a,b)=> (P[a].tier-P[b].tier) || ((usageOf(b)?.usd||0)-(usageOf(a)?.usd||0)));
document.getElementById('tableWrap').innerHTML = `<table class="pf"><thead><tr>
<th>Проект</th><th>Цель</th><th>Готовность</th><th>ИБ</th><th class="num">Потрачено</th>
<th class="num">Осталось</th><th class="num">Строк</th><th class="num">Коммитов</th><th>LLM</th><th>Последнее</th>
</tr></thead><tbody>` + order.map(k=>{
  const p=P[k], u=usageOf(k), r=R[k]||{}, e=E[k]||{}, rd=p.readiness||{};
  const g=r.git||{};
  return `<tr data-k="${k}">
   <td><div class="pname">${esc(p.name)}</div><div class="ptag">${esc(p.status)}</div></td>
   <td>${esc(p.goal||'')}</td>
   <td><span class="pct">${rd.functional??'—'}%</span>${bar(rd.functional||0)}</td>
   <td><span class="chip ${(rd.security||0)>=70?'ok':(rd.security||0)>=40?'warn':'bad'}">${rd.security??'—'}%</span></td>
   <td class="num">${u?money(u.usd):'—'}</td>
   <td class="num">${e.remaining_usd?money(e.remaining_usd):'—'}</td>
   <td class="num">${r.code?nf(r.code.loc_total):'—'}</td>
   <td class="num">${g.commits!=null?g.commits:'<span style="color:var(--bad)">нет git</span>'}</td>
   <td>${p.llm&&p.llm.used?'<span class="chip ok">да</span>':'<span class="chip idle">—</span>'}</td>
   <td class="mono" style="color:var(--ink3)">${esc(g.last_commit||(u?u.last_seen.slice(0,10):'—'))}</td>
  </tr>`;
}).join('') + `</tbody></table>`;

/* ---------- Карточки ---------- */
function list(arr, cls){ return !arr||!arr.length ? '' :
  `<ul class="tight ${cls||''}">`+arr.map(x=>`<li>${esc(x)}</li>`).join('')+`</ul>`; }

document.getElementById('cards').innerHTML = order.map(k=>{
  const p=P[k], u=usageOf(k), r=R[k]||{}, e=E[k]||{}, c=C[k]||{}, rd=p.readiness||{}, g=r.git||{};
  const sec=p.security||{}, ar=p.architecture||{}, cm=p.compute||{}, llm=p.llm||{};

  const readRows = [['Функционально','functional'],['Эксплуатация','production'],['Документация','docs'],
    ['Тесты','tests'],['Безопасность','security']].map(([l,f])=>
    `<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
      <div style="width:112px;color:var(--ink3);font-size:12.5px">${l}</div>
      <div style="flex:1">${bar(rd[f]||0)}</div>
      <div class="pct" style="width:38px;text-align:right">${rd[f]??'—'}%</div></div>`).join('');

  const backlog = (e.items||[]).length ? `<table class="bl"><thead><tr>
    <th>#</th><th>Задача</th><th>Разм.</th><th>Статус</th><th style="text-align:right">Токенов</th><th style="text-align:right">$</th>
    </tr></thead><tbody>` + e.items.map(b=>`<tr>
      <td class="mono" style="white-space:nowrap">${esc(b.priority)} ${esc(b.id)}</td>
      <td>${esc(b.title)}${b.why?`<div style="color:var(--ink3);font-size:12px;margin-top:3px">${esc(b.why)}</div>`:''}</td>
      <td><span class="size ${b.size}">${b.size}</span></td>
      <td><span class="chip ${b.blocked?'bad':b.status==='done'?'ok':'idle'}">${esc(b.status||'—')}</span>${b.days?`<div style="color:var(--ink3);font-size:11.5px;margin-top:3px">${esc(b.days)}</div>`:''}</td>
      <td class="num">${tok(b.est_tokens)}</td><td class="num">${money(b.est_usd)}</td></tr>`).join('') +
    `</tbody></table><div style="margin-top:9px;font-size:12.5px;color:var(--ink3)">
      Итого по бэклогу: <b style="color:var(--ink)">${tok(e.remaining_tokens)} токенов · ${money(e.remaining_usd)}</b>.
      Оценка по ${esc(e.rate_basis)} — ${nf(e.tokens_per_commit)} токенов (${money(e.usd_per_commit)}) на коммит.</div>` : '<p style="color:var(--ink3)">Бэклог пуст.</p>';

  const secOpen = [].concat(sec.open_critical||[], sec.open_high||[], sec.open||[]);
  const competitors = (c.items||[]).map(it=>`<div class="comp">
      <div class="nm">${esc(it.name)}${it.url?` <a href="${esc(it.url)}" target="_blank" rel="noopener" style="font-weight:400;font-size:12px">↗</a>`:''}</div>
      <div class="ln">${esc(it.what)}</div>
      <div class="ln"><b>Сильная сторона:</b> ${esc(it.strength)}</div>
      <div class="ln"><b>Чего не делает:</b> ${esc(it.gap)}</div>
      ${it.note?`<div class="ln" style="color:var(--ink3)">${esc(it.note)}</div>`:''}
    </div>`).join('');

  return `<details class="card" id="p-${k}"><summary>
    <span class="arrow">▶</span>
    <h2>${esc(p.name)}</h2>
    <span class="tag">${esc(p.tagline)}</span>
    <span class="chip">${esc(p.goal||'')}</span>
    <span class="chip ${rd.functional>=70?'ok':rd.functional>=40?'warn':'bad'}">${rd.functional??'—'}% готово</span>
    ${u?`<span class="chip">${money(u.usd)}</span>`:''}
  </summary><div class="body">

  <div class="grid2">
    <div class="block"><h3>Цель</h3><p>${esc(p.goal_long||'')}</p></div>
    <div class="block"><h3>Готовность</h3>${readRows}
      <div style="color:var(--ink3);font-size:12px;margin-top:7px">${esc(rd.basis||'')}</div></div>
  </div>

  <div class="block"><h3>Что сделано</h3>${list(p.done)}</div>

  <div class="grid2">
    <div class="block"><h3>Архитектура</h3><dl class="kv">
      ${Object.entries(ar).map(([kk,v])=>`<dt>${esc({style:'Стиль',backend:'Бэкенд',frontend:'Фронтенд',stack:'Стек',modules:'Модули',packages:'Пакеты',facts:'Факты',rbac:'Доступ',guard:'Сверка',docs:'Документы',data:'Данные',deploy:'Публикация',state:'Состояние',notes:'Заметки',quality_gate:'Гейт качества',device_profile:'Профиль устройства'}[kk]||kk)}</dt><dd>${esc(v)}</dd>`).join('')}
    </dl></div>
    <div class="block"><h3>Мощности и LLM</h3><dl class="kv">
      ${Object.entries(cm).map(([kk,v])=>`<dt>${esc({where:'Где',containers:'Контейнеры',limits:'Лимиты',gpu:'GPU',disk:'Диск',notes:'Заметки',target:'Целевое'}[kk]||kk)}</dt><dd>${esc(v)}</dd>`).join('')}
      ${llm.used?Object.entries(llm).filter(([kk])=>kk!=='used').map(([kk,v])=>`<dt>${esc({runtime:'Рантайм',active_model:'Модель',catalog:'Каталог',pipeline:'Конвейер',role:'Роль',models:'Модели',note:'Примечание'}[kk]||kk)}</dt><dd>${esc(v)}</dd>`).join(''):`<dt>LLM</dt><dd>не используется${llm.note?' — '+esc(llm.note):''}</dd>`}
    </dl></div>
  </div>

  <div class="block"><h3>Безопасность</h3>
    <div class="note" style="margin-bottom:10px">${esc(sec.verdict||'')}</div>
    ${secOpen.length?`<div>${secOpen.map(s=>`<div class="sec-item">${esc(s)}</div>`).join('')}</div>`:''}
    ${sec.open_medium_low?`<div style="margin-top:9px;color:var(--ink2);font-size:13px">${esc(sec.open_medium_low)}</div>`:''}
    ${sec.good&&sec.good!=='—'?`<div style="margin-top:11px"><b style="font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--ok)">Сделано правильно</b><div style="font-size:13.5px;margin-top:4px">${esc(sec.good)}</div></div>`:''}
    ${sec.audit?`<div style="margin-top:9px;color:var(--ink3);font-size:12.5px">Источник: <span class="mono">${esc(sec.audit)}</span></div>`:''}
  </div>

  <div class="block"><h3>Бэклог и оценка затрат</h3>${backlog}</div>

  <div class="block"><h3>Что делать дальше</h3>${list(p.next_actions)}</div>

  <div class="block"><h3>Технический срез</h3><dl class="kv">
    <dt>Строк кода</dt><dd>${r.code?nf(r.code.loc_total):'—'}${r.code&&Object.keys(r.code.loc).length?' — '+Object.entries(r.code.loc).slice(0,4).map(([l,n])=>`${l} ${nf(n)}`).join(', '):''}</dd>
    <dt>Файлов / тестовых</dt><dd>${r.code?nf(r.code.files_total)+' / '+r.code.test_files:'—'}</dd>
    <dt>Документация</dt><dd>${r.code?r.code.doc_files+' файлов, '+nf(r.code.doc_lines)+' строк':'—'}</dd>
    <dt>TODO/FIXME</dt><dd>${r.code?r.code.todo_markers:'—'}</dd>
    <dt>Git</dt><dd>${g.commits!=null?`${g.commits} коммитов, ${g.branches} веток, ветка <span class="mono">${esc(g.branch)}</span>, ${g.first_commit} → ${g.last_commit}${g.dirty_files?`, <b style="color:var(--warn)">${g.dirty_files} незакоммичено</b>`:''}`:'<b style="color:var(--bad)">репозитория нет</b>'}</dd>
    ${g.last_subject?`<dt>Последний коммит</dt><dd>${esc(g.last_subject)}</dd>`:''}
    <dt>На диске</dt><dd>${r.disk_bytes?(r.disk_bytes/1e9).toFixed(2).replace('.',',')+' ГБ':'—'}</dd>
    <dt>Гигиена</dt><dd>${r.hygiene?Object.entries({gitignore:'.gitignore',readme:'README',license:'LICENSE',ci:'CI',docker:'Docker',lockfile:'lock-файл',env_example:'.env.example',claude_md:'CLAUDE.md'}).map(([f,l])=>`<span class="chip ${r.hygiene[f]?'ok':'idle'}" style="margin:0 3px 3px 0">${l}${r.hygiene[f]?'':' нет'}</span>`).join(''):'—'}</dd>
    ${u?`<dt>Токены</dt><dd class="mono">вход ${nf(u.input)} · выход ${nf(u.output)} · запись кеша ${nf(u.cache_write)} · чтение кеша ${nf(u.cache_read)} · размышление ${nf(u.thinking)}</dd>
    <dt>Модели</dt><dd class="mono">${Object.entries(u.by_model).filter(([m])=>!m.startsWith('<')).map(([m,v])=>`${m} — ${nf(v.requests)} запр., ${money(v.usd)}`).join(' · ')}</dd>
    <dt>По месяцам</dt><dd class="mono">${Object.entries(u.by_month).map(([m,v])=>`${m}: ${money(v.usd)}`).join(' · ')}</dd>`:''}
  </dl></div>

  ${(c.items||[]).length?`<div class="block"><h3>Конкуренты</h3>
    ${c.market?`<div class="note" style="margin-bottom:11px">${esc(c.market)}</div>`:''}
    ${competitors}
    ${(c.advantage||[]).length?`<div style="margin-top:13px"><b style="font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--ok)">В чём этот проект превосходит</b>${list(c.advantage)}</div>`:''}
    ${(c.threats||[]).length?`<div style="margin-top:11px"><b style="font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--bad)">Чем они сильнее</b>${list(c.threats)}</div>`:''}
    ${c.benchmark?`<div class="note" style="margin-top:11px">${esc(c.benchmark)}</div>`:''}
    ${c.verdict?`<div class="verdict" style="margin-top:11px">${esc(c.verdict)}</div>`:''}
  </div>`:''}

  ${p.assessment&&p.assessment.verdict!=='—'?`<div class="block"><h3>Оценка</h3>
    ${p.assessment.strength&&p.assessment.strength!=='—'?`<p><b>Сильно.</b> ${esc(p.assessment.strength)}</p>`:''}
    ${p.assessment.weakness&&p.assessment.weakness!=='—'?`<p><b>Слабо.</b> ${esc(p.assessment.weakness)}</p>`:''}
    <div class="verdict">${esc(p.assessment.verdict)}</div></div>`:''}

  </div></details>`;
}).join('');

document.querySelectorAll('table.pf tbody tr').forEach(tr=>tr.onclick=()=>{
  const d=document.getElementById('p-'+tr.dataset.k);
  d.open=true; d.scrollIntoView({behavior:'smooth',block:'start'});
});

/* ---------- Комментарии ---------- */
const CM = D.comments.comments||[];
document.getElementById('commentsWrap').innerHTML = `<div class="section-h">Комментарии — ${CM.length}</div>` +
 (CM.length ? `<div class="alerts" style="border-left-color:var(--accent)">` + CM.map(c=>`<div class="alert">
    <span class="sev ${c.status==='processed'?'sev-warn':'sev-crit'}" style="${c.status==='processed'?'background:rgba(18,133,91,.13);color:var(--ok)':''}">${c.status==='processed'?'В БЭКЛОГЕ':'НОВЫЙ'}</span>
    <div class="txt">${esc(c.text)}<div class="who">${esc(c.project||'портфель')} · ${esc(c.kind||'комментарий')} · ${esc(c.date||'')}${c.moved_to?' → '+esc(c.moved_to):''}</div></div></div>`).join('') + `</div>`
  : `<div class="note">Комментариев нет. Добавить: <code>py tools/pm.py comment &lt;проект&gt; "текст"</code> — при следующем обновлении он станет задачей бэклога.</div>`);

document.getElementById('foot').innerHTML =
  `Данные: <code>data/projects.json</code> (ручной источник истины) · <code>usage.json</code> (${nf(D.usage.scan.deduped_rows)} ответов из ${D.usage.scan.files} журналов) · ` +
  `<code>repos.json</code> · <code>estimates.json</code> · <code>competitors.json</code> · <code>comments.json</code>.<br>` +
  `Обновление: <code>py tools/pm.py refresh</code>. Конкуренты исследованы ${esc(D.competitors.researched)}. ` +
  `Деньги — эквивалент по прайсу Anthropic API (Opus 5 $5/$25, Sonnet 5 $2/$10 за 1M; запись кеша 1.25× 5m / 2× 1h, чтение 0.1×), а не счёт подписки.`;

const t=document.getElementById('theme');
t.onclick=()=>{ const cur=document.documentElement.getAttribute('data-theme');
  const next = cur==='dark'?'light':cur==='light'?'dark':(matchMedia('(prefers-color-scheme: dark)').matches?'light':'dark');
  document.documentElement.setAttribute('data-theme',next);
  try{localStorage.setItem('tt-theme',next)}catch(e){} };
try{ const s=localStorage.getItem('tt-theme'); if(s) document.documentElement.setAttribute('data-theme',s); }catch(e){}
</script>
</body>
</html>
"""


def main():
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "projects": load("projects.json"),
        "usage": load("usage.json"),
        "repos": load("repos.json"),
        "estimates": load("estimates.json"),
        "competitors": load("competitors.json"),
        "comments": load("comments.json", {"comments": []}),
        "snapshots": load_snapshots(),
    }
    blob = json.dumps(payload, ensure_ascii=False).replace("</script>", "<\\/script>")
    html = HTML.replace("__DATA__", blob)
    # Аргументом можно задать другой путь — так проверки собирают во временный файл,
    # не трогая закоммиченный dashboard.html.
    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dashboard.html")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(html)
    print("дашборд собран: %s (%.0f КБ)" % (dest, os.path.getsize(dest) / 1024))


if __name__ == "__main__":
    main()
