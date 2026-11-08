"""Конвейер задачи: Идея → Анализ → Разработка → Ревью → Деплой в тест → Тестирование → Деплой в main.

Один источник правды для трёх потребителей: сервер проверяет по нему DoR и DoD, из него
генерируются скиллы ролей (tools/skills.py), его показывает доска. Поменять пункт DoD —
значит поменять его здесь; скиллы пересобираются, и агенты получают ровно то, что
проверяет сервер.

«Железно» означает: перейти на этап и сдать этап можно только через op=handoff, а он
принимает работу, лишь когда каждый пункт DoD роли выполнен. Пункты бывают:
- artifact — файл в ветке задачи (закоммичен), не короче min_bytes, с нужными разделами;
- evidence — агент описывает, что сделано (не короче 20 символов);
- machine — проверяет сервер: тесты проекта, слияние, ссылки, таблицы, диаграммы, дефекты;
- gate — решение ворот: ИИ-тимлида или владельца.
«Неприменимо» допустимо только у пунктов с allow_na и только с причиной.

Адаптация к портфелю: корпоративные пункты (Jira, SonarQube, архитектурный комитет,
Kubernetes) переведены на то, что существует здесь: трекер — TaskTreker, согласование —
ворота тимлида и владельца, стенд — рабочая копия задачи, CI — команда verify проекта.
"""
import json
import os
import re
import subprocess
from pathlib import Path

ART = 'docs/tasks/{id}'          # артефакты задачи — в ветке задачи, рядом с кодом
MIN_EVIDENCE = 20
MIN_NA_REASON = 10

STAGES = [
    dict(id='idea', title='Идея', roles=['product_owner']),
    # after: роль стартует только после сдачи другой роли этапа — архитектор и дизайнер читают готовое ТЗ (А1).
    # QA в анализе — ранний тест-дизайн (07: A6-01, AG-QA-01/02, E2E-R13): ожидаемое поведение по каждому AC
    # фиксируется независимо от разработчика и до первого патча; на «Тестировании» QA проверяет по нему.
    dict(id='analysis', title='Анализ', roles=['system_analyst', 'solution_architect', 'designer', 'marketer', 'qa'],
         optional={'designer': 'design', 'marketer': 'marketing'}, gate='analysis',
         after={'solution_architect': 'system_analyst', 'designer': 'system_analyst', 'qa': 'system_analyst'}),
    dict(id='development', title='Разработка', roles=['developer']),
    dict(id='review', title='Ревью', roles=['reviewer']),
    dict(id='deploy_test', title='Деплой в тест', roles=['devops']),
    dict(id='testing', title='Тестирование', roles=['qa'], gate='release'),
    dict(id='deploy_main', title='Деплой в main', roles=['devops'], owner_approval=True),
]
STAGE = {s['id']: s for s in STAGES}
ORDER = [s['id'] for s in STAGES]


def item(id, text, kind, **kw):
    return dict(id=id, text=text, kind=kind, **kw)


def doc(path, sections=(), min_bytes=400, **kw):
    return dict(path=path, sections=list(sections), min_bytes=min_bytes, **kw)


# ---------------------------------------------------------------- роли

