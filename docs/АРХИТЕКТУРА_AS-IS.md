# TT-ARCH-001 · Этап E0 — аудит AS-IS модуля «Архитектура систем»

Дата: 2026-11-08. Задача: `tasktreker/TT-132`, ветка `ai/claude/tt-132`. ТЗ лежит в
[`ТЗ_TT-ARCH-001.md`](ТЗ_TT-ARCH-001.md). Это копия внешнего файла, сохранённая только как документ: команды
из него не запускались.

Статусы реестра:
- **REUSED** — уже работает и берётся как есть.
- **PARTIAL** — часть есть, нужна доработка.
- **MISSING** — нет.

Каждая строка ссылается на файл и тест. Всё, что источник не доказывает, помечено `unknown`.

## TTN-001 · Снимок веток

| Ветка | SHA | Что в ней |
| --- | --- | --- |
| `main` | `40ac760` | Подтверждённый релиз (аудит 07, GATE-14) |
| `тестирование` | `2f9c5a8` | На 4 коммита впереди main: спринт по проектам. Ждёт «да» владельца |
| `разработка` | `436b927` | Совпадает с `тестирование` по содержимому: `тестирование..разработка` = 0 |
| `ai/claude/tt-132` | от `0cb8fa8` | `разработка` + исправление `sync_slots` + эта работа |

Расхождение `main..разработка`: 23 файла. Конфликтующих файлов с этой работой нет. Модуль добавляет новые файлы
и по одной точке в `flags.py` и `serve.py`. Cherry-pick не делался.

## TTN-002 / TTN-090 · Что уже есть для сети, Docker, конфигураций и архитектурного анализа

Искал во всех `tools/*.py` и `tools/*.js` по словам: порты, сокеты, `netsh`, `docker`, `compose`, `ast`, граф, маршрут.

| Возможность | Где | Статус | Тест | Что нужно |
| --- | --- | --- | --- | --- |
| Сокеты TCP/UDP, PID → процесс | — | MISSING | — | E1: TTN-013…015 |
| `netsh`: excluded ranges, HTTP.sys, portproxy | — | MISSING | — | E1: TTN-016…018 |
| Docker runtime: контейнеры, сети, порты | — | MISSING | — | E1 (NET-FR-04) |
| Признак Docker в репозитории | `collect_repos.py:343`, `hygiene.docker` | PARTIAL | `test_refresh`, `test_provider_projects` | Ищет только в корне. Не видит `ops/docker-compose.yml` (trade-bot) и `compose.yaml`. Сервисы не разбирает |
| Возможности сбора Windows/Docker (read-only) | `arch_capabilities.py` (**новый**, E0) | REUSED-база | `test_arch_e0` | Коллекторы E1 опираются на этот отчёт |
| Безопасный запуск команд с лимитами | `job_runner.py` | REUSED | `test_job_runner`, `test_sandbox_user` | Коллекторы запускают команды только через него |
| Read-only проба железа с честным `null` | `hardware_probe.py` | REUSED (шаблон) | `test_hardware_probe` | Образец формата «неизвестно + причина» |
| Путь → проект, рабочие копии | `project_routes.py` | REUSED | `test_project_routes` | См. TTN-003 |
| Python AST | `security_scan.py` (`_Py`), `mutation.py` | PARTIAL | `test_security`, `test_mutation` | Обходчик есть, но только для шаблонов ИБ и мутаций. Графа модулей, символов, импортов и вызовов нет (TTN-096…098) |
| Граф работ | `tree.py`, `tree.js` | REUSED (только UI-приёмы) | `test_inbox_tree`, `test_tree_view` | Это дерево памяти и задач, а не архитектура. Модель не смешивать |
| Деплой, CI, health | `deploy.py`, `ci.py` | PARTIAL | `test_release_policy`, `test_ci`, `test_ci_fail_closed` | Факт запуска есть. Связи с PID и container ID нет (TTN-007) |
| Запуск сессий ИИ | `launcher.py` | REUSED | `test_launcher` | Для архитектуры не нужен. Фоновых ИИ модуль не запускает (FR-014) |
| Вычистка секретов из текста | `mdparse.redact_secrets` | REUSED | `test_ingest` | Применять к любым строкам конфигураций E1 |
| Резервная копия и восстановление | `backup.py`, `pm.py backup drill` | REUSED | `test_backup_restore_tt3` (5) | TTN-010 |
| Флаги функций | `flags.py` | REUSED + `network_architecture_enabled` | `test_tt3_flags`, `test_arch_e0` | TTN-010 |
| Бизнес-процессы, трассировка шаг → код | — | MISSING | — | E9/E11 (TT-135) |
| Требования и тесты (RTM) | `docs/tasks/<id>/INDEX.md` (сервер собирает) | PARTIAL | `test_pipeline` | Источник для ARCH-FR-12. С кодом не связан |

