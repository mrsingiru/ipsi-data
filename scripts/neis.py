"""나이스 교육정보 개방 포털 Open API 공통 도구.

- 인증키는 환경 변수 NEIS_KEY(GitHub 비밀 값)에서만 읽습니다. 코드·파일·기록에 남기지 않습니다.
- 한 번에 최대 1,000건씩 페이지를 넘겨 받습니다.
- 하루 호출 한도(ERROR-337)를 만나면 DailyLimit 예외를 내서, 그때까지 받은 것을 저장하고 멈추게 합니다.
"""
import datetime
import json
import os
import time
import urllib.parse
import urllib.request

BASE = 'https://open.neis.go.kr/hub/'
PAGE = 1000


class NeisError(RuntimeError):
    pass


class DailyLimit(NeisError):
    pass


def kst_now():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)


def kst_today():
    return kst_now().strftime('%Y-%m-%d')


def get_key():
    key = os.environ.get('NEIS_KEY', '').strip()
    if not key:
        raise NeisError('비밀 값 NEIS_KEY가 없습니다(저장소 Settings → Secrets and variables → Actions)')
    return key


def http_get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def fetch(service, params, key, get=http_get, pause=0.25, max_pages=200):
    """Return every row of one NEIS service for the given filters (empty list when there is no data)."""
    rows, page = [], 1
    while True:
        q = dict(params)
        q.update({'KEY': key, 'Type': 'json', 'pIndex': page, 'pSize': PAGE})
        d = get(BASE + service + '?' + urllib.parse.urlencode(q))
        if service not in d:
            res = d.get('RESULT', {})
            code, msg = res.get('CODE', ''), res.get('MESSAGE', '')
            if code == 'INFO-200':          # 해당하는 데이터가 없습니다
                return rows
            if code == 'ERROR-337':         # 일별 트래픽 제한
                raise DailyLimit('나이스 하루 호출 한도에 걸렸습니다: %s' % msg)
            raise NeisError('나이스 API 오류: %s %s' % (code, msg))
        head = d[service][0]['head']
        total = int(head[0]['list_total_count'])
        res = head[1]['RESULT']
        if res.get('CODE') != 'INFO-000':
            raise NeisError('나이스 API 오류: %s %s' % (res.get('CODE'), res.get('MESSAGE')))
        got = d[service][1]['row']
        rows.extend(got)
        if not got or len(rows) >= total or page >= max_pages:
            break
        page += 1
        time.sleep(pause)
    return rows
