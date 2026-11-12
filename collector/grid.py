"""把公示页 HTML 重建为可追溯到原单元格的逻辑网格。

只负责结构：rowspan/colspan 展开、隐藏单元格、<sup> 脚注标记、
重叠合并单元格检测。语义解释在 semantics.py 中完成。
"""
from dataclasses import dataclass, field
from html.parser import HTMLParser

MAX_SPAN = 100


class GridError(Exception):
    """结构性问题，页面应被隔离而不是继续猜测。"""

    def __init__(self, code, message, coordinate=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.coordinate = coordinate

    def reason(self):
        return dict(code=self.code, message=self.message, cell=self.coordinate)


@dataclass
class RawCell:
    text: str
    notes: list
    header: bool
    hidden: bool
    rowspan: int
    colspan: int


@dataclass
class GridCell:
    origin: tuple  # (row, col)，指向网格内起始单元格
    cell: RawCell
    covered: bool  # True 表示该位置由上方/左方的合并单元格覆盖


@dataclass
class LogicalTable:
    index: int
    grid: list  # grid[row][col] -> GridCell 或 None（缺口）

    @property
    def rows(self):
        return len(self.grid)

    @property
    def columns(self):
        return max((len(row) for row in self.grid), default=0)

    def at(self, row, col):
        if 0 <= row < len(self.grid) and 0 <= col < len(self.grid[row]):
            return self.grid[row][col]
        return None


def _span(attrs, name):
    value = attrs.get(name)
    if value is None:
        return 1
    try:
        number = int(value)
    except ValueError:
        raise GridError('bad_span', f'{name}={value!r} 不是有效整数')
    if number < 1 or number > MAX_SPAN:
        raise GridError('bad_span', f'{name}={value!r} 超出支持范围 1..{MAX_SPAN}')
    return number


def _hidden(attrs):
    style = attrs.get('style', '').replace(' ', '').lower()
    return (
        'hidden' in attrs
        or attrs.get('aria-hidden') == 'true'
        or 'display:none' in style
        or 'visibility:hidden' in style
    )


class PageParser(HTMLParser):
    """解析整页，保留每个单元格的来源坐标与跨距。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._rows = None
        self._row = None
        self._cell = None
        self._sup = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table' and self._rows is None:
            self._rows = []
        elif tag == 'tr' and self._rows is not None:
            self._row = []
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = dict(
                text=[], notes=[], header=(tag == 'th'), hidden=_hidden(attrs),
                rowspan=_span(attrs, 'rowspan'), colspan=_span(attrs, 'colspan'))
        elif tag == 'sup' and self._cell is not None:
            self._sup += 1

    def handle_endtag(self, tag):
        if tag == 'sup' and self._sup:
            self._sup -= 1
        elif tag in ('td', 'th') and self._cell is not None:
            cell = self._cell
            self._row.append(RawCell(
                text=''.join(cell['text']).strip(),
                notes=[note.strip() for note in cell['notes'] if note.strip()],
                header=cell['header'], hidden=cell['hidden'],
                rowspan=cell['rowspan'], colspan=cell['colspan']))
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            if self._row:
                self._rows.append(self._row)
            self._row = None
        elif tag == 'table' and self._rows is not None:
            self.tables.append(self._rows)
            self._rows = None

    def handle_data(self, data):
        if self._cell is None:
            return
        if self._sup:
            self._cell['notes'].append(data)
        else:
            self._cell['text'].append(data)


def build_table(index, rows):
    """把原始行展开成矩形逻辑网格，每个位置记录来源单元格坐标。"""
    occupancy = {}
    origins = {}
    for r, row in enumerate(rows):
        c = 0
        for cell in row:
            while (r, c) in occupancy:
                c += 1
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    slot = (r + dr, c + dc)
                    if slot in occupancy:
                        raise GridError(
                            'overlap',
                            f'合并单元格在 第{r + dr + 1}行第{c + dc + 1}列 重叠',
                            coordinate=[index, r + dr, c + dc])
                    occupancy[slot] = (r, c)
            origins[(r, c)] = cell
            c += cell.colspan
    height = max((r for r, _ in occupancy), default=-1) + 1
    width = max((c for _, c in occupancy), default=-1) + 1
    grid = []
    for r in range(height):
        line = []
        for c in range(width):
            origin = occupancy.get((r, c))
            if origin is None:
                line.append(None)
            else:
                line.append(GridCell(origin=origin, cell=origins[origin], covered=origin != (r, c)))
        grid.append(line)
    return LogicalTable(index=index, grid=grid)


def parse_page(html):
    """解析一页，返回 LogicalTable 列表；结构非法时抛出 GridError。"""
    parser = PageParser()
    parser.feed(html)
    parser.close()
    return [build_table(index, rows) for index, rows in enumerate(parser.tables)]
