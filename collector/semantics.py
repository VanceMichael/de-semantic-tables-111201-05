"""理解逻辑网格的语义结构，映射到稳定语义列后才生成正式记录。

规则要点：
- 组合表头按列压平，叶子表头去掉单位注记后与 columns.json 的必需列精确匹配；
- 数量与金额使用不同的单位、千位分隔与脚注处理规则（FIELD_RULES）；
- 跨行取值只来自真实的 rowspan 覆盖，绝不对全表空白向下填充；
- 不认识的必需列、单位冲突、重叠合并单元格等一律隔离整页，不挪列猜测。
"""
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .grid import GridError, parse_page

RULE_VERSION = 2

_AMOUNT_RE = re.compile(r'^\d+(\.\d+)?$|^\d{1,3}(,\d{3})+(\.\d+)?$')
_QUANTITY_RE = re.compile(r'^\d+$|^\d{1,3}(,\d{3})+$')
_UNIT_RE = re.compile(r'^(?P<base>.*?)[（(](?P<unit>[^（）()]+)[）)]$')

# 金额单位换算为元的倍率；允许列表中出现但未登记倍率的单位按单位冲突隔离
UNIT_FACTORS = {'元': Decimal(1), '千元': Decimal(1000), '万元': Decimal(10000)}


class Spec:
    """columns.json 描述的当前字段含义。"""

    def __init__(self, required, units, footnotes):
        if not required:
            raise ValueError('字段说明缺少必需列')
        self.required = list(required)
        self.units = {name: list(values) for name, values in units.items()}
        self.footnotes = dict(footnotes)

    @classmethod
    def from_dict(cls, data):
        return cls(data.get('required', []), data.get('units', {}), data.get('footnotes', {}))

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding='utf-8')))


def _reason(code, message, cell=None):
    return dict(code=code, message=message, cell=cell)


def _parse_quantity(text):
    """数量规则：非负整数，允许千位分隔，不允许小数。"""
    if not _QUANTITY_RE.match(text):
        raise ValueError(f'数量 {text!r} 不是非负整数')
    return int(text.replace(',', ''))


def _parse_amount(text, unit):
    """金额规则：非负有限小数，允许千位分隔，按表头单位换算为元。"""
    if not _AMOUNT_RE.match(text):
        raise ValueError(f'金额 {text!r} 格式无效')
    try:
        number = Decimal(text.replace(',', ''))
    except InvalidOperation as error:
        raise ValueError(f'金额 {text!r} 格式无效') from error
    if not number.is_finite() or number < 0:
        raise ValueError(f'金额 {text!r} 必须为非负有限值')
    factor = UNIT_FACTORS.get(unit)
    if factor is None:
        raise ValueError(f'金额单位「{unit}」未登记换算倍率')
    return str(number * factor)


def _split_unit(label):
    match = _UNIT_RE.match(label)
    if match:
        return match.group('base').strip(), match.group('unit').strip()
    return label.strip(), None