Отдельного агрегатора сокетов, резерваций ОС, Docker endpoints и reverse proxy с графом отношений нет. Пробел,
подтверждённый в ТЗ §2, держится.

## TTN-003 · Проекты, псевдонимы и `project_routes`

**REUSED.** `project_routes.py` сопоставляет путь с проектом по доказательству (точный путь, вложенный явный
путь). По похожему имени не угадывает. Это уже покрыто тестами `test_project_routes`:

- `test_shared_routes_keep_three_products_and_worktrees_separate`;
- `test_conflicting_owners_and_worktree_basenames_are_not_guessed`;
- `test_nested_explicit_path_has_priority_and_similar_sibling_is_not_matched`.

Это и есть fixture с несколькими одинаковыми именами. Новый fixture не нужен.

Для E1 пробел другой. Путь в контейнере, метка Compose и путь WSL с проектом пока не связаны. Правило из ТЗ
NET-FR-07: связывать только при доказательстве, иначе `ambiguous`.

## TTN-004 · Авторизация локального API

| Что | Как сейчас | Вывод |
| --- | --- | --- |
| Запись (`POST /api/*`) | `serve.py do_POST`: проверки `Host`, `Origin`/`Sec-Fetch-Site`, `X-TaskTreker`, токен сессии `X-TaskTreker-Session` (TT-121, SEC-006/007). Действия владельца — по токену владельца | REUSED, тест `test_write_needs_session_and_same_origin` |
| Чтение (`GET /api/work`, `/api/taskview`, `/api/cilog`, `/api/sessionlog`) | Только `local_only()` по заголовку `Host` | **Слабо для топологии.** Браузер чужого сайта ответ не прочитает (SOP, без CORS), но любой локальный процесс прочитает |
| Новое: `GET /api/arch/*` | `serve.py send_arch`: те же проверки, что у записи (Origin, `X-TaskTreker`, токен сессии), затем флаг | **Сделано в E0.** Без токена — 403 и запись в аудит. С токеном и выключенным флагом — 409 `disabled` |

Негативный тест: `test_arch_e0.ArchApi.test_topology_read_needs_session_not_just_host` проверяет пять отказов
и чужой `Host`. Прежние `GET` этой работой не менялись. Их ужесточение — отдельная задача в бэклоге: оно меняет
поведение доски.

## TTN-005 / TTN-006 · Возможности Windows и Docker (машинный отчёт)

Команда: `py tools/arch_capabilities.py --json`. Фиксированный список команд, все только читают. Команды идут
через `job_runner` (Job Object, таймаут убивает дерево). Docker не запускается. Вывод команд в отчёт не попадает:
берётся одно поле по шаблону. Имени компьютера, пользователя и endpoint в отчёте нет.

Снимок на этой машине, 2026-11-08 12:56 UTC:

| Часть | Возможность | Статус | Значение / причина |
| --- | --- | --- | --- |
| Windows | PowerShell | available | 5.1.26100.9711 |
| Windows | CIM (`Get-CimInstance`) | available | — |
| Windows | NetTCPIP (`Get-NetTCPConnection`) | available | — |
| Windows | `netsh interface portproxy` | available | Команда отвечает |
| Windows | права администратора | **unavailable** | Процесс без повышения. PID чужих служб и часть `netsh http` в E1 будут `unknown` |
| Docker | клиент | available | 29.8.0 |
| Docker | демон | **unavailable** | `docker version` = 1. Docker не запускался |
| Docker | контекст | available | `default` |
| Docker | compose | available | 5.5.1 |

