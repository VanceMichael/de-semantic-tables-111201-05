import json
import threading
import unittest
from pathlib import Path
import tempfile

from collector.semantics import RULE_VERSION, Spec, interpret_page
from collector.storage import Store

SPEC = Spec.from_dict({
    'required': ['机构', '编号', '设备', '数量', '金额'],
    'units': {'数量': ['台'], '金额': ['元', '万元']},
    'footnotes': {'1': '含附件'},
})

# 模拟规则升级：允许金额使用千元单位
SPEC_V3 = Spec.from_dict({
    'required': ['机构', '编号', '设备', '数量', '金额'],
    'units': {'数量': ['台'], '金额': ['元', '万元', '千元']},
    'footnotes': {'1': '含附件'},
})

GOOD = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>'
        '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>').encode('utf-8')
BAD = ('<table><tr><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>'
       '<tr><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>').encode('utf-8')
NEW_UNIT = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（千元）</th></tr>'
            '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>').encode('utf-8')


def interpret(raw, spec=SPEC):
    result = interpret_page(raw, spec)
    detail = dict(mapping=result['mapping'], reasons=result['reasons'],
                  record_keys=[record['key'] for record in result['records']])
    return result, detail


class PageStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'store.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def ingest(self, source, raw, rule_version=RULE_VERSION):
        result, detail = interpret(raw)
        return result, self.store.ingest_page(
            source, raw, result['status'], detail, result['records'], rule_version)

    def test_publish_and_quarantine_flow(self):
        result, stored = self.ingest('月度公示', GOOD)
        self.assertEqual(result['status'], 'published')
        self.assertTrue(stored['created'])
        self.assertEqual([r['key'] for r in self.store.catalog()], ['甲所/E1'])

        result, stored = self.ingest('问题页面', BAD)
        self.assertEqual(result['status'], 'quarantined')
        quarantine = self.store.quarantine()
        self.assertEqual(len(quarantine), 1)
        self.assertEqual(quarantine[0]['source'], '问题页面')
        self.assertEqual(quarantine[0]['reasons'][0]['code'], 'missing_column')
        # 原页已留存，可用于核对与重放
        self.assertEqual(self.store.raw_object(quarantine[0]['digest']), BAD)
        # 隔离页面不产生正式记录，已发布数据不受影响
        self.assertEqual(len(self.store.catalog()), 1)

    def test_same_page_same_version_is_idempotent(self):
        for unused in range(3):
            result, stored = self.ingest('月度公示', GOOD)
        self.assertFalse(stored['created'])
        batches = self.store.connection.execute('SELECT count(*) FROM batches').fetchone()[0]
        self.assertEqual(batches, 1)
        self.assertEqual(len(self.store.catalog()), 1)

    def test_concurrent_replay_creates_single_batch(self):
        result, detail = interpret(GOOD)
        barrier = threading.Barrier(2)
        outcomes = []

        def replay():
            store = Store(self.path)
            try:
                barrier.wait(timeout=5)
                outcomes.append(store.ingest_page('月度公示', GOOD, result['status'], detail,
                                                  result['records'], RULE_VERSION))
            finally:
                store.close()

        threads = [threading.Thread(target=replay) for unused in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sum(1 for outcome in outcomes if outcome['created']), 1)
        batches = self.store.connection.execute('SELECT count(*) FROM batches').fetchone()[0]
        self.assertEqual(batches, 1)
        rows = self.store.connection.execute('SELECT count(*) FROM catalog_records').fetchone()[0]
        self.assertEqual(rows, 1)

    def test_rule_upgrade_replay_keeps_version_relation(self):
        # 规则 v2 下页面因换用千元单位被隔离，原页保留
        result, detail = interpret(NEW_UNIT)
        self.assertEqual(result['status'], 'quarantined')
        self.assertEqual(result['reasons'][0]['code'], 'unit_conflict')
        stored = self.store.ingest_page('月度公示', NEW_UNIT, result['status'], detail,
                                        result['records'], rule_version=2)
        self.assertEqual(len(self.store.quarantine()), 1)
        # 规则升级到 v3 后重放同一页面（同一来源同一原文），按新单位发布
        result, detail = interpret(NEW_UNIT, spec=SPEC_V3)
        self.assertEqual(result['status'], 'published')
        again = self.store.ingest_page('月度公示', NEW_UNIT, result['status'], detail,
                                       result['records'], rule_version=3)
        self.assertTrue(again['created'])
        self.assertEqual(again['digest'], stored['digest'])
        # 隔离列表清空，目录采用新版本并按千元换算
        self.assertEqual(self.store.quarantine(), [])
        catalog = self.store.catalog()
        self.assertEqual([r['key'] for r in catalog], ['甲所/E1'])
        self.assertEqual(catalog[0]['amount_yuan'], '100000')
        self.assertEqual(catalog[0]['amount_unit'], '千元')
        # 两个批次都保留，版本关系可追溯
        detail = self.store.page_detail(stored['digest'])
        self.assertEqual([(b['rule_version'], b['outcome']) for b in detail['batches']],
                         [(2, 'quarantined'), (3, 'published')])
        self.assertEqual(detail['batches'][0]['detail']['reasons'][0]['code'], 'unit_conflict')
        # 同一版本的重放仍然幂等
        repeat = self.store.ingest_page('月度公示', NEW_UNIT, result['status'],
                                        detail, result['records'], rule_version=3)
        self.assertFalse(repeat['created'])
        self.assertEqual(len(self.store.catalog()), 1)

    def test_published_versions_retain_coordinates(self):
        result, stored = self.ingest('月度公示', GOOD, rule_version=2)
        updated = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>'
                   '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>5</td><td>300</td></tr></table>').encode('utf-8')
        result, detail = interpret(updated)
        self.store.ingest_page('月度公示', updated, result['status'], detail,
                               result['records'], rule_version=3)
        current = self.store.catalog()
        self.assertEqual(current[0]['quantity'], 5)
        versions = self.store.catalog_versions()
        self.assertEqual([(v['record_key'], v['rule_version']) for v in versions],
                         [('甲所/E1', 2), ('甲所/E1', 3)])
        # 每个已发布版本的原始坐标引用都保留
        self.assertEqual(versions[0]['payload']['cells']['金额'], [0, 1, 4])
        self.assertEqual(versions[0]['payload']['quantity'], 2)

    def test_pages_overview_for_business(self):
        self.ingest('月度公示', GOOD)
        self.ingest('问题页面', BAD)
        pages = {page['source']: page for page in self.store.pages()}
        self.assertEqual(pages['月度公示']['status'], 'published')
        self.assertEqual(pages['问题页面']['status'], 'quarantined')
        self.assertEqual(pages['月度公示']['batches'][0]['rule_version'], RULE_VERSION)


if __name__ == '__main__':
    unittest.main()
