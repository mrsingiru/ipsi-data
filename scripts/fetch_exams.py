"""
나이스 학사일정(Open API) → 초·중·고등학교 시험(중간·기말·지필평가)과 주요 행사 날짜만 골라 data/exams.json에 모읍니다.

- 나이스 학사일정은 시도교육청 코드와 학교 코드가 필수라서, 학교마다 한 번씩 부릅니다.
- 매일 학교의 1/7씩 돌아가며 받아, 모든 학교를 1주일에 한 번 새로 받습니다(MODE=rotate).
  처음 한 번은 MODE=all로 전부 받습니다.
- 하루 호출 한도(ERROR-337)에 걸리면 그때까지 받은 것을 저장하고 멈춥니다. 한 번도 못 받은 학교는 다음 실행 때 먼저 받습니다.
- 학교가 나이스에 올린 행사 이름을 기준으로 거릅니다. 이름이 학교마다 달라서, 거른 이름과 놓친 이름을
  data/exam_names.md에 적어 규칙을 고칠 수 있게 합니다.

결과 파일
- data/exams.json      : {'updated', 'source', 'window', 'schools': {학교코드: {'r': '부산 동래구', 'n': 학교명, 'lv': 'm'|'h',
                          'at': 받은 날, 'x': [[시작일, 끝일, 시험 이름, 학년('123')], ...],
                          'o': [[시작일, 끝일, 행사 이름, 학년, 종류(vac·off·cer·pick·gifted·meet)], ...]}}}
- data/exams_run.json  : 마지막 실행 정보(받은 학교 수, 시험이 있는 학교 수, 오류, 한도 도달 여부)
- data/exam_names.md   : 시험으로 본 이름과 비슷하지만 뺀 이름(규칙 점검용)
"""
import collections
import csv
import datetime
import hashlib
import json
import os
import re
import sys
import time

from neis import fetch, get_key, kst_now, kst_today, DailyLimit, NeisError

OUT = os.environ.get('OUT_DIR', 'data')
MODE = os.environ.get('MODE', 'rotate').strip() or 'rotate'
DAYS_BACK, DAYS_AHEAD, KEEP_PAST = 14, 210, 30
PAUSE = float(os.environ.get('PAUSE', '0.3'))
MAX_PER_RUN = int(os.environ.get('MAX_PER_RUN', '2500'))   # 2026-10-21: 하루 5,728+843번 불러도 한도에 안 걸림 → 초등학교를 4일 안에 채움
LEVELS = tuple(x for x in os.environ.get('LEVELS', 'e,m,h').split(',') if x in ('e', 'm', 'h'))

INCLUDE = re.compile(r'(중간|기말|지필|정기)\s*(고사|평가|시험)|[1-4]\s*(차|회)\s*(지필|정기)?\s*(고사|평가|시험)|학기\s*(고사|시험)')
EXCLUDE = re.compile(r'모의|학력평가|연합|수능|수학능력|진단|기초학력|학업성취|체력|듣기|면접|실기|수행|입학|입시|결과|답안|확인|대비|성적|원서|응시|재시험|추가시험|채점|이의|감독|출제|협의')
NEAR = re.compile(r'고사|평가|시험')
# v2 (2026-10-21): other events families plan around. type: vac 방학·개학 · off 재량휴업 · cer 입학·졸업·종업 · pick 과목 선택 ·
# gifted 영재(학급·교육원 관련) · meet 학부모 상담·설명회. Names are what the school typed; nothing personal is kept.
OTHER = [('vac', re.compile(r'방학|개학')), ('off', re.compile(r'재량\s*휴업|학교장\s*재량|재량\s*휴일|개교\s*기념일')),
         ('cer', re.compile(r'졸업식|입학식|종업식|시업식')), ('pick', re.compile(r'수강\s*신청|과목\s*선택|선택\s*과목|교육과정\s*(선택|수요)')),
         ('gifted', re.compile(r'영재')), ('meet', re.compile(r'학부모\s*(상담|총회|연수|설명회)|상담\s*주간|입학\s*설명회|교육과정\s*설명회'))]
OTHER_EXCLUDE = re.compile(r'토요|공휴일|대체|휴무|봉사|준비|협의|회의|연수\(교원|교직원|교사')
GRADE_FIELDS = ['ONE_GRADE_EVENT_YN', 'TW_GRADE_EVENT_YN', 'THREE_GRADE_EVENT_YN',
                'FR_GRADE_EVENT_YN', 'FIV_GRADE_EVENT_YN', 'SIX_GRADE_EVENT_YN']