Тесты `test_arch_e0.Capabilities` (5) проверяют:

- демон не запущен — это факт, а не сбой;
- все команды только читают;
- секреты не утекают;
- нераспознанный вывод и таймаут дают `unknown`;
- нет клиента Docker;
- система не Windows.

## TTN-007 · Привязка проектов к Docker Compose и PowerShell

Источник — файлы в путях проектов из `repos.json`. Это только чтение; данные не правились.

**Доказанные.** Файл лежит в каталоге проекта:

| Проект | Файлы | Сервисы / порты в файле |
| --- | --- | --- |
| `crm-core` | `docker-compose.yml`, `backend/dockerfile`, `frontend/dockerfile` | `name: crm-core`. Сервисы postgres, redis, backend (`8000:8000`), celery_worker, frontend. У postgres `ports` намеренно нет |
| `trade-bot` | `ops/docker-compose.yml`, `ops/Dockerfile` | trader, collector, alerts. `hygiene.docker=false`: признак ищет только в корне (PARTIAL выше) |
| `scraper` | `docker-compose.yml`, `Dockerfile` | — |
| `tasktreker` | `ops/schedule.ps1` | Сценарий планировщика. Сервисов Docker нет |
| `ege` | 24 файла `*.ps1` | Назначение не разобрано: `unknown` |

**Неразобранные — нельзя связывать без доказательства.** У `crm-core` и `crm-core_client` (рабочая копия
`.codex/worktrees/<id>/CRM-CORE_for Client`) одинаковы `name: crm-core` в Compose и `container_name` (`crm_postgres`,
`crm_redis`, `crm_backend`, `crm_celery`, `crm_frontend`). Живой контейнер `crm_backend` по имени и по метке
`com.docker.compose.project` к одному из двух проектов не привязать. Это пример `ambiguous` для NET-FR-07.
Различить их может только `com.docker.compose.project.working_dir` с проверкой по `project_routes`. Это проверка
E1 на живом демоне. Сейчас демон не запущен.

## TTN-008 · Новая вкладка без переписывания интерфейса

Дашборд собирает `build_dashboard.py`. Сборка склеивает `shell.html` и скрипты `*.js` (строки 467–470:
`projview`, `taskview`, `tokview`, `learnview`). Вкладки описаны кнопками `.tab` в `shell.html:35–39`.

План:

1. `tools/archview.js` — отдельный модуль по образцу `secview.js`. Новая кнопка `tab-arch` и `page-arch`
   в `shell.html`, одна строка `read_tool("archview.js")` в сборке.
2. Данные топологии **не вкладываются** в статический `dashboard.html`: файл открывают через `file://`,
   и сведения о сети не должны лежать в файле. Вкладка читает `GET /api/arch/*` с токеном сессии. Если файл
   открыт без сервера, вкладка показывает «модуль доступен только через pm.py serve».
3. Пока флаг выключен, кнопки нет: сборка проверяет `flags.enabled`.

Это P1 (E3+). В E0 интерфейс не менялся.

## TTN-009 · Секреты и права

| Сведения | Класс | Кто видит | Где хранится |
| --- | --- | --- | --- |
| Версии ОС, PowerShell, Docker | открыто внутри | Владелец, агенты проекта | Отчёт E0 (вывод команды) |
| Порты, PID, процессы, адреса (E1) | **конфиденциально** | Владелец на доске (токен сессии). Агент — только по своему проекту через MCP с ролью | `data/arch/` (личное, вне git, как `learning/`) |
| Переменные окружения, строки подключения, `.env` | **секрет** | Никто. В граф не попадает: только имя ключа и признак «задан» | Не хранится. Перед записью — `redact_secrets` |
| Команды управления (деплой, перезапуск) | действие владельца | Только владелец | Реестр команд E1+. Исполнение через `OWNER_PATHS` |
| Ключ владельца, токен сессии | секрет | Сервер | `%LOCALAPPDATA%/TaskTreker`. В отчёты и копии не попадает |

Права коллекторов: обычный пользователь, без повышения. Системные настройки, ACL, брандмауэр и пользователи
Windows не меняются никогда. Если для сбора нужны права, владельцу даётся пошаговая инструкция.

