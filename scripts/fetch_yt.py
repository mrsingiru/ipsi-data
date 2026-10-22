"""유튜브 주제 레이더 — 교육 채널의 최근 30일 영상 제목·게시일·조회수를 모은다(2026-10-23).

    python scripts/fetch_yt.py watch/yt_channels.json data/yt/latest.json data/yt/channels.json

목적: 부모들이 지금 궁금해하는 주제를 아는 '신호'. 사실의 근거로 쓰지 않는다(질문 은행 예약이 공식 원문으로 확인한다).
읽는 방법(둘 중 되는 것):
  1) YouTube Data API v3 — 저장소 Secrets에 YOUTUBE_KEY가 있으면. 공식 방법이고 조회수가 정확하다(하루 할당 10,000 중 약 50 사용).
  2) 채널 RSS(https://www.youtube.com/feeds/videos.xml) — 열쇠가 없을 때. robots.txt가 막으면 열지 않는다(우회하지 않음).
남기는 것: 채널 이름·영상 id·제목(120자 안)·게시일·조회수·하루 평균 조회수. 설명·자막·댓글·썸네일은 받지 않는다.
최근 30일만 남긴다(YouTube API 이용 정책: API 자료는 30일 안에 새로 고침). 공개 페이지에는 쓰지 않는다(운영실·예약 작업만 읽음).
"""
import datetime, json, os, re, sys, time
import xml.etree.ElementTree as ET
import requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_official as fo

CFG, OUT, CACHE = sys.argv[1], sys.argv[2], sys.argv[3]
KEY = os.environ.get('YOUTUBE_KEY', '').strip()
API = 'https://www.googleapis.com/youtube/v3/'
DAYS = 30
NOW = datetime.datetime.now(datetime.timezone.utc)
KST = datetime.timezone(datetime.timedelta(hours=9))
cfg = json.load(open(CFG, encoding='utf-8'))
cache = json.load(open(CACHE, encoding='utf-8')) if os.path.exists(CACHE) else {}
fo.PER_HOST = 40
F = fo.Fetcher()
api_units = 0


def api(path, **params):
    global api_units
    params['key'] = KEY
    r = requests.get(API + path, params=params, timeout=20)
    api_units += 100 if path == 'search' else 1
    if r.status_code != 200:
        raise RuntimeError('api %s %d %s' % (path, r.status_code, r.text[:160]))
    return r.json()


def short(t, n=120):
    t = re.sub(r'\s+', ' ', t or '').strip()
    return t if len(t) <= n else t[:n - 1] + '…'


def ok_title(title, expect):
    return bool(title) and expect.lower() in title.lower()


# ---------- channel id ----------
def resolve_api(c):
    for h in c.get('handles') or []:
        try:
            d = api('channels', part='id,snippet,contentDetails', forHandle=h)
        except RuntimeError:
            continue
        for it in d.get('items') or []:
            if ok_title(it['snippet']['title'], c['expect']):
                return it['id'], it['snippet']['title'], it['contentDetails']['relatedPlaylists']['uploads'], 'handle'
    if c.get('q'):
        d = api('search', part='snippet', type='channel', q=c['q'], maxResults=5, regionCode='KR', relevanceLanguage='ko')
        for it in d.get('items') or []:
            if ok_title(it['snippet']['title'], c['expect']):
                cid = it['snippet']['channelId']
                d2 = api('channels', part='id,snippet,contentDetails', id=cid)
                for x in d2.get('items') or []:
                    return x['id'], x['snippet']['title'], x['contentDetails']['relatedPlaylists']['uploads'], 'search'
    return None


def resolve_page(c):
    """열쇠 없이: 채널 페이지(@주소)를 robots를 지켜 한 번 열어 채널 id와 이름을 읽는다."""
    last = 'not-found'
    for h in c.get('handles') or []:
        url = 'https://www.youtube.com/' + requests.utils.quote(h, safe='@')
        st, html, final, _ = F.get(url)
        if st != 'ok':
            last = st
            continue
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html) or re.search(r'"(?:externalId|channelId)":"(UC[\w-]{22})"', html)
        t = re.search(r'<meta property="og:title" content="([^"]*)"', html)
        title = t.group(1) if t else ''
        if m and ok_title(title, c['expect']):
            return (m.group(1), title, 'UU' + m.group(1)[2:], 'page'), None
        last = 'not-found'
    return None, last