class TableView:
    """一张逻辑表的表头压平与列映射结果。"""

    def __init__(self, table, spec):
        self.table = table
        self.spec = spec
        self.reasons = []
        self.hidden_columns = self._find_hidden_columns()
        self.title_rows, self.header_rows = self._find_header()
        self.leaves = {}
        self.column_map = {}
        self.unmapped = []
        if not self.reasons:
            self._flatten_header()
            self._map_columns()

    def _find_hidden_columns(self):
        hidden = []
        for c in range(self.table.columns):
            cells = [self.table.at(r, c) for r in range(self.table.rows)]
            cells = [g for g in cells if g is not None]
            if cells and all(g.cell.hidden for g in cells):
                hidden.append(c)
        return hidden

    def _visible(self, r):
        return [g for c, g in enumerate(self.table.grid[r])
                if g is not None and c not in self.hidden_columns]

    def _find_header(self):
        titles = []
        header = []
        for r in range(self.table.rows):
            cells = self._visible(r)
            if not cells:
                titles.append(r)
                continue
            if all(g.cell.header for g in cells):
                header.append(r)
                continue
            origins = {g.origin for g in cells}
            if not header and len(origins) == 1 and not cells[0].cell.header:
                titles.append(r)  # 整行只有一个普通单元格：标题行
                continue
            break
        if not header:
            self.reasons.append(_reason('no_header', f'表{self.table.index} 缺少表头行'))
        return titles, header

    def _flatten_header(self):
        for c in range(self.table.columns):
            if c in self.hidden_columns:
                continue
            path = []
            for r in self.header_rows:
                g = self.table.at(r, c)
                if g is not None and (not path or path[-1] != g.cell.text):
                    path.append(g.cell.text)
            self.leaves[c] = path

    def _map_columns(self):
        seen = set()
        for c, path in self.leaves.items():
            leaf = path[-1] if path else ''
            base, unit = _split_unit(leaf)
            if base not in self.spec.required:
                self.unmapped.append(dict(column=c, path=path))
                continue
            if base in seen:
                self.reasons.append(_reason(
                    'duplicate_column', f'语义列「{base}」被多个物理列匹配', cell=[self.table.index, self.header_rows[-1], c]))
                continue
            seen.add(base)
            allowed = self.spec.units.get(base, [])
            source = 'header'
            if unit is None and allowed:
                if len(allowed) == 1:
                    unit, source = allowed[0], 'default'
                else:
                    self.reasons.append(_reason(
                        'unit_missing', f'列「{leaf}」缺少单位注记，无法确定是 {"/".join(allowed)}',
                        cell=[self.table.index, self.header_rows[-1], c]))
                    continue
            if unit is not None and allowed and unit not in allowed:
                self.reasons.append(_reason(
                    'unit_conflict', f'列「{leaf}」的单位「{unit}」不在允许范围 {"、".join(allowed)}',
                    cell=[self.table.index, self.header_rows[-1], c]))
                continue
            self.column_map[c] = base
            self.leaves[c] = dict(path=path, semantic=base, unit=unit, unit_source=source)
        if self.column_map:
            for name in self.spec.required:
                if name not in seen:
                    self.reasons.append(_reason('missing_column', f'表{self.table.index} 缺少必需列「{name}」'))

    def is_data_table(self):
        return bool(self.column_map)

    def repeated_header(self, r):
        cells = self._visible(r)
        if not cells:
            return False
        for c, g in enumerate(self.table.grid[r]):
            if g is None or c in self.hidden_columns:
                continue
            entry = self.leaves.get(c)
            leaf = entry['path'][-1] if isinstance(entry, dict) and entry['path'] else None
            if not g.cell.header and g.cell.text != leaf:
                return False
        return True

    def report(self):
        columns = []
        for c, entry in sorted(self.leaves.items()):
            if isinstance(entry, dict):
                columns.append(dict(column=c, path=entry['path'], semantic=entry['semantic'],
                                    unit=entry['unit'], unit_source=entry['unit_source']))
        return dict(
            index=self.table.index, rows=self.table.rows, columns=self.table.columns,
            header_rows=[r + 1 for r in self.header_rows],
            title_rows=[r + 1 for r in self.title_rows],
            hidden_columns=[dict(column=c, label=self._column_label(c)) for c in self.hidden_columns],
            column_map=columns,
            unmapped_columns=self.unmapped)

    def _column_label(self, c):
        for r in range(self.table.rows):
            g = self.table.at(r, c)
            if g is not None and g.cell.text:
                return g.cell.text
        return ''


def _resolve_notes(cell, spec):
    resolved = []
    for marker in cell.notes:
        resolved.append(spec.footnotes.get(marker, f'未登记脚注{marker}'))
    return resolved