def safe(msg):
    # never let the API key reach a log or a file
    return re.sub(r'KEY=[^&\s]+', 'KEY=***', str(msg))


def is_exam(name):
    return bool(INCLUDE.search(name)) and not EXCLUDE.search(name)


def other_type(name):
    if OTHER_EXCLUDE.search(name):
        return ''
    for t, rx in OTHER:
        if rx.search(name):
            return t
    return ''


def group_other(rows):
    """Rows of one school → [[start, end, name, grades, type], ...] for OTHER events (consecutive days merged)."""
    items = []
    for r in rows:
        name = (r.get('EVENT_NM') or '').strip()
        d = ymd(r.get('AA_YMD'))
        t = other_type(name) if d and name and not is_exam(name) else ''
        if t:
            items.append((re.sub(r'\s+', ' ', name)[:30], d, grades_of(r), t))
    items.sort(key=lambda x: (x[0], x[1]))
    out = []
    for name, d, gr, t in items:
        if out and out[-1][2] == name and (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(out[-1][1])).days <= 3:
            out[-1][1] = max(out[-1][1], d)
            out[-1][3] = ''.join(sorted(set(out[-1][3] + gr)))
            continue
        out.append([d, d, name, gr, t])
    out.sort(key=lambda x: x[0])
    return out[:40]


def base_name(name):
    n = re.sub(r'\(?\s*\d\s*일\s*차\s*\)?', '', name)
    n = re.sub(r'\(?\s*[\d,~·\s-]*\d\s*학년\s*\)?', '', n)
    n = re.sub(r'\s+', ' ', n).strip(' -·')
    return n or name.strip()


def ymd(s):
    s = (s or '').strip()
    return '%s-%s-%s' % (s[0:4], s[4:6], s[6:8]) if len(s) == 8 else ''


def grades_of(r):
    return ''.join(str(i + 1) for i, f in enumerate(GRADE_FIELDS) if (r.get(f) or '').strip().upper() == 'Y')


def group_exams(rows):
    """Rows of one school → [[start, end, name, grades], ...] (consecutive days of one exam become one range)."""
    items = []
    for r in rows:
        name = (r.get('EVENT_NM') or '').strip()
        d = ymd(r.get('AA_YMD'))
        if d and name and is_exam(name):
            items.append((base_name(name), d, grades_of(r)))
    items.sort(key=lambda x: (x[0], x[1]))
    out = []
    for name, d, gr in items:
        if out and out[-1][2] == name:
            last = datetime.date.fromisoformat(out[-1][1])
            if (datetime.date.fromisoformat(d) - last).days <= 4:
                out[-1][1] = max(out[-1][1], d)
                out[-1][3] = ''.join(sorted(set(out[-1][3] + gr)))
                continue
        out.append([d, d, name, gr])
    out.sort(key=lambda x: x[0])
    return out


def pick_today(schools, mode, today):
    if mode == 'all':
        return list(schools)
    k = today.toordinal() % 7
    return [s for s in schools if int(hashlib.md5(s['code'].encode()).hexdigest(), 16) % 7 == k]


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return default


