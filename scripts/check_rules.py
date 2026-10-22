"""규정 점검 — 규정 대장(watch/rules.json)의 원문 주소를 열어, 바뀐 부분이 있는지 매주 본다.

    python scripts/check_rules.py watch/rules.json data/rules_status.json data/rules_report.md

지키는 것(출처 점검과 같음): robots.txt를 먼저 읽고 막힌 주소는 열지 않는다. 사이트마다 1초 넘게 쉰다.
원문은 저장하지 않는다. 남기는 것은 상태·부분(제목)별 지문·제목 글(80자 안)·페이지가 밝힌 수정일·감시 낱말이 있는지(개수)뿐이다.

알리는 기준
- high: 감시 낱말이 지난번엔 있었는데 이번에 없어짐(우리가 기대는 사실이 바뀌었을 수 있음), 또는 페이지 수정일이 바뀌고 부분도 바뀜
- mid: 부분(제목 아래 내용)이 바뀌거나, 부분이 새로 생기거나 없어짐
- low: 두 번 연속 못 엶, 본문이 너무 짧아 읽지 못함(자바스크립트로만 보이는 페이지 등), 처음 기록
method 'ai'·'manual'은 여기서 열지 않는다(마케팅 주간 예약이 차례를 보고 확인한다).
"""
import datetime, hashlib, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_official as fo
from bs4 import BeautifulSoup

fo.PER_HOST = 20
fo.HOST_LIMIT = {}
KST = datetime.timezone(datetime.timedelta(hours=9))
NOISE = re.compile(r'(조회|조회수|방문자|추천수|댓글|공유하기|좋아요|현재\s*시각|접속자|copyright|ⓒ|©|was this helpful|send feedback|need more help|sign in|cookie|쿠키|skip to main|on this page|table of contents|language|english|한국어)', re.I)
DATE = r'(\d{4}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}\s*일?|[A-Z][a-z]{2,8}\.? \d{1,2},? \d{4}|\d{1,2} [A-Z][a-z]{2,8},? \d{4})'
# 페이지 꼬리의 '마지막 수정'을 먼저 찾고, 없을 때만 넓은 낱말로 찾는다(개정 이력 본문의 옛 날짜를 잡지 않게)
UPDATED = [re.compile(r'(Last updated|최종\s*업데이트|최종\s*수정일?)\s*[:：]?\s*' + DATE),
           re.compile(r'(Updated on|Effective|시행일|개정일)\s*[:：]?\s*' + DATE),
           re.compile(r'(Updated|업데이트|수정일)\s*[:：]?\s*' + DATE)]
THIN = 600   # 본문 글자 수가 이보다 적으면 '못 읽음(본문 짧음)'


def now_kst():
    return datetime.datetime.now(KST)


