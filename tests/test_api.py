import json
import threading
import unittest
from pathlib import Path
import tempfile

from collector.api import make_server
from collector.semantics import Spec, interpret_page
from collector.storage import Store
from collector.transport import Client

SPEC = Spec.from_dict({
    'required': ['机构', '编号', '设备', '数量', '金额'],
    'units': {'数量': ['台'], '金额': ['元', '万元']},
    'footnotes': {'1': '含附件'},
})

GOOD = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>'
        '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>').encode('utf-8')
BAD = ('<table><tr><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>'
       '<tr><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>').encode('utf-8')


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temporary.name) / 'store.sqlite3')
        store = Store(self.database)
        for source, raw in [('月度公示', GOOD), ('问题页面', BAD)]:
            result = interpret_page(raw, SPEC)
            detail = dict(mapping=result['mapping'], reasons=result['reasons'],
                          record_keys=[r['key'] for r in result['records']])
            store.ingest_page(source, raw, result['status'], detail,
                              result['records'], result['rule_version'])
        store.close()
        self.server = make_server(self.database, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def get(self, path):
        status, headers, body = Client().request(self.base + path)
        return status, json.loads(body)

    def test_business_endpoints(self):
        status, catalog = self.get('/catalog')
        self.assertEqual(status, 200)
        self.assertEqual([r['key'] for r in catalog], ['甲所/E1'])

        status, pages = self.get('/pages')
        self.assertEqual(status, 200)
        self.assertEqual({p['source']: p['status'] for p in pages},
                         {'月度公示': 'published', '问题页面': 'quarantined'})

        status, quarantine = self.get('/quarantine')
        self.assertEqual(status, 200)
        self.assertEqual(len(quarantine), 1)
        self.assertEqual(quarantine[0]['reasons'][0]['code'], 'missing_column')

        digest = quarantine[0]['digest']
        status, detail = self.get(f'/page/{digest}')
        self.assertEqual(status, 200)
        self.assertEqual(detail['batches'][0]['outcome'], 'quarantined')
        # 业务人员能看到该页被解释出的列映射与隔离原因
        self.assertIn('mapping', detail['batches'][0]['detail'])

        status, missing = self.get('/page/0' * 64)
        self.assertEqual(status, 404)
        status, error = self.get('/unknown')
        self.assertEqual(status, 404)

    def test_legacy_records_endpoint_still_works(self):
        status, records = self.get('/records')
        self.assertEqual(status, 200)
        self.assertEqual(records, [])


if __name__ == '__main__':
    unittest.main()