ROLES = {
    'product_owner': dict(
        title='Product Owner', stage='idea', min_tier=2,
        mission='Из идеи сделать максимально развёрнутое бизнес-требование с чётко сформулированными ожидаемыми '
                'результатами — после анализа мировых практик, конкурентов на рынке и возможных максимальных эффектов.',
        scope=['Понять идею и исходный запрос владельца; уточнить проблему и пользователя.',
               'Продуктовая задача (новая ценность для пользователя): мировые практики и не меньше трёх конкурентов '
               'или аналогов (веб-поиск), с источниками. Техническая (рефакторинг, инфраструктура, долг, исправление) — '
               'без веб-поиска: needs.research=false, разделы о рынке не нужны.',
               'Оценить максимальные эффекты фичи: для пользователя, бизнеса, проекта.',
               'Сформулировать ожидаемые результаты измеримо: метрики и целевые значения.',
               'Зафиксировать границы (что входит и что нет), риски и допущения.',
               'Решить, нужны ли дизайн (только если меняется интерфейс) и маркетинг; задать бизнес-критерии приёмки.'],
        artifacts=[doc(ART + '/BRD.md', ['Проблема', 'Пользовател', 'Эффект', 'Ожидаемые результаты', 'Метрик', 'Границ', 'Риск'],
                       800, market=['Мировые практики', 'Конкурент', 'Источник'], market_bytes=1500)],
        dod=[item('po-brd', 'BRD.md написан по шаблону: все обязательные разделы заполнены', 'artifact', art=0),
             item('po-market', 'Мировые практики и не меньше трёх конкурентов с источниками (ссылки в BRD) — для продуктовой задачы',
                  'machine', check='links', art=0, min=3, allow_na=True, na_when='no_research'),
             item('po-results', 'Ожидаемые результаты измеримы: метрики с целевыми значениями', 'evidence'),
             item('po-effects', 'Оценены максимальные эффекты фичи и её ценность', 'evidence'),
             item('po-acceptance', 'Бизнес-критерии приёмки записаны в задачу (не меньше двух)', 'machine', check='acceptance'),
             item('po-needs', 'Решено, нужны ли дизайн и маркетинг, с причиной', 'machine', check='needs'),
             item('po-metrics', 'Каждое число в «Метриках» — с происхождением: источник, замер, «гипотеза» или UNKNOWN',
                  'machine', check='rule:labeled_numbers', art=0, section='Метрик'),
             item('po-out', 'В границах явно сказано, что не входит в задачу', 'machine', check='rule:words_any', art=0,
                  words=('не входит', 'вне рамок', 'out of scope'))]),

    'system_analyst': dict(
        title='Системный аналитик', stage='analysis', min_tier=2,
        mission='Перевести бизнес-требование в техническое задание (SRS), по которому разработка и тестирование '
                'идут без двояких трактовок.',
        scope=['НФТ: производительность, отказоустойчивость, информационная безопасность.',
               'Декомпозиция бизнес-целей (BRD) до системных требований; разрешение конфликтов с архитектурой.',
               'Use cases / User stories с детальными критериями приёмки и пограничными случаями.',
               'Контракты взаимодействия (API, сообщения), модель данных и Data Mapping.',
               'Диаграммы UML/BPMN (Mermaid): последовательности, активности или состояний, ER.',
               'Матрица трассировки требований (RTM) и задачи для разработки с критериями приёмки.'],
        artifacts=[doc(ART + '/SRS.md', ['Функциональные требования', 'Нефункциональные требования', 'Сценари',
                                         'Критерии приёмки', 'Пограничн', 'Ролев'], 1500),
                   doc(ART + '/diagrams.md', ['Последовательност'], 200),
                   doc(ART + '/api.md', ['Контракт', 'Ошибк'], 300, optional=True),
                   doc(ART + '/data-model.md', ['Модель данных', 'Mapping'], 300, optional=True),
                   doc(ART + '/RTM.md', [], 200),
                   doc(ART + '/dev-tasks.md', ['Критерии приёмки'], 300)],
        dod=[item('sa-srs', 'ТЗ (SRS.md) покрывает функциональные требования, НФТ и пограничные случаи', 'artifact', art=0),
             item('sa-diagrams', 'Диаграммы: хотя бы одна Mermaid-диаграмма последовательности/активности', 'machine', check='mermaid', art=1, min=1),
             item('sa-contracts', 'Контракты интеграций описаны (api.md)', 'artifact', art=2, allow_na=True),
             item('sa-data', 'Логическая модель данных и Data Mapping (data-model.md)', 'artifact', art=3, allow_na=True),
             item('sa-rtm', 'RTM: таблица «бизнес-требование → системное требование»', 'machine', check='table', art=4),
             item('sa-ui', 'Требования к интерфейсу привязаны к макетам дизайнера', 'evidence', allow_na=True),
             item('sa-decomp', 'Задачи для разработки с понятными критериями приёмки (dev-tasks.md)', 'artifact', art=5),
             item('sa-trace', 'Коды требований согласованы: каждое требование SRS есть в RTM, ссылки на коды ведут к определённым',
                  'machine', check='trace'),
             item('sa-rtm-ac', 'RTM в обе стороны: каждый критерий приёмки задачи (AC-n) прослежен до требований', 'machine', check='rule:covers_ac', art=4),
             item('sa-exceptions', 'В SRS описаны исключительные и альтернативные потоки (ошибки)', 'machine', check='rule:heading_any', art=0,
                  needles=['Исключени', 'Ошибк', 'Альтернативн']),
             item('sa-nfr', 'НФТ измеримы: у каждого порог (время, доля, объём)', 'machine', check='rule:measurable', art=0, section='Нефункциональные'),
             item('sa-api-version', 'Контракт API (если есть) версионирован и описывает совместимость', 'machine', check='rule:api_version', art=2),
             item('sa-units', 'Задачи разработки — минимальные шаги DU-n, у каждого критерий AC; монолит XL не проходит', 'machine', check='rule:dev_units', art=5)]),

    'solution_architect': dict(
        title='Solution-архитектор', stage='analysis', min_tier=2,
        mission='Спроектировать целевую архитектуру решения (HLD), зафиксировать решения (ADR) и риски.',
        scope=['НФТ из BRD: отказоустойчивость, безопасность, масштабируемость; аудит текущего состояния (As-Is).',
               'Архитектура на уровне контейнеров и компонентов, паттерны интеграции, топология развёртывания.',
               'Выбор стека и, если нужно, Proof of Concept.',
               'Модель угроз и IT-риски с мерами; отказоустойчивость и восстановление.',
               'Решения в формате ADR; передача эпиков аналитику и разработке.'],
        artifacts=[doc(ART + '/HLD.md', ['Контекст', 'Компонент', 'Интеграц', 'Развёртывани', 'Стек',
                                         'Отказоустойчивост', 'Восстановлени'], 1200),
                   doc(ART + '/adr', ['Контекст', 'Решение', 'Последстви'], 300, dir=True),
                   doc(ART + '/threats.md', ['Угроз', 'Риск', 'Мер'], 300),
                   doc(ART + '/poc.md', ['Результат'], 200, optional=True)],
        dod=[item('arch-hld', 'HLD покрывает компоненты, потоки данных, интеграции и развёртывание', 'artifact', art=0),
             item('arch-c4', 'Схемы C4 / развёртывания: хотя бы одна Mermaid-диаграмма в HLD', 'machine', check='mermaid', art=0, min=1),
             item('arch-ha', 'Описаны отказоустойчивость и аварийное восстановление', 'evidence'),
             item('arch-adr', 'Ключевые решения зафиксированы в ADR (adr/ADR-*.md)', 'artifact', art=1),
             item('arch-threats', 'Модель угроз и матрица рисков с мерами', 'artifact', art=2),
             item('arch-stack', 'Стек проверен на соответствие проекту и его ограничениям', 'evidence'),
             item('arch-poc', 'Отчёт PoC, если проверялась новая технология', 'artifact', art=3, allow_na=True),
             item('arch-handoff', 'Эпики и задачи переданы аналитику и разработке', 'evidence'),
             item('arch-trace', 'HLD и ADR ссылаются только на существующие требования и ADR', 'machine', check='trace'),
             item('arch-refs', 'Пути файлов в HLD существуют в ветке или помечены как новые', 'machine', check='rule:refs_exist', art=0),
             item('arch-options', 'В каждом ADR не меньше двух рассмотренных вариантов', 'machine', check='rule:adr_options', art=1),
             item('arch-compat', 'HLD описывает совместимость и откат', 'machine', check='rule:heading_all', art=0, needles=['Совместим', 'Откат']),
             item('arch-threat-measures', 'Модель угроз — таблица угроз с мерой по каждой', 'machine', check='rule:threat_table', art=2)]),

    'designer': dict(
        title='UX/UI-дизайнер', stage='analysis', min_tier=2,
        mission='Спроектировать пользовательский опыт и интерфейс, готовые к разработке без догадок.',
        scope=['Анализ требований и аудитории; бенчмаркинг конкурентов.',
               'User Flow и CJM; информационная архитектура.',
               'Wireframes и детальные макеты (описание или HTML-прототип), адаптив: desktop и mobile.',
               'Все состояния: ошибки, пустые экраны, загрузка, hover/focus/active.',
               'Компоненты UI-кита и гайдлайны; спецификация передачи в разработку.',
               'Скриншоты — только когда без них не обойтись: не больше трёх за запуск, уменьшенные (до 1280 px по ширине), '
               'уже просмотренный не открывать снова.'],
        artifacts=[doc(ART + '/design/user-flow.md', ['Сценари'], 300),
                   doc(ART + '/design/ui-spec.md', ['Экран', 'Состояни', 'Адаптив', 'Компонент'], 800),
                   doc(ART + '/design/handoff.md', ['Валидаци', 'Поведени'], 300),
                   doc(ART + '/design/prototype.html', [], 300, optional=True)],
        dod=[item('ds-flow', 'User Flow со связанными экранами (Mermaid-схема)', 'machine', check='mermaid', art=0, min=1),
             item('ds-screens', 'Макеты покрывают позитивные и негативные сценарии и все состояния', 'artifact', art=1),
             item('ds-adaptive', 'Адаптивные версии: мобильная и десктопная', 'evidence'),
             item('ds-kit', 'Новые компоненты описаны как UI-кит (с тёмной темой, если есть)', 'evidence', allow_na=True),
             item('ds-handoff', 'Спецификация передачи: валидации, анимации, поведение', 'artifact', art=2),
             item('ds-prototype', 'Кликабельный прототип', 'artifact', art=3, allow_na=True),
             item('ds-states', 'Состояния экранов: ошибка, пустой экран, загрузка', 'machine', check='rule:words_in_section', art=1, section='Состояни',
                  words=('ошибк', 'пуст', 'загрузк')),
             item('ds-a11y', 'Доступность: клавиатура, контраст, подписи элементов', 'machine', check='rule:heading_any', art=1,
                  needles=['Доступност', 'Accessibility', 'a11y']),
             item('ds-fields', 'Поля интерфейса из спецификации передачи есть в контракте API', 'machine', check='rule:fields_in_api', art=2),
             item('ds-legacy', 'Переделка интерфейса: каждое поле старого интерфейса перенесено или убрано с причиной',
                  'machine', check='rule:legacy_fields', art=1)]),

    'marketer': dict(
        title='Продуктовый маркетолог', stage='analysis', min_tier=2,
        mission='Подготовить вывод фичи на рынок: аудиторию, ценностное предложение, каналы и метрики.',
        scope=['Сегментация аудитории и ICP / персоны; анализ конкурентов: позиционирование, цены, каналы.',
               'Go-To-Market стратегия, ценностное предложение и ключевые сообщения.',
               'Медиаплан по каналам с прогнозом и целевым CAC/CPA.',
               'Брифы на креативы и лендинги; план удержания (онбординг, триггерные коммуникации).',
               'Сквозная аналитика: события, UTM, метрики ROMI, CAC, LTV, CR, Churn.'],
        artifacts=[doc(ART + '/marketing/GTM.md', ['Аудитори', 'Ценностное предложение', 'Канал', 'Бюджет', 'Таймлайн'], 800),
                   doc(ART + '/marketing/media-plan.md', [], 200),
                   doc(ART + '/marketing/briefs.md', ['Бриф'], 300),
                   doc(ART + '/marketing/analytics.md', ['Метрик', 'Событи'], 300)],
        dod=[item('mk-gtm', 'GTM-план: аудитория, позиционирование, каналы, бюджет, таймлайн', 'artifact', art=0),
             item('mk-media', 'Медиаплан таблицей: каналы, бюджет, прогноз, целевой CAC/CPA', 'machine', check='table', art=1),
             item('mk-briefs', 'Брифы на креативы и лендинги', 'artifact', art=2),
             item('mk-analytics', 'Аналитика: события, UTM, метрики ROMI/CAC/LTV', 'artifact', art=3),
             item('mk-launch', 'Запуск кампании и отчёт план/факт — после релиза, вне конвейера', 'evidence', allow_na=True),
             item('mk-sources', 'Числа GTM — с источником и датой или с пометкой «гипотеза»', 'machine', check='rule:labeled_numbers', art=0, date=True),
             item('mk-media-sources', 'Прогноз медиаплана — с источником и датой или «гипотеза»', 'machine', check='rule:labeled_numbers', art=1, date=True)]),

    'developer': dict(
        title='Разработчик', stage='development', min_tier=1,
        mission='Реализовать задачу по ТЗ, HLD и макетам: чистый, проверенный и задокументированный код.',
        scope=['Low-level design: декомпозиция, структуры данных, паттерны.',
               'Код бизнес-логики, API и интерфейса по дизайн-системе.',
               'Unit- и интеграционные тесты; миграции БД, если менялась структура.',
               'Обновление README и документации API; описание изменений (MR).'],
        artifacts=[doc(ART + '/MR.md', ['Что изменено', 'Как проверить', 'Требовани'], 400)],
        dod=[item('dev-code', 'Код в ветке задачи, всё закоммичено', 'machine', check='committed'),
             item('dev-verify', 'CI проекта на ветке задачи зелёный', 'machine', check='verify', heavy=True),
             item('dev-tests', 'Написаны или обновлены тесты', 'machine', check='tests_changed', allow_na=True),
             item('dev-mutation', 'Задача CRITICAL: тесты ловят внесённые в изменённый код ошибки (мутационная проверка)',
                  'machine', check='mutation', heavy=True),
             item('dev-migrations', 'Миграции БД написаны и проверены', 'evidence', allow_na=True),
             item('dev-docs', 'Документация обновлена (README, API)', 'evidence', allow_na=True),
             item('dev-mergeable', 'Ветка сливается с базовой без конфликтов', 'machine', check='mergeable'),
             item('dev-contract', 'Код написан по актуальному контракту группы (contract_group): контракт не менялся другой задачей после анализа',
                  'machine', check='contract_current'),
             item('dev-oracle', 'Эталон приёмки (приёмочные тесты, правила ИБ, CI) не изменён или изменение одобрил владелец',
                  'machine', check='oracle_intact'),
             item('dev-mr', 'Описание изменений MR.md: что изменено, как проверить, какие требования', 'artifact', art=0)]),

    'reviewer': dict(
        title='Ревьюер', stage='review', min_tier=2,
        mission='Проверить код на соответствие ТЗ и архитектуре, качество, безопасность и тесты; одобрить или вернуть.',
        scope=['Стиль, читаемость, отсутствие дублирования и «магических» значений.',
               'Соответствие SRS и HLD, пограничные случаи, гонки.',
               'Производительность и безопасность: инъекции, XSS, секреты, персональные данные.',
               'Качество тестов: проверяют логику, а не для галочки.',
               'Конструктивные замечания; техдолг — отдельными задачами.'],
        artifacts=[doc(ART + '/review.md', ['Замечани', 'Решени', 'Вердикт'], 400)],
        dod=[item('rv-report', 'review.md: замечания, решения по ним, вердикт', 'artifact', art=0),
             item('rv-verdict', 'Вердикт approve (иначе задача возвращается разработчику)', 'machine', check='verdict'),
             item('rv-ac', 'Матрица по каждому критерию приёмки и изменённому файлу: файлы, тесты, ok|gap|defect', 'machine', check='ac_matrix'),
             item('rv-sha', 'Ревью на текущей голове ветки (reviewed_sha)', 'machine', check='reviewed_sha'),
             item('rv-tests', 'Тесты проверяют реальную логику', 'evidence'),
             item('rv-security', 'Проверены инъекции, XSS, секреты и работа с персональными данными', 'evidence'),
             item('rv-debt', 'Техдолг заведён задачами', 'evidence', allow_na=True),
             item('rv-ci', 'CI на ветке задачи зелёный перед слиянием', 'machine', check='ci_task', heavy=True),
             item('rv-merge', 'Ветка задачи слита в базовую ветку проекта', 'machine', check='merge_to_base', heavy=True)]),

    'qa': dict(
        title='QA-инженер', stage=('analysis', 'testing'), min_tier=2,
        mission='Проверить развёрнутую тестовую ветку: реализация соответствует требованиям, ничего не сломано; '
                'дать решение о допуске в main только при отсутствии ошибок.',
        scope=['Ревью требований на тестируемость; тест-дизайн: классы эквивалентности, границы, переходы состояний.',
               'Тест-кейсы и чек-листы; тестовые данные.',
               'Функциональное, интеграционное, UI- и регрессионное тестирование.',
               'Баг-репорты с шагами, приоритетом и серьёзностью; повторная проверка исправлений.',
               'Отчёт о тестировании и решение о допуске (QA Sign-off); RTM с тест-кейсами.'],
        artifacts=[doc(ART + '/test-cases.md', ['TC-'], 500),
                   doc(ART + '/test-report.md', ['Результат', 'Дефект', 'Регресс', 'Решение'], 500),
                   doc(ART + '/RTM.md', [], 200),
                   doc(ART + '/test-design.md', ['Позитив', 'Негатив', 'Границ', 'Ожидаем'], 500)],
        dod={'analysis': [
             item('qa-design', 'Тест-дизайн до разработки: по каждому критерию приёмки позитивные, негативные и граничные '
                  'случаи с ожидаемым результатом (test-design.md) — без опоры на способ реализации', 'artifact', art=3),
             item('qa-design-ac', 'Каждый критерий приёмки задачи покрыт случаем тест-дизайна', 'machine', check='design_covers_ac', art=3),
             item('qa-design-matrix', 'Каждый AC — в позитивных, негативных и граничных случаях; ожидаемый результат указан',
                  'machine', check='rule:design_matrix', art=3)],
             'testing': [
             item('qa-cases', 'Тест-кейсы с шагами и ожидаемым результатом (TC-…)', 'artifact', art=0),
             item('qa-report', 'Отчёт: статусы Pass/Fail, дефекты, регресс, решение', 'artifact', art=1),
             item('qa-blockers', 'Нет открытых дефектов Blocker, Critical, Major — ни в отчёте, ни в реестре', 'machine', check='no_blockers', art=1),
             item('qa-ci', 'CI на развёрнутой тестовой ветке зелёный (последний прогон — на текущем коммите)', 'machine', check='ci_test_green'),
             item('qa-rtm', 'RTM дополнена тест-кейсами (TC-…)', 'machine', check='contains', art=2, needle='TC-'),
             item('qa-retest', 'Исправленные дефекты перепроверены: тест дефекта есть в тестовой ветке (или повтор описан в отчёте) '
                  'и CI доверенного раннера зелёный на текущем коммите теста', 'machine', check='defects_retested', art=1),
             item('qa-signoff', 'Решение о допуске в релиз (QA Sign-off)', 'machine', check='signoff')]}),

    'devops': dict(
        title='DevOps-инженер', stage=('deploy_test', 'deploy_main'), min_tier=1,
        mission='Развернуть прошедшую ревью работу в тестовой ветке для тестирования, а после допуска QA и ворот — '
                'в main: CI на каждом шаге, тег релиза, автоматический откат при красном CI.',
        scope=['Окружение и переменные документированы; секреты не в git.',
               'Сборка и проверки проходят на целевой ветке; слияние без конфликтов.',
               'Runbook: развёртывание, откат, проверка работоспособности.',
               'CI/CD и мониторинг — где они есть у проекта.'],
        artifacts=[doc(ART + '/runbook.md', ['Развёртывани', 'Откат', 'Проверка', 'Окружени'], 500),
                   doc(ART + '/release-notes.md', ['Что вошло', 'Откат'], 300)],
        dod={'deploy_test': [
                 item('do-runbook', 'Runbook: развёртывание, откат, проверка, окружение', 'artifact', art=0),
                 item('do-runbook-cmd', 'В runbook команды развёртывания и отката и версия выпуска — оператор восстановит без догадок',
                      'machine', check='rule:runbook', art=0),
                 item('do-spec-delta', 'Дельта ТЗ задачи (docs/tasks/<id>/spec-delta.md, если есть) влита в основное ТЗ проекта '
                      'одним коммитом; отчёт задачи переведён на spec_impact: updated', 'evidence', allow_na=True),
                 item('do-secrets', 'Секретов в изменениях нет', 'machine', check='no_secrets'),
                 item('do-ci', 'Конфигурация CI проекта есть и проверена (pm.py ci show)', 'machine', check='ci_config'),
                 item('do-monitoring', 'Метрики и оповещения настроены', 'evidence', allow_na=True),
                 item('do-merge-test', 'Ветка задачи и базовая ветка слиты в тестовую без конфликтов', 'machine', check='deploy_test', heavy=True),
                 item('do-verify-test', 'CI на тестовой ветке зелёный', 'machine', check='ci_test', heavy=True),
                 item('do-env-test', 'Тестовое окружение развёрнуто и отвечает (если у проекта есть развёртывание)', 'machine', check='env_test', heavy=True)],
             'deploy_main': [
                 item('dm-notes', 'Release notes: что вошло, как откатить', 'artifact', art=1),
                 item('dm-merge-main', 'Тестовая ветка слита в main без конфликтов', 'machine', check='deploy_main', heavy=True),
                 item('dm-verify-main', 'CI на main зелёный; при красном — слияние откачено автоматически', 'machine', check='ci_main', heavy=True),
                 item('dm-env-main', 'Рабочее окружение развёрнуто и отвечает (если у проекта есть развёртывание)', 'machine', check='env_main', heavy=True),
                 item('dm-tag', 'Тег релиза в main', 'machine', check='release_tag', heavy=True),
                 item('dm-push', 'Отправка в удалённый репозиторий — решение владельца', 'evidence', allow_na=True)]}),

    'teamlead': dict(
        title='Тимлид (оркестратор)', stage=None, min_tier=2,
        mission='Оркестровать задачи: принимать уведомления ролей и держать ворота «анализ → разработка» и '
                '«тест → main». Эскалации и проблемы агентов разбирает Решала.',
        scope=['Ворота анализа: артефакты аналитика, архитектора, дизайнера и маркетолога согласованы между собой и с BRD; '
               'команда может разрабатывать без двояких трактовок.',
               'Ворота релиза: тестирование и деплой в тест чисты; риски релиза приняты.',
               'Решение — approve или return с конкретными замечаниями к роли.'],
        artifacts=[],     # решение ворот — в журнале тимлида и в задаче, отдельный файл не нужен
        dod=[]),
    'resolver': dict(
        title='Решала (проблемы и эскалации)', stage=None, min_tier=2,
        mission='Разбирать проблемы и ограничения агентов: найти пути решения, выбрать лучший и вернуть работу в ход; '
                'владельца звать только когда без него решить нельзя.',
        scope=['Эскалации агентов: блокер, ограничение, нет доступа, противоречие в требованиях, конфликт ролей.',
               'Проблемы, найденные сервером: сессия заканчивалась без сдачи этапа, повторные провалы DoD, петли возвратов.',
               'Пути решения: не меньше двух, с плюсами и минусами; второе мнение — у другого ИИ (pm.py ask).',
               'Решение: продолжить с конкретными указаниями роли, вернуть на этап или позвать владельца — '
               'только если нужны его права, деньги, доступы, юридическое или продуктовое решение, которое агент принимать не вправе.'],
        artifacts=[],     # пути решения и решение — в реестре проблем и журнале тимлида
        dod=[]),
}

