"""출처 점검 — 페이지가 근거로 쓰는 주소(watch/sources.json)가 살아 있는지, 내용이 바뀌었는지 매주 본다.

    python scripts/check_sources.py watch/sources.json data/sources_status.json data/sources_report.md

지키는 것(공식 공고 읽기와 같음): robots.txt를 먼저 읽고 막힌 주소는 열지 않는다. 로그인·명단 주소는 열지 않는다.
사이트마다 1.2초 넘게 쉬고, 한 번에 사이트당 40번까지만 연다(남은 것은 다음 주). 원문은 저장하지 않고
상태 코드·최종 주소·문서 종류·크기·내용 지문(해시)·제목만 남긴다.
- cycle 'w'(공식 원문, 날짜가 걸린 곳)는 매주, 'm'(기사·학습 사이트)은 28일이 지나면 다시 연다.
- 바뀜: 지문이 지난번과 다르면 changedAt을 적는다. 세 번 연속 바뀌는 곳(조회수·광고로 늘 바뀌는 곳)은 noisy로 두고 알리지 않는다.
- 끊김: 두 번 연속 못 열면(4xx·5xx·오류) broken.
- (지문 v2 — 2026-11-12 자료 개선안 2026-11-09-d1, 대표 11/9 승인: 바뀐 공식 원문 59·61개가 매주 거의 다 '바뀜'이었는데 열어 본 5곳 중
  날짜가 바뀐 곳 0) 지문은 본문 글만: 게시판 목록(링크가 든 줄 5개 넘는 표)은 글 제목 목록만, 그 밖은 본문에서 오늘 날짜·시각·
  세션 값·긴 16진 값 줄을 빼고 잰다. 본문의 날짜 글자(2026.11.12 · 2026-11-12 · 11월 12일 · 11. 12.)만 따로 모은 지문(dhash)도 둔다.
  2주 연속 바뀌었는데 날짜 글자는 그대로면(quietStreak 2) noisy. 지문 방식이 바뀐 첫 주는 비교하지 않고 새 기준만 적는다(fpv).
"""
import datetime, hashlib, json, os, re, sys, time, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_official as fo
from bs4 import BeautifulSoup

fo.PER_HOST = 40
fo.HOST_LIMIT = {}
NOISE = re.compile(r'(조회|조회수|hit|views?|방문자|추천수|댓글|공유하기|좋아요|현재\s*시각|접속자|copyright|ⓒ|©)', re.I)
DENY = ('login', 'logout', '로그인', '명단', '합격자')

KST = datetime.timezone(datetime.timedelta(hours=9))

def kst_now():
    return datetime.datetime.now(KST)   # 2026-10-21: '+09:00'으로 적힘(전에는 한국 시각에 '+00:00'이 붙었음)

def host(u):
    h = (urllib.parse.urlsplit(u).hostname or '').lower()
    return h[4:] if h.startswith('www.') else h

FPV = 2   # 지문 방식 번호 — 바뀌면 첫 주는 비교하지 않고 새 기준만 적는다
DATE_RX = re.compile(r'(20\d\d)\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})|(\d{1,2})\s*월\s*(\d{1,2})\s*일|(?<![\d.])(\d{1,2})\.\s?(\d{1,2})\.(?!\d)')
TIME_RX = re.compile(r'\b\d{1,2}:\d{2}(:\d{2})?\b')
SESSION_RX = re.compile(r'(jsessionid|sessionid|phpsessid|csrf|token)[=:;][^\s&"\']+|\b[0-9a-f]{24,}\b', re.I)


def _today_forms(today):
    y, m, d = today.year, today.month, today.day
    return {'%d.%02d.%02d' % (y, m, d), '%d-%02d-%02d' % (y, m, d), '%d/%02d/%02d' % (y, m, d), '%d.%d.%d' % (y, m, d), '%d년 %d월 %d일' % (y, m, d)}


def _board_titles(main):
    """게시판 목록이면 글 제목(링크 글)만 — 조회수·작성일·번호 칸은 뺌. 링크가 든 줄이 5개 넘는 표가 없으면 None."""
    for tb in main.find_all('table'):
        rows = [tr for tr in tb.find_all('tr') if tr.find('a')]
        if len(rows) > 5:
            out = []
            for tr in rows:
                a = max(tr.find_all('a'), key=lambda x: len(x.get_text(' ', strip=True)))
                t = re.sub(r'\s+', ' ', a.get_text(' ', strip=True)).strip()
                if t:
                    out.append(t)
            if len(out) > 5:
                return out
    return None


def date_sig(text):
    """본문에 적힌 날짜 글자만 모아 정렬한 지문 — 글은 바뀌어도 날짜가 그대로인지 보려고."""
    ds = set()
    for m in DATE_RX.finditer(text):
        g = m.groups()
        ds.add('%s-%02d-%02d' % (g[0], int(g[1]), int(g[2])) if g[0] else ('%02d-%02d' % (int(g[3]), int(g[4])) if g[3] else '%02d-%02d' % (int(g[5]), int(g[6]))))
    return hashlib.sha256('\n'.join(sorted(ds)).encode('utf-8')).hexdigest()[:16], len(ds)


