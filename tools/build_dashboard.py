# -*- coding: utf-8 -*-
"""Сборка дашборда: data/*.json -> dashboard.html (один файл, данные внутри).

Данные вшиваются в HTML, потому что file:// запрещает fetch соседних файлов.
Открывается двойным кликом, работает без сервера и без сети.

Аргументом можно задать другой путь вывода — так проверки собирают во временный
файл, не трогая закоммиченный dashboard.html.
"""
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
TOOLS = os.path.join(ROOT, "tools")


def load(name, default=None):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return default if default is not None else {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_snapshots():
    """Снимки по дням, по возрастанию даты. Пустой список — если истории ещё нет."""
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


def read_tool(name):
    with open(os.path.join(TOOLS, name), encoding="utf-8") as f:
        return f.read()


CSS = r"""
:root{
  --bg:#f9f9f7; --panel:#ffffff; --panel2:#fbfcfd; --ink:#0b0b0b; --ink2:#52514e;
  --ink3:#8a8a86; --line:#e3e7ec; --line2:#eef1f4; --surface:#fcfcfb;
  --accent:#2a78d6; --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.04);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  /* Категориальные слоты — фиксированный порядок, проверенный на различимость
     при цветовой слепоте. Девятого слота нет: девятый ряд идёт в «прочее». */
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;
  --s5:#e87ba4; --s6:#008300; --s7:#4a3aa7; --s8:#e34948;
  /* Последовательная шкала: один тон, светлое = near zero. */
  --q1:#cde2fb; --q2:#9ec5f4; --q3:#6da7ec; --q4:#3987e5;
  --q5:#256abf; --q6:#184f95; --q7:#0d366b;
  --empty:#f0efec; --cell-ink-low:#0b0b0b; --cell-ink-high:#ffffff;
  /* Статус — зарезервирован, рядом всегда стоит подпись, а не только цвет. */
  --st-ok:#0ca30c; --st-watch:#fab219; --st-act:#d03b3b; --st-serious:#ec835a;
  --ok:#0ca30c; --warn:#b26a00; --bad:#d03b3b; --idle:#8a8a86;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0d0d0d; --panel:#161a21; --panel2:#1b2029; --ink:#ffffff; --ink2:#c3c2b7;
    --ink3:#78828f; --line:#262d38; --line2:#1f2630; --surface:#1a1a19;
    --accent:#3987e5; --shadow:0 1px 2px rgba(0,0,0,.4);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
    --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
    --q1:#0d366b; --q2:#184f95; --q3:#256abf; --q4:#3987e5;
    --q5:#5598e7; --q6:#86b6ef; --q7:#cde2fb;
    --empty:#383835; --cell-ink-low:#e8ecf1; --cell-ink-high:#0b0b0b;
    --ok:#3fbd8a; --warn:#e0a44a; --bad:#f2647a; --idle:#78828f;
  }
}
:root[data-theme="dark"]{
  --bg:#0d0d0d; --panel:#161a21; --panel2:#1b2029; --ink:#ffffff; --ink2:#c3c2b7;
  --ink3:#78828f; --line:#262d38; --line2:#1f2630; --surface:#1a1a19;
  --accent:#3987e5; --shadow:0 1px 2px rgba(0,0,0,.4);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
  --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
  --q1:#0d366b; --q2:#184f95; --q3:#256abf; --q4:#3987e5;
  --q5:#5598e7; --q6:#86b6ef; --q7:#cde2fb;
  --empty:#383835; --cell-ink-low:#e8ecf1; --cell-ink-high:#0b0b0b;
  --ok:#3fbd8a; --warn:#e0a44a; --bad:#f2647a; --idle:#78828f;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1280px;margin:0 auto;padding:26px 16px 80px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

header.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:25px;margin:0 0 4px;letter-spacing:-.4px;font-weight:650}
.sub{color:var(--ink2);font-size:13.5px}
.btn{background:var(--panel);border:1px solid var(--line);color:var(--ink2);border-radius:8px;
  padding:7px 12px;font-size:13px;cursor:pointer;font-family:inherit}
.btn:hover{border-color:var(--accent);color:var(--accent)}

/* вкладки */
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:20px}
.tab{background:none;border:none;border-bottom:2px solid transparent;color:var(--ink2);
  font:inherit;font-size:14.5px;padding:9px 15px;cursor:pointer;margin-bottom:-1px;border-radius:6px 6px 0 0}
.tab:hover{color:var(--ink);background:var(--panel2)}
.tab[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
.page[hidden]{display:none}

.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:10px;margin-bottom:20px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:13px 15px;box-shadow:var(--shadow)}
.kpi .v{font-size:23px;font-weight:660;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.kpi .l{font-size:11.5px;color:var(--ink3);text-transform:uppercase;letter-spacing:.5px;margin-top:3px}
.kpi .n{font-size:11.5px;color:var(--ink2);margin-top:5px;line-height:1.35}

.section-h{font-size:12px;text-transform:uppercase;letter-spacing:.9px;color:var(--ink3);
  margin:28px 0 11px;font-weight:620}

/* --- диаграммы --- */
.card-viz{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px 14px;box-shadow:var(--shadow);margin-bottom:12px}
.viz-title{font-size:15.5px;font-weight:620;letter-spacing:-.15px;margin-bottom:3px}
.viz-sub{font-size:12.5px;color:var(--ink2);margin-bottom:11px;line-height:1.45}
.card-viz svg{display:block;width:100%;height:auto;overflow:visible}
.viz-grid{stroke:var(--line);stroke-width:1}
.viz-tick{font-size:11px;fill:var(--ink3)}
.viz-axis{font-size:11px;fill:var(--ink3)}
.viz-label{font-size:12.5px;fill:var(--ink2)}
.viz-value{font-size:12px;fill:var(--ink2);font-variant-numeric:tabular-nums}
.viz-cell{font-size:12px;fill:var(--cell-ink-low);font-variant-numeric:tabular-nums}
.viz-cell-on{font-size:12px;fill:var(--cell-ink-high);font-variant-numeric:tabular-nums}
.viz-legend{display:flex;gap:15px;flex-wrap:wrap;font-size:12px;color:var(--ink2);margin-top:9px}
.viz-legend i{display:inline-block;width:16px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
.viz-note{margin-top:11px;padding-top:10px;border-top:1px solid var(--line2);
  font-size:12.5px;color:var(--ink2);line-height:1.5}
.viz-tip{position:fixed;z-index:99;pointer-events:none;opacity:0;transition:opacity .1s;
  background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 11px;
  font-size:12.5px;line-height:1.45;color:var(--ink);box-shadow:0 4px 14px rgba(0,0,0,.18);max-width:320px}
.viz-tip.on{opacity:1}
.grid-viz{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:12px}

/* --- сигналы --- */
.signals{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:10px}
.sig{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:13px 15px;
  box-shadow:var(--shadow);border-left:3px solid var(--idle)}
.sig.act{border-left-color:var(--st-act)}
.sig.watch{border-left-color:var(--st-watch)}
.sig.ok{border-left-color:var(--st-ok)}
.sig .hd{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.sig .nm{font-size:13.5px;font-weight:620}
.sig .val{font-size:19px;font-weight:660;font-variant-numeric:tabular-nums;letter-spacing:-.3px}
.sig .lv{font-size:10px;font-weight:700;letter-spacing:.4px;padding:2px 7px;border-radius:5px;text-transform:uppercase}
.lv.act{background:rgba(208,59,59,.14);color:var(--st-act)}
.lv.watch{background:rgba(250,178,25,.18);color:var(--warn)}
.lv.ok{background:rgba(12,163,12,.13);color:var(--ok)}
.sig .what{font-size:12.5px;color:var(--ink);margin-top:7px;line-height:1.45}
.sig .why{font-size:12px;color:var(--ink3);margin-top:6px;line-height:1.45}

.alerts{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--bad);
  border-radius:10px;padding:4px 0;box-shadow:var(--shadow)}
.alert{display:flex;gap:11px;padding:11px 16px;border-bottom:1px solid var(--line2);align-items:flex-start}
.alert:last-child{border-bottom:none}
.alert .sev{font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:5px;white-space:nowrap;margin-top:1px;letter-spacing:.3px}
.sev-crit{background:rgba(208,59,59,.14);color:var(--bad)}
.sev-warn{background:rgba(250,178,25,.18);color:var(--warn)}
.alert .txt{font-size:13.5px;line-height:1.5}
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

.chip{display:inline-block;font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid var(--line);color:var(--ink2);white-space:nowrap}
.chip.ok{color:var(--ok);border-color:rgba(12,163,12,.35);background:rgba(12,163,12,.08)}
.chip.warn{color:var(--warn);border-color:rgba(250,178,25,.35);background:rgba(250,178,25,.10)}
.chip.bad{color:var(--bad);border-color:rgba(208,59,59,.35);background:rgba(208,59,59,.08)}
.chip.idle{color:var(--idle)}

.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin-bottom:12px;box-shadow:var(--shadow);overflow:hidden}
.card>summary{padding:15px 18px;cursor:pointer;list-style:none;display:flex;gap:13px;align-items:center;flex-wrap:wrap}
.card>summary::-webkit-details-marker{display:none}
.card>summary:hover{background:var(--panel2)}
.card>summary .arrow{color:var(--ink3);font-size:11px;transition:transform .15s;width:10px}
.card[open]>summary .arrow{transform:rotate(90deg)}
.card>summary h2{font-size:16.5px;margin:0;font-weight:640;letter-spacing:-.2px}
.card>summary .tag{color:var(--ink2);font-size:13px;flex:1;min-width:160px}
.body{padding:2px 18px 18px;border-top:1px solid var(--line2)}

.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:15px}
.block{margin-top:17px}
.block>h3{font-size:11.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--ink3);margin:0 0 8px;font-weight:620}
.block p{margin:0 0 8px}
ul.tight{margin:0;padding-left:17px}
ul.tight li{margin-bottom:5px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:5px 13px;font-size:13.5px}
.kv dt{color:var(--ink3);white-space:nowrap}
.kv dd{margin:0}
.mono{font-family:var(--mono);font-size:12.5px}
.note{background:var(--panel2);border:1px solid var(--line2);border-radius:9px;padding:11px 13px;font-size:13.5px}
.verdict{background:var(--panel2);border-left:3px solid var(--accent);border-radius:0 9px 9px 0;padding:11px 14px;font-size:14px;line-height:1.55}

table.bl{width:100%;border-collapse:collapse;font-size:13px}
table.bl th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--ink3);font-weight:600;padding:6px 9px;border-bottom:1px solid var(--line)}
table.bl td{padding:8px 9px;border-bottom:1px solid var(--line2);vertical-align:top}
table.bl tr:last-child td{border-bottom:none}
.size{display:inline-block;min-width:24px;text-align:center;font-size:10.5px;font-weight:700;padding:2px 5px;border-radius:5px;background:var(--line);color:var(--ink2)}
.size.L,.size.XL{background:rgba(250,178,25,.18);color:var(--warn)}
.size.S{background:rgba(12,163,12,.13);color:var(--ok)}

.sec-item{padding:8px 0;border-bottom:1px solid var(--line2);font-size:13.5px}
.sec-item:last-child{border-bottom:none}
.comp{border:1px solid var(--line);border-radius:9px;padding:11px 13px;margin-bottom:8px;background:var(--panel2)}
.comp .nm{font-weight:620;font-size:13.5px}
.comp .ln{font-size:13px;margin-top:4px}
.comp .ln b{color:var(--ink3);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px}

.qbreak{display:grid;grid-template-columns:auto 1fr auto;gap:5px 10px;align-items:center;font-size:12.5px;margin-top:6px}
.qbreak .qn{color:var(--ink2)}
.qbreak .qb{height:5px;background:var(--line);border-radius:3px;overflow:hidden}
.qbreak .qb i{display:block;height:100%;background:var(--s1);border-radius:3px}
.qbreak .qv{font-variant-numeric:tabular-nums;color:var(--ink3);font-size:11.5px}

.foot{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);color:var(--ink3);font-size:12.5px}
.foot code{background:var(--panel);border:1px solid var(--line);border-radius:5px;padding:1px 6px;font-family:var(--mono);font-size:12px}
@media(max-width:720px){
  .wrap{padding:16px 16px 60px}
  table.pf{display:block;overflow-x:auto}
  h1{font-size:21px}
  .grid-viz{grid-template-columns:1fr}
}
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
        "quality": load("quality.json", {"projects": {}, "portfolio_signals": []}),
        "snapshots": load_snapshots(),
    }
    blob = json.dumps(payload, ensure_ascii=False).replace("</script>", "<\\/script>")

    html = (
        "<!DOCTYPE html>\n<html lang=\"ru\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Портфель проектов</title>\n<style>" + CSS + "</style>\n</head>\n<body>\n"
        + read_tool("shell.html")
        + "\n<script id=\"DATA\" type=\"application/json\">" + blob + "</script>\n"
        + "<script>\n" + read_tool("charts.js") + "\n</script>\n"
        + "<script>\n" + read_tool("app.js") + "\n</script>\n"
        + "</body>\n</html>\n"
    )

    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dashboard.html")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(html)
    print("дашборд собран: %s (%.0f КБ)" % (dest, os.path.getsize(dest) / 1024))


if __name__ == "__main__":
    main()
