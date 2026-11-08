"""Флаги функций TT3 (TT3-008; ТЗ §11 п.3, §9 п.2, NFR-36, IT-24).

Все флаги по умолчанию выключены: с выключенными флагами система работает по старому
маршруту. Хранятся в `workflow.json` → `settings.flags`, меняет их только владелец
(`op=settings changes.flags`, owner-only как любые настройки).

Правила, которые держат флаги честными:
  * флаг без реализации включить нельзя: включённый флаг, который ничего не делает, врёт
    владельцу о состоянии системы;
  * у функции один выключатель. Если функция уже управляется своей настройкой, флаг —
    её отображение, а не второй переключатель (`tt3_finish_first` ↔ `flow_mode`);
  * исправления опасного CI (fail-closed, TT-102) и политика владельца (TT-104/105) флагами
    не выключаются никогда — это защита, а не функция.

Имена из §9 п.2 ТЗ сведены к флагам §11 п.3: `flow_control` → `tt3_finish_first`,
`local_llm` → `tt3_local_agent`, `provider_balancer` → `tt3_provider_balance` (E1B).
`mono_failover` не флаг: передача при лимите действовала и раньше (настройка `failover`),
подтверждение восстановления — защита от ping-pong. `adaptive_pipeline` → `tt3_adaptive_pipeline`
(E2: выключен = shadow, маршрут считается, но не действует). `dashboard_v2`,
`context_broker`, `fts5`, `batch_mcp` добавляются в реестр вместе
со своей реализацией (E1B, E2–E5). `remote_deploy` — не флаг: это профиль окружений
и грант владельца (TT-104/105).
"""

REGISTRY = {
    'tt3_finish_first': dict(title='Поток «сначала довести начатое» (FR-014)', stage='E1-d', implemented=True,
                             bound=('flow_mode', 'rolling', 'batch')),
    'tt3_provider_balance': dict(title='Баланс Claude/Codex по принятой работе (FR-015/016)', stage='E1B', implemented=True,
                                 bound=('balance_mode', 'balanced', 'prefer')),
    'tt3_adaptive_pipeline': dict(title='Адаптивные маршруты FAST/STANDARD/CRITICAL (FR-001)', stage='E2', implemented=True,
                                  bound=('adaptive_mode', 'enforce', 'shadow')),
    'tt3_native_runtime': dict(title='Нативный runtime локальной модели (C++ worker)', stage='E3', implemented=False),
    'tt3_model_registry': dict(title='Реестр локальных моделей', stage='E3', implemented=False),
    'tt3_local_agent': dict(title='Локальный агент на GGUF', stage='E3', implemented=False),
    'tt3_consensus': dict(title='Консенсус нескольких моделей', stage='E4', implemented=False),
    'tt3_machine_oracle': dict(title='Машинный оракул проверок', stage='E4', implemented=False),
    'tt3_failure_triage': dict(title='Разбор сбоев', stage='E4', implemented=False),
    'tt3_learning': dict(title='Обучение на истории', stage='E5', implemented=False),
    'tt3_training': dict(title='Дообучение моделей', stage='E5', implemented=False),
    # TT-ARCH-001 §12: модуль «Архитектура систем». В E0 есть только отчёт возможностей и закрытый адрес
    # /api/arch/; граф и коллекторы — E1+, до них флаг включить нельзя.
    'network_architecture_enabled': dict(title='Модуль «Архитектура систем» (TT-ARCH-001)', stage='ARCH-E1',
                                         implemented=False),
}


def stored(settings):
    return dict((settings or {}).get('flags') or {})


def value(name, settings):
    """Состояние флага: связанный флаг читается из своей настройки, остальные — из flags."""
    f = REGISTRY[name]
    if f.get('bound'):
        key, on, _ = f['bound']
        return (settings or {}).get(key) == on
    return bool(stored(settings).get(name)) and f['implemented']


def enabled(name, settings):
    if name not in REGISTRY:
        raise KeyError('Неизвестный флаг: %s' % name)
    return value(name, settings)


def view(settings):
    return [dict(name=k, title=f['title'], stage=f['stage'], implemented=f['implemented'], enabled=value(k, settings),
                 **({'bound_to': f['bound'][0]} if f.get('bound') else {})) for k, f in REGISTRY.items()]


def apply(changes, settings):
    """Проверить и разложить changes.flags: {флаг: bool} -> правки настроек (dict).
    Связанный флаг меняет свою настройку; хранимые флаги — словарь flags целиком."""
    if not isinstance(changes, dict) or any(type(v) is not bool for v in changes.values()):
        raise ValueError('flags: {"имя_флага": true|false}')
    unknown = sorted(set(changes) - set(REGISTRY))
    if unknown:
        raise ValueError('Неизвестные флаги: %s. Реестр: %s' % (', '.join(unknown), ', '.join(REGISTRY)))
    out = {}; flags = stored(settings)
    for name, on in changes.items():
        f = REGISTRY[name]
        if on and not f['implemented']:
            raise ValueError('Флаг %s (%s) не реализован — этап %s; включить нельзя' % (name, f['title'], f['stage']))
        if f.get('bound'):
            key, yes, no = f['bound']
            out[key] = yes if on else no
        else:
            flags[name] = on
    if any(not REGISTRY[n].get('bound') for n in changes):
        out['flags'] = {k: v for k, v in flags.items() if v}
    return out
