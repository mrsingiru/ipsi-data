"""
나이스 학교기본정보(Open API) → data/ 폴더에 학교 목록을 저장하고, 지난번과 달라진 학교를 기록합니다.

- 매일 GitHub Actions가 실행합니다(.github/workflows/neis.yml).
- 인증키는 저장소의 비밀 값 NEIS_KEY에서 읽습니다. 코드나 파일에는 남기지 않습니다.
- 받은 학교 수가 지난번보다 10% 넘게 줄면 API 오류로 보고 저장하지 않습니다(목록이 통째로 지워지는 사고 방지).
- 2026-07-01 행정구역 개편을 반영합니다: 광주·전남 → '전남광주', 인천 중구·동구 → 제물포구·영종구, 서구 → 서해구·검단구.

결과 파일
- data/schools.csv   : 전체 목록(사람이 보기 쉬운 표, 시험 날짜 수집에도 씀)
- data/schools.json  : 페이지용 목록 {'map': {'부산 동래구': {'e'|'m'|'h': [[학교명, 누리집(, 고교 유형)], ...]}}}
- data/changes.md    : 날짜별 변경 기록(새 학교·없어진 학교·이름·누리집·주소 변경)
- data/run.json      : 마지막 실행 정보(시각, 학교 수, 결과)
"""
import csv
import json
import os
import re
import sys

from neis import fetch, get_key, kst_today

OUT = os.environ.get('OUT_DIR', 'data')
DROP_LIMIT = 0.10

SIDO = {
    '서울특별시': '서울', '부산광역시': '부산', '대구광역시': '대구', '인천광역시': '인천',
    '광주광역시': '전남광주', '전라남도': '전남광주', '대전광역시': '대전', '울산광역시': '울산', '세종특별자치시': '세종',
    '경기도': '경기', '강원특별자치도': '강원', '강원도': '강원', '충청북도': '충북', '충청남도': '충남',
    '전북특별자치도': '전북', '전라북도': '전북', '경상북도': '경북', '경상남도': '경남', '제주특별자치도': '제주'
}
LEVEL = {'초등학교': 'e', '중학교': 'm', '고등학교': 'h'}
FIELDS = ['code', 'atpt', 'name', 'kind', 'level', 'hstype', 'sido', 'sigungu', 'gu_guess', 'office', 'found', 'coedu', 'home', 'address', 'loaded']

# 2026-07-01 인천 개편: 법정동으로 새 구를 고릅니다(주소 끝 괄호 안의 동 이름).
YEONGJONG = {'중산동', '운남동', '운서동', '운북동', '을왕동', '남북동', '덕교동', '무의동'}
GEOMDAN = {'대곡동', '불로동', '마전동', '금곡동', '오류동', '왕길동', '당하동', '원당동'}


def sido_of(name):
    name = (name or '').strip()
    if '전남광주' in name:
        return '전남광주'
    return SIDO.get(name, name)


def homepage(u):
    u = (u or '').strip()
    if not u or u in ('http://', 'https://'):
        return ''
    if not u.startswith(('http://', 'https://')):
        u = 'http://' + u
    return u if ' ' not in u and '.' in u else ''


def dong_of(address):
    m = re.search(r'\(([^)]*)\)\s*$', address or '')
    return m.group(1).split(',')[0].strip() if m else ''


def sigungu(sido, address):
    """Return (구·군·시, guessed?) from the road address."""
    parts = (address or '').split()
    if sido == '세종' or len(parts) < 2:
        return '', False
    gu = parts[1]
    # some addresses leave out the 시·도 ('평택시 고덕국제5로 165'): then the first word is the 시·군·구
    if not re.search(r'(도|특별시|광역시|특별자치시|특별자치도)$', parts[0]) and re.search(r'(시|군|구)$', parts[0]):
        gu = parts[0]
    if sido == '인천' and gu in ('중구', '동구', '서구'):
        dong = dong_of(address)
        if gu == '중구':
            return ('영종구' if dong in YEONGJONG else '제물포구'), not dong
        if gu == '동구':
            return '제물포구', False
        return ('검단구' if dong in GEOMDAN else '서해구'), not dong
    return gu, False


def normalize(r):
    sido = sido_of(r.get('LCTN_SC_NM'))
    kind = (r.get('SCHUL_KND_SC_NM') or '').strip()
    address = (r.get('ORG_RDNMA') or '').strip()
    gu, guess = sigungu(sido, address)
    return {
        'code': (r.get('SD_SCHUL_CODE') or '').strip(),
        'atpt': (r.get('ATPT_OFCDC_SC_CODE') or '').strip(),
        'name': (r.get('SCHUL_NM') or '').strip(),
        'kind': kind,
        'level': LEVEL.get(kind, ''),
        'hstype': (r.get('HS_SC_NM') or '').strip() if kind == '고등학교' else '',
        'sido': sido,
        'sigungu': gu,
        'gu_guess': 'Y' if guess else '',
        'office': (r.get('JU_ORG_NM') or '').strip(),
        'found': (r.get('FOND_SC_NM') or '').strip(),
        'coedu': (r.get('COEDU_SC_NM') or '').strip(),
        'home': homepage(r.get('HMPG_ADRES')),
        'address': address,
        'loaded': (r.get('LOAD_DTM') or '').strip(),
    }


