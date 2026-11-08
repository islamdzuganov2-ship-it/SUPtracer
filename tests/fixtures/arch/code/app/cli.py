"""Команды CLI fixture: таблица команд, argparse и фоновое задание."""
import argparse
import threading


def cmd_run(args):
    return 0


def cmd_serve(args):
    threading.Thread(target=worker, daemon=True).start()
    return 0


def worker():
    return None


def run(script, *args):
    return script


COMMANDS = {"run": cmd_run, "sync": lambda args: run("sync.py", *args)}


def main(argv=None):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    s = sub.add_parser('serve')
    s.set_defaults(func=cmd_serve)
    return p