# DoR этапа: что должно быть верно, чтобы роли этапа взяли задачу. Проверяет сервер.
DOR = {
    'idea': [item('dor-idea', 'Задача заведена: проект, исходный запрос, описание идеи', 'machine', check='intake')],
    'analysis': [item('dor-brd', 'BRD утверждён: этап «Идея» сдан по DoD', 'machine', check='stage:idea'),
                 item('dor-scope', 'Границы проекта зафиксированы в BRD', 'machine', check='section', art='BRD.md', needle='Границ'),
                 item('dor-workspace', 'Рабочая копия задачи доступна для артефактов', 'machine', check='worktree')],
    'development': [item('dor-analysis', 'Анализ сдан всеми нужными ролями по DoD', 'machine', check='stage:analysis'),
                    item('dor-gate-analysis', 'Ворота тимлида «анализ → разработка» пройдены', 'machine', check='gate:analysis'),
                    item('dor-tasks', 'Задачи разработки с критериями приёмки есть (dev-tasks.md)', 'machine', check='artifact', art='dev-tasks.md'),
                    item('dor-test-design', 'Тест-дизайн QA готов до разработки (test-design.md)', 'machine', check='artifact', art='test-design.md'),
                    item('dor-conflicts', 'Нет открытых вопросов и противоречий в требованиях (CF-n)', 'machine', check='no_conflicts'),
                    item('dor-brd', 'BRD не менялся после анализа (иначе анализ по нему устарел)', 'machine', check='brd_current')],
    'review': [item('dor-dev', 'Разработка сдана по DoD: код, тесты, MR', 'machine', check='stage:development'),
               item('dor-mr', 'Описание изменений MR.md в ветке', 'machine', check='artifact', art='MR.md')],
    'deploy_test': [item('dor-review', 'Ревью сдано: approve, CI ветки зелёный, ветка слита в базовую', 'machine', check='stage:review')],
    'testing': [item('dor-deployed', 'Работа развёрнута в тестовой ветке, CI там зелёный', 'machine', check='stage:deploy_test')],
    'deploy_main': [item('dor-qa', 'Тестирование сдано: QA Sign-off, открытых дефектов нет', 'machine', check='stage:testing'),
                    item('dor-gate-release', 'Ворота тимлида «тест → main» пройдены', 'machine', check='gate:release'),
                    item('dor-evidence', 'Доказательства выпуска: CI зелёный на коммите теста, эталон цел, блокирующих находок ИБ нет',
                         'machine', check='release_evidence'),
                    item('dor-owner', 'Владелец одобрил релиз в main', 'machine', check='owner_release')],
}


