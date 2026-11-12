import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .storage import Store


def make_server(database, port):
    Path(database).parent.mkdir(parents=True, exist_ok=True)

    def query(callback):
        store = Store(database)
        try:
            return callback(store)
        finally:
            store.close()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *arguments):
            return None

        def do_GET(self):
            path, _, remainder = self.path.lstrip('/').partition('/')
            if self.path == '/records':
                status, data = 200, query(lambda store: store.records())
            elif self.path == '/catalog':
                status, data = 200, query(lambda store: store.catalog())
            elif self.path == '/pages':
                status, data = 200, query(lambda store: store.pages())
            elif self.path == '/quarantine':
                status, data = 200, query(lambda store: store.quarantine())
            elif path == 'page' and remainder:
                detail = query(lambda store: store.page_detail(remainder))
                status, data = (200, detail) if detail else (404, {'error': '页面不存在'})
            else:
                status, data = 404, {'error': '资源不存在'}
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def serve(database, port):
    server = make_server(database, port)
    print(f'采集记录服务启动于 127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
