# -*- coding: utf-8 -*-
"""Оценка затрат на оставшиеся задачи — в токенах и деньгах.

Калибруется по собственной истории: сколько токенов фактически стоил один
коммит в каждом проекте (usage.json / repos.json). Проекты без git берут
медиану по портфелю. Размер задачи S/M/L/XL -> коммиты -> токены -> $.

-> data/estimates.json
"""
import json
import os
import statistics
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    with open(os.path.join(ROOT, "data", name), encoding="utf-8") as f:
        return json.load(f)


def main():
    proj = load("projects.json")
    usage = load("usage.json")["projects"]
    repos = load("repos.json")["repos"]
    cal = proj["size_calibration"]

    # ключ проекта -> ключ в usage.json (usage строится по имени каталога, в нижнем регистре)
    def usage_for(key):
        cands = [key, key.lower(), key.lower().replace("-", "_")]
        base = os.path.basename(proj["projects"][key]["path"].rstrip("/"))
        cands += [base, base.lower()]
        if "/Documents/" in proj["projects"][key]["path"]:
            cands += ["doc:" + base.lower(), "doc:" + base.lower().replace("_", "-")]
        for c in cands:
            if c in usage:
                return usage[c]
        return None

    per_commit = {}
    for key in proj["projects"]:
        u = usage_for(key)
        r = repos.get(key) or {}
        g = r.get("git") or {}
        commits = g.get("commits") or 0
        if u and commits >= 5:
            per_commit[key] = {
                "tokens": u["billable"] / commits,
                "usd": u["usd"] / commits,
                "output": u["output"] / commits,
                "basis": "собственная история: %d коммитов" % commits,
            }

    med_tokens = statistics.median([v["tokens"] for v in per_commit.values()]) if per_commit else 25e6
    med_usd = statistics.median([v["usd"] for v in per_commit.values()]) if per_commit else 14.0

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "calibration": {
            "per_project": {k: {"tokens_per_commit": round(v["tokens"]), "usd_per_commit": round(v["usd"], 2),
                                "basis": v["basis"]} for k, v in per_commit.items()},
            "portfolio_median": {"tokens_per_commit": round(med_tokens), "usd_per_commit": round(med_usd, 2)},
            "note": "Оценка по фактическому расходу на коммит в этом же проекте. Где истории нет — медиана портфеля. "
                    "Это стоимость работы ассистента, а не трудозатраты человека.",
        },
        "projects": {},
        "totals": {},
    }

    grand_tok = grand_usd = 0.0
    for key, p in proj["projects"].items():
        rate = per_commit.get(key) or {"tokens": med_tokens, "usd": med_usd, "basis": "медиана портфеля (своей истории мало)"}
        items = []
        tok_sum = usd_sum = 0.0
        for b in p.get("backlog", []):
            size = b.get("size", "M")
            commits = cal.get(size, cal["M"])["commits"]
            tok = rate["tokens"] * commits
            usd = rate["usd"] * commits
            blocked = b.get("status") in ("blocked",)
            items.append({
                "id": b.get("id"), "title": b.get("title"), "size": size,
                "priority": b.get("priority", ""), "status": b.get("status", ""),
                "days": b.get("days", ""), "why": b.get("why", ""),
                "est_tokens": round(tok), "est_usd": round(usd, 2), "blocked": blocked,
            })
            tok_sum += tok
            usd_sum += usd
        out["projects"][key] = {
            "rate_basis": rate["basis"],
            "tokens_per_commit": round(rate["tokens"]),
            "usd_per_commit": round(rate["usd"], 2),
            "items": items,
            "remaining_tokens": round(tok_sum),
            "remaining_usd": round(usd_sum, 2),
        }
        grand_tok += tok_sum
        grand_usd += usd_sum

    out["totals"] = {"remaining_tokens": round(grand_tok), "remaining_usd": round(grand_usd, 2)}

    dest = os.path.join(ROOT, "data", "estimates.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    fmt = lambda n: "{:,}".format(int(n)).replace(",", " ")
    print("калибровка (токенов на коммит):")
    for k, v in out["calibration"]["per_project"].items():
        print("  %-16s %12s  $%7.2f   %s" % (k, fmt(v["tokens_per_commit"]), v["usd_per_commit"], v["basis"]))
    print("  %-16s %12s  $%7.2f   медиана портфеля" % ("—", fmt(med_tokens), med_usd))
    print("\nостаток по проектам:")
    for k, v in out["projects"].items():
        if v["items"]:
            print("  %-16s задач %2d · %14s токенов · $%8.2f" % (
                k, len(v["items"]), fmt(v["remaining_tokens"]), v["remaining_usd"]))
    print("\nИТОГО осталось: %s токенов · $%s" % (fmt(grand_tok), fmt(grand_usd)))
    print("-> " + dest)


if __name__ == "__main__":
    main()
