"""공공데이터포털 오픈 API 세 가지 받기(2026-11-14 대표 활용신청 승인 · 비밀 값 DATA_GO_KR_KEY — 키는 찍지 않음).

1) 대학 기본정보 — 한국대학교육협의회_대학 및 전문대학정보_GW(15158665) /getSchoolInfo
   → data/univ_info.json : 학교코드·이름·영문·학교 구분·설립·본분교·지역·주소·누리집(조사년도 하나)
2) 학교별 학과 — 한국대학교육협의회_대학 학과 정보_GW(15158955) /getUniversityMajorCode (학교마다 부름)
   → data/univ_major.json : 4년제(대학·교육대학)만, 학과 코드·이름·단과대학·주야·캠퍼스 지역·표준분류(긴 설명 칸은 뺌)
3) 공휴일 — 한국천문연구원_특일 정보(15012690) /getRestDeInfo
   → data/holidays.json : 올해·내년 공휴일·대체공휴일(날짜·이름·쉬는 날 여부)
모두 '이용허락범위 제한 없음'. 화면에 쓸 때는 출처(기관·데이터 이름·공공데이터포털)를 적는다.
실행 결과는 data/public_apis_run.json.
"""
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SCHOOL = 'https://apis.data.go.kr/B340014/SchoolInfoService/getSchoolInfo'
MAJOR = 'https://apis.data.go.kr/B340014/BasicInformationService_1/getUniversityMajorCode'
REST = 'https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo'
FOUR_YEAR = {'대학', '교육대학'}
MAJOR_KEEP = ['schlId', 'korSchlNm', 'psbsDivNm', 'mjrId', 'kediMjrId', 'korMjrNm', 'clgNm', 'dghtDivNm', 'mjrAreaNm', 'mjrAreaSignguNm',
              'korSrsLclftNm', 'korSrsMclftNm', 'korSrsSclftNm', 'mjrStatNm', 'pbnfDgriCrseDivNm', 'lsnTrmNm', 'svyYr']


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def kst_year():
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)).year


def call(url, q, key, tries=4):
    qs = urllib.parse.urlencode(dict(q, serviceKey=key))
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(url + '?' + qs, timeout=90) as r:
                root = ET.fromstring(r.read())
            code = root.findtext('.//resultCode')
            if code not in ('00', '0', None):
                raise RuntimeError('resultCode %s %s' % (code, root.findtext('.//resultMsg')))
            items = [{c.tag: (c.text or '').strip() for c in it} for it in root.iter('item')]
            return int(root.findtext('.//totalCount') or 0), items
        except Exception as e:   # 키가 든 주소는 찍지 않음
            last = '%s %s' % (type(e).__name__, str(e)[:120])
            time.sleep(4 * (i + 1))
    raise RuntimeError('%s 받기 실패: %s' % (url.rsplit('/', 1)[-1], last))


def all_pages(url, q, key, per=1000):
    total, items = call(url, dict(q, pageNo=1, numOfRows=per), key)
    for p in range(2, -(-total // per) + 1):
        items += call(url, dict(q, pageNo=p, numOfRows=per), key)[1]
        time.sleep(0.2)
    return total, items


def save(path, obj):
    old = None
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding='utf-8'))
        except Exception:
            pass
    body = {k: v for k, v in obj.items() if k != 'fetchedAt'}
    if old and {k: v for k, v in old.items() if k != 'fetchedAt'} == body:
        return False
    json.dump(dict(obj, fetchedAt=now_utc()), open(path, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    return True


def main():
    key = os.environ.get('DATA_GO_KR_KEY', '').strip()
    if not key:
        print('::notice::비밀 값 DATA_GO_KR_KEY가 없어 건너뜁니다.')
        return 0
    run, t0, bad = {'workflow': 'public_apis'}, time.time(), []
    # 1) 대학 기본정보 — 올해 조사년도가 아직 없으면 지난해
    yr, schools, stotal = None, [], None
    for y in (kst_year(), kst_year() - 1, kst_year() - 2):
        total, schools = all_pages(SCHOOL, {'svyYr': y}, key)
        if total and schools:
            yr, stotal = y, total
            break
    if not schools:
        bad.append('대학 기본정보 0곳')
    else:
        keep = sorted(schools, key=lambda s: (s.get('schlNm', ''), s.get('psbsDivNm', '')))
        # 2026-11-14: API 전체 건수(totalCount)도 남김 — 받은 줄이 정확히 2,000이라 최대치에 걸렸는지 확인용(ipsi-site tools/univ_name_check.py가 읽음)
        run['univInfo'] = {'svyYr': yr, 'schools': len(keep), 'total': stotal, 'changed': save('data/univ_info.json', {
            'source': {'org': '한국대학교육협의회', 'name': '대학 및 전문대학정보_GW', 'portal': '공공데이터포털', 'id': '15158665', 'license': '이용허락범위 제한 없음'},
            'svyYr': yr, 'rows': keep})}
    # 2) 공휴일(먼저 — 학과가 오래 걸려도 공휴일은 저장되게) — 올해·내년
    hol = {}
    for y in (kst_year(), kst_year() + 1):
        _, items = all_pages(REST, {'solYear': y}, key, per=100)
        hol[str(y)] = sorted(({'date': '%s-%s-%s' % (i['locdate'][:4], i['locdate'][4:6], i['locdate'][6:8]), 'name': i.get('dateName'),
                               'holiday': i.get('isHoliday') == 'Y'} for i in items if i.get('locdate')), key=lambda h: h['date'])
    run['holidays'] = {y: len(v) for y, v in hol.items()}
    run['holidays']['changed'] = save('data/holidays.json', {
        'source': {'org': '한국천문연구원', 'name': '특일 정보(공휴일 정보 조회)', 'portal': '공공데이터포털', 'id': '15012690', 'license': '이용허락범위 제한 없음'},
        'note': '대체공휴일·임시공휴일은 관보 공포 뒤에 반영(제공기관 안내)', 'years': hol})
    # 3) 학교별 학과 — 4년제만, 학교마다 부름
    four = [s for s in schools if s.get('schlDivNm') in FOUR_YEAR and s.get('schlId')]
    majors, myr, miss = [], None, []
    for y in ([yr] if yr else []) + ([yr - 1] if yr else []):
        majors, miss = [], []
        for s in four:
            try:
                _, items = all_pages(MAJOR, {'svyYr': y, 'schlId': s['schlId']}, key)
            except Exception as e:
                miss.append(s['schlId']); continue
            for it in items:
                majors.append({k: it.get(k) for k in MAJOR_KEEP if it.get(k) not in (None, '')} | {'schlId': s['schlId']})
            time.sleep(0.15)
        if majors:
            myr = y
            break
    if not majors:
        bad.append('학과 0줄')
    else:
        majors.sort(key=lambda m: (m.get('korSchlNm', ''), m.get('clgNm', ''), m.get('korMjrNm', '')))
        run['univMajor'] = {'svyYr': myr, 'schools': len(four), 'majors': len(majors), 'missSchools': len(miss), 'changed': save('data/univ_major.json', {
            'source': {'org': '한국대학교육협의회', 'name': '대학 학과 정보_GW', 'portal': '공공데이터포털', 'id': '15158955', 'license': '이용허락범위 제한 없음'},
            'svyYr': myr, 'note': '4년제(대학·교육대학)만 · 긴 설명 칸(교육목표·교과목 등)은 뺌', 'rows': majors})}
    run.update(finishedAt=now_utc(), secs=round(time.time() - t0, 1), problems=bad)
    json.dump(run, open('data/public_apis_run.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(json.dumps(run, ensure_ascii=False))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
