"""Аппаратная диагностика (TT-107; ТЗ TT3-005): только чтение, неизвестное не угадывается."""
import unittest
from unittest.mock import patch

from base import TOOLS  # noqa: F401 — пути к tools до импорта модулей системы
import hardware_probe as H

CSV = 'NVIDIA GeForce Test GPU, 8192, 5886, 2306, 621.14, 20\n'
HEAD_NEW = '| NVIDIA-SMI 621.14     KMD Version: 621.14     CUDA UMD Version: 13.5     |'
HEAD_OLD = '| NVIDIA-SMI 551.86     Driver Version: 551.86     CUDA Version: 12.4     |'


def fake(answers):
    calls = []

    def run(cmd, timeout=H.TIMEOUT):
        calls.append(cmd)
        return answers[len(calls) - 1]
    run.calls = calls
    return run


class Probe(unittest.TestCase):
    def test_gpu_parsed(self):
        run = fake([(0, CSV), (0, HEAD_NEW)])
        r = H.probe(run=run)
        g = r['gpus'][0]
        self.assertEqual((g['name'], g['vram_total_mb'], g['vram_free_mb'], g['driver']), ('NVIDIA GeForce Test GPU', 8192, 5886, '621.14'))
        self.assertEqual(r['cuda_driver'], '13.5')
        self.assertNotIn('gpu', r['unknown'])
        self.assertTrue(all(c[0] == 'nvidia-smi' and not any(x.startswith(('-r', '--gpu-reset', '-pl', '-ac', '-pm')) for x in c)
                            for c in run.calls))                                  # только запросы, ничего не меняет

    def test_old_cuda_header(self):
        self.assertEqual(H.cuda_version(HEAD_OLD), '12.4')

    def test_no_nvidia_smi_is_marked_not_guessed(self):
        r = H.probe(run=fake([(None, 'не найдено: nvidia-smi')]))
        self.assertEqual(r['gpus'], []); self.assertIsNone(r['cuda_driver'])
        self.assertIn('не найдено', r['unknown']['gpu'])

    def test_memory_unknown(self):
        with patch.object(H, 'memory', return_value=(None, None)):
            r = H.probe(run=fake([(0, CSV), (1, '')]))
        self.assertIn('ram', r['unknown']); self.assertIn('cuda', r['unknown'])
        self.assertIn('ОЗУ: всего None', H.text(r))

    def test_real_machine_smoke(self):
        r = H.probe()                                                             # на любой машине — без исключений
        self.assertIn('logical_cores', r['cpu'])