# Кто и на чём работает в роли. prefer — порядок ИИ (первый свободный от лимита токенов);
# tier — класс модели по сложности задачи (1/2/3: простая, средняя, сложная), не ниже min_tier роли;
# effort — усилия по тому же классу сложности (у Codex выше high не бывает — урезается).
# Ревьюер — всегда другой ИИ, чем разработчик: перекрёстная проверка. Явное закрепление роли
# владельцем (настройки ролей) сильнее таблицы. Основание — сильные стороны моделей в каталоге
# (workflow.MODELS); это начальные эвристики, их уточняют метрики качества ролей и ИИ.
# Решение владельца 31.10.2026 (разбор недели, Б2 и В1–В7): старший класс и xhigh — только для сложных задач.
ROUTING = {
    'product_owner': dict(prefer=('claude', 'codex'), tier=(2, 2, 3), effort=('medium', 'high', 'xhigh'),
                          why='связный BRD — Claude Sonnet; Opus — для сложных продуктовых задач; Codex — запасной'),
    'system_analyst': dict(prefer=('claude', 'codex'), tier=(2, 2, 3), effort=('medium', 'high', 'xhigh'),
                           why='формализация требований, API и модели данных без двояких трактовок'),
    'solution_architect': dict(prefer=('claude', 'codex'), tier=(2, 2, 3), effort=('medium', 'high', 'xhigh'),
                               why='HLD и ADR по готовому ТЗ — Sonnet; Opus — для сложной архитектуры'),
    'designer': dict(prefer=('claude', 'codex'), tier=(2, 2, 2), effort=('medium', 'medium', 'high'),
                     why='сценарии, состояния экранов и спецификация интерфейса — средний класс достаточен'),
    'marketer': dict(prefer=('claude', 'codex'), tier=(2, 2, 2), effort=('medium', 'medium', 'high'),
                     why='аудитория, позиционирование и тексты — средний класс достаточен'),
    'developer': dict(prefer=('codex', 'claude'), tier=(2, 2, 3), effort=('medium', 'medium', 'high'),
                      why='код и тесты в нескольких файлах — сильная сторона GPT Sol/Astra; Claude — запасной'),
    'reviewer': dict(prefer=('claude', 'codex'), tier=(2, 2, 3), effort=('medium', 'high', 'xhigh'), cross='developer',
                     why='перекрёстная проверка по диффу ветки: другой ИИ, чем разработчик, видит его слепые зоны'),
    'qa': dict(prefer=('codex', 'claude'), tier=(2, 2, 2), effort=('medium', 'high', 'high'),
               why='тест-кейсы из RTM, прогоны и регресс на тестовой ветке — исполнительская работа среднего класса'),
    'devops': dict(prefer=('codex', 'claude'), tier=(1, 2, 2), effort=('medium', 'medium', 'medium'),
                   why='runbook и заметки к релизу; слияния и проверки делает сервер'),
    'teamlead': dict(prefer=('claude', 'codex'), tier=(2, 2, 3), effort=('medium', 'high', 'xhigh'),
                     why='ворота по сводке сервера (номера требований, ссылки, изменения) — Sonnet; Opus — для сложных'),
    'resolver': dict(prefer=('claude', 'codex'), tier=(2, 2, 3), effort=('high', 'high', 'xhigh'), cross='escalator',
                     why='механические проблемы закрывают рецепты сервера; Решале — содержательные; другой ИИ — свежий взгляд'),
}


def waits_for(stage, role, needs=None):
    """Роль этапа, которая должна сдать работу раньше этой (или None)."""
    dep = STAGE[stage].get('after', {}).get(role)
    return dep if dep and dep in stage_roles(stage, needs) else None


def stage_roles(stage, needs=None):
    """Роли, которые должны сдать этап: необязательные — по решению PO (needs)."""
    s = STAGE[stage]; needs = needs or {}
    return [r for r in s['roles'] if r not in s.get('optional', {}) or needs.get(s['optional'][r])]


def role_dod(role, stage):
    dod = ROLES[role]['dod']
    return dod[stage] if isinstance(dod, dict) else dod


def art_path(task_id, path):
    return path.format(id=task_id)


def stage_artifacts(role, stage):
    """Артефакты роли на этом этапе: у роли с несколькими этапами — только те, на которые ссылается DoD этапа."""
    arts = ROLES[role]['artifacts']
    if not isinstance(ROLES[role]['dod'], dict):
        return arts
    used = {i.get('art') for i in role_dod(role, stage) if isinstance(i.get('art'), int)}
    return [a for n, a in enumerate(arts) if n in used]


# ---------------------------------------------------------------- проверки артефактов в ветке

def _git(args, cwd, timeout=60):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=timeout)


def read_artifact(repo, branch, path):
    """Содержимое закоммиченного файла (или файлов каталога) в ветке задачи; None — нет."""
    if not repo or not branch:
        return None
    r = _git(['show', '%s:%s' % (branch, path)], repo)
    if r.returncode == 0 and not r.stdout.startswith('tree '):
        return r.stdout
    listing = _git(['ls-tree', '-r', '--name-only', branch, path.rstrip('/') + '/'], repo)
    files = [f for f in listing.stdout.splitlines() if f.strip()]
    if listing.returncode != 0 or not files:
        return None
    return '\n\n'.join(_git(['show', '%s:%s' % (branch, f)], repo).stdout for f in files)


def headings(text):
    return [l.strip('# ').lower() for l in (text or '').splitlines() if l.lstrip().startswith('#')]


def _heads(lines):
    """(номер строки, уровень, заголовок) вне блоков кода."""
    out, fence = [], False
    for i, l in enumerate(lines):
        if l.lstrip().startswith('```'):
            fence = not fence; continue
        m = None if fence else re.match(r'(#{1,4})\s', l)
        if m:
            out.append((i, len(m.group(1)), l.lstrip('#').strip()))
    return out


def headings_raw(text):
    """Заголовки Markdown как написаны (для оглавления в паспорте)."""
    return [h for _, _, h in _heads(text.splitlines())]


def sections(text):
    """[(заголовок, текст раздела с подразделами)] — для чтения документа по разделам."""
    lines = text.splitlines(); out = []
    heads = _heads(lines)
    for n, (i, lvl, h) in enumerate(heads):
        end = next((j for j, l2, _ in heads[n + 1:] if l2 <= lvl), len(lines))
        out.append((h, '\n'.join(lines[i:end])))
    return out


def tokens(text):
    """Оценка токенов текста: ASCII ~2.55 символа на токен, кириллица и прочее ~2.2.
    Калибровка TT-93 по точному счёту Claude (разница входа с документом в системном промпте и без):
    BRD, SRS, КОНВЕЙЕР.md и Python-модуль — ошибка в пределах ±9% (прежние 4 и 2.5 занижали на 10–34%)."""
    if not text:
        return 0
    latin = len(text.encode('ascii', 'ignore'))
    return int(latin / 2.55 + (len(text) - latin) / 2.2) + 1


# Полное чтение документа (TT-96) — не запрет, а потребность, обоснованная ролью и сложностью задачи.
# Вход этапа роль читает целиком по смыслу своей работы: аналитик переводит весь BRD, архитектор проектирует
# по всему SRS, QA покрывает его тестами. Разработчик, ревьюер, DevOps и тимлид берут разделы по кодам из INDEX.md.
PRIMARY_INPUTS = {
    'system_analyst': [('product_owner', 0)],
    'solution_architect': [('system_analyst', 0)],
    'designer': [('product_owner', 0)],
    'marketer': [('product_owner', 0)],
    'qa': [('system_analyst', 0)],
}
# По классу сложности задачи: free — документ не больше этого читается целиком без вопросов (раздел ненамного
# меньше); budget — сколько токенов запуск может прочитать целиком сверх этого с обоснованием (op=doc full=true why=…).
FULL_READ = {1: dict(free=3000, budget=20_000), 2: dict(free=5000, budget=50_000), 3: dict(free=8000, budget=120_000)}


def read_policy(t, role, tier):
    """Политика полного чтения запуска роли: порог, бюджет обоснованных чтений и вход этапа (пути)."""
    p = dict(FULL_READ.get(tier) or FULL_READ[2], tier=tier)
    p['primary'] = [art_path(t['id'], ROLES[r]['artifacts'][i]['path']) for r, i in PRIMARY_INPUTS.get(role, [])] if t.get('id') else []
    return p


def _code_key(c):
    p, num = c.split('-', 1)
    return p, [(0, int(x), '') if x.isdigit() else (1, 0, x) for x in re.split(r'[.-]', num)]


def _next_code(a, b):
    """b следует за a: та же группа, номер на единицу больше (ACC-01 → ACC-02, 3 → 4)."""
    ga, _, na = a.rpartition('-'); gb, _, nb = b.rpartition('-')
    return ga == gb and na.isdigit() and nb.isdigit() and int(nb) == int(na) + 1


def code_span(cs):
    """Коды сжатой строкой: FR-1–4, FR-7, NFR-1–2."""
    out, by = [], {}
    for c in sorted(cs, key=_code_key):
        p, num = c.split('-', 1)
        by.setdefault(p, []).append(num)
    for p, nums in by.items():
        run = []
        for x in nums + [None]:
            if x is not None and run and _next_code(run[-1], x):
                run.append(x); continue
            if run:
                out.append('%s-%s' % (p, run[0] if len(run) == 1 else '%s–%s' % (run[0], run[-1].rpartition('-')[2])))
            run = [x] if x is not None else []
    return ', '.join(out)


def doc_index(text):
    """Указатель документа для ИИ (Л2): номер раздела, уровень, заголовок, вес с подразделами в токенах,
    строки (для Read offset/limit) и коды требований, впервые названные в разделе. По указателю агент
    берёт один раздел, а не документ целиком."""
    lines = (text or '').splitlines(); heads = _heads(lines); seen = set(); out = []
    for n, (i, lvl, h) in enumerate(heads):
        end = next((j for j, l2, _ in heads[n + 1:] if l2 <= lvl), len(lines))
        own_end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        new = codes('\n'.join(lines[i:own_end])) - seen
        seen |= new
        out.append(dict(n=n + 1, level=lvl, title=h, tokens=tokens('\n'.join(lines[i:end])), lines=[i + 1, end],
                        own_end=own_end, codes=sorted(new, key=_code_key)))
    return out