def fingerprint(data, ctype, url, today=None):
    """Stable digest of the page's own content (not the counters around it), its title, and a digest of the dates in it.
    v2(2026-11-12): 게시판 목록은 글 제목만 · 오늘 날짜·시각·세션 값 줄은 뺌."""
    if isinstance(data, bytes):
        h = hashlib.sha256(data).hexdigest()[:16]
        return h, '', h
    today = today or kst_now().date()
    soup = BeautifulSoup(data, 'lxml')
    title = (soup.title.get_text(' ', strip=True) if soup.title else '')[:120]
    for t in soup(['script', 'style', 'noscript', 'iframe', 'header', 'footer', 'nav', 'aside', 'form']):
        t.decompose()
    main = soup.find('article') or soup.find('main') or soup.body or soup
    tf = _today_forms(today)
    lines = _board_titles(main)
    if lines is None:
        lines = []
        for ln in main.get_text('\n', strip=True).split('\n'):
            ln = re.sub(r'\s+', ' ', ln).strip()
            if not ln or NOISE.search(ln) or any(f in ln for f in tf):
                continue
            ln = SESSION_RX.sub('', TIME_RX.sub('', ln)).strip()
            if ln:
                lines.append(ln)
    text = '\n'.join(lines)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16], title, date_sig(text)[0]

def due(item, prev, today):
    p = prev.get(item['id'])
    if not p or not p.get('checkedAt'):
        return True
    last = datetime.date.fromisoformat(p['checkedAt'][:10])
    return (today - last).days >= (6 if item.get('cycle') == 'w' else 28)