# ---------- recent videos ----------
def videos_api(uploads):
    d = api('playlistItems', part='snippet,contentDetails', playlistId=uploads, maxResults=25)
    items = []
    for it in d.get('items') or []:
        vid = it['contentDetails']['videoId']
        at = it['contentDetails'].get('videoPublishedAt') or it['snippet'].get('publishedAt')
        items.append({'vid': vid, 'title': short(it['snippet'].get('title')), 'at': at})
    ids = [x['vid'] for x in items]
    views = {}
    for i in range(0, len(ids), 50):
        d2 = api('videos', part='statistics', id=','.join(ids[i:i + 50]))
        for v in d2.get('items') or []:
            views[v['id']] = int(v.get('statistics', {}).get('viewCount', 0) or 0)
    for x in items:
        x['views'] = views.get(x['vid'])
    return items, 'ok'


def videos_rss(cid):
    url = 'https://www.youtube.com/feeds/videos.xml?channel_id=' + cid
    st, xml, final, _ = F.get(url)
    if st != 'ok':
        return [], st
    ns = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015', 'm': 'http://search.yahoo.com/mrss/'}
    try:
        root = ET.fromstring(xml.encode('utf-8') if isinstance(xml, str) else xml)
    except ET.ParseError:
        return [], 'error'
    items = []
    for e in root.findall('a:entry', ns):
        vid = e.findtext('yt:videoId', '', ns)
        stt = e.find('m:group/m:community/m:statistics', ns)
        items.append({'vid': vid, 'title': short(e.findtext('a:title', '', ns)), 'at': e.findtext('a:published', '', ns),
                      'views': int(stt.get('views')) if stt is not None and stt.get('views', '').isdigit() else None})
    return items, 'ok'


def when(s):
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None


chans, vids = [], []
mode = 'api' if KEY else 'rss'
for c in cfg['channels']:
    row = {'key': c['key'], 'label': c['label'], 'kind': c['kind'], 'status': '', 'n': 0}
    try:
        got = None
        cc = cache.get(c['key'])
        if cc and cc.get('expect') == c['expect'] and cc.get('id'):
            got = (cc['id'], cc['title'], cc['uploads'], cc.get('how', 'cache'))
        elif KEY:
            got = resolve_api(c)
            if not got:
                row['status'] = 'not-found'
        else:
            got, why = resolve_page(c)
            if not got:
                row['status'] = why or 'not-found'
        if got:
            cache[c['key']] = {'id': got[0], 'title': got[1], 'uploads': got[2], 'how': got[3], 'expect': c['expect'], 'at': NOW.strftime('%Y-%m-%dT%H:%M:%SZ')}
            row.update({'channelId': got[0], 'title': got[1]})
            items, st = videos_api(got[2]) if KEY else videos_rss(got[0])
            row['status'] = st
            for x in items:
                t = when(x['at'])
                if not t or (NOW - t).days > DAYS:
                    continue
                age = max(1.0, (NOW - t).total_seconds() / 86400)
                x.update({'ch': c['key'], 'at': t.astimezone(KST).strftime('%Y-%m-%d'), 'url': 'https://www.youtube.com/watch?v=' + x['vid'],
                          'perDay': round(x['views'] / age) if x.get('views') is not None else None})
                vids.append(x)
                row['n'] += 1
    except Exception as e:   # one channel failing must not stop the rest
        row['status'] = 'error'
        row['err'] = str(e)[:160]
    chans.append(row)
    time.sleep(0.5)

vids.sort(key=lambda x: -(x.get('perDay') or 0))
okc = sum(1 for r in chans if r['status'] == 'ok')
out = {'checkedAt': NOW.strftime('%Y-%m-%dT%H:%M:%SZ'), 'checkedKst': NOW.astimezone(KST).strftime('%Y-%m-%d %H:%M'), 'mode': mode,
       'days': DAYS, 'apiUnits': api_units if KEY else 0, 'channels': chans, 'videos': vids[:300],
       'summary': {'channels': len(chans), 'ok': okc, 'videos': len(vids),
                   'blocked': [r['label'] for r in chans if r['status'] == 'robots'], 'notFound': [r['label'] for r in chans if r['status'] == 'not-found'],
                   'failed': [r['label'] for r in chans if r['status'] not in ('ok', 'robots', 'not-found')]},
       'needKey': (not KEY) and okc == 0,
       'rules': '제목·게시일·조회수만(30일). 주제 신호일 뿐 근거가 아님. 공개 페이지에 채널·사람 이름을 쓰지 않음. 영상 문장을 옮기지 않음.'}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
json.dump(cache, open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print(json.dumps(out['summary'], ensure_ascii=False), 'mode', mode, 'units', api_units)
