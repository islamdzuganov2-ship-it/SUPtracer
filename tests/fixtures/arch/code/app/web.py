"""Маршруты fixture: декораторы и ветки http.server."""
import json
from http.server import BaseHTTPRequestHandler

import requests
from flask import Flask

from app import core
from app.core import helper, Store

app = Flask(__name__)


@app.get('/items')
def items():
    helper()
    core.helper()
    return json.dumps([Store().save()])


@app.route('/orders', methods=['GET', 'POST'])
def orders():
    return requests.get('http://198.51.100.7/orders')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/health':
            return self.send_health()
        if path in ('/', '/index.html'):
            return self.reply(200, 'ok')
        if path.startswith('/api/'):
            data = core.helper()
            return self.reply(200, data)
        return self.reply(404, 'нет')

    def do_POST(self):
        path = self.path
        if path not in ('/api/save', '/api/sync'):
            return self.reply(404, 'нет')
        if path == '/api/save':
            return self.save()
        self.reply(200, 'синхронизация')

    def save(self):
        return self.reply(200, 'ok')

    def send_health(self):
        return self.reply(200, 'ok')

    def reply(self, code, body):
        self.send_response(code)
