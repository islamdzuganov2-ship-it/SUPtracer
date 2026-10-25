"""Конвейер задачи: Идея → Анализ → Разработка → Ревью → Тестирование → Деплой в тест → Деплой в main.

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
    dict(id='analysis', title='Анализ', roles=['system_analyst', 'solution_architect', 'designer', 'marketer'],
         optional={'designer': 'design', 'marketer': 'marketing'}, gate='analysis'),
    dict(id='development', title='Разработка', roles=['developer']),
    dict(id='review', title='Ревью', roles=['reviewer']),
    dict(id='testing', title='Тестирование', roles=['qa']),
    dict(id='deploy_test', title='Деплой в тест', roles=['devops'], gate='release'),
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
        title='Product Owner', stage='idea', min_tier=3,
        mission='Из идеи сделать максимально развёрнутое бизнес-требование с чётко сформулированными ожидаемыми '
                'результатами — после анализа мировых практик, конкурентов на рынке и возможных максимальных эффектов.',
        scope=['Понять идею и исходный запрос владельца; уточнить проблему и пользователя.',
               'Найти мировые практики и не меньше трёх конкурентов или аналогов (веб-поиск), с источниками.',
               'Оценить максимальные эффекты фичи: для пользователя, бизнеса, проекта.',
               'Сформулировать ожидаемые результаты измеримо: метрики и целевые значения.',
               'Зафиксировать границы (что входит и что нет), риски и допущения.',
               'Решить, нужны ли дизайн и маркетинг; задать бизнес-критерии приёмки.'],
        artifacts=[doc(ART + '/BRD.md', ['Проблема', 'Пользовател', 'Мировые практики', 'Конкурент', 'Эффект',
                                         'Ожидаемые результаты', 'Метрик', 'Границ', 'Риск', 'Источник'], 1500)],
        dod=[item('po-brd', 'BRD.md написан по шаблону: все обязательные разделы заполнены', 'artifact', art=0),
             item('po-market', 'Мировые практики и не меньше трёх конкурентов с источниками (ссылки в BRD)', 'machine', check='links', art=0, min=3),
             item('po-results', 'Ожидаемые результаты измеримы: метрики с целевыми значениями', 'evidence'),
             item('po-effects', 'Оценены максимальные эффекты фичи и её ценность', 'evidence'),
             item('po-acceptance', 'Бизнес-критерии приёмки записаны в задачу (не меньше двух)', 'machine', check='acceptance'),
             item('po-needs', 'Решено, нужны ли дизайн и маркетинг, с причиной', 'machine', check='needs')]),

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
             item('sa-decomp', 'Задачи для разработки с понятными критериями приёмки (dev-tasks.md)', 'artifact', art=5)]),

    'solution_architect': dict(
        title='Solution-архитектор', stage='analysis', min_tier=3,
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
             item('arch-handoff', 'Эпики и задачи переданы аналитику и разработке', 'evidence')]),

    'designer': dict(
        title='UX/UI-дизайнер', stage='analysis', min_tier=2,
        mission='Спроектировать пользовательский опыт и интерфейс, готовые к разработке без догадок.',
        scope=['Анализ требований и аудитории; бенчмаркинг конкурентов.',
               'User Flow и CJM; информационная архитектура.',
               'Wireframes и детальные макеты (описание или HTML-прототип), адаптив: desktop и mobile.',
               'Все состояния: ошибки, пустые экраны, загрузка, hover/focus/active.',
               'Компоненты UI-кита и гайдлайны; спецификация передачи в разработку.'],
        artifacts=[doc(ART + '/design/user-flow.md', ['Сценари'], 300),
                   doc(ART + '/design/ui-spec.md', ['Экран', 'Состояни', 'Адаптив', 'Компонент'], 800),
                   doc(ART + '/design/handoff.md', ['Валидаци', 'Поведени'], 300),
                   doc(ART + '/design/prototype.html', [], 300, optional=True)],
        dod=[item('ds-flow', 'User Flow со связанными экранами (Mermaid-схема)', 'machine', check='mermaid', art=0, min=1),
             item('ds-screens', 'Макеты покрывают позитивные и негативные сценарии и все состояния', 'artifact', art=1),
             item('ds-adaptive', 'Адаптивные версии: мобильная и десктопная', 'evidence'),
             item('ds-kit', 'Новые компоненты описаны как UI-кит (с тёмной темой, если есть)', 'evidence', allow_na=True),
             item('ds-handoff', 'Спецификация передачи: валидации, анимации, поведение', 'artifact', art=2),
             item('ds-prototype', 'Кликабельный прототип', 'artifact', art=3, allow_na=True)]),

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
             item('mk-launch', 'Запуск кампании и отчёт план/факт — после релиза, вне конвейера', 'evidence', allow_na=True)]),

    'developer': dict(
        title='Разработчик', stage='development', min_tier=1,
        mission='Реализовать задачу по ТЗ, HLD и макетам: чистый, проверенный и задокументированный код.',
        scope=['Low-level design: декомпозиция, структуры данных, паттерны.',
               'Код бизнес-логики, API и интерфейса по дизайн-системе.',
               'Unit- и интеграционные тесты; миграции БД, если менялась структура.',
               'Обновление README и документации API; описание изменений (MR).'],
        artifacts=[doc(ART + '/MR.md', ['Что изменено', 'Как проверить', 'Требовани'], 400)],
        dod=[item('dev-code', 'Код в ветке задачи, всё закоммичено', 'machine', check='committed'),
             item('dev-verify', 'Проверки проекта (verify) проходят', 'machine', check='verify', heavy=True),
             item('dev-tests', 'Написаны или обновлены тесты', 'machine', check='tests_changed', allow_na=True),
             item('dev-migrations', 'Миграции БД написаны и проверены', 'evidence', allow_na=True),
             item('dev-docs', 'Документация обновлена (README, API)', 'evidence', allow_na=True),
             item('dev-mergeable', 'Ветка сливается с базовой без конфликтов', 'machine', check='mergeable'),
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
             item('rv-tests', 'Тесты проверяют реальную логику', 'evidence'),
             item('rv-security', 'Проверены инъекции, XSS, секреты и работа с персональными данными', 'evidence'),
             item('rv-debt', 'Техдолг заведён задачами', 'evidence', allow_na=True),
             item('rv-merge', 'Ветка задачи слита в базовую ветку проекта', 'machine', check='merge_to_base', heavy=True)]),

    'qa': dict(
        title='QA-инженер', stage='testing', min_tier=2,
        mission='Проверить, что реализация соответствует требованиям, ничего не сломано, и дать решение о допуске.',
        scope=['Ревью требований на тестируемость; тест-дизайн: классы эквивалентности, границы, переходы состояний.',
               'Тест-кейсы и чек-листы; тестовые данные.',
               'Функциональное, интеграционное, UI- и регрессионное тестирование.',
               'Баг-репорты с шагами, приоритетом и серьёзностью; повторная проверка исправлений.',
               'Отчёт о тестировании и решение о допуске (QA Sign-off); RTM с тест-кейсами.'],
        artifacts=[doc(ART + '/test-cases.md', ['TC-'], 500),
                   doc(ART + '/test-report.md', ['Результат', 'Дефект', 'Регресс', 'Решение'], 500),
                   doc(ART + '/RTM.md', [], 200)],
        dod=[item('qa-cases', 'Тест-кейсы с шагами и ожидаемым результатом (TC-…)', 'artifact', art=0),
             item('qa-report', 'Отчёт: статусы Pass/Fail, дефекты, регресс, решение', 'artifact', art=1),
             item('qa-blockers', 'Нет открытых дефектов Blocker, Critical, Major', 'machine', check='no_blockers', art=1),
             item('qa-regression', 'Регресс: проверки проекта на базовой ветке зелёные', 'machine', check='verify_base', heavy=True),
             item('qa-rtm', 'RTM дополнена тест-кейсами (TC-…)', 'machine', check='contains', art=2, needle='TC-'),
             item('qa-signoff', 'Решение о допуске в релиз (QA Sign-off)', 'machine', check='signoff')]),

    'devops': dict(
        title='DevOps-инженер', stage=('deploy_test', 'deploy_main'), min_tier=1,
        mission='Довести проверенную работу до тестовой ветки и, после одобрения, до main — воспроизводимо и с откатом.',
        scope=['Окружение и переменные документированы; секреты не в git.',
               'Сборка и проверки проходят на целевой ветке; слияние без конфликтов.',
               'Runbook: развёртывание, откат, проверка работоспособности.',
               'CI/CD и мониторинг — где они есть у проекта.'],
        artifacts=[doc(ART + '/runbook.md', ['Развёртывани', 'Откат', 'Проверка', 'Окружени'], 500),
                   doc(ART + '/release-notes.md', ['Что вошло', 'Откат'], 300)],
        dod={'deploy_test': [
                 item('do-runbook', 'Runbook: развёртывание, откат, проверка, окружение', 'artifact', art=0),
                 item('do-secrets', 'Секретов в изменениях нет', 'machine', check='no_secrets'),
                 item('do-ci', 'CI/CD настроен или обновлён', 'evidence', allow_na=True),
                 item('do-monitoring', 'Метрики и оповещения настроены', 'evidence', allow_na=True),
                 item('do-merge-test', 'Базовая ветка слита в тестовую без конфликтов', 'machine', check='deploy_test', heavy=True),
                 item('do-verify-test', 'Проверки проекта на тестовой ветке зелёные', 'machine', check='verify_test', heavy=True)],
             'deploy_main': [
                 item('dm-notes', 'Release notes: что вошло, как откатить', 'artifact', art=1),
                 item('dm-merge-main', 'Тестовая ветка слита в main без конфликтов', 'machine', check='deploy_main', heavy=True),
                 item('dm-verify-main', 'Проверки проекта на main зелёные', 'machine', check='verify_main', heavy=True),
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
        title='Решала (проблемы и эскалации)', stage=None, min_tier=3,
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
                    item('dor-tasks', 'Задачи разработки с критериями приёмки есть (dev-tasks.md)', 'machine', check='artifact', art='dev-tasks.md')],
    'review': [item('dor-dev', 'Разработка сдана по DoD: код, тесты, MR', 'machine', check='stage:development'),
               item('dor-mr', 'Описание изменений MR.md в ветке', 'machine', check='artifact', art='MR.md')],
    'testing': [item('dor-review', 'Ревью сдано: approve и ветка слита в базовую', 'machine', check='stage:review')],
    'deploy_test': [item('dor-qa', 'Тестирование сдано, есть QA Sign-off', 'machine', check='stage:testing')],
    'deploy_main': [item('dor-test', 'Деплой в тест сдан, проверки на тестовой ветке зелёные', 'machine', check='stage:deploy_test'),
                    item('dor-gate-release', 'Ворота тимлида «тест → main» пройдены', 'machine', check='gate:release'),
                    item('dor-owner', 'Владелец одобрил релиз в main', 'machine', check='owner_release')],
}


# Кто и на чём работает в роли. prefer — порядок ИИ (первый свободный от лимита токенов);
# tier — класс модели по сложности задачи (1/2/3: простая, средняя, сложная), не ниже min_tier роли;
# effort — усилия по тому же классу сложности (у Codex выше high не бывает — урезается).
# Ревьюер — всегда другой ИИ, чем разработчик: перекрёстная проверка. Явное закрепление роли
# владельцем (настройки ролей) сильнее таблицы. Основание — сильные стороны моделей в каталоге
# (workflow.MODELS); это начальные эвристики, их уточняют метрики качества ролей и ИИ.
ROUTING = {
    'product_owner': dict(prefer=('claude', 'codex'), tier=(3, 3, 3), effort=('high', 'high', 'xhigh'),
                          why='исследование рынка и длинный связный BRD с источниками — сильная сторона Claude Opus'),
    'system_analyst': dict(prefer=('claude', 'codex'), tier=(2, 2, 3), effort=('medium', 'high', 'xhigh'),
                           why='формализация требований, API и модели данных без двояких трактовок'),
    'solution_architect': dict(prefer=('claude', 'codex'), tier=(3, 3, 3), effort=('high', 'high', 'xhigh'),
                               why='архитектура и ADR — задача старшего класса при любой сложности'),
    'designer': dict(prefer=('claude', 'codex'), tier=(2, 2, 2), effort=('medium', 'medium', 'high'),
                     why='сценарии, состояния экранов и спецификация интерфейса — средний класс достаточен'),
    'marketer': dict(prefer=('claude', 'codex'), tier=(2, 2, 2), effort=('medium', 'medium', 'high'),
                     why='аудитория, позиционирование и тексты — средний класс достаточен'),
    'developer': dict(prefer=('codex', 'claude'), tier=(1, 2, 3), effort=('medium', 'medium', 'high'),
                      why='код и тесты в нескольких файлах — сильная сторона GPT Sol/Astra; Claude — запасной'),
    'reviewer': dict(prefer=('claude', 'codex'), tier=(2, 2, 3), effort=('high', 'high', 'xhigh'), cross='developer',
                     why='перекрёстная проверка: другой ИИ, чем разработчик, видит его слепые зоны'),
    'qa': dict(prefer=('codex', 'claude'), tier=(2, 2, 2), effort=('medium', 'high', 'high'),
               why='тест-кейсы, прогоны и регресс — исполнительская работа среднего класса'),
    'devops': dict(prefer=('codex', 'claude'), tier=(1, 2, 2), effort=('medium', 'medium', 'medium'),
                   why='runbook и заметки к релизу; слияния и проверки делает сервер'),
    'teamlead': dict(prefer=('claude', 'codex'), tier=(3, 3, 3), effort=('high', 'high', 'xhigh'),
                     why='ворота: согласованность всех артефактов — старший класс'),
    'resolver': dict(prefer=('claude', 'codex'), tier=(3, 3, 3), effort=('high', 'xhigh', 'xhigh'), cross='escalator',
                     why='разбор проблем — старший класс; другой ИИ, чем агент с проблемой, — свежий взгляд'),
}


def stage_roles(stage, needs=None):
    """Роли, которые должны сдать этап: необязательные — по решению PO (needs)."""
    s = STAGE[stage]; needs = needs or {}
    return [r for r in s['roles'] if r not in s.get('optional', {}) or needs.get(s['optional'][r])]


def role_dod(role, stage):
    dod = ROLES[role]['dod']
    return dod[stage] if isinstance(dod, dict) else dod


def art_path(task_id, path):
    return path.format(id=task_id)


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


def check_item(it, role, ctx, state):
    """(ok, detail). Тяжёлые проверки (тесты, слияния) — только в фоновом задании."""
    entry = (ctx.handoff.get('items') or {}).get(it['id']) or {}
    status, evidence = entry.get('status'), (entry.get('evidence') or '').strip()
    if status == 'na':
        if not it.get('allow_na'):
            return False, 'пункт нельзя пометить «неприменимо»'
        if len(evidence) < MIN_NA_REASON:
            return False, 'для «неприменимо» нужна причина (не короче %d символов)' % MIN_NA_REASON
        return True, 'неприменимо: ' + evidence
    kind = it['kind']
    if kind == 'evidence':
        if len(evidence) < MIN_EVIDENCE:
            return False, 'нужно доказательство: что сделано и где (не короче %d символов)' % MIN_EVIDENCE
        return True, evidence
    if kind == 'artifact':
        spec = _role_art(role, it['art'])
        reason = check_document(ctx.artifact(spec), spec)
        return (reason is None), (reason or 'есть: ' + art_path(ctx.t['id'], spec['path']))
    if kind == 'machine':
        return machine(it, role, ctx, state, evidence)
    return False, 'неизвестный вид пункта'


def machine(it, role, ctx, state, evidence=''):
    check = it['check']; t = ctx.t
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
        return not bad, ('открытые дефекты: ' + '; '.join(bad[:3])) if bad else 'открытых Blocker/Critical/Major нет'
    if check == 'acceptance':
        acc = ctx.handoff.get('acceptance') or t.get('acceptance') or []
        ok = isinstance(acc, list) and len([a for a in acc if isinstance(a, str) and len(a.strip()) >= 10]) >= 2
        return ok, 'критериев приёмки: %d' % (len(acc) if isinstance(acc, list) else 0)
    if check == 'needs':
        needs = ctx.handoff.get('needs') or (t.get('pipeline') or {}).get('needs')
        ok = isinstance(needs, dict) and type(needs.get('design')) == bool and type(needs.get('marketing')) == bool \
            and len(str(needs.get('reason') or '')) >= MIN_NA_REASON
        return ok, 'needs: design/marketing — да или нет, с причиной' if not ok else \
            'дизайн: %s, маркетинг: %s' % ('да' if needs['design'] else 'нет', 'да' if needs['marketing'] else 'нет')
    if check == 'verdict':
        v = ctx.handoff.get('verdict')
        return v == 'approve', 'вердикт: %s' % (v or 'не указан')
    if check == 'signoff':
        return ctx.handoff.get('signoff') is True, 'QA Sign-off: %s' % ('да' if ctx.handoff.get('signoff') is True else 'нет')
    if check in ('committed', 'mergeable', 'tests_changed', 'no_secrets'):
        return git_check(check, ctx, evidence)
    if check.startswith('verify') or check in ('merge_to_base', 'deploy_test', 'deploy_main'):
        if not state.get('heavy'):
            return None, 'проверит сервер в фоне'
        import deploy
        return deploy.run(check, ctx, evidence)
    return False, 'неизвестная проверка ' + check


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
    out = []
    for it in DOR[stage]:
        check = it['check']
        if check == 'intake':
            ok = bool(ctx.t.get('root') and ctx.t.get('title') and ctx.t.get('why')); detail = 'исходный запрос и описание есть' if ok else 'нет исходного запроса или описания'
        elif check.startswith('stage:'):
            st = check.split(':', 1)[1]; done = (pipe.get('stages') or {}).get(st, {}).get('passed')
            ok, detail = bool(done), 'этап «%s» сдан' % STAGE[st]['title'] if done else 'этап «%s» не сдан' % STAGE[st]['title']
        elif check.startswith('gate:'):
            g = (pipe.get('gates') or {}).get(check.split(':', 1)[1]) or {}
            ok, detail = g.get('decision') == 'approve', 'ворота: %s' % (g.get('decision') or 'не пройдены')
        elif check == 'owner_release':
            a = (pipe.get('approvals') or {}).get('release')
            ok, detail = bool(a), ('одобрил %s' % a.get('at', '')[:16]) if a else 'ждёт «Одобрить релиз» владельца'
        elif check == 'worktree':
            ok = bool(ctx.branch); detail = 'ветка %s' % ctx.branch if ok else 'нет рабочей копии задачи'
        elif check in ('section', 'artifact'):
            text = read_artifact(ctx.repo, ctx.branch, art_path(ctx.t['id'], ART + '/' + it['art']))
            ok = text is not None and (check == 'artifact' or any(it['needle'].lower() in h for h in headings(text)))
            detail = ('%s есть' % it['art']) if ok else ('нет %s%s' % (it['art'], '' if check == 'artifact' else ' с разделом «%s»' % it['needle']))
        else:
            ok, detail = False, 'неизвестная проверка'
        out.append((it, ok, detail))
    return out


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