def key_of(s):
    return s['code'] or ('이름:' + s['sido'] + ':' + s['name'])


def read_old(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8-sig', newline='') as f:
        return {key_of(r): r for r in csv.DictReader(f)}


def diff(old, new):
    added = [new[k] for k in new if k not in old]
    removed = [old[k] for k in old if k not in new]
    changed = []
    for k in new:
        if k in old:
            what = [f for f in ('name', 'home', 'address', 'kind') if (old[k].get(f) or '') != (new[k].get(f) or '')]
            if what:
                changed.append((old[k], new[k], what))
    return added, removed, changed


def region_key(s):
    return s['sido'] if not s['sigungu'] else s['sido'] + ' ' + s['sigungu']


def page_json(schools, day):
    regions = {}
    for s in schools:
        if not s['level'] or s['name'].startswith('(가칭)'):
            continue
        item = [s['name'], s['home']] + ([s['hstype']] if s['level'] == 'h' and s['hstype'] else [])
        regions.setdefault(region_key(s), {}).setdefault(s['level'], []).append(item)
    for gu in regions.values():
        for lv in gu:
            gu[lv].sort(key=lambda x: x[0])
    return {'updated': day, 'source': '나이스 교육정보 개방 포털 학교기본정보', 'map': regions}


def changes_md(day, added, removed, changed, previous_text):
    lines = ['## %s' % day, '']
    if not (added or removed or changed):
        lines.append('- 바뀐 학교 없음')
    for s in added:
        lines.append('- 새로 생김: %s %s %s (%s)' % (s['sido'], s['sigungu'], s['name'], s['kind']))
    for s in removed:
        lines.append('- 목록에서 빠짐: %s %s %s (%s)' % (s['sido'], s['sigungu'], s['name'], s['kind']))
    for o, n, what in changed:
        detail = ', '.join('%s: %s → %s' % (w, o.get(w) or '(없음)', n.get(w) or '(없음)') for w in what)
        lines.append('- 바뀜: %s %s — %s' % (n['sido'], n['name'], detail))
    head = '# 학교 목록 변경 기록\n\n나이스 학교기본정보를 매일 받아 지난번과 비교한 결과입니다. 최신이 위에 있습니다.\n\n'
    body = previous_text.split('\n\n', 2)[2] if previous_text.startswith('# 학교 목록 변경 기록') and previous_text.count('\n\n') >= 2 else ''
    return head + '\n'.join(lines) + '\n\n' + body


def main(rows=None):
    os.makedirs(OUT, exist_ok=True)
    day = kst_today()
    csv_path = os.path.join(OUT, 'schools.csv')
    if rows is None:
        rows = fetch('schoolInfo', {}, get_key())
    schools = [normalize(r) for r in rows]
    schools = [s for s in schools if s['name']]
    new = {key_of(s): s for s in schools}
    old = read_old(csv_path)
    if old and len(new) < len(old) * (1 - DROP_LIMIT):
        raise RuntimeError('학교 수가 %d → %d로 10%% 넘게 줄어 저장하지 않았습니다(API 오류 의심)' % (len(old), len(new)))
    added, removed, changed = diff(old, new) if old else ([], [], [])
    schools.sort(key=lambda s: (s['sido'], s['sigungu'], s['level'] or 'z', s['name']))
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(schools)
    with open(os.path.join(OUT, 'schools.json'), 'w', encoding='utf-8') as f:
        json.dump(page_json(schools, day), f, ensure_ascii=False, separators=(',', ':'))
    ch_path = os.path.join(OUT, 'changes.md')
    prev = open(ch_path, encoding='utf-8').read() if os.path.exists(ch_path) else ''
    if not old or added or removed or changed or not prev:
        with open(ch_path, 'w', encoding='utf-8') as f:
            f.write(changes_md(day, added, removed, changed, prev) if old else
                    '# 학교 목록 변경 기록\n\n나이스 학교기본정보를 매일 받아 지난번과 비교한 결과입니다. 최신이 위에 있습니다.\n\n## %s\n\n- 처음 받음: 학교 %d곳\n' % (day, len(new)))
    unknown_sido = sorted({s['sido'] for s in schools if s['sido'] not in SIDO.values()})
    run = {'date': day, 'schools': len(new), 'added': len(added), 'removed': len(removed), 'changed': len(changed),
           'gu_guess': sum(1 for s in schools if s['gu_guess']), 'unknown_sido': unknown_sido}
    with open(os.path.join(OUT, 'run.json'), 'w', encoding='utf-8') as f:
        json.dump(run, f, ensure_ascii=False)
    print('학교 %d곳 · 새로 %d · 빠짐 %d · 바뀜 %d · 구 추정 %d · 모르는 시도 %s' % (
        len(new), len(added), len(removed), len(changed), run['gu_guess'], unknown_sido or '없음'))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # show a clear message in the Actions log and mark the run as failed
        print('실패:', re.sub(r'KEY=[^&\s]+', 'KEY=***', str(e)))
        sys.exit(1)
