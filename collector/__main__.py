import argparse
import json
from pathlib import Path
import sys
import tempfile

from .domain import collect
from .semantics import RULE_VERSION, Spec, interpret_page
from .storage import Store
from .transport import Client, replay_server


def _interpret_and_store(store, source, raw, spec):
    result = interpret_page(raw, spec)
    detail = dict(mapping=result['mapping'], reasons=result['reasons'],
                  record_keys=[record['key'] for record in result['records']])
    stored = store.ingest_page(source, raw, result['status'], detail,
                               result['records'], result['rule_version'])
    return result, stored


def main():
    parser = argparse.ArgumentParser(description='本地采集与凭据查看')
    parser.add_argument('command', choices=['demo', 'import', 'explain', 'catalog', 'quarantine', 'replay', 'show', 'serve'])
    parser.add_argument('--input', default='fixtures/sample.json')
    parser.add_argument('--columns', default='fixtures/columns.json')
    parser.add_argument('--db', default='var/records.sqlite3')
    parser.add_argument('--port', type=int, default=8080)
    arguments = parser.parse_args()
    if arguments.command == 'serve':
        from .api import serve
        serve(arguments.db, arguments.port)
        return
    if arguments.command == 'demo':
        spec = Spec.load(arguments.columns)
        with tempfile.TemporaryDirectory() as directory:
            page = Path('fixtures/equipment.html').read_bytes()
            payload = Path(arguments.input).read_bytes()
            routes = {'/source': [dict(body=payload.decode('utf-8'))],
                      '/page': [dict(body=page.decode('utf-8'))]}
            with replay_server(routes) as (base, calls):
                client = Client()
                status, headers, raw = client.request(base + '/source')
                if status != 200:
                    raise RuntimeError('本地来源未返回成功响应')
                store = Store(Path(directory) / 'demo.sqlite3')
                rows = collect(json.loads(raw))
                digest = store.ingest('本地演示来源', raw, rows)
                status, headers, raw_page = client.request(base + '/page')
                if status != 200:
                    raise RuntimeError('本地页面来源未返回成功响应')
                result, stored = _interpret_and_store(store, '本地演示页面', raw_page, spec)
                print(json.dumps(dict(
                    digest=digest, records=store.records(),
                    page=dict(status=result['status'], rule_version=RULE_VERSION, **stored),
                    catalog=store.catalog(), requests=len(calls)), ensure_ascii=False, indent=2))
                store.close()
        return
    if arguments.command == 'explain':
        raw = Path(arguments.input).read_bytes()
        result = interpret_page(raw, Spec.load(arguments.columns))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    Path(arguments.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(arguments.db)
    try:
        if arguments.command == 'import':
            raw = Path(arguments.input).read_bytes()
            source = str(Path(arguments.input))
            if arguments.input.endswith('.json'):
                store.ingest(source, raw, collect(json.loads(raw)))
                print(json.dumps(store.records(), ensure_ascii=False, indent=2))
                return
            result, stored = _interpret_and_store(store, source, raw, Spec.load(arguments.columns))
            print(json.dumps(dict(status=result['status'], reasons=result['reasons'], **stored),
                             ensure_ascii=False, indent=2))
            if result['status'] != 'published':
                sys.exit(1)
            return
        if arguments.command == 'catalog':
            print(json.dumps(store.catalog(), ensure_ascii=False, indent=2))
            return
        if arguments.command == 'quarantine':
            print(json.dumps(store.quarantine(), ensure_ascii=False, indent=2))
            return
        if arguments.command == 'replay':
            spec = Spec.load(arguments.columns)
            summary = []
            for page in store.quarantined_pages():
                result, stored = _interpret_and_store(store, page['source'], page['raw'], spec)
                summary.append(dict(source=page['source'],
                                    status=result['status'], rule_version=RULE_VERSION, **stored))
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return
        print(json.dumps(store.records(), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
