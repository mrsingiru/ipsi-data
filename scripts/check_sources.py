"""출처 점검 — 페이지가 근거로 쓰는 주소(watch/sources.json)가 살아 있는지, 내용이 바뀌었는지 매주 본다.

    python scripts/check_sources.py watch/sources.json data/sources_status.json data/sources_report.md

지키는 것(공식 공고 읽기와 같음): robots.txt를 먼저 읽고 막힌 주소는 열지 않는다. 로그인·명단 주소는 열지 않는다.
사이트마다 1.2초 넘게 쉬고, 한 번에 사이트당 40번까지만 연다(남은 것은 다음 주). 원문은 저장하지 않고
상태 코드·최종 주소·문서 종류·크기·내용 지문(해시)·제목만 남긴다.
- cycle 'w'(공식 원문, 날짜가 걸린 곳)는 매주, 'm'(기사·학습 사이트)은 28일이 지나면 다시 연다.
- 바뀜: 지문이 지난번과 다르면 changedAt을 적는다. 세 번 연속 바뀌는 곳(조회수·광고로 늘 바뀌는 곳)은 noisy로 두고 알리지 않는다.
- 끊김: 두 번 연속 못 열면(4xx·5xx·오류) broken.
"""
import datetime, hashlib, json, os, re, sys, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_official as fo
from bs4 import BeautifulSoup

fo.PER_HOST = 40
fo.HOST_LIMIT = {}
NOISE = re.compile(r'(조회|조회수|hit|views?|방문자|추천수|댓글|공유하기|좋아요|현재\s*시각|접속자|copyright|ⓒ|©)', re.I)
DENY = ('login', 'logout', '로그인', '명단', '합격자')

def kst_now():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)

def fingerprint(data, ctype, url):
    """Stable digest of the page's own content (not the counters around it) and its title."""
    if isinstance(data, bytes):
        return hashlib.sha256(data).hexdigest()[:16], ''
    soup = BeautifulSoup(data, 'lxml')
    title = (soup.title.get_text(' ', strip=True) if soup.title else '')[:120]
    for t in soup(['script', 'style', 'noscript', 'iframe', 'header', 'footer', 'nav', 'aside', 'form']):
        t.decompose()
    main = soup.find('article') or soup.find('main') or soup.body or soup
    lines = []
    for ln in main.get_text('\n', strip=True).split('\n'):
        ln = re.sub(r'\s+', ' ', ln).strip()
        if ln and not NOISE.search(ln):
            lines.append(ln)
    return hashlib.sha256('\n'.join(lines).encode('utf-8')).hexdigest()[:16], title

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
    order = sorted(reg['sources'], key=lambda x: (prev.get(x['id'], {}).get('checkedAt', ''), x['cycle'] != 'w'))
    for it in order:
        p = dict(prev.get(it['id'], {}))
        p.update({'url': it['url'], 'kind': it['kind'], 'cat': it['cat'], 'used': it['used'][:12], 'cycle': it['cycle']})
        if any(w in it['url'].lower() for w in DENY):
            p.update({'status': 'skip-deny'}); items[it['id']] = p; counts['skipped'] += 1; continue
        if not due(it, prev, today):
            items[it['id']] = p; counts['skipped'] += 1; continue
        binary = bool(re.search(r'\.(pdf|hwpx?|zip)(\?|$)', it['url'], re.I))
        st, data, final, ctype = F.get(it['url'], binary=binary)
        if st == 'limit':
            items[it['id']] = p; counts['limit'] += 1; continue
        counts['checked'] += 1
        p['checkedAt'] = now.isoformat(timespec='minutes')
        p['status'] = st
        if st == 'ok':
            h, title = fingerprint(data, ctype, it['url'])
            changed = bool(p.get('hash')) and p['hash'] != h
            p['changeStreak'] = (p.get('changeStreak', 0) + 1) if changed else 0
            p['noisy'] = p['changeStreak'] >= 3 or (p.get('noisy') and p['changeStreak'] > 0)
            if changed:
                p['changedAt'] = now.isoformat(timespec='minutes'); counts['changed'] += 1
            p.update({'hash': h, 'title': title, 'final': final if final != it['url'] else '', 'ctype': (ctype or '').split(';')[0][:40],
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
    moved = [(k, v) for k, v in items.items() if v.get('final') and urllib.parse.urlsplit(v['final']).netloc != urllib.parse.urlsplit(v['url']).netloc]
    doc = {'checkedAt': now.isoformat(timespec='minutes'), 'registry': {'ver': reg.get('ver'), 'generated': reg.get('generated'), 'count': reg.get('count')},
           'run': counts, 'summary': {'broken': len(broken), 'changedOfficial': len(changed), 'moved': len(moved),
                                      'robots': sum(1 for v in items.values() if v.get('status') == 'robots'),
                                      'neverOk': sum(1 for v in items.values() if v.get('checkedAt') and not v.get('lastOk'))},
           'items': items}
    json.dump(doc, open(out_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    L = ['# 출처 점검 ' + today.isoformat(), '',
         '페이지 ' + str(reg.get('ver')) + '가 쓰는 주소 ' + str(reg.get('count')) + '곳 중 이번에 ' + str(counts['checked']) + '곳을 열었습니다 '
         '(열림 ' + str(counts['ok']) + ' · robots로 안 엶 ' + str(counts['robots']) + ' · 못 엶 ' + str(counts['failed']) + ' · 다음 차례 ' + str(counts['skipped'] + counts['limit']) + ').', '']
    def block(title, rows):
        L.append('## ' + title + ' (' + str(len(rows)) + ')')
        for k, v in rows[:60]:
            L.append('- ' + v['url'] + ' — ' + ', '.join(v.get('used', [])[:6]) + ((' → ' + v['final']) if v.get('final') else '') + ((' · ' + str(v.get('error') or v.get('status'))) if v.get('failStreak') else ''))
        L.append('')
    block('끊김: 두 번 연속 못 엶', broken)
    block('바뀜: 이번 주 내용이 바뀐 공식 원문', changed)
    block('옮김: 다른 사이트로 넘어감', moved)
    open(report_path, 'w', encoding='utf-8').write('\n'.join(L))
    print(json.dumps(doc['run'], ensure_ascii=False), json.dumps(doc['summary'], ensure_ascii=False))

if __name__ == '__main__':
    main(*sys.argv[1:4])
