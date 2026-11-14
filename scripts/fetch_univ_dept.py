"""대학알리미 신입생 충원 현황(학과별) 받기 — 공공데이터포털 자동변환 오픈 API.

출처: 교육부, 「대학알리미_신입생 충원 현황_대학_학과별」, 공공데이터포털 데이터 번호 15163924
      (공공누리 제1유형 — 출처 표시하면 상업적 이용·변경 가능). 2026-11-14 대표 활용신청 승인.
요청: https://api.odcloud.kr/api/15163924/v1/uddi:a850d2f9-33fc-491b-943c-a7d34b9151c4 (page·perPage·serviceKey)

- 인증키는 환경 변수 DATA_GO_KR_KEY(GitHub 비밀 값, 일반 인증키 Decoding)에서만 읽습니다. 코드·파일·기록에 남기지 않습니다.
- 전체(2011~ 모든 해, 약 31만 줄)를 페이지로 받아, 최근 기준연도 2개 해만 칸을 줄여 data/univ_dept/latest.json에 저장합니다.
- 해마다 한 번 새 해가 올라오므로(포털 차기 등록 예정 2027-08-31) 달마다 확인하고, 바뀐 것이 없으면 파일도 그대로입니다.
- 실행 결과는 data/univ_dept/run.json(받은 줄 수·해별 줄 수·걸린 시간 — 키는 없음).
"""
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

URL = 'https://api.odcloud.kr/api/15163924/v1/uddi:a850d2f9-33fc-491b-943c-a7d34b9151c4'
PER = 5000
OUT = 'data/univ_dept'
KEEP_YEARS = 2
COLS = ['공시연도', '기준연도', '학교코드', '학교명', '본분교명', '학교상태', '지역', '단과대학', '학과코드', '학과명',
        '주야간구분', '학과특성', '표준분류_대계열', '표준분류_중계열', '입학정원수', '모집인원_계', '모집인원_정원내', '모집인원_정원외',
        '지원자_계', '지원자_정원내', '지원자_정원외', '입학자_계', '정원내 신입생 충원율', '경쟁률']
SOURCE = {'org': '교육부', 'name': '대학알리미_신입생 충원 현황_대학_학과별', 'portal': '공공데이터포털', 'id': '15163924',
          'url': 'https://www.data.go.kr/data/15163924/fileData.do', 'license': '공공누리 제1유형(출처표시)'}


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def get(page, key, tries=4):
    q = urllib.parse.urlencode({'page': page, 'perPage': PER, 'returnType': 'JSON', 'serviceKey': key})
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(URL + '?' + q, timeout=120) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:   # 키가 든 주소는 찍지 않음 — 오류 종류만
            last = '%s %s' % (type(e).__name__, getattr(e, 'code', ''))
            time.sleep(5 * (i + 1))
    raise RuntimeError('page %d 받기 실패: %s' % (page, last))


def main():
    key = os.environ.get('DATA_GO_KR_KEY', '').strip()
    if not key:
        print('::notice::비밀 값 DATA_GO_KR_KEY가 없어 건너뜁니다.')
        return 0
    t0 = time.time()
    first = get(1, key)
    total = int(first.get('totalCount') or first.get('matchCount') or 0)
    pages = max(1, -(-total // PER))
    rows = list(first.get('data') or [])
    for p in range(2, pages + 1):
        rows += get(p, key).get('data') or []
        time.sleep(0.3)
    if total and len(rows) != total:
        raise RuntimeError('받은 줄 수가 다름: %d / %d' % (len(rows), total))
    miss = [c for c in COLS if rows and c not in rows[0]]
    if miss:
        raise RuntimeError('칸 이름이 바뀜: %s' % ', '.join(miss))
    by_year = {}
    for r in rows:
        y = r.get('기준연도')
        by_year[y] = by_year.get(y, 0) + 1
    years = sorted((y for y in by_year if y), reverse=True)[:KEEP_YEARS]
    keep = [[r.get(c) for c in COLS] for r in rows if r.get('기준연도') in years]
    keep.sort(key=lambda x: (-int(x[1] or 0), str(x[3]), str(x[4]), str(x[9])))
    os.makedirs(OUT, exist_ok=True)
    out = {'source': SOURCE, 'note': '최근 기준연도 %d개 해만(전형 구분 없음 — 모든 전형 합계). 숫자를 화면에 쓸 때는 출처·기준연도를 함께 적는다.' % KEEP_YEARS,
           'totalRows': total, 'years': years, 'cols': COLS, 'rows': keep}
    path = os.path.join(OUT, 'latest.json')
    old = None
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding='utf-8'))
        except Exception:
            old = None
    changed = not old or old.get('rows') != keep or old.get('years') != years
    if changed:
        out['fetchedAt'] = now_utc()
        json.dump(out, open(path, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    run = {'workflow': 'univ_dept', 'finishedAt': now_utc(), 'secs': round(time.time() - t0, 1), 'pages': pages,
           'totalRows': total, 'rowsByYear': {str(k): v for k, v in sorted(by_year.items(), key=lambda kv: str(kv[0]))},
           'keptYears': years, 'keptRows': len(keep), 'changed': changed}
    json.dump(run, open(os.path.join(OUT, 'run.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(json.dumps({k: run[k] for k in ('totalRows', 'pages', 'keptYears', 'keptRows', 'changed', 'secs')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
