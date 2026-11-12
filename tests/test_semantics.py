import json
import unittest
from pathlib import Path

from collector.grid import GridError, parse_page
from collector.semantics import RULE_VERSION, Spec, interpret_page

SPEC = Spec.from_dict({
    'required': ['机构', '编号', '设备', '数量', '金额'],
    'units': {'数量': ['台'], '金额': ['元', '万元']},
    'footnotes': {'1': '含附件'},
})

HEADER = ('<tr><th rowspan="2">机构</th><th rowspan="2">编号</th><th rowspan="2">设备</th>'
          '<th colspan="2">配置</th></tr><tr><th>数量（台）</th><th>金额（万元）</th></tr>')


def page(body, header=HEADER):
    return f'<table>{header}{body}</table>'.encode('utf-8')


class GridTests(unittest.TestCase):
    def test_spans_expand_with_origin_coordinates(self):
        (table,) = parse_page(page('<tr><td rowspan="2">联合实验室</td><td>E1</td><td>光谱仪</td><td>2</td><td>1.25</td></tr>'
                                   '<tr><td>E2</td><td>探测仪</td><td>1</td><td>2.00</td></tr>').decode('utf-8'))
        self.assertEqual((table.rows, table.columns), (4, 5))
        covered = table.at(3, 0)
        self.assertTrue(covered.covered)
        self.assertEqual(covered.origin, (2, 0))
        self.assertEqual(covered.cell.text, '联合实验室')

    def test_overlapping_merges_are_rejected(self):
        html = ('<table><tr><td colspan="2">A</td><td rowspan="2">B</td></tr>'
                '<tr><td colspan="3">C</td></tr></table>')
        with self.assertRaises(GridError) as caught:
            parse_page(html)
        self.assertEqual(caught.exception.code, 'overlap')
        self.assertEqual(caught.exception.coordinate, [0, 1, 2])

    def test_bad_span_is_rejected(self):
        with self.assertRaises(GridError) as caught:
            parse_page('<table><tr><td rowspan="0">A</td></tr></table>')
        self.assertEqual(caught.exception.code, 'bad_span')

    def test_hidden_and_footnote_marks(self):
        (table,) = parse_page('<table><tr><td>光谱仪<sup>1</sup></td>'
                              '<td style="display:none">内部</td><td hidden>密</td></tr></table>')
        self.assertEqual(table.at(0, 0).cell.text, '光谱仪')
        self.assertEqual(table.at(0, 0).cell.notes, ['1'])
        self.assertTrue(table.at(0, 1).cell.hidden)
        self.assertTrue(table.at(0, 2).cell.hidden)