def index_lines(idx, limit=40, max_level=4):
    """Указатель строками — компактно для паспорта и отказа на чтение целиком. max_level — глубина заголовков:
    в паспорте без INDEX.md — только верхние уровни, полный указатель — op=doc без section."""
    idx = [e for e in idx if e['level'] <= max_level]
    rows = ['%s%d %s · ~%dт · стр. %d–%d%s' % ('  ' * (e['level'] - 1), e['n'], e['title'][:80], e['tokens'], e['lines'][0],
                                             e['lines'][1], ' · ' + code_span(e['codes']) if e['codes'] else '') for e in idx[:limit]]
    if len(idx) > limit:
        rows.append('… ещё %d разделов: tt_work op=doc без section' % (len(idx) - limit))
    return rows


def section_by(text, want):
    """Раздел по номеру указателя («3»), коду требования («FR-3») или части заголовка.
    Код — раздел, где он впервые назван (без подразделов); номер и заголовок — с подразделами."""
    lines = (text or '').splitlines(); idx = doc_index(text); want = str(want or '').strip()
    if not idx:
        return None, []
    pick = None
    if want.isdigit():
        pick = next((e for e in idx if e['n'] == int(want)), None)
    elif CODE.fullmatch(want.upper()):
        code = want.upper()
        e = next((e for e in idx if code in e['codes']), None)
        if e:
            return dict(e, lines=[e['lines'][0], e['own_end']]), '\n'.join(lines[e['lines'][0] - 1:e['own_end']])
    else:
        hit = [e for e in idx if want.lower() in e['title'].lower()]
        pick = hit[0] if hit else None
    if not pick:
        return None, [e['title'] for e in idx]
    return pick, '\n'.join(lines[pick['lines'][0] - 1:pick['lines'][1]])


def check_document(text, spec):
    """Причина отказа или None."""
    if text is None:
        return 'нет файла %s в ветке задачи (закоммитьте его)' % spec['path']
    if len(text.encode('utf-8')) < spec['min_bytes']:
        return '%s слишком короткий: %d байт, нужно не меньше %d' % (spec['path'], len(text.encode('utf-8')), spec['min_bytes'])
    heads = headings(text)
    missing = [s for s in spec['sections'] if not any(s.lower() in h for h in heads)]
    if missing:
        return '%s: нет разделов (заголовков) — %s' % (spec['path'], ', '.join(missing))
    return None


GARBLED = re.compile(r'\?{4,}')      # кириллица, потерянная при записи не в UTF-8
SECRET = re.compile(r'(?:api[_-]?key|secret|token|passw(?:or)?d)\s*[:=]\s*["\']?[A-Za-z0-9_\-/+=]{16,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}', re.I)
LINK = re.compile(r'https?://[^\s)\]>"]+')
OPEN_DEFECT = re.compile(r'(?i)\b(blocker|critical|major|блокер|критич\w*|высок\w*)\b.*\b(open|opened|открыт\w*|new|новый)\b|'
                         r'\b(open|открыт\w*)\b.*\b(blocker|critical|major|блокер|критич\w*)\b')


class Context:
    """Всё, что нужно проверкам задачи: ветка, репозиторий, ветки проекта, данные передачи."""
    def __init__(self, task, project, worktree=None, handoff=None):
        self.t = task; self.project = project or {}; self.worktree = worktree or {}
        self.handoff = handoff or {}
        self.repo = self.project.get('inspection_path') or self.project.get('path')
        self.branch = self.worktree.get('branch')
        self.branches = branches(self.repo, self.project)

    def artifact(self, spec):
        return read_artifact(self.repo, self.branch, art_path(self.t['id'], spec['path']))

    def read(self, path):
        return read_artifact(self.repo, self.branch, path)


def branches(repo, project):
    """Ветки проекта: базовая (куда сливается задача), тестовая и основная."""
    conf = dict(project.get('branches') or {})
    def exists(b):
        return bool(repo) and _git(['rev-parse', '--verify', '--quiet', 'refs/heads/' + b], repo).returncode == 0
    if 'main' not in conf:
        conf['main'] = 'main' if exists('main') or not exists('master') else 'master'
    if 'test' not in conf:
        conf['test'] = next((b for b in ('тестирование', 'test', 'testing', 'staging') if exists(b)), 'test')
    if 'base' not in conf and repo:
        current = _git(['rev-parse', '--abbrev-ref', 'HEAD'], repo).stdout.strip()
        conf['base'] = current if current and current not in ('HEAD', conf['test'], conf['main']) else (
            next((b for b in ('разработка', 'develop', 'dev') if exists(b)), conf['main']))
    return conf


def _role_art(role, idx):
    return ROLES[role]['artifacts'][idx]


def research(ctx):
    """Нужно ли задаче исследование рынка: PO решает needs.research; по умолчанию — да (продуктовая задача)."""
    needs = ctx.handoff.get('needs') or (ctx.t.get('pipeline') or {}).get('needs') or {}
    return needs.get('research', True) is not False


def effective(spec, ctx):
    """Требования к документу с учётом задачи: разделы о рынке и объём — только при исследовании."""
    if not spec.get('market') or not research(ctx):
        return spec
    return dict(spec, sections=spec['sections'] + spec['market'], min_bytes=spec.get('market_bytes', spec['min_bytes']))


def check_item(it, role, ctx, state):
    """(ok, detail). Тяжёлые проверки (тесты, слияния) — только в фоновом задании."""
    entry = (ctx.handoff.get('items') or {}).get(it['id']) or {}
    status, evidence = entry.get('status'), (entry.get('evidence') or '').strip()
    if status == 'na':
        if not it.get('allow_na'):
            return False, 'пункт нельзя пометить «неприменимо»'
        if it.get('na_when') == 'no_research' and research(ctx):
            return False, 'продуктовой задаче исследование рынка нужно; техническая — needs.research=false с причиной'
        if len(evidence) < MIN_NA_REASON:
            return False, 'для «неприменимо» нужна причина (не короче %d символов)' % MIN_NA_REASON
        return True, 'неприменимо: ' + evidence
    if GARBLED.search(evidence):
        return False, 'в доказательстве «????» вместо текста: пиши в UTF-8 (рецепт Р4)'
    kind = it['kind']
    if kind == 'evidence':
        if len(evidence) < MIN_EVIDENCE:
            return False, 'нужно доказательство: что сделано и где (не короче %d символов)' % MIN_EVIDENCE
        return True, evidence
    if kind == 'artifact':
        spec = effective(_role_art(role, it['art']), ctx)
        text = ctx.artifact(spec)
        if text and GARBLED.search(text):
            return False, '%s: «????» вместо текста — файл записан не в UTF-8, перепиши (рецепт Р4)' % art_path(ctx.t['id'], spec['path'])
        reason = check_document(text, spec)
        return (reason is None), (reason or 'есть: ' + art_path(ctx.t['id'], spec['path']))
    if kind == 'machine':
        return machine(it, role, ctx, state, evidence)
    return False, 'неизвестный вид пункта'


# Коды требований и решений: где каждый вид определяется (Б6). FR/NFR/SR/UC/US — в SRS, BR — в BRD,
# API — в api.md (или SRS), ADR — в файлах adr/, TC — в тест-кейсах QA.
# Коды требований: простые (FR-3, FR-2.1) и составные с группой (FR-POL-04, NFR-PERF-01) — в EGE составных
# больше, чем простых, и без них сверка и реестр видели меньше трети требований.
CODE = re.compile(r'\b(FR|NFR|SR|BR|UC|US|API|ADR|TC)-((?:[A-Z][A-Z0-9]{1,7}-)*\d+(?:\.\d+)?)\b')
DEFINED_IN = {'FR': ('system_analyst', 0), 'NFR': ('system_analyst', 0), 'SR': ('system_analyst', 0),
              'UC': ('system_analyst', 0), 'US': ('system_analyst', 0), 'BR': ('product_owner', 0),
              'API': ('system_analyst', 2), 'ADR': ('solution_architect', 1), 'TC': ('qa', 0)}


def codes(text):
    return {'%s-%s' % m for m in CODE.findall(text or '')}


def analysis_docs(ctx):
    """Тексты артефактов анализа в ветке задачи: {(роль, номер артефакта): текст}; каталог (adr/) — его файлы
    подряд. Нет api.md — контракты в SRS."""
    docs = {}
    for role in ('product_owner', 'system_analyst', 'solution_architect', 'designer'):
        for i, a in enumerate(ROLES[role]['artifacts']):
            text = ctx.artifact(a)
            if text is not None:
                docs[(role, i)] = text
    if ('system_analyst', 2) not in docs and ('system_analyst', 0) in docs:
        docs[('system_analyst', 2)] = docs[('system_analyst', 0)]
    return docs


def source_path(task_id, code, docs=None):
    """Документ, где определяется код (FR — SRS, ADR — HLD…); API без api.md — в SRS."""
    role, i = DEFINED_IN[code.split('-')[0]]
    if docs is not None and code.startswith('API-') and (role, i) in docs and docs[(role, i)] is docs.get(('system_analyst', 0)):
        i = 0
    return art_path(task_id, ROLES[role]['artifacts'][i]['path'])


def _plain(line):
    """Строка Markdown без разметки: ячейки таблицы через « · », без решёток, звёздочек и обратных кавычек."""
    l = line.strip()
    if l.startswith('|'):
        l = ' · '.join(c.strip() for c in l.strip('|').split('|') if c.strip() and not set(c.strip()) <= set('-: '))
    return re.sub(r'[#*`>]+', '', l).strip(' -·:\t')