def h12(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()[:12]


def analyse(html, watch):
    soup = BeautifulSoup(html, 'lxml')
    title = (soup.title.get_text(' ', strip=True) if soup.title else '')[:120]
    flat = re.sub(r'\s+', ' ', soup.get_text(' ', strip=True))
    m = None
    for rx in UPDATED:
        m = rx.search(flat)
        if m:
            break
    updated = re.sub(r'\s+', ' ', m.group(1) + ' ' + m.group(2)).strip() if m else ''
    for t in soup(['script', 'style', 'noscript', 'iframe', 'header', 'footer', 'nav', 'aside', 'form', 'svg', 'button']):
        t.decompose()
    main = soup.find('article') or soup.find('main') or soup.body or soup
    for hd in main.find_all(re.compile(r'^h[1-4]$')):
        txt = re.sub(r'\s+', ' ', hd.get_text(' ', strip=True))[:80]
        hd.clear()
        hd.append('\n§§H§§' + txt + '\n')
    sections, cur, buf, body = [], '(첫 부분)', [], []
    for ln in main.get_text('\n', strip=True).split('\n'):
        ln = re.sub(r'\s+', ' ', ln).strip()
        if not ln:
            continue
        if ln.startswith('§§H§§'):
            if buf:
                sections.append([cur, h12('\n'.join(buf))])
            cur, buf = ln[5:] or '(제목 없음)', []
            continue
        if NOISE.search(ln) and len(ln) < 80:
            continue
        buf.append(ln); body.append(ln)
    if buf:
        sections.append([cur, h12('\n'.join(buf))])
    text = '\n'.join(body)
    low = text.lower()
    counts = {w: low.count(w.lower()) for w in watch}
    return {'title': title, 'updated': updated, 'chars': len(text), 'hash': h12(text),
            'sections': sections[:80], 'watch': counts}


def compare(prev, cur):
    """Return (level, changes) for one source."""
    ch = {}
    if not prev or not prev.get('hash'):
        return 'first', ch
    ps = {}
    for name, hh in prev.get('sections', []):
        ps.setdefault(name, []).append(hh)
    cs = {}
    for name, hh in cur['sections']:
        cs.setdefault(name, []).append(hh)
    changed = [n for n in cs if n in ps and cs[n] != ps[n]]
    added = [n for n in cs if n not in ps]
    removed = [n for n in ps if n not in cs]
    if changed: ch['changed'] = changed[:20]
    if added: ch['added'] = added[:20]
    if removed: ch['removed'] = removed[:20]
    if prev.get('updated') != cur['updated'] and (prev.get('updated') or cur['updated']):
        ch['updated'] = [prev.get('updated', ''), cur['updated']]
    pw = prev.get('watch', {})
    lost = [w for w, n in cur['watch'].items() if pw.get(w, 0) > 0 and n == 0]
    gained = [w for w, n in cur['watch'].items() if pw.get(w, 0) == 0 and n > 0 and w in pw]
    if lost: ch['lost'] = lost
    if gained: ch['gained'] = gained
    if lost or ('updated' in ch and (changed or added or removed)):
        return 'high', ch
    if changed or added or removed:
        return 'mid', ch
    if cur['hash'] != prev.get('hash'):
        return 'quiet', ch   # 줄 바꿈·공백 정도만 다름(부분 지문은 같음)
    return 'same', ch


def due(src, prev, today):
    if not prev or not prev.get('checkedAt'):
        return True
    last = datetime.date.fromisoformat(prev['checkedAt'][:10])
    return (today - last).days >= (6 if src.get('cycle') == 'w' else 27)


def main(reg_path, out_path, report_path):
    reg = json.load(open(reg_path, encoding='utf-8'))
    try:
        old = json.load(open(out_path, encoding='utf-8'))
    except (OSError, ValueError):
        old = {}
    prevs = old.get('sources', {})
    now = now_kst(); today = now.date()
    rules_of = {}
    for r in reg['rules']:
        for s in r.get('src', []):
            rules_of.setdefault(s, []).append(r['id'])
    F = fo.Fetcher()
    out, events = {}, []
    run = {'auto': 0, 'opened': 0, 'ok': 0, 'robots': 0, 'failed': 0, 'thin': 0, 'notDue': 0}
    for src in reg['sources']:
        sid = src['id']
        p = dict(prevs.get(sid, {}))
        p.update({'name': src['name'], 'owner': src.get('owner', ''), 'url': src.get('url', ''), 'method': src['method'],
                  'cycle': src.get('cycle', 'w'), 'rules': rules_of.get(sid, [])})
        if src['method'] != 'auto' or not src.get('url'):
            out[sid] = p; continue
        run['auto'] += 1
        if not due(src, prevs.get(sid), today):
            run['notDue'] += 1; out[sid] = p; continue
        # 영어 원문은 영어로 받는다(한국어 번역판이 오면 감시 낱말·지문이 어긋남, 2026-10-22 첫 실행에서 확인)
        F.s.headers['Accept-Language'] = 'ko-KR,ko;q=0.9' if src.get('lang') == 'ko' else 'en-US,en;q=0.9'
        st, data, final, ctype = F.get(src['url'])
        if st == 'limit':
            out[sid] = p; continue
        run['opened'] += 1
        p['checkedAt'] = now.isoformat(timespec='minutes')
        p['status'] = st
        p['final'] = final if final and final != src['url'] else ''
        if st == 'ok' and isinstance(data, str):
            cur = analyse(data, src.get('watch', []))
            if cur['chars'] < src.get('minChars', THIN):   # 고객센터 껍데기만 오는 페이지는 원문별로 기준을 높임
                p.update({'status': 'thin', 'chars': cur['chars'], 'title': cur['title'], 'failStreak': 0})
                run['thin'] += 1
                if prevs.get(sid, {}).get('status') != 'thin':
                    events.append({'source': sid, 'rules': p['rules'], 'level': 'low', 'what': '본문이 짧아 자동으로 읽지 못함(직접 확인·마케팅 주간이 원문 확인)'})
                out[sid] = p; continue
            level, ch = compare(prevs.get(sid), cur)
            p.update({k: cur[k] for k in ('title', 'updated', 'chars', 'hash', 'sections', 'watch')})
            p['lastOk'] = now.isoformat(timespec='minutes'); p['failStreak'] = 0
            missing0 = [w for w, n in cur['watch'].items() if n == 0]
            p['watchMissing'] = missing0
            if level == 'first':
                events.append({'source': sid, 'rules': p['rules'], 'level': 'low',
                               'what': '처음 기록' + (('(처음부터 없는 감시 낱말: ' + ', '.join(missing0) + ')') if missing0 else '')})
            elif level in ('high', 'mid'):
                p['changedAt'] = now.isoformat(timespec='minutes'); p['changes'] = ch
                bits = []
                if ch.get('lost'): bits.append('감시 낱말이 사라짐: ' + ', '.join(ch['lost']))
                if ch.get('updated'): bits.append('수정일 ' + (ch['updated'][0] or '없음') + ' → ' + (ch['updated'][1] or '없음'))
                if ch.get('changed'): bits.append('바뀐 부분: ' + ' / '.join(ch['changed'][:6]))
                if ch.get('added'): bits.append('새 부분: ' + ' / '.join(ch['added'][:6]))
                if ch.get('removed'): bits.append('없어진 부분: ' + ' / '.join(ch['removed'][:6]))
                if ch.get('gained'): bits.append('새로 보인 감시 낱말: ' + ', '.join(ch['gained']))
                events.append({'source': sid, 'rules': p['rules'], 'level': level, 'what': ' · '.join(bits)})
            run['ok'] += 1
        elif st == 'robots':
            p['failStreak'] = 0; run['robots'] += 1
            if prevs.get(sid, {}).get('status') != 'robots':
                events.append({'source': sid, 'rules': p['rules'], 'level': 'low', 'what': 'robots.txt가 막아 열지 않음 → 직접 확인으로 다룸'})
        else:
            p['failStreak'] = p.get('failStreak', 0) + 1; run['failed'] += 1
            p['error'] = (data if isinstance(data, str) else st)[:160] if st == 'error' else st
            if p['failStreak'] == 2:
                events.append({'source': sid, 'rules': p['rules'], 'level': 'low', 'what': '두 번 연속 못 엶(' + str(p['error']) + ')'})
        out[sid] = p
    order = {'high': 0, 'mid': 1, 'low': 2}
    events.sort(key=lambda e: (order.get(e['level'], 3), e['source']))
    doc = {'checkedAt': now.isoformat(timespec='minutes'), 'registry': {'updated': reg.get('updated'), 'rules': len(reg['rules']), 'sources': len(reg['sources'])},
           'run': run, 'summary': {lv: sum(1 for e in events if e['level'] == lv) for lv in ('high', 'mid', 'low')},
           'events': events, 'sources': out}
    json.dump(doc, open(out_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)

    title = {r['id']: r['where'] + ' — ' + r['title'] for r in reg['rules']}
    L = ['# 규정 점검 ' + today.isoformat(), '',
         '규정 ' + str(len(reg['rules'])) + '개 · 원문 ' + str(len(reg['sources'])) + '곳 중 자동 감시 ' + str(run['auto']) + '곳, 이번에 ' + str(run['opened']) + '곳을 열었습니다 '
         '(읽음 ' + str(run['ok']) + ' · 본문 짧아 못 읽음 ' + str(run['thin']) + ' · robots로 안 엶 ' + str(run['robots']) + ' · 못 엶 ' + str(run['failed']) + ').', '',
         '높음 ' + str(doc['summary']['high']) + ' · 보통 ' + str(doc['summary']['mid']) + ' · 낮음 ' + str(doc['summary']['low']) +
         ' — 높음·보통은 마케팅 주간 예약(일요일 14:42)이 원문을 열어 우리 영향을 판단하고 운영실에 올립니다.', '']
    names = {'high': '높음: 우리가 기대는 사실이 바뀌었을 수 있음', 'mid': '보통: 내용이 바뀐 부분이 있음', 'low': '낮음: 처음 기록·읽지 못함'}
    for lv in ('high', 'mid', 'low'):
        rows = [e for e in events if e['level'] == lv]
        L.append('## ' + names[lv] + ' (' + str(len(rows)) + ')')
        for e in rows[:60]:
            s = out.get(e['source'], {})
            L.append('- ' + s.get('owner', '') + ' ' + s.get('name', '') + ' — ' + e['what'])
            L.append('  - 주소: ' + s.get('url', ''))
            for rid in e['rules'][:4]:
                L.append('  - 관련 규정 ' + rid + ': ' + title.get(rid, ''))
        L.append('')
    manual = [s for s in reg['sources'] if s['method'] in ('ai', 'manual')]
    L.append('## 자동으로 보지 않는 규정 (' + str(len(manual)) + ') — 마케팅 주간이 차례(확인일 + 주기)를 보고 확인')
    for s in manual:
        L.append('- [' + ('AI 확인' if s['method'] == 'ai' else '대표 확인') + ' · ' + {'w': '매주', 'm': '월 1회', 'q': '분기 1회', 'y': '해마다'}.get(s.get('cycle'), '') + '] ' + s['name'] + (' — ' + s['note'] if s.get('note') else ''))
    open(report_path, 'w', encoding='utf-8').write('\n'.join(L) + '\n')
    print(json.dumps(run, ensure_ascii=False), json.dumps(doc['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main(*sys.argv[1:4])
