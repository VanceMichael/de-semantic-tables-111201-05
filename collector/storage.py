import hashlib
import json
import sqlite3
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS raw_objects(
                digest TEXT PRIMARY KEY, payload BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS imports(
                id INTEGER PRIMARY KEY, source TEXT NOT NULL,
                digest TEXT NOT NULL REFERENCES raw_objects(digest),
                UNIQUE(source,digest));
            CREATE TABLE IF NOT EXISTS records(
                record_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                import_id INTEGER NOT NULL REFERENCES imports(id));
            CREATE TABLE IF NOT EXISTS batches(
                id INTEGER PRIMARY KEY,
                import_id INTEGER NOT NULL REFERENCES imports(id),
                rule_version INTEGER NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('published','quarantined')),
                detail TEXT NOT NULL,
                UNIQUE(import_id, rule_version));
            CREATE TABLE IF NOT EXISTS catalog_records(
                record_key TEXT NOT NULL,
                rule_version INTEGER NOT NULL,
                batch_id INTEGER NOT NULL REFERENCES batches(id),
                payload TEXT NOT NULL,
                PRIMARY KEY(record_key, rule_version));
        """)

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def ingest(self, source, raw, records):
        digest = hashlib.sha256(raw).hexdigest()
        with self.transaction():
            self.connection.execute("INSERT OR IGNORE INTO raw_objects VALUES (?,?)", (digest, raw))
            self.connection.execute("INSERT OR IGNORE INTO imports(source,digest) VALUES (?,?)", (source, digest))
            import_id = self.connection.execute("SELECT id FROM imports WHERE source=? AND digest=?", (source, digest)).fetchone()[0]
            for record in records:
                key = record.get('key')
                if not isinstance(key, str) or not key:
                    raise ValueError('记录缺少有效标识')
                encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
                self.connection.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(record_key) DO UPDATE SET payload=excluded.payload, import_id=excluded.import_id", (key, encoded, import_id))
        return digest

    def records(self):
        return [json.loads(row[0]) for row in self.connection.execute("SELECT payload FROM records ORDER BY record_key")]

    def ingest_page(self, source, raw, outcome, detail, records, rule_version):
        """把一页的解释结果落库。同一页面在同一规则版本下只产生一个批次，
        并发重放由 BEGIN IMMEDIATE 与 UNIQUE(import_id, rule_version) 保证幂等。"""
        digest = hashlib.sha256(raw).hexdigest()
        encoded_detail = json.dumps(detail, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self.transaction():
            self.connection.execute("INSERT OR IGNORE INTO raw_objects VALUES (?,?)", (digest, raw))
            self.connection.execute("INSERT OR IGNORE INTO imports(source,digest) VALUES (?,?)", (source, digest))
            import_id = self.connection.execute("SELECT id FROM imports WHERE source=? AND digest=?", (source, digest)).fetchone()[0]
            created = self.connection.execute(
                "INSERT OR IGNORE INTO batches(import_id, rule_version, outcome, detail) VALUES (?,?,?,?)",
                (import_id, rule_version, outcome, encoded_detail)).rowcount == 1
            batch_id = self.connection.execute(
                "SELECT id FROM batches WHERE import_id=? AND rule_version=?",
                (import_id, rule_version)).fetchone()[0]
            if created and outcome == 'published':
                for record in records:
                    key = record.get('key')
                    if not isinstance(key, str) or not key:
                        raise ValueError('记录缺少有效标识')
                    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
                    self.connection.execute(
                        "INSERT INTO catalog_records VALUES (?,?,?,?) "
                        "ON CONFLICT(record_key, rule_version) DO UPDATE SET payload=excluded.payload, batch_id=excluded.batch_id",
                        (key, rule_version, batch_id, encoded))
        return dict(digest=digest, batch_id=batch_id, created=created)

    def catalog(self):
        """正式目录当前视图：每个业务主键取已发布的最高规则版本。"""
        rows = self.connection.execute("""
            SELECT c.payload FROM catalog_records c
            JOIN (SELECT record_key, MAX(rule_version) AS version FROM catalog_records GROUP BY record_key) latest
              ON latest.record_key=c.record_key AND latest.version=c.rule_version
            ORDER BY c.record_key""")
        return [json.loads(row[0]) for row in rows]

    def catalog_versions(self):
        """全部已发布版本，保留数据与原始坐标的版本关系。"""
        rows = self.connection.execute(
            "SELECT record_key, rule_version, payload FROM catalog_records ORDER BY record_key, rule_version")
        return [dict(record_key=row[0], rule_version=row[1], payload=json.loads(row[2])) for row in rows]

    def pages(self):
        """全部页面及其批次概览，供业务人员核对。"""
        result = []
        for row in self.connection.execute("""
                SELECT i.source, i.digest, b.rule_version, b.outcome
                FROM imports i LEFT JOIN batches b ON b.import_id=i.id
                ORDER BY i.id, b.rule_version"""):
            if result and result[-1]['digest'] == row[1] and result[-1]['source'] == row[0]:
                page = result[-1]
            else:
                page = dict(source=row[0], digest=row[1], batches=[])
                result.append(page)
            if row[2] is not None:
                page['batches'].append(dict(rule_version=row[2], outcome=row[3]))
        for page in result:
            page['status'] = page['batches'][-1]['outcome'] if page['batches'] else 'pending'
        return result

    def quarantine(self):
        """当前仍处于隔离状态的页面（最新批次为 quarantined）及原因。"""
        rows = self.connection.execute("""
            SELECT i.source, i.digest, b.rule_version, b.detail
            FROM imports i
            JOIN batches b ON b.import_id=i.id
            JOIN (SELECT import_id, MAX(rule_version) AS version FROM batches GROUP BY import_id) latest
              ON latest.import_id=b.import_id AND latest.version=b.rule_version
            WHERE b.outcome='quarantined'
            ORDER BY i.id""")
        result = []
        for row in rows:
            detail = json.loads(row[3])
            result.append(dict(source=row[0], digest=row[1], rule_version=row[2],
                               reasons=detail.get('reasons', []), mapping=detail.get('mapping')))
        return result

    def page_detail(self, digest):
        row = self.connection.execute("SELECT id, source FROM imports WHERE digest=?", (digest,)).fetchone()
        if row is None:
            return None
        batches = self.connection.execute(
            "SELECT rule_version, outcome, detail FROM batches WHERE import_id=? ORDER BY rule_version", (row[0],))
        return dict(source=row[1], digest=digest, batches=[
            dict(rule_version=b[0], outcome=b[1], detail=json.loads(b[2])) for b in batches])

    def raw_object(self, digest):
        row = self.connection.execute("SELECT payload FROM raw_objects WHERE digest=?", (digest,)).fetchone()
        return row[0] if row else None

    def quarantined_pages(self):
        """重放候选：当前仍隔离的页面及其原文。"""
        return [dict(source=entry['source'], digest=entry['digest'],
                     raw=self.raw_object(entry['digest']))
                for entry in self.quarantine()]

    def provenance(self, key):
        row = self.connection.execute("SELECT imports.source, imports.digest FROM records JOIN imports ON imports.id=records.import_id WHERE record_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def close(self):
        self.connection.close()