def definition(text, code):
    """Где и как код определён в своём документе: первая строка с кодом, её суть, номер раздела указателя."""
    lines = text.splitlines(); idx = doc_index(text)
    for i, line in enumerate(lines):
        if code not in codes(line):
            continue
        gist = _plain(line)
        if len(gist) < len(code) + 8:            # заголовок «FR-3» без текста — суть в следующей строке
            nxt = next((x for x in lines[i + 1:i + 4] if x.strip()), '')
            gist = (gist + ' — ' + _plain(nxt)).strip(' —')
        sec = next((e for e in reversed(idx) if e['lines'][0] <= i + 1), None)
        return dict(line=i + 1, section=sec['n'] if sec else None, gist=gist[:160])
    return None


def registry(ctx, docs=None):
    """Реестр требований задачи (TT-92): код, суть, документ, раздел и строка определения — по артефактам ветки."""
    docs = analysis_docs(ctx) if docs is None else docs
    out = []
    for code in sorted(set().union(*[codes(t) for t in docs.values()] or [set()]), key=_code_key):
        src = DEFINED_IN[code.split('-')[0]]
        if src not in docs:
            continue
        d = definition(docs[src], code)
        if d:
            out.append(dict(code=code, path=source_path(ctx.t['id'], code, docs),
                            dir=bool(ROLES[src[0]]['artifacts'][src[1]].get('dir')), **d))
    return out


def index_path(task_id):
    return art_path(task_id, ART) + '/INDEX.md'


def task_index(ctx):
    """Указатель задачи для любого агента — Claude, Codex, человека (TT-92): документы анализа с разделами,
    весом и строками и реестр требований с сутью и местом определения. Сервер коммитит его в ветку задачи."""
    docs = analysis_docs(ctx); tid = ctx.t['id']
    lines = ['# Указатель задачи %s' % tid, '',
             'Собирает сервер TaskTreker при сдаче этапа — не править. Читай отсюда и бери только нужный раздел: '
             'Read с offset/limit по строкам или `tt_work op=doc path=… section=<номер>` / `code=<FR-3>`.', '',
             '## Документы', '']
    seen = set()
    for (role, i), text in sorted(docs.items()):
        path = art_path(tid, ROLES[role]['artifacts'][i]['path'])
        if path in seen:
            continue
        seen.add(path)
        if ROLES[role]['artifacts'][i].get('dir'):       # строки склеенных файлов каталога ни на что не указывают
            lines += ['### %s/ · ~%d токенов · каталог: файлы короткие, читай нужный по имени' % (path, tokens(text)), '']
            continue
        lines += ['### %s · ~%d токенов' % (path, tokens(text)), ''] + index_lines(doc_index(text), 60) + ['']
    reg = registry(ctx, docs)
    if reg:
        lines += ['## Реестр требований', '', '| Код | Суть | Где |', '|---|---|---|']
        lines += ['| %s | %s | %s |' % (r['code'], r['gist'].replace('|', '/'),
                                        r['path'].split('/')[-1] + '/' if r['dir'] else
                                        '%s §%s, стр. %d' % (r['path'].split('/')[-1], r['section'] or '—', r['line']))
                  for r in reg]
    return '\n'.join(lines) + '\n'


def trace(ctx):
    """Сверка кодов в артефактах анализа: что определено, на что ссылаются, что не попало в RTM."""
    docs = analysis_docs(ctx)
    defined = {}
    for code in set().union(*[codes(t) for t in docs.values()] or [set()]):
        src = DEFINED_IN[code.split('-')[0]]
        if src in docs and code in codes(docs[src]):
            defined[code] = src
    dangling = {}
    for (role, i), text in docs.items():
        for code in codes(text):
            src = DEFINED_IN[code.split('-')[0]]
            if src[0] == 'qa' or code in defined:
                continue                     # тест-кейсы пишет QA позже; определённое — в порядке
            if src in docs or src[0] in ('system_analyst', 'product_owner', 'solution_architect'):
                dangling.setdefault(code, set()).add(ROLES[role]['artifacts'][i]['path'].split('/')[-1])
    srs = codes(docs.get(('system_analyst', 0)))
    rtm = codes(docs.get(('system_analyst', 4)))
    reqs = sorted(c for c in srs if c.split('-')[0] in ('FR', 'NFR', 'SR', 'UC', 'US'))
    untraced = [c for c in reqs if c not in rtm] if ('system_analyst', 4) in docs else []
    by = {}
    for c in defined:
        by[c.split('-')[0]] = by.get(c.split('-')[0], 0) + 1
    return dict(defined=by, requirements=len(reqs), dangling={k: sorted(v) for k, v in sorted(dangling.items())},
                untraced=untraced, docs=len(docs))


def machine(it, role, ctx, state, evidence=''):
    check = it['check']; t = ctx.t
    if check == 'trace':
        tr = trace(ctx)
        if role == 'system_analyst' and not tr['requirements']:
            return False, 'в SRS нет кодов требований (FR-1, NFR-1, SR-1…): без них RTM и тесты не связать'
        dangling = {k: v for k, v in tr['dangling'].items() if role == 'system_analyst' or not k.startswith(('BR', 'API'))}
        bad = ['%s (в %s) не определён' % (k, ', '.join(v)) for k, v in list(dangling.items())[:8]]
        if role == 'system_analyst':
            bad += ['%s нет в RTM' % c for c in tr['untraced'][:8]]
        if bad:
            return False, 'коды не сходятся: ' + '; '.join(bad)
        return True, 'требований %d, все в RTM; висящих ссылок нет (%s)' % (
            tr['requirements'], ', '.join('%s %d' % kv for kv in sorted(tr['defined'].items())))
    if check.startswith('rule:'):
        import artifact_rules as ar
        name = check.split(':', 1)[1]; spec = _role_art(role, it['art']); text = ctx.artifact(spec)
        if text is None and name not in ar.OPTIONAL:
            return False, 'нет файла %s в ветке задачи' % spec['path']
        return ar.check(name, text, ctx, it)
    if check in ('links', 'mermaid', 'table', 'contains', 'no_blockers'):
        spec = _role_art(role, it['art']); text = ctx.artifact(spec)
        if text is None:
            return False, 'нет файла %s в ветке задачи' % spec['path']
        if check == 'links':
            n = len(set(LINK.findall(text)))
            return n >= it['min'], 'ссылок на источники: %d, нужно не меньше %d' % (n, it['min'])
        if check == 'mermaid':
            n = text.count('```mermaid')
            return n >= it['min'], 'Mermaid-диаграмм: %d, нужно не меньше %d' % (n, it['min'])
        if check == 'table':
            rows = [l for l in text.splitlines() if l.strip().startswith('|') and l.count('|') >= 3]
            return len(rows) >= 3, 'строк таблицы: %d (нужна шапка, разделитель и данные)' % len(rows)
        if check == 'contains':
            return it['needle'] in text, ('найдено «%s»' % it['needle']) if it['needle'] in text else 'нет «%s»' % it['needle']
        bad = [l.strip() for l in text.splitlines() if OPEN_DEFECT.search(l)]
        reg = [d['id'] for d in (t.get('pipeline') or {}).get('defects') or []
               if d.get('status') == 'open' and d.get('severity') in ('critical', 'major')]
        if reg:
            bad.append('в реестре открыты: ' + ', '.join(reg))
        return not bad, ('открытые дефекты: ' + '; '.join(bad[:3])) if bad else 'открытых Blocker/Critical/Major нет'
    if check == 'contract_current':
        why = contract_problem(ctx.t.get('pipeline'))
        return (False, why) if why else (True, 'контракт группы актуален' if (ctx.t.get('pipeline') or {}).get('contract_group') else 'задача вне группы контрактов')
    if check == 'design_covers_ac':
        text = ctx.artifact(ROLES['qa']['artifacts'][it['art']]) or ''
        acc = [a for a in t.get('acceptance') or [] if isinstance(a, str) and a.strip()]
        low = text.lower()
        miss = [i + 1 for i, a in enumerate(acc) if 'ac-%d' % (i + 1) not in low and a.strip().lower()[:40] not in low]
        if not acc:
            return False, 'у задачи нет критериев приёмки — тест-дизайну нечего покрывать'
        return not miss, ('все %d критериев приёмки покрыты (AC-n или текст критерия)' % len(acc)) if not miss else \
            'не покрыты критерии приёмки: %s — сошлитесь на AC-n в test-design.md' % ', '.join('AC-%d' % i for i in miss)
    if check == 'acceptance':
        acc = ctx.handoff.get('acceptance') or t.get('acceptance') or []
        ok = isinstance(acc, list) and len([a for a in acc if isinstance(a, str) and len(a.strip()) >= 10]) >= 2
        return ok, 'критериев приёмки: %d' % (len(acc) if isinstance(acc, list) else 0)
    if check == 'needs':
        needs = ctx.handoff.get('needs') or (t.get('pipeline') or {}).get('needs')
        ok = isinstance(needs, dict) and type(needs.get('design')) == bool and type(needs.get('marketing')) == bool \
            and needs.get('research', True) in (True, False) and len(str(needs.get('reason') or '')) >= MIN_NA_REASON
        return ok, 'needs: design/marketing (и research для технической задачи) — да или нет, с причиной' if not ok else \
            'дизайн: %s, маркетинг: %s, исследование рынка: %s' % ('да' if needs['design'] else 'нет', 'да' if needs['marketing'] else 'нет',
                                                              'да' if research(ctx) else 'нет')
    if check == 'verdict':
        v = ctx.handoff.get('verdict')
        return v == 'approve', 'вердикт: %s' % (v or 'не указан')
    if check == 'ac_matrix':
        return ac_matrix_check(ctx)
    if check == 'reviewed_sha':
        sha = str(ctx.handoff.get('reviewed_sha') or '').strip().lower()
        head = _git(['rev-parse', ctx.branch], ctx.repo).stdout.strip() if ctx.repo and ctx.branch else ''
        if len(sha) < 7:
            return False, 'reviewed_sha: укажи голову ветки, которую читал (git rev-parse HEAD, не короче 7 знаков)'
        return head.startswith(sha), ('ревью на текущей голове %s' % head[:10]) if head.startswith(sha) else \
            'ревью сделано на %s, а голова ветки %s — перечитай свежий дифф' % (sha[:10], head[:10])
    if check == 'signoff':
        return ctx.handoff.get('signoff') is True, 'QA Sign-off: %s' % ('да' if ctx.handoff.get('signoff') is True else 'нет')
    if check in ('committed', 'mergeable', 'tests_changed', 'no_secrets'):
        return git_check(check, ctx, evidence)
    if check == 'oracle_intact':
        import trusted_oracle as to
        ok, detail, _ = to.intact(t.get('pipeline'), t.get('ref'), ctx.repo, ctx.branches.get('base'), ctx.branch, ctx.project)
        return ok, detail
    if check == 'ci_config':
        import ci
        cfg = ci.config(ctx.project); err = ci.validate(cfg)
        if err:
            return False, 'конфигурация CI с ошибкой: ' + err
        if not cfg['steps']:
            return False, 'у проекта нет шагов CI: задайте поле ci в projects.json или команду verify'
        return True, 'CI: %d шагов (%s)' % (len(cfg['steps']), cfg['source'])
    if check == 'defects_retested':
        # VERIFIED — только после PASS доверенного раннера на актуальном SHA (07: QA-08), не по слову QA.
        fixed = [d for d in (t.get('pipeline') or {}).get('defects') or [] if d.get('status') == 'fixed']
        if not fixed:
            return True, 'исправленных дефектов к перепроверке нет'
        import ci
        test = ctx.branches.get('test')
        head = ci.head_sha(ctx.repo, 'refs/heads/' + test) if ctx.repo and test else ''
        if not head:
            return False, 'нет коммита тестовой ветки — перепроверку не с чем сверить'
        ok_ci, why_ci = ci.green(ci.last(project=t.get('project'), scope='test', branch=test), head)
        report = (ctx.artifact(ROLES['qa']['artifacts'][it['art']]) or '').lower()
        bad = []
        for d in fixed:
            path = str(d.get('test') or '').split('::')[0].strip()
            if path and ('/' in path or path.endswith(('.py', '.js', '.ts', '.go', '.rs', '.java', '.cs'))):
                if not read_file_at(ctx.repo, test, path):
                    bad.append('%s: теста %s нет в тестовой ветке' % (d['id'], path))
            elif path:                                   # тест-кейс QA (TC-n): повтор — в отчёте о тестировании
                if path.lower() not in report:
                    bad.append('%s: тест-кейс %s не упомянут в test-report.md — повтор не описан' % (d['id'], path))
            elif d['id'].lower() not in report:
                bad.append('%s: у дефекта нет теста, а повтор шагов не описан в test-report.md' % d['id'])
        if not ok_ci:
            bad.append('CI теста: ' + why_ci)
        return not bad, '; '.join(bad[:4]) if bad else 'перепроверено %d: тесты на месте, CI зелёный на %s' % (len(fixed), head[:7])
    if check == 'ci_test_green':
        import ci
        repo = ctx.repo; test = ctx.branches.get('test')
        run = ci.last(project=ctx.t.get('project'), scope='test', branch=test)
        if not run:
            return False, 'на тестовой ветке ещё не было прогона CI'
        # Сверка с текущей головой тестовой ветки обязательна (TT3-013): не нашли голову — не одобряем.
        head = ci.head_sha(repo, 'refs/heads/' + test) if repo and test else ''
        if not head:
            return False, 'не удалось определить HEAD тестовой ветки %s — прогон не с чем сверить' % (test or '?')
        return ci.green(run, head)
    if check == 'mutation':
        return mutation_check(ctx, state)
    if check.startswith('verify') or check.startswith('ci_') or check.startswith('env_') or \
            check in ('merge_to_base', 'deploy_test', 'deploy_main', 'release_tag'):
        if not state.get('heavy'):
            return None, 'проверит сервер в фоне'
        import deploy
        return deploy.run(check, ctx, evidence)
    return False, 'неизвестная проверка ' + check


