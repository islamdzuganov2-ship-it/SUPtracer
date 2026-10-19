"""Типы задач, новый исходный запрос из формы и рабочее место ИИ (TT-50…TT-52)."""
import os
from pathlib import Path
from base import DataCase
import workflow as w


class TaskTypeTests(DataCase):
    def setUp(self):
        super().setUp(); self.projects()
        (Path(self.data)/'memory').mkdir(exist_ok=True)
        w.save('memory/index.json', {'records':[{'id':'request','project':'alpha-api','kind':'запрос','status':'активна','title':'Старый запрос'}]})
        self.base=dict(title='Задача',why='Нужна',size='S',priority='🟡',risk='low',acceptance=['Проверено'])

    def plan(self, *tasks):
        return w.action(dict(op='plan',project='alpha-api',tasks=[dict(self.base,**t) for t in tasks]))

    def raw(self, tid):
        return next(t for t in w.read('projects.json',{})['projects']['alpha-api']['backlog'] if t['id']==tid)

    # --- новый исходный запрос (TT-50)

    def test_new_root_created_with_task(self):
        r=self.plan(dict(task_type='dev_by_spec',spec_ref='docs/ТЗ.md §3',new_root=dict(title='Нужен экспорт',text='Владелец просит экспорт в CSV')))
        tid=r['added'][0]; root=self.raw(tid)['root']
        self.assertTrue(root.startswith('mem-'))
        import memory
        rec=memory.get(root)
        self.assertEqual((rec['kind'],rec['project']),('запрос','alpha-api'))
        self.assertIn(root,[x['id'] for x in w.snapshot()['roots']])

    def test_same_title_does_not_overwrite_existing_request(self):
        a=self.plan(dict(task_type='dev_by_spec',spec_ref='x',new_root=dict(title='Одно',text='первый')))
        b=self.plan(dict(task_type='dev_by_spec',spec_ref='x',new_root=dict(title='Одно',text='второй')))
        self.assertNotEqual(self.raw(a['added'][0])['root'],self.raw(b['added'][0])['root'])

    def test_existing_root_still_works_and_both_forbidden(self):
        self.plan(dict(root='request',task_type='dev_by_spec',spec_ref='x'))
        with self.assertRaises(ValueError):
            self.plan(dict(root='request',new_root=dict(title='a',text='b'),task_type='dev_by_spec',spec_ref='x'))

    def test_invalid_plan_creates_no_request(self):
        mem_dir=Path(self.data)/'memory'
        before=sorted(os.listdir(mem_dir))
        with self.assertRaises(ValueError):
            self.plan(dict(task_type='dev_by_spec',new_root=dict(title='Запрос',text='текст')))   # нет ТЗ
        with self.assertRaises(ValueError):
            self.plan(dict(task_type='dev_by_spec',spec_ref='x',new_root=dict(title='',text='текст')))
        self.assertEqual(before,sorted(os.listdir(mem_dir)))

    def test_id_assigned_automatically_in_series(self):
        r=self.plan(dict(root='request',task_type='dev_by_spec',spec_ref='x'),dict(root='request',task_type='dev_by_spec',spec_ref='y'))
        self.assertEqual(r['added'],['AL-03','AL-04'])
        self.assertEqual(w.snapshot()['projects']['alpha-api']['next_id'],'AL-05')

    # --- типы задач (TT-51)

    def test_required_fields_per_type(self):
        cases={'dev_by_spec':dict(spec_ref='docs/ТЗ.md'),
               'analysis_spec':dict(question='Какой формат',inputs='Интервью',deliverable='docs/ТЗ_02.md'),
               'decomposition':dict(directions=['дизайн','маркетинг']),
               'direction_task':dict(direction='продажи',deliverable='Скрипт продаж')}
        for kind,fields in cases.items():
            for missing in fields:
                with self.subTest(kind=kind,missing=missing), self.assertRaisesRegex(ValueError,'Не заполнено'):
                    self.plan(dict({k:v for k,v in fields.items() if k!=missing},root='request',task_type=kind))
            r=self.plan(dict(fields,root='request',task_type=kind))
            self.assertTrue(r['ok'])

    def test_work_type_defaults_and_is_limited_by_type(self):
        r=self.plan(dict(root='request',task_type='analysis_spec',question='q',inputs='i',deliverable='d'))
        t=self.raw(r['added'][0])
        self.assertEqual((t['work_type'],t['direction']),('research','аналитика'))
        with self.assertRaisesRegex(ValueError,'Вид работы'):
            self.plan(dict(root='request',task_type='dev_by_spec',spec_ref='x',work_type='research'))
        with self.assertRaises(ValueError):
            self.plan(dict(root='request',task_type='decomposition',directions=['космос']))
        with self.assertRaises(ValueError):
            self.plan(dict(root='request',task_type='unknown'))

    def test_type_fields_in_packet_and_update(self):
        r=self.plan(dict(root='request',task_type='dev_by_spec',spec_ref='docs/ТЗ.md §2',scope=['src']))
        ref='alpha-api/'+r['added'][0]
        p=w.packet(ref)
        self.assertEqual(p['task_type']['spec_ref'],'docs/ТЗ.md §2')
        with self.assertRaises(ValueError):
            w.action(dict(op='update',ref=ref,changes={'task_type':'analysis_spec'}))   # без полей аналитики
        w.action(dict(op='update',ref=ref,changes={'task_type':'analysis_spec','work_type':'research','question':'q','inputs':'i','deliverable':'d'}))
        self.assertEqual(w.task_for(ref)['task_type'],'analysis_spec')

    def test_legacy_plan_without_type_still_valid(self):
        r=self.plan(dict(root='request',work_type='implementation',id='AL-09'))
        self.assertEqual(r['added'],['AL-09'])

    # --- рабочее место (TT-52)

    def test_workspace_from_project(self):
        repo=Path(self.tmp)/'repos'/'alpha-api'/'.git'; repo.mkdir(parents=True)
        (repo/'config').write_text('[core]\n\tbare = false\n[remote "origin"]\n\turl = https://example.invalid/alpha.git\n',encoding='utf-8')
        r=self.plan(dict(root='request',task_type='dev_by_spec',spec_ref='x'))
        ws=w.packet('alpha-api/'+r['added'][0])['workspace']
        self.assertEqual(ws['repo'],'https://example.invalid/alpha.git')
        self.assertTrue(ws['exists']); self.assertTrue(ws['path'].endswith('alpha-api'))
        self.assertEqual(w.snapshot()['projects']['alpha-api']['repo'],'https://example.invalid/alpha.git')

    def test_unknown_project_message_explains_why(self):
        with self.assertRaisesRegex(ValueError,'Выберите проект'):
            w.action(dict(op='plan',project='',tasks=[self.base]))

    def test_new_root_on_edit(self):
        r=self.plan(dict(root='request',task_type='dev_by_spec',spec_ref='x'))
        ref='alpha-api/'+r['added'][0]
        with self.assertRaises(ValueError):
            w.action(dict(op='update',ref=ref,changes={'new_root':{'title':'','text':'t'}}))
        w.action(dict(op='update',ref=ref,changes={'new_root':{'title':'Уточнённый запрос','text':'Владелец уточнил'}}))
        self.assertNotEqual(w.task_for(ref)['root'],'request')