def main(fetch_school=None, now=None):
    os.makedirs(OUT, exist_ok=True)
    now = now or kst_now()
    today = now.date()
    frm = (today - datetime.timedelta(days=DAYS_BACK)).strftime('%Y%m%d')
    to = (today + datetime.timedelta(days=DAYS_AHEAD)).strftime('%Y%m%d')
    with open(os.path.join(OUT, 'schools.csv'), encoding='utf-8-sig', newline='') as f:
        schools = [s for s in csv.DictReader(f) if s.get('level') in LEVELS and s.get('code') and s.get('atpt')]
    if fetch_school is None:
        key = get_key()
        def fetch_school(s):
            return fetch('SchoolSchedule', {'ATPT_OFCDC_SC_CODE': s['atpt'], 'SD_SCHUL_CODE': s['code'],
                                            'AA_FROM_YMD': frm, 'AA_TO_YMD': to}, key, pause=PAUSE)
    path = os.path.join(OUT, 'exams.json')
    data = load_json(path, {'schools': {}})
    keep = {s['code'] for s in schools}
    data['schools'] = {c: v for c, v in data.get('schools', {}).items() if c in keep}
    # never-fetched schools first (so a run stopped by the daily limit is finished on the next days), then today's share
    # v3 (2026-10-21): 초등학교도 받는다(방학·재량휴업·영재 행사). Order: never-fetched 중·고 → today's share → never-fetched 초,
    # so adding 6천여 초등학교 never holds back the weekly 중·고 refresh; they fill in over a few days under MAX_PER_RUN.
    never = [s for s in schools if s['code'] not in data['schools']]
    never_mh = [s for s in never if s['level'] != 'e']
    never_e = [s for s in never if s['level'] == 'e']
    seen = {s['code'] for s in never}
    todo = never_mh + [s for s in pick_today(schools, MODE, today) if s['code'] not in seen] + never_e
    if MODE != 'all':
        todo = todo[:MAX_PER_RUN]
    stats = collections.Counter()
    names, near, every = collections.Counter(), collections.Counter(), collections.Counter()
    limit_hit, errors = False, []
    for s in todo:
        try:
            rows = fetch_school(s)
        except DailyLimit as e:
            limit_hit = True
            errors.append(safe(e))
            break
        except Exception as e:  # one school failing must not stop the rest
            stats['error'] += 1
            if len(errors) < 20:
                errors.append('%s %s: %s' % (s['code'], s['name'], safe(e)))
            continue
        stats['fetched'] += 1
        for r in rows:
            n = (r.get('EVENT_NM') or '').strip()
            if is_exam(n):
                names[base_name(n)] += 1
            elif NEAR.search(n):
                near[n] += 1
            if n:
                every[re.sub(r'\s+', ' ', n)[:30]] += 1
        ex = group_exams(rows)
        ot = group_other(rows)
        region = s['sido'] if not s.get('sigungu') else s['sido'] + ' ' + s['sigungu']
        data['schools'][s['code']] = {'r': region, 'n': s['name'], 'lv': s['level'], 'at': today.isoformat(), 'x': ex, 'o': ot}
        if ex:
            stats['with_exams'] += 1
        time.sleep(PAUSE)
    cut = (today - datetime.timedelta(days=KEEP_PAST)).isoformat()
    for v in data['schools'].values():
        v['x'] = [e for e in v.get('x', []) if e[1] >= cut]
        if 'o' in v:
            v['o'] = [e for e in v['o'] if e[1] >= cut]
    data.update({'updated': today.isoformat(), 'source': '나이스 교육정보 개방 포털 학사일정(학교가 올린 행사 이름 기준)',
                 'window': [ymd(frm), ymd(to)]})
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    total_with = sum(1 for v in data['schools'].values() if v['x'])
    run = {'date': today.isoformat(), 'mode': MODE, 'planned': len(todo), 'fetched': stats['fetched'],
           'errors': stats['error'], 'with_exams_today': stats['with_exams'], 'schools_total': len(data['schools']),
           'schools_with_exams': total_with, 'limit_hit': limit_hit, 'error_samples': errors[:10]}
    with open(os.path.join(OUT, 'exams_run.json'), 'w', encoding='utf-8') as f:
        json.dump(run, f, ensure_ascii=False, indent=1)
    lines = ['# 시험 이름 점검(%s, %s)' % (today.isoformat(), MODE), '',
             '## 시험으로 본 이름(많은 순 40개)', ''] + ['- %s (%d)' % kv for kv in names.most_common(40)] + \
            ['', '## 고사·평가·시험이 들어가지만 뺀 이름(많은 순 40개)', ''] + ['- %s (%d)' % kv for kv in near.most_common(40)] + \
            ['', '## 이번 실행의 모든 행사 이름(많은 순 150개, 규칙 점검용)', ''] + \
            ['- %s (%d)%s' % (k, c, ' — ' + other_type(k) if other_type(k) else (' — 시험' if is_exam(k) else '')) for k, c in every.most_common(150)]
    with open(os.path.join(OUT, 'exam_names.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('시험 날짜: 오늘 %d곳 중 %d곳 받음 · 시험 있는 곳 %d · 오류 %d · 한도 %s · 누적 %d곳(시험 있음 %d)' % (
        len(todo), stats['fetched'], stats['with_exams'], stats['error'], '걸림' if limit_hit else '안 걸림',
        len(data['schools']), total_with))
    if limit_hit:
        print('하루 호출 한도에 걸려, 남은 학교는 다음 실행 때 먼저 받습니다.')


if __name__ == '__main__':
    try:
        main()
    except (NeisError, OSError, ValueError) as e:
        print('실패:', safe(e))
        sys.exit(1)