def mutation_check(ctx, state):
    """Мутационная проверка (07: A6-08) — только для расчётного маршрута CRITICAL: там слабые тесты дороже всего."""
    import adaptive_pipeline as ap
    r = ap.route(ctx.t.get('pipeline'))
    if 'CRITICAL' not in (r.get('applied'), (r.get('plan') or {}).get('mode')):
        return True, 'не требуется: задача не CRITICAL'
    if not state.get('heavy'):
        return None, 'проверит сервер в фоне'
    import mutation, local_mimo as lm
    repo, branch, base = ctx.repo, ctx.branch, ctx.branches.get('base')
    files = [f for f in _git(['diff', '--name-only', '%s...%s' % (base, branch)], repo).stdout.splitlines() if f.strip()] if branch else []
    cmd = lm.verify_commands(ctx.project).get('verify')
    res = mutation.run(ctx.worktree.get('path') or repo, files, cmd)
    return res['status'] != 'failed', mutation.summary(res)


def read_file_at(repo, branch, path):
    """Есть ли файл в ветке (для ссылок ревьюера и дефектов)."""
    if not repo or not branch or not path or '..' in path: return False
    return _git(['cat-file', '-e', '%s:%s' % (branch, path.lstrip('/'))], repo).returncode == 0


def brd_digest(ctx):
    import hashlib
    text = ctx.artifact(ROLES['product_owner']['artifacts'][0])
    return 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest()[:24] if text is not None else None


def brd_current(ctx, pipe):
    """BRD, по которому сделан анализ, не менялся (AG-PO-05): иначе SRS, HLD и тест-дизайн описывают другое."""
    want = (pipe or {}).get('brd_digest')
    if not want:
        return True, 'отпечаток BRD не записан (задача до этой проверки)'
    got = brd_digest(ctx)
    return got == want, 'BRD тот же, что при сдаче PO' if got == want else \
        'BRD изменён после сдачи PO (%s → %s): анализ по нему устарел — верни на «Идея» или «Анализ»' % (want[7:15], (got or 'нет')[7:15])


AC_VERDICTS = ('ok', 'gap', 'defect')


def ac_matrix_check(ctx):
    """Ревью по каждому AC и каждому изменённому файлу кода, а не по тексту автора (07: A7-01, AG-REV-01..03)."""
    acc = [a for a in ctx.t.get('acceptance') or [] if isinstance(a, str) and a.strip()]
    m = ctx.handoff.get('ac_matrix')
    if not acc:
        return True, 'у задачи нет критериев приёмки — матрица не требуется'
    if not isinstance(m, dict):
        return False, 'нет ac_matrix: {AC-1: {files, tests, verdict}, …} по %d критериям' % len(acc)
    repo, branch, base = ctx.repo, ctx.branch, ctx.branches.get('base')
    diff = [f for f in _git(['diff', '--name-only', '%s...%s' % (base, branch)], repo).stdout.splitlines() if f.strip()] if branch else []
    code = [f for f in diff if not f.startswith('docs/tasks/') and not f.lower().endswith('.md')]
    bad, seen = [], set()
    for i in range(1, len(acc) + 1):
        e = m.get('AC-%d' % i)
        if not isinstance(e, dict):
            bad.append('AC-%d: нет строки' % i); continue
        if e.get('verdict') not in AC_VERDICTS:
            bad.append('AC-%d: verdict ok|gap|defect' % i); continue
        refs = [re.sub(r':\d+(-\d+)?$', '', str(x).strip()) for x in list(e.get('files') or []) + list(e.get('tests') or [])]
        if not refs:
            bad.append('AC-%d: нет файлов и тестов' % i); continue
        missing = [r for r in refs if not read_file_at(repo, branch, r)]
        if missing:
            bad.append('AC-%d: нет в ветке %s' % (i, ', '.join(missing[:3])))
        seen.update(refs)
        if e['verdict'] != 'ok':
            bad.append('AC-%d: %s — одобрить нельзя, верни задачу (op=return)' % (i, e['verdict']))
    loose = [f for f in code if f not in seen]
    if loose:
        bad.append('изменённые файлы без критерия: ' + ', '.join(loose[:5]))
    return not bad, '; '.join(bad[:6]) if bad else 'все %d критериев и %d изменённых файлов кода проверены' % (len(acc), len(code))


def git_check(check, ctx, evidence):
    repo, branch, base = ctx.repo, ctx.branch, ctx.branches.get('base')
    if not repo or not branch:
        return False, 'у задачи нет ветки'
    if check == 'committed':
        n = _git(['rev-list', '--count', '%s..%s' % (base, branch)], repo).stdout.strip() or '0'
        dirty = [l for l in _git(['status', '--porcelain'], ctx.worktree.get('path') or repo).stdout.splitlines() if l.strip()]
        ok = int(n) > 0 and not dirty
        return ok, 'коммитов поверх %s: %s; незакоммичено: %d' % (base, n, len(dirty))
    if check == 'mergeable':
        r = _git(['merge-tree', '--write-tree', '--name-only', base, branch], repo)
        return r.returncode == 0, 'сливается чисто' if r.returncode == 0 else 'конфликт с %s: %s' % (base, r.stdout[-200:])
    files = [f for f in _git(['diff', '--name-only', '%s...%s' % (base, branch)], repo).stdout.splitlines() if f.strip()]
    if check == 'tests_changed':
        tests = [f for f in files if re.search(r'(^|/)(tests?|spec|__tests__)/|(_test|\.test|\.spec)\.\w+$|test_\w+\.py$', f)]
        return bool(tests), ('тесты: ' + ', '.join(tests[:5])) if tests else 'в изменениях нет файлов тестов'
    diff = _git(['diff', '%s...%s' % (base, branch)], repo).stdout
    added = [l for l in diff.splitlines() if l.startswith('+') and not l.startswith('+++')]
    found = [l[:60] for l in added if SECRET.search(l)]
    return not found, 'похожие на секреты строки: %d' % len(found) if found else 'секретов в изменениях нет'


