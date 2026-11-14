"""공공데이터포털 자동변환 API(odcloud) 응답 살펴보기 — 키는 찍지 않음. 2026-11-14 첫 실행 '{code:0, msg:정상}'만 오고 줄이 없어서."""
import json, os, urllib.parse, urllib.request
URL = 'https://api.odcloud.kr/api/15163924/v1/uddi:a850d2f9-33fc-491b-943c-a7d34b9151c4'
key = os.environ.get('DATA_GO_KR_KEY', '').strip()
print('key length', len(key), '| has %:', '%' in key, '| ends with ==:', key.endswith('=='))
tries = [
    ('query decoding perPage=10', {'page': 1, 'perPage': 10, 'serviceKey': key}, {}),
    ('query decoding perPage=10 returnType=JSON', {'page': 1, 'perPage': 10, 'returnType': 'JSON', 'serviceKey': key}, {}),
    ('query raw (no re-encode) perPage=10', None, {}),
    ('header Infuser perPage=10', {'page': 1, 'perPage': 10}, {'Authorization': 'Infuser ' + key}),
]
for name, q, h in tries:
    if q is None:
        url = URL + '?page=1&perPage=10&serviceKey=' + key
    else:
        url = URL + '?' + urllib.parse.urlencode(q)
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode('utf-8'))
        print(name, '->', {k: (v if k != 'data' else len(v)) for k, v in d.items()})
    except Exception as e:
        print(name, '-> error', type(e).__name__, getattr(e, 'code', ''), (getattr(e, 'read', lambda: b'')() or b'')[:200].decode('utf-8', 'replace'))