class SemanticsTests(unittest.TestCase):
    def test_fixture_page_is_published(self):
        raw = Path('fixtures/equipment.html').read_bytes()
        result = interpret_page(raw, Spec.load('fixtures/columns.json'))
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['rule_version'], RULE_VERSION)
        records = {record['key']: record for record in result['records']}
        self.assertEqual(sorted(records), ['联合实验室/E1', '联合实验室/E2'])
        first = records['联合实验室/E1']
        self.assertEqual(first['quantity'], 2)
        self.assertEqual(first['quantity_unit'], '台')
        self.assertEqual(first['amount_yuan'], '12500.00')
        self.assertEqual(first['amount_unit'], '万元')
        self.assertEqual(first['notes'], {'设备': ['含附件']})
        second = records['联合实验室/E2']
        self.assertEqual(second['org'], '联合实验室')
        self.assertEqual(second['cells']['机构'], [0, 2, 0])  # 跨行取值追溯到原单元格
        mapping = result['mapping']['tables'][0]
        self.assertEqual(mapping['header_rows'], [1, 2])
        self.assertEqual([c['semantic'] for c in mapping['column_map']],
                         ['机构', '编号', '设备', '数量', '金额'])

    def test_repeated_header_rows_are_not_data(self):
        body = ('<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>1.25</td></tr>'
                + HEADER +
                '<tr><td>甲所</td><td>E2</td><td>探测仪</td><td>1</td><td>2.00</td></tr>')
        result = interpret_page(page(body), SPEC)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(len(result['records']), 2)
        self.assertEqual(result['mapping']['tables'][0]['repeated_header_rows'], [4, 5])
        for record in result['records']:
            self.assertIsInstance(record['quantity'], int)

    def test_repeated_header_as_plain_cells_is_skipped(self):
        body = ('<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>1.25</td></tr>'
                '<tr><td>机构</td><td>编号</td><td>设备</td><td>数量（台）</td><td>金额（万元）</td></tr>'
                '<tr><td>甲所</td><td>E2</td><td>探测仪</td><td>1</td><td>2.00</td></tr>')
        result = interpret_page(page(body), SPEC)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(len(result['records']), 2)

    def test_hidden_column_is_excluded_from_mapping(self):
        html = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th>'
                '<th style="display:none">内部备注</th></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>1,200</td><td>1,200.50</td>'
                '<td style="display:none">机密</td></tr></table>')
        result = interpret_page(html.encode('utf-8'), SPEC)
        self.assertEqual(result['status'], 'published')
        record = result['records'][0]
        self.assertEqual(record['quantity'], 1200)
        self.assertEqual(record['amount_yuan'], '1200.50')
        self.assertNotIn('内部备注', json.dumps(record, ensure_ascii=False))
        hidden = result['mapping']['tables'][0]['hidden_columns']
        self.assertEqual(hidden, [dict(column=5, label='内部备注')])

    def test_multiple_tables_merge_records(self):
        one = '<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>' \
              '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>'
        two = '<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>' \
              '<tr><td>乙所</td><td>E2</td><td>探测仪</td><td>1</td><td>200</td></tr></table>'
        result = interpret_page((one + two).encode('utf-8'), SPEC)
        self.assertEqual(result['status'], 'published')
        self.assertEqual([r['key'] for r in result['records']], ['甲所/E1', '乙所/E2'])
        self.assertEqual(result['records'][1]['cells']['机构'][0], 1)

    def test_title_row_is_skipped(self):
        html = ('<table><tr><td colspan="5">2026年设备公示</td></tr>'
                '<tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>')
        result = interpret_page(html.encode('utf-8'), SPEC)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['mapping']['tables'][0]['title_rows'], [1])

    def test_quantity_without_unit_uses_single_allowed_default(self):
        html = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量</th><th>金额（元）</th></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>')
        result = interpret_page(html.encode('utf-8'), SPEC)
        self.assertEqual(result['status'], 'published')
        column = result['mapping']['tables'][0]['column_map'][3]
        self.assertEqual(column['unit'], '台')
        self.assertEqual(column['unit_source'], 'default')

    def test_extra_unknown_column_is_reported_not_fatal(self):
        html = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th><th>备注</th></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td><td>在用</td></tr></table>')
        result = interpret_page(html.encode('utf-8'), SPEC)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['mapping']['tables'][0]['unmapped_columns'],
                         [dict(column=5, path=['备注'])])

    def quarantine_codes(self, html):
        result = interpret_page(html.encode('utf-8') if isinstance(html, str) else html, SPEC)
        self.assertEqual(result['status'], 'quarantined')
        self.assertEqual(result['records'], [])
        return {reason['code'] for reason in result['reasons']}

    def test_missing_required_column_quarantines(self):
        html = ('<table><tr><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>'
                '<tr><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>')
        self.assertIn('missing_column', self.quarantine_codes(html))

    def test_unit_conflict_quarantines(self):
        html = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（千元）</th></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>')
        self.assertIn('unit_conflict', self.quarantine_codes(html))
        html = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（箱）</th><th>金额（元）</th></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>')
        self.assertIn('unit_conflict', self.quarantine_codes(html))

    def test_ambiguous_amount_unit_quarantines(self):
        html = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额</th></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>')
        self.assertIn('unit_missing', self.quarantine_codes(html))

    def test_duplicate_semantic_column_quarantines(self):
        html = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>数量（台）</th><th>金额（元）</th></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>3</td><td>100</td></tr></table>')
        self.assertIn('duplicate_column', self.quarantine_codes(html))

    def test_blank_key_cell_is_not_forward_filled(self):
        html = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr>'
                '<tr><td>甲所</td><td></td><td>探测仪</td><td>1</td><td>200</td></tr></table>')
        codes = self.quarantine_codes(html)
        self.assertIn('blank_cell', codes)

    def test_overlap_quarantines_whole_page(self):
        html = ('<table><tr><td colspan="2">A</td><td rowspan="2">B</td></tr>'
                '<tr><td colspan="3">C</td></tr></table>')
        self.assertIn('overlap', self.quarantine_codes(html))

    def test_invalid_numbers_quarantine_with_cell_coordinate(self):
        html = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>1.5</td><td>100</td></tr></table>')
        result = interpret_page(html.encode('utf-8'), SPEC)
        self.assertEqual(result['status'], 'quarantined')
        reason = result['reasons'][0]
        self.assertEqual(reason['code'], 'invalid_quantity')
        self.assertEqual(reason['cell'], [0, 1, 3])

    def test_conflicting_duplicate_key_quarantines(self):
        html = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>200</td></tr></table>')
        self.assertIn('duplicate_key', self.quarantine_codes(html))

    def test_identical_duplicate_key_is_deduplicated(self):
        html = ('<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr>'
                '<tr><td>甲所</td><td>E1</td><td>光谱仪</td><td>2</td><td>100</td></tr></table>')
        result = interpret_page(html.encode('utf-8'), SPEC)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(len(result['records']), 1)

    def test_header_only_page_quarantines(self):
        html = '<table><tr><th>机构</th><th>编号</th><th>设备</th><th>数量（台）</th><th>金额（元）</th></tr></table>'
        self.assertIn('no_data', self.quarantine_codes(html))

    def test_non_table_page_quarantines(self):
        self.assertIn('no_table', self.quarantine_codes('<p>没有表格</p>'))

    def test_non_utf8_page_quarantines(self):
        self.assertIn('encoding', self.quarantine_codes(b'\xff\xfe\xfd'))

    def test_deterministic_results(self):
        raw = Path('fixtures/equipment.html').read_bytes()
        spec = Spec.load('fixtures/columns.json')
        self.assertEqual(interpret_page(raw, spec), interpret_page(raw, spec))


if __name__ == '__main__':
    unittest.main()