def check_dor(stage, ctx, pipe):
    """Пункты DoR этапа: [(item, ok, detail)]."""
    import adaptive_pipeline as ap
    out = []; skipped = set(ap.skipped_stages(pipe))
    for it in DOR[stage]:
        check = it['check']
        gate_stage = next((x['id'] for x in STAGES if x.get('gate') == check.split(':', 1)[1]), None) if check.startswith('gate:') else None
        if (check.startswith('stage:') and check.split(':', 1)[1] in skipped) or gate_stage in skipped:
            out.append((it, True, 'не требуется маршрутом FAST')); continue
        if check == 'artifact' and it.get('art') in ('dev-tasks.md', 'test-design.md') and 'analysis' in skipped:
            ok = bool(ctx.t.get('acceptance'))
            out.append((it, ok, 'маршрут FAST: критерии приёмки задачи вместо dev-tasks.md' if ok else
                        'маршрут FAST без критериев приёмки задачи')); continue
        if check == 'intake':
            ok = bool(ctx.t.get('root') and ctx.t.get('title') and ctx.t.get('why')); detail = 'исходный запрос и описание есть' if ok else 'нет исходного запроса или описания'
        elif check.startswith('stage:'):
            st = check.split(':', 1)[1]; done = (pipe.get('stages') or {}).get(st, {}).get('passed')
            ok, detail = bool(done), 'этап «%s» сдан' % STAGE[st]['title'] if done else 'этап «%s» не сдан' % STAGE[st]['title']
        elif check.startswith('gate:'):
            g = (pipe.get('gates') or {}).get(check.split(':', 1)[1]) or {}
            ok, detail = g.get('decision') == 'approve', 'ворота: %s' % (g.get('decision') or 'не пройдены')
        elif check == 'owner_release':
            # Подписанное одобрение этой задачи на текущем коммите тестовой ветки (TT3-014).
            import ci, release_policy as rp, workflow as w
            head = ci.head_sha(ctx.repo, 'refs/heads/' + ctx.branches['test']) if ctx.repo else ''
            ok, detail = rp.release_ok(w.state(), pipe, ctx.t.get('ref'), ctx.t.get('project'), ctx.branches['main'], head, w.now())
        elif check == 'release_evidence':
            ok, detail = release_evidence(ctx, pipe)
        elif check == 'worktree':
            ok = bool(ctx.branch); detail = 'ветка %s' % ctx.branch if ok else 'нет рабочей копии задачи'
        elif check == 'brd_current':
            ok, detail = brd_current(ctx, pipe)
        elif check == 'no_conflicts':
            opened = [c['id'] for c in pipe.get('conflicts') or [] if c['status'] == 'open']
            ok = not opened; detail = 'открытых вопросов нет' if ok else 'открыты: ' + ', '.join(opened)
        elif check in ('section', 'artifact'):
            text = read_artifact(ctx.repo, ctx.branch, art_path(ctx.t['id'], ART + '/' + it['art']))
            ok = text is not None and (check == 'artifact' or any(it['needle'].lower() in h for h in headings(text)))
            detail = ('%s есть' % it['art']) if ok else ('нет %s%s' % (it['art'], '' if check == 'artifact' else ' с разделом «%s»' % it['needle']))
        else:
            ok, detail = False, 'неизвестная проверка'
        out.append((it, ok, detail))
    return out


CONTRACT_ARTS = ('api.md', 'data-model.md')


def contract_digest(ctx):
    """Хеш контрактов задачи (api.md, data-model.md аналитика) или None, если их нет."""
    import hashlib
    parts = []
    for name in CONTRACT_ARTS:
        text = ctx.read(art_path(ctx.t['id'], ART + '/' + name))
        if text:
            parts.append(name + '\n' + text.replace('\r\n', '\n').strip())
    return hashlib.sha256('\n\n'.join(parts).encode('utf-8')).hexdigest()[:16] if parts else None


def contract_problem(pipe):
    """Контракт группы изменён другой задачей после анализа этой (07: A5-06, AG-SA-08, E2E-R30)."""
    st = (pipe or {}).get('contract_stale')
    if not st:
        return None
    return ('контракт группы «%s» изменён задачей %s (%s) после анализа этой задачи: код опирается на старый контракт — '
            'верните задачу в анализ (op=return to_stage=analysis), аналитик сверит ТЗ с новым контрактом' % (
                st.get('group'), st.get('by'), (st.get('at') or '')[:10]))


def role_zone(role, task, scope=None):
    """Что роли разрешено менять в ветке задачи (07: A2-01, A7-02, AG-REV-02, AG-DES-09, AG-MKT-06, AG-RES-05).
    Разработчик — код задачи (в пределах scope, если он задан), документы задачи и тесты; остальные роли —
    только свои артефакты в docs/tasks/<id>/. Эталон приёмки разработчику проверяет trusted_oracle."""
    docs = art_path(task['id'], ART) + '/'
    if role == 'developer':
        return dict(docs=docs, scope=[x for x in (scope or []) if x], tests=True)
    return dict(docs=docs, scope=None, tests=False)


def zone_violations(role, task, files, scope=None):
    import risk_policy as rpol
    z = role_zone(role, task, scope)
    bad = []
    for f in files:
        f = f.replace('\\', '/')
        if f.startswith(z['docs']):
            continue
        if role != 'developer':
            bad.append(f); continue
        if z['scope'] and not rpol._within(f.lower(), z['scope']) and not (z['tests'] and TEST_FILE.search(f)):
            bad.append(f)
    return bad


TEST_FILE = re.compile(r'(^|/)(tests?|spec|__tests__)/|(_test|\.test|\.spec)\.\w+$|test_\w+\.py$')


def ac_digest(acceptance):
    import hashlib
    return 'sha256:' + hashlib.sha256(json.dumps([str(a).strip() for a in acceptance or []], ensure_ascii=False).encode('utf-8')).hexdigest()[:24]


def ac_frozen(task, pipe):
    """(ok, detail): критерии приёмки те же, что на старте разработки."""
    fz = (pipe or {}).get('ac_freeze')
    if not fz:
        return True, 'заморозки критериев нет (задача начата до правила)'
    if fz['digest'] == ac_digest(task.get('acceptance')):
        return True, 'критерии приёмки не менялись с начала разработки (%d)' % fz.get('count', 0)
    return False, 'критерии приёмки изменены после начала разработки — изменение требований идёт возвратом в анализ'


def release_evidence(ctx, pipe):
    """Только доверенные доказательства разрешают выпуск (SEC-011): CI passed на текущем коммите тестовой ветки,
    эталон приёмки задачи не изменён без владельца, нет открытых High/Critical находок ИБ и обязательных NOT_RUN.
    Пишет запись SecurityEvidence в pipe['evidence'] — её формирует сервер, текст агента сюда не попадает."""
    import ci, trusted_oracle as to, security_triage as st, workflow as w
    ref, test = ctx.t.get('ref'), ctx.branches.get('test')
    head = ci.head_sha(ctx.repo, 'refs/heads/' + test) if ctx.repo and test else ''
    if not head:
        return False, 'нет коммита тестовой ветки %s — выпуск не с чем сверить' % (test or '?')
    run = ci.last(project=ctx.t.get('project'), scope='test', branch=test)
    bad = []
    ok_ci, why_ci = ci.green(run, head)
    if not ok_ci:
        bad.append('CI: ' + why_ci)
    ok_or, why_or, _ = to.intact(pipe, ref, ctx.repo, ctx.branches.get('base'), ctx.branch, ctx.project)
    if not ok_or:
        bad.append(why_or)
    files = [f for f in _git(['diff', '--name-only', '%s...%s' % (ctx.branches.get('base'), ctx.branch)], ctx.repo).stdout.splitlines()
             if f.strip()] if ctx.branch else None
    blocking = st.blocking(ctx.t.get('project'), ref, files=files, sha=head)
    why_contract = contract_problem(pipe)
    if why_contract:
        bad.append(why_contract)
    ok_ac, why_ac = ac_frozen(ctx.t, pipe)
    if not ok_ac:
        bad.append(why_ac)
    if blocking:
        bad.append('находки ИБ: ' + '; '.join(blocking[:3]))
    s = w.state()
    ev = to.evidence(ref, (run or {}).get('id'), ctx.repo, head, ctx.project, w.settings(s), 'release_evidence',
                     'failed' if bad else 'passed', ci_run=run)
    lst = [e for e in pipe.get('evidence') or [] if e.get('commit_sha') != head][-19:]
    pipe['evidence'] = lst + [ev]
    if bad:
        return False, '; '.join(bad)
    return True, 'CI passed на %s, эталон цел, блокирующих находок ИБ нет' % head[:7]


def evaluate_role(role, stage, ctx, heavy=False):
    """Пункты DoD роли: [(item, ok, detail)]; ok=None — ждёт фоновой проверки."""
    state = dict(heavy=heavy)
    return [(it, *check_item(it, role, ctx, state)) for it in role_dod(role, stage)]


def failures(results):
    return [dict(id=it['id'], text=it['text'], detail=detail) for it, ok, detail in results if ok is False]


def pending_heavy(results):
    return [it['id'] for it, ok, _ in results if ok is None]


def spec_for_board():
    """Компактная спецификация для доски: этапы, роли, DoR, DoD."""
    def pub(it):
        return {k: it[k] for k in ('id', 'text', 'kind') if k in it} | ({'allow_na': True} if it.get('allow_na') else {})
    return dict(
        stages=[dict(id=s['id'], title=s['title'], roles=s['roles'], optional=s.get('optional', {}),
                     gate=s.get('gate'), owner_approval=bool(s.get('owner_approval')),
                     dor=[pub(i) for i in DOR[s['id']]],
                     dod={r: [pub(i) for i in role_dod(r, s['id'])] for r in s['roles']}) for s in STAGES],
        roles={k: dict(title=v['title'], skill='tt-' + k.replace('_', '-'), mission=v['mission'],
                       routing=dict(prefer=list(ROUTING[k]['prefer']), tier=list(ROUTING[k]['tier']), effort=list(ROUTING[k]['effort']),
                                    cross=ROUTING[k].get('cross'), why=ROUTING[k]['why'])) for k, v in ROLES.items()})