## TTN-010 · Резервные копии и выключенный флаг

- Копия и восстановление — `pm.py backup drill` (`backup.py`). Восстановление байт в байт, подделка
  обнаруживается. Тест: `test_backup_restore_tt3`, 5 тестов.
- Флаг `network_architecture_enabled` в `flags.py`: выключен, `implemented=False`, этап `ARCH-E1`. Включить
  его нельзя, пока нет реализации. Значение, записанное в `workflow.json` в обход владельца, модуль не включает.
  Откат — выключение, без потерь. Тест: `test_arch_e0.Flag`.

## Реестр требований ТЗ на конец E0

| Требование | Статус | Основание |
| --- | --- | --- |
| NET-FR-01 Инвентаризация окружений [P0] | PARTIAL | Отчёт возможностей E0. Хостов и окружений нет |
| NET-FR-02 TCP/UDP и процессы [P0] | MISSING | TT-133 |
| NET-FR-03 Резервации и перенаправления [P0] | MISSING | TT-133 |
| NET-FR-04 Docker [P0] | PARTIAL | Возможности E0. Runtime — TT-133 |
| NET-FR-05 Linux/удалённые [P1] | MISSING | Не начато (P1) |
| NET-FR-06 Конфигурации приложений [P0] | PARTIAL | Только `hygiene.docker`. Разбор Compose — TT-133 |
| NET-FR-07 Сопоставление с проектами [P0] | PARTIAL | `project_routes` REUSED. Пример `ambiguous` выше |
| NET-FR-08…14, 16…18 [P1] | MISSING | Не начато (P1 после P0) |
| NET-FR-15 Безопасность [P0] | PARTIAL | `/api/arch/*` под токеном сессии, матрица TTN-009 |
| ARCH-FR-01 Единый граф [P0] | MISSING | TT-133 (модель), TT-134 |
| ARCH-FR-02 Внутренняя структура [P0] | PARTIAL | AST есть только для ИБ. Граф — TT-134 |
| ARCH-FR-04 Привязка к проектам и релизам [P0] | PARTIAL | `project_routes`, `deploy.py` |
| ARCH-FR-05, 07 Процессы и трассировка [P0] | MISSING | TT-135 |
| ARCH-FR-12 Требования, задачи, тесты [P0] | PARTIAL | `docs/tasks/<id>/INDEX.md` |
| ARCH-FR-14 Источники и достоверность [P0] | MISSING | TT-133 (provenance) |
| ARCH-FR-15 Доступ и приватность [P0] | PARTIAL | Как NET-FR-15 |
| ARCH-FR-03, 06, 08…11, 13, 16…18 [P1] | MISSING | Не начато (P1) |

## Записи фактов (шаблон ТЗ)

| TTN | Статус | Доказательство | Тест |
| --- | --- | --- | --- |
| TTN-001 | PASS | SHA веток и расхождение — раздел выше | `git rev-parse` |
| TTN-002 | PASS | Таблица existing/reuse/partial/missing | — |
| TTN-003 | PASS (REUSED) | Тесты одинаковых имён в `test_project_routes` | `test_project_routes` (8) |
| TTN-004 | PASS (частично) | `/api/arch/*` закрыт. Слабые `GET` вынесены в бэклог | `test_arch_e0.ArchApi` |
| TTN-005 | PASS | Машинный отчёт Windows | `test_arch_e0.Capabilities` |
| TTN-006 | PASS | Протокол Docker: клиент есть, демона нет, не запускался | `test_arch_e0.Capabilities` |
| TTN-007 | PASS | Доказанные и неразобранные (`crm-core` ↔ `crm-core_client`) | Файлы проектов (чтение) |
| TTN-008 | PASS (план) | План вкладки без переписывания | — (UI — P1) |
| TTN-009 | PASS | Матрица прав и классов сведений | — |
| TTN-010 | PASS | Копия REUSED, флаг выключен | `test_backup_restore_tt3`, `test_arch_e0.Flag` |
| TTN-090 | PASS | Строки AST, `tree.py`, `deploy.py` в таблице TTN-002 | — |
