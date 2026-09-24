# -*- coding: utf-8 -*-
"""Прайс Anthropic API, $ за 1M токенов. Источник: skill claude-api.

ПРОВЕРЕНО — дата в CHECKED ниже. Прайс меняется, а на нём держится весь денежный
счёт портфеля, поэтому tools/check.py предупреждает, когда проверка устарела.
Как обновить: спросить у skill `claude-api` актуальные цены, поправить таблицу
и передвинуть CHECKED.

Правила кеша: запись 5m = 1.25x input, запись 1h = 2x input, чтение = 0.1x input
(для Fable 5.1 чтение = 0.025x -> $0.25/MTok).
"""

CHECKED = "2026-09-21"   # дата последней сверки со skill claude-api
STALE_DAYS = 60          # после скольких дней проверка начинает ругаться

PRICES = {
    "claude-fable-5-1":  {"in": 10.0, "out": 50.0, "read_mult": 0.025},
    "claude-mythos-5-1": {"in": 10.0, "out": 50.0, "read_mult": 0.1},
    "claude-fable-5":    {"in": 10.0, "out": 50.0, "read_mult": 0.1},
    "claude-opus-5":     {"in": 5.0,  "out": 25.0, "read_mult": 0.1},
    "claude-opus-4-8":   {"in": 5.0,  "out": 25.0, "read_mult": 0.1},
    "claude-opus-4-7":   {"in": 5.0,  "out": 25.0, "read_mult": 0.1},
    "claude-opus-4-6":   {"in": 5.0,  "out": 25.0, "read_mult": 0.1},
    "claude-sonnet-5":   {"in": 2.0,  "out": 10.0, "read_mult": 0.1},
    "claude-sonnet-4-6": {"in": 3.0,  "out": 15.0, "read_mult": 0.1},
    "claude-haiku-4-5":  {"in": 1.0,  "out": 5.0,  "read_mult": 0.1},
}
DEFAULT = {"in": 5.0, "out": 25.0, "read_mult": 0.1}
WRITE_5M = 1.25
WRITE_1H = 2.0


def price_for(model: str):
    if not model:
        return DEFAULT
    m = model.strip().lower()
    if m in PRICES:
        return PRICES[m]
    if m.startswith("<"):           # <synthetic> — локальные сообщения, не биллятся
        return {"in": 0.0, "out": 0.0, "read_mult": 0.0}
    for k in PRICES:
        if m.startswith(k):
            return PRICES[k]
    return DEFAULT


def cost_usd(model, input_t=0, output_t=0, cw5=0, cw1h=0, cread=0):
    p = price_for(model)
    return (
        input_t / 1e6 * p["in"]
        + output_t / 1e6 * p["out"]
        + cw5 / 1e6 * p["in"] * WRITE_5M
        + cw1h / 1e6 * p["in"] * WRITE_1H
        + cread / 1e6 * p["in"] * p["read_mult"]
    )