def main(reg_path, out_path, report_path):
    reg = json.load(open(reg_path, encoding='utf-8'))
    try:
        old = json.load(open(out_path, encoding='utf-8'))
    except (OSError, ValueError):
        old = {}
    prev = old.get('items', {})
    now = kst_now(); today = now.date()
    F = fo.Fetcher()
    items = {}
    counts = {'checked': 0, 'ok': 0, 'robots': 0, 'failed': 0, 'changed': 0, 'skipped': 0, 'limit': 0}
    retried = 0
    order = sorted(reg['sources'], key=lambda x: (prev.get(x['id'], {}).get('checkedAt', ''), x['cycle'] != 'w'))
    for it in order:
        p = dict(prev.get(it['id'], {}))
        p.update({'url': it['url'], 'kind': it['kind'], 'cat': it['cat'], 'used': it['used'][:12], 'cycle': it['cycle']})
        if any(w in it['url'].lower() for w in DENY):
            p.update({'status': 'skip-deny'}); items[it['id']] = p; counts['skipped'] += 1; continue
        # 2026-10-21 이용 조건: 언론(일부 매체 'AI 학습·이용 금지' 표시)과 GED(서면 동의 없는 DB 구축 금지)는 자동으로 열지 않는다.
        # 사람이 링크만 걸고, 같은 사실의 공식 원문으로 바꿔 간다.
        if it.get('cat') == '언론' or 'ged.kedi.re.kr' in it['url']:
            p.update({'status': 'skip-terms'}); items[it['id']] = p; counts['skipped'] += 1; continue
        if not due(it, prev, today):
            items[it['id']] = p; counts['skipped'] += 1; continue
        binary = bool(re.search(r'\.(pdf|hwpx?|zip)(\?|$)', it['url'], re.I))
        st, data, final, ctype = F.get(it['url'], binary=binary)
        # 2026-11-13 대표 '출처 점검이 멈춰 있는데 이유 분석': 울산대 입학처 파일이 두 주 연속 서버 오류(500)로 '끊김'이 됐지만 사람이 열면 열림
        #  (일요일·월요일 이른 아침 서버 점검 시간 추정) → 서버 쪽 오류·연결 오류는 30초 뒤 한 번만 더 열어 보고 센다(한 실행에 10곳까지).
        if (st == 'error' or str(st).startswith('http-5')) and retried < 10:
            retried += 1; time.sleep(30)
            st, data, final, ctype = F.get(it['url'], binary=binary)
            p['retried'] = now.isoformat(timespec='minutes')
        if st == 'limit':
            items[it['id']] = p; counts['limit'] += 1; continue
        counts['checked'] += 1
        p['checkedAt'] = now.isoformat(timespec='minutes')
        p['status'] = st
        if st == 'ok' and final and final != it['url'] and any(w in final.lower() for w in DENY):
            # 로그인 화면으로 넘어가는 주소: 더 읽지 않고 표시만 한다(옮김·끊김으로 세지 않음).
            p.update({'status': 'login', 'final': final, 'failStreak': 0}); counts['login'] = counts.get('login', 0) + 1
            items[it['id']] = p; continue
        if st == 'ok':
            h, title, dh = fingerprint(data, ctype, it['url'], today)
            same_fp = p.get('fpv') == FPV   # 지문 방식이 바뀐 첫 주는 비교하지 않음(모두 '바뀜'으로 나오지 않게)
            changed = same_fp and bool(p.get('hash')) and p['hash'] != h
            dchanged = changed and bool(p.get('dhash')) and p['dhash'] != dh
            p['changeStreak'] = (p.get('changeStreak', 0) + 1) if changed else 0
            p['quietStreak'] = (p.get('quietStreak', 0) + 1) if (changed and not dchanged) else 0   # 바뀌었는데 날짜 글자는 그대로인 주가 이어진 수
            # 날짜 글자가 바뀐 주는 흔들림으로 숨기지 않는다(그게 우리가 찾는 바뀜)
            p['noisy'] = (not dchanged) and (p['changeStreak'] >= 3 or p['quietStreak'] >= 2 or bool(p.get('noisy') and changed))
            p['datesChanged'] = dchanged
            if changed:
                p['changedAt'] = now.isoformat(timespec='minutes'); counts['changed'] += 1
                if dchanged:
                    counts['datesChanged'] = counts.get('datesChanged', 0) + 1
            p.update({'hash': h, 'dhash': dh, 'fpv': FPV, 'title': title, 'final': final if final != it['url'] else '', 'ctype': (ctype or '').split(';')[0][:40],
                      'lastOk': now.isoformat(timespec='minutes'), 'failStreak': 0})
            counts['ok'] += 1
        elif st == 'robots':
            p['failStreak'] = 0; counts['robots'] += 1
        else:
            p['failStreak'] = p.get('failStreak', 0) + 1; p['error'] = (data if isinstance(data, str) else st)[:160] if st == 'error' else st
            counts['failed'] += 1
        items[it['id']] = p
    broken = [(k, v) for k, v in items.items() if v.get('failStreak', 0) >= 2]
    changed = [(k, v) for k, v in items.items() if v.get('changedAt', '')[:10] == today.isoformat() and not v.get('noisy') and v.get('kind') == '공식']
    changed.sort(key=lambda kv: not kv[1].get('datesChanged'))   # 날짜 글자가 바뀐 곳 먼저
    moved = [(k, v) for k, v in items.items() if v.get('status') == 'ok' and v.get('final') and host(v['final']) != host(v['url'])]
    login = [(k, v) for k, v in items.items() if v.get('status') == 'login']
    terms = sum(1 for v in items.values() if v.get('status') == 'skip-terms')
    doc = {'checkedAt': now.isoformat(timespec='minutes'), 'registry': {'ver': reg.get('ver'), 'generated': reg.get('generated'), 'count': reg.get('count')},
           'run': counts, 'summary': {'broken': len(broken), 'changedOfficial': len(changed), 'changedDates': sum(1 for k, v in changed if v.get('datesChanged')), 'moved': len(moved),
                                      'robots': sum(1 for v in items.values() if v.get('status') == 'robots'),
                                      'login': len(login), 'skipTerms': terms,
                                      'neverOk': sum(1 for v in items.values() if v.get('checkedAt') and not v.get('lastOk'))},
           'items': items}
    json.dump(doc, open(out_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    L = ['# 출처 점검 ' + today.isoformat(), '',
         '페이지 ' + str(reg.get('ver')) + '가 쓰는 주소 ' + str(reg.get('count')) + '곳 중 이번에 ' + str(counts['checked']) + '곳을 열었습니다 '
         '(열림 ' + str(counts['ok']) + ' · robots로 안 엶 ' + str(counts['robots']) + ' · 못 엶 ' + str(counts['failed']) + ' · 로그인 화면 ' + str(counts.get('login', 0)) + ').', '',
         '열지 않은 곳: 이용 조건으로 건너뜀(언론·GED) ' + str(terms) + ' · 다음 차례(아직 확인 주기 안 됨·한도) ' + str(counts['skipped'] - terms + counts['limit']) + '.', '']
    def block(title, rows):
        L.append('## ' + title + ' (' + str(len(rows)) + ')')
        for k, v in rows[:60]:
            L.append('- ' + v['url'] + ' — ' + ', '.join(v.get('used', [])[:6]) + ((' → ' + v['final']) if v.get('final') else '') + ((' · ' + str(v.get('error') or v.get('status'))) if v.get('failStreak') else '') + (' · 날짜 글자 바뀜' if v.get('datesChanged') and v.get('changedAt', '')[:10] == today.isoformat() else ''))
        L.append('')
    block('끊김: 두 번 연속 못 엶', broken)
    block('바뀜: 이번 주 내용이 바뀐 공식 원문', changed)
    block('옮김: 다른 사이트로 넘어감', moved)
    block('로그인 화면으로 넘어감(자동으로 더 보지 않음)', login)
    open(report_path, 'w', encoding='utf-8').write('\n'.join(L))
    print(json.dumps(doc['run'], ensure_ascii=False), json.dumps(doc['summary'], ensure_ascii=False))

if __name__ == '__main__':
    main(*sys.argv[1:4])
