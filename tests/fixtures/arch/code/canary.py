"""Если анализ исполнит этот файл, рядом появится метка — тест её ищет."""
import os
import sys

open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'CANARY_RAN'), 'w').write('исполнен')
os.system('echo canary')
sys.exit(3)
