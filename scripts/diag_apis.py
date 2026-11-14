"""공공데이터포털 API 응답 모양 살펴보기(키는 찍지 않음) — 2026-11-14 대학 기본정보(getSchoolInfo)·공휴일(특일 정보) 수집기 만들기 전."""
import os, re, urllib.parse, urllib.request
key = os.environ.get('DATA_GO_KR_KEY', '').strip()
T = [
    ('대학 기본정보 getSchoolInfo 2025', 'https://apis.data.go.kr/B340014/SchoolInfoService/getSchoolInfo', {'pageNo': 1, 'numOfRows': 3, 'svyYr': 2025}),
    ('대학 기본정보 getSchoolInfo 2026', 'https://apis.data.go.kr/B340014/SchoolInfoService/getSchoolInfo', {'pageNo': 1, 'numOfRows': 3, 'svyYr': 2026}),
    ('공휴일 getRestDeInfo 2027', 'https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo', {'pageNo': 1, 'numOfRows': 50, 'solYear': 2027}),
    ('국경일 getHoliDeInfo 2027', 'https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getHoliDeInfo', {'pageNo': 1, 'numOfRows': 50, 'solYear': 2027}),
]
for name, url, q in T:
    q = dict(q, serviceKey=key)
    try:
        with urllib.request.urlopen(url + '?' + urllib.parse.urlencode(q), timeout=60) as r:
            x = r.read().decode('utf-8', 'replace')
    except Exception as e:
        x = 'ERROR %s %s %s' % (type(e).__name__, getattr(e, 'code', ''), (getattr(e, 'read', lambda: b'')() or b'')[:300].decode('utf-8', 'replace'))
    x = x.replace(key, '***') if key else x
    tags = sorted(set(re.findall(r'<([A-Za-z0-9_]+)>', x)))
    print('==', name, '| 길이', len(x), '| 태그:', ','.join(tags)[:600])
    print(x[:1500])
