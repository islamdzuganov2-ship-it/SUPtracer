"""Динамические места: их нельзя разрешить без исполнения."""
import importlib
import app.core

from app import core


def reflect(name):
    getattr(core, name)()
    getattr(core, 'helper')()
    importlib.import_module('app.' + name)
    __import__('os')
    return eval('1 + 1')