def _row_record(view, r, spec):
    """按业务主键（机构+编号）组合一行；取值只来自网格覆盖，不做向下填充。"""
    values = {}
    notes = {}
    cells = {}
    errors = []
    for c, name in view.column_map.items():
        g = view.table.at(r, c)
        text = g.cell.text if g is not None else ''
        coordinate = [view.table.index, g.origin[0], g.origin[1]] if g is not None else [view.table.index, r, c]
        cells[name] = coordinate
        if g is not None and g.cell.notes:
            resolved = _resolve_notes(g.cell, spec)
            if resolved:
                notes[name] = resolved
        if name == '数量':
            try:
                values['quantity'] = _parse_quantity(text)
            except ValueError as error:
                errors.append(_reason('invalid_quantity', str(error), cell=coordinate))
            values['quantity_unit'] = view.leaves[c]['unit']
        elif name == '金额':
            unit = view.leaves[c]['unit']
            try:
                values['amount_yuan'] = _parse_amount(text, unit)
            except ValueError as error:
                errors.append(_reason('invalid_amount', str(error), cell=coordinate))
            values['amount_unit'] = unit
            values['amount_raw'] = text
        else:
            field = {'机构': 'org', '编号': 'code', '设备': 'equipment'}[name]
            if not text:
                errors.append(_reason('blank_cell', f'「{name}」为空且没有合并单元格覆盖，拒绝向下填充猜测', cell=coordinate))
            values[field] = text
    if errors:
        return None, errors
    record = dict(
        key=f"{values['org']}/{values['code']}",
        org=values['org'], code=values['code'], equipment=values['equipment'],
        quantity=values['quantity'], quantity_unit=values['quantity_unit'],
        amount_yuan=values['amount_yuan'], amount_unit=values['amount_unit'],
        amount_raw=values['amount_raw'], cells=cells)
    if notes:
        record['notes'] = notes
    return record, []


def interpret_page(raw, spec):
    """解释一页公示网页，返回 published 或 quarantined 结果，绝不抛出结构异常。"""
    if isinstance(raw, bytes):
        try:
            html = raw.decode('utf-8')
        except UnicodeDecodeError:
            return _quarantined([_reason('encoding', '页面不是有效的 UTF-8 文本')])
    else:
        html = raw
    try:
        tables = parse_page(html)
    except GridError as error:
        return _quarantined([error.reason()])
    if not tables:
        return _quarantined([_reason('no_table', '页面中没有表格')])

    reasons = []
    records = []
    report = dict(rule_version=RULE_VERSION, tables=[], footnotes=spec.footnotes)
    for table in tables:
        view = TableView(table, spec)
        reasons.extend(view.reasons)
        table_report = view.report()
        if not view.is_data_table():
            table_report['skipped'] = '未识别出任何必需语义列，按非数据表跳过'
            report['tables'].append(table_report)
            continue
        if view.reasons:
            # 列映射不完整或存在冲突：整页隔离，不挪列继续猜测
            report['tables'].append(table_report)
            continue
        repeated = []
        data_rows = 0
        header_set = set(view.header_rows) | set(view.title_rows)
        for r in range(table.rows):
            if r in header_set:
                continue
            if not any(g is not None and g.cell.text for g in table.grid[r]):
                continue
            if view.repeated_header(r):
                repeated.append(r + 1)
                continue
            record, errors = _row_record(view, r, spec)
            reasons.extend(errors)
            if record is not None:
                records.append(record)
                data_rows += 1
        table_report['repeated_header_rows'] = repeated
        table_report['data_rows'] = data_rows
        report['tables'].append(table_report)

    merged = {}
    for record in records:
        existing = merged.get(record['key'])
        if existing is None:
            merged[record['key']] = record
            continue
        if {k: v for k, v in existing.items() if k != 'cells'} != {k: v for k, v in record.items() if k != 'cells'}:
            reasons.append(_reason('duplicate_key', f'业务主键 {record["key"]} 在页内出现冲突取值'))
    if not merged and not reasons:
        reasons.append(_reason('no_data', '页面没有产生任何数据行'))
    if reasons:
        return _quarantined(reasons, mapping=report)
    return dict(status='published', rule_version=RULE_VERSION,
                mapping=report, records=list(merged.values()), reasons=[])


def _quarantined(reasons, mapping=None):
    return dict(status='quarantined', rule_version=RULE_VERSION,
                mapping=mapping, records=[], reasons=reasons)
