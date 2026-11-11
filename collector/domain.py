from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser


class Table(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        if tag in ('td', 'th'):
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(''.join(self.cell).strip())
            self.cell = None
        if tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def amount(value):
    try:
        number = Decimal(value.replace(',', ''))
    except InvalidOperation as error:
        raise ValueError('金额格式无效') from error
    if not number.is_finite() or number < 0:
        raise ValueError('金额必须为非负有限值')
    return str(number)


def collect(payload):
    table = Table()
    table.feed(payload['html'])
    if not table.rows or table.rows[0] != ['编号', '设备', '数量', '金额（元）']:
        raise ValueError('单层表头不匹配')
    result = []
    for row in table.rows[1:]:
        if len(row) != 4:
            raise ValueError('列数不匹配')
        result.append(dict(key=row[0], equipment=row[1], quantity=int(row[2]), amount_yuan=amount(row[3])))
    return result
