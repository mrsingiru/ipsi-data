"""공식 공고 자동 읽기 — 영재원·학교·교육청 누리집의 모집 공고에서 '날짜가 든 줄'만 뽑아 둡니다.

왜: AI 입시 컨설턴트의 자료 점검 예약(Claude)은 웹 읽기 도구가 https만 열고, 첨부 PDF·HWP 내려받기가 막혀
    '자동 확인 불가'가 쌓였습니다(2026-10-19 기준 32건). GitHub Actions는 http 누리집·첨부 파일을 직접 받을 수 있어,
    여기서 날짜 줄을 뽑아 두면 자료 점검이 이 결과를 원문 근거로 씁니다(대표의 수동 확인을 줄이기 위해).

지키는 것
- robots.txt를 먼저 읽고, 막힌 주소는 열지 않습니다(403·401이면 막힌 것으로 봄). 우회 주소·캐시·미러를 쓰지 않습니다.
- 정직한 이름(User-Agent)으로, 사이트마다 1초 넘게 쉬며, 한 번 실행에 사이트당 최대 15번만 엽니다.
- 로그인·회원 전용 페이지는 열지 않습니다. 제목에 '명단'·'합격자'가 든 글·파일은 열지 않습니다(개인정보).
- 원문 전체를 저장하지 않습니다. 날짜가 든 짧은 줄(160자 안)과 주소만 남깁니다(사실만 옮김, 저작권).

쓰는 법: python scripts/fetch_official.py [watch/targets.json] [data/official]
결과: data/official/<id>.json (대상마다), data/official_run.json(요약)
"""
import datetime
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.robotparser
import zipfile
import zlib

import requests
from bs4 import BeautifulSoup

UA = 'ipsi-data-bot/1.0 (+https://github.com/mrsingiru/ipsi-data; public admission notice dates)'
TIMEOUT = 30
MAX_BYTES = 20 * 1024 * 1024
PER_HOST = 15
HOST_LIMIT = {'https://ged.kedi.re.kr': 45}   # the GED notice list is 25 pages
PAUSE = 1.2
DENY = ('명단', '합격자', '로그인', 'login')
FILE_EXT = ('.pdf', '.hwp', '.hwpx')
GED_LIST = 'https://ged.kedi.re.kr/slct/noti/slctNotiStat.do?pageIndex={}'
GED_PAGES = 25


def kst_now():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)


class Fetcher:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({'User-Agent': UA, 'Accept-Language': 'ko-KR,ko;q=0.9'})
        self.robots = {}
        self.count = {}
        self.last = {}
        self.debug = []

    def _host(self, url):
        p = urllib.parse.urlsplit(url)
        return p.scheme + '://' + p.netloc

    def allowed(self, url):
        host = self._host(url)
        if host not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(host + '/robots.txt', timeout=TIMEOUT)
                if r.status_code in (401, 403):
                    rp.disallow_all = True
                elif r.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None   # robots.txt could not be read: do not open the site this run
            self.robots[host] = rp
        rp = self.robots[host]
        return bool(rp) and rp.can_fetch(UA, url)

    def post(self, url, data, binary=False):
        """A form the site's own button sends (GED '다운로드' opens its file list this way). Same robots and pace rules."""
        if not self.allowed(url):
            return 'robots', None, url, ''
        host = self._host(url)
        if self.count.get(host, 0) >= HOST_LIMIT.get(host, PER_HOST):
            return 'limit', None, url, ''
        wait = PAUSE - (time.time() - self.last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self.count[host] = self.count.get(host, 0) + 1
        try:
            r = self.s.post(url, data=data, timeout=TIMEOUT)
            self.last[host] = time.time()
            if r.status_code >= 400:
                return 'http-%d' % r.status_code, None, r.url, ''
            if len(r.content) > MAX_BYTES:
                return 'too-big', None, r.url, ''
            if binary:
                return 'ok', r.content, r.url, r.headers.get('Content-Type', '')
            return 'ok', decode_html(r.content, r), r.url, r.headers.get('Content-Type', '')
        except requests.RequestException as e:
            return 'error', str(e)[:200], url, ''

    def get(self, url, binary=False):
        """Returns (status, text_or_bytes, final_url, content_type). status: ok | robots | limit | http-NNN | error"""
        if not self.allowed(url):
            return 'robots', None, url, ''
        host = self._host(url)
        if self.count.get(host, 0) >= HOST_LIMIT.get(host, PER_HOST):
            return 'limit', None, url, ''
        wait = PAUSE - (time.time() - self.last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self.count[host] = self.count.get(host, 0) + 1
        try:
            r = self.s.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True)
            self.last[host] = time.time()
            if r.status_code >= 400:
                return 'http-%d' % r.status_code, None, r.url, ''
            if not self.allowed(r.url):
                return 'robots', None, r.url, ''
            data = b''
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > MAX_BYTES:
                    return 'too-big', None, r.url, ''
            ctype = r.headers.get('Content-Type', '')
            if binary:
                return 'ok', data, r.url, ctype
            return 'ok', decode_html(data, r), r.url, ctype
        except requests.RequestException as e:
            return 'error', str(e)[:200], url, ''


def decode_html(data, r):
    m = re.search(rb'charset=["\']?([A-Za-z0-9_-]+)', data[:4000])
    for enc in ([m.group(1).decode('ascii', 'ignore')] if m else []) + [r.encoding or '', 'utf-8', 'cp949']:
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode('utf-8', 'replace')


# ---------- text from files ----------
def text_pdf(data):
    from pdfminer.high_level import extract_text
    return extract_text(io.BytesIO(data))


def text_hwp(data):
    """HWP 5 (OLE): BodyText/Section* streams, raw-deflate records, PARA_TEXT (tag 67) as UTF-16LE."""
    import olefile
    ole = olefile.OleFileIO(io.BytesIO(data))
    header = ole.openstream('FileHeader').read()
    compressed = bool(header[36] & 1)
    out = []
    secs = sorted([s for s in ole.listdir() if s[0] == 'BodyText'], key=lambda s: int(re.sub(r'\D', '', s[1]) or 0))
    for s in secs:
        raw = ole.openstream(s).read()
        if compressed:
            raw = zlib.decompress(raw, -15)
        i = 0
        while i + 4 <= len(raw):
            h = int.from_bytes(raw[i:i + 4], 'little')
            tag, size = h & 0x3FF, (h >> 20) & 0xFFF
            i += 4
            if size == 0xFFF:
                size = int.from_bytes(raw[i:i + 4], 'little')
                i += 4
            body = raw[i:i + size]
            i += size
            if tag == 67:
                out.append(hwp_para(body))
    return '\n'.join(out)


def hwp_para(b):
    chars, i = [], 0
    while i + 1 < len(b):
        c = int.from_bytes(b[i:i + 2], 'little')
        if c < 32:
            if c in (10, 13):
                chars.append('\n')
            elif c == 9:
                chars.append('\t')
            # extended / inline controls take 8 code units (16 bytes) in total
            i += 16 if c in (1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23) else 2
            continue
        chars.append(chr(c))
        i += 2
    # pairs of UTF-16 halves → one character; a lone half (broken file) is dropped so the JSON can be written
    return ''.join(chars).encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'ignore')


def text_hwpx(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    parts = sorted(n for n in z.namelist() if re.match(r'Contents/section\d+\.xml$', n))
    out = []
    for n in parts:
        x = z.read(n).decode('utf-8', 'replace')
        x = re.sub(r'</hp:p>', '\n', x)
        out.append(re.sub(r'<[^>]+>', '', x))
    return '\n'.join(out)


def file_text(data, name, ctype):
    kind, text = _file_text(data, name, ctype)
    return kind, text.encode('utf-8', 'ignore').decode('utf-8')


def _file_text(data, name, ctype):
    low = (name or '').lower()
    if data[:4] == b'%PDF' or low.endswith('.pdf') or 'pdf' in ctype:
        return 'pdf', text_pdf(data)
    if data[:8] == bytes.fromhex('d0cf11e0a1b11ae1') or low.endswith('.hwp'):
        return 'hwp', text_hwp(data)
    if data[:2] == b'PK' or low.endswith('.hwpx'):
        return 'hwpx', text_hwpx(data)
    return 'unknown', ''


# ---------- dates ----------
FULL = re.compile(r'(20\d\d)\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?')
MD_KO = re.compile(r'(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일(?:\s*\(\s*([월화수목금토일])\s*\))?')
MD_DOT = re.compile(r'(?<![\d.])(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*\(\s*([월화수목금토일])\s*\)')
WD = '월화수목금토일'


def pick_year(mo, d, wd, today, after=None):
    """No year written. 1) an earlier full date on the same line sets the year ('2026. 12. 28. ~ 1. 3.' → next year);
    2) the weekday in brackets decides between years within 270 days of today; 3) else the year nearest to today."""
    if after:
        try:
            c = datetime.date(after.year, mo, d)
            return after.year if c >= after else after.year + 1
        except ValueError:
            pass
    cands = []
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            cands.append(datetime.date(y, mo, d))
        except ValueError:
            pass
    if wd:
        ok = [c for c in cands if WD[c.weekday()] == wd and abs((c - today).days) <= 270]
        if len(ok) == 1:
            return ok[0].year
    if not cands:
        return today.year
    return min(cands, key=lambda c: abs((c - today).days)).year


def dates_in(line, today):
    found, spans = [], []
    for m in FULL.finditer(line):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        spans.append(m.span())
        try:
            datetime.date(y, mo, d)
            found.append((m.start(), y, mo, d, True))
        except ValueError:
            pass
    for rx in (MD_KO, MD_DOT):
        for m in rx.finditer(line):
            if any(a <= m.start() < b for a, b in spans):
                continue
            mo, d = int(m.group(1)), int(m.group(2))
            before = [x for x in found if x[0] < m.start() and x[4]]
            after = datetime.date(before[-1][1], before[-1][2], before[-1][3]) if before else None
            try:
                found.append((m.start(), pick_year(mo, d, m.group(3), today, after), mo, d, False))
            except ValueError:
                pass
    out = []
    for _, y, mo, d, _full in sorted(found):
        try:
            out.append(datetime.date(y, mo, d).isoformat())
        except ValueError:
            pass
    return out


def date_lines(text, near, today, src, limit=40):
    out, seen, prev = [], set(), []
    for raw in re.split(r'[\r\n]+', text):
        line = re.sub(r'\s+', ' ', raw).strip()
        if not line:
            continue
        ds = dates_in(line, today) if len(line) >= 5 else []
        if not ds:
            prev = (prev + [line])[-2:]
            continue
        orig = line
        hit = [w for w in near if w in line]
        if near and not hit:   # a table cell with only the dates: take the label from the cell(s) just before
            ctx = [p for p in prev if any(w in p for w in near) and len(p) <= 60 and not dates_in(p, today)]
            if ctx:
                line = ctx[-1] + ' | ' + line
                hit = [w for w in near if w in line]
        prev = (prev + [orig])[-2:]
        if near and not hit:
            continue
        short = line if len(line) <= 160 else line[:157] + '…'
        if short in seen:
            continue
        seen.add(short)
        out.append({'text': short, 'dates': ds, 'words': hit, 'src': src})
        if len(out) >= limit:
            break
    return out


# ---------- pages ----------
def soup_of(html):
    return BeautifulSoup(html, 'lxml')


def visible_text(soup):
    for t in soup(['script', 'style', 'noscript', 'head', 'title']):
        t.decompose()
    for br in soup.find_all(['br', 'p', 'div', 'li', 'tr', 'td', 'th', 'h1', 'h2', 'h3', 'h4']):
        br.append('\n')
    return soup.get_text()


def link_items(soup, base):
    """(text, absolute url) for every link, JS links of the common school CMS resolved to their article address."""
    out = []
    for a in soup.find_all('a'):
        t = re.sub(r'\s+', ' ', a.get_text(' ', strip=True))
        href = (a.get('href') or '').strip()
        on = a.get('onclick') or ''
        url = ''
        if href and not href.startswith(('javascript', '#', 'mailto:', 'tel:')):
            url = urllib.parse.urljoin(base.replace('post:', ''), href)
        else:
            m = re.search(r"['\"](\d{5,})['\"]", on + ' ' + href) or re.search(r"\(\s*(\d{5,})\s*[,)]", on + ' ' + href)
            if not m and re.fullmatch(r'\d{5,}', a.get('data-id') or ''):
                m = re.match(r'(\d+)', a.get('data-id'))
            gd = re.search(r"fn_down\('(FB_[A-Za-z0-9_.]+)'\s*,\s*'([^']*)'\)", on)
            if gd and 'ged.kedi.re.kr' in base:
                out.append((t, 'post:https://ged.kedi.re.kr/inno/fileDownload.do?' + urllib.parse.urlencode({'FilePath': 'dataFiles', 'PhysFileNm': gd.group(1), 'LogiFileNm': gd.group(2)})))
                continue
            fd = re.search(r'file_download\((\d+)', href + ' ' + on)
            if fd and '/bbs_shop/read.htm' in base:   # geig.ac.kr: the page's own download address
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(base).query))
                url = urllib.parse.urljoin(base, '/bbs_shop/file_download.php?' + urllib.parse.urlencode({'board_code': q.get('board_code', 'notice'), 'board_idx': q.get('idx', ''), 'sel_no': fd.group(1)}))
            elif re.search(r"fncDetailView\('(\d+)'\)", href) and 'list0010v.do' in base:
                url = 'post:' + base.replace('list0010v.do', 'view0010v.do').split('?')[0] + '?' + urllib.parse.urlencode({'board_seq': re.search(r"fncDetailView\('(\d+)'\)", href).group(1)})
            elif m and 'selectNttList.do' in base:
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(base).query))
                url = urllib.parse.urljoin(base, 'selectNttInfo.do?' + urllib.parse.urlencode({'mi': q.get('mi', ''), 'bbsId': q.get('bbsId', ''), 'nttSn': m.group(1)}))
        if url:
            out.append((t, url))
    return out


def anchor_dump(soup, words):
    """For the debug artifact only: links that look like what we were after, with their raw href/onclick."""
    out = []
    for a in soup.find_all('a'):
        t = re.sub(r'\s+', ' ', a.get_text(' ', strip=True))
        raw = (a.get('href') or '') + ' ' + (a.get('onclick') or '')
        if any(w in t or w in raw for w in words):
            out.append({'text': t[:60], 'href': (a.get('href') or '')[:160], 'onclick': (a.get('onclick') or '')[:120]})
        if len(out) >= 15:
            break
    return out


def script_dump(html, names):
    """For the debug artifact only: the page's own JS functions that open posts or files (fn_down, file_download …)."""
    out = []
    for nm in names:
        for m in re.finditer(r'function\s+' + re.escape(nm) + r'\s*\([^)]*\)\s*\{', html):
            out.append(html[m.start():m.start() + 900])
            break
    return out


def denied(text):
    return any(w in text for w in DENY)


def read_post(F, url, tgt, today, res, depth_files=True):
    if url.startswith('post:'):
        pu, _, qs = url[5:].partition('?')
        st, html, final, _ = F.post(pu, dict(urllib.parse.parse_qsl(qs)))
        final = pu + '?' + qs
    else:
        st, html, final, _ = F.get(url)
    res['opened'].append({'url': url, 'status': st})
    if st != 'ok':
        return
    read_post_html(F, html, final, tgt, today, res, depth_files)


def read_post_html(F, html, final, tgt, today, res, depth_files=True):
    soup = soup_of(html)
    title = re.sub(r'\s+', ' ', (soup.title.get_text() if soup.title else '')).strip()[:120]
    links = link_items(soup, final)
    res['lines'] += date_lines(visible_text(soup), tgt.get('near', []), today, final)
    if not depth_files:
        return
    n_files = len(res['files'])
    consent = re.search(r'function\s+file_download\s*\([^)]*\)\s*\{[^}]*file_download_popup\(', html or '')
    for t, u in links:
        if consent and 'file_download.php' in u:
            continue   # the site asks people to agree before downloading: do not skip that step automatically
        low = (t + ' ' + u).lower()
        is_file = any(e in low for e in FILE_EXT) or re.search(r'(download|filedown|fileDown|atchFile|file_down|attach)', u, re.I)
        if not is_file or denied(t):
            continue
        if not any(w in t for w in ('모집', '요강', '선발', '공고', '안내', '일정', '계획')) and not any(e in t.lower() for e in FILE_EXT):
            continue
        if u.startswith('post:'):   # a download the site's own form sends (GED)
            pu, _, qs = u[5:].partition('?')
            st2, data, f2, ctype = F.post(pu, dict(urllib.parse.parse_qsl(qs)), binary=True)
        else:
            st2, data, f2, ctype = F.get(u, binary=True)
        rec = {'url': u if not u.startswith('post:') else u[5:].partition('?')[0] + '?' + 'file=' + urllib.parse.quote(t[:60]), 'name': t[:80], 'status': st2}
        if st2 == 'ok':
            try:
                kind, text = file_text(data, t, ctype)
                rec.update({'type': kind, 'bytes': len(data), 'sha1': hashlib.sha1(data).hexdigest()[:12]})
                got = date_lines(text, tgt.get('near', []), today, rec['url'] if not u.startswith('post:') else (res.get('srcPage') or rec['url']))
                res['lines'] += got
                if not got:
                    F.debug.append({'id': tgt['id'], 'what': 'file-no-lines', 'url': u, 'name': t[:80], 'type': kind, 'chars': len(text), 'sample': re.sub(r'\s+', ' ', text)[:300]})
            except Exception as e:  # a broken file should not stop the others
                rec.update({'status': 'parse-error', 'error': str(e)[:120]})
        res['files'].append(rec)
        if len(res['files']) >= 6:
            break
    if len(res['files']) == n_files:
        F.debug.append({'id': tgt['id'], 'what': 'post-no-files', 'url': final, 'anchors': anchor_dump(soup_of(html), ['첨부', '다운', 'down', 'Down', 'hwp', 'pdf', 'file', 'File']),
                        'scripts': script_dump(html, ['fn_fileDownload', 'file_download', 'fn_download', 'fn_fileDown', 'download', 'fnDown', 'fileDown']),
                        'html': re.sub(r'\s+', ' ', html)[:1500] if 'ged.kedi.re.kr' in final else ''})
    if title:
        res['titles'].append({'url': final, 'title': title})


def pick_notices(links, words):
    out = []
    for t, u in links:
        if not t or denied(t):
            continue
        if all(w in t for w in words) and any(w in t for w in ('모집', '선발', '요강', '공고', '전형', '입학')):
            out.append((t, u))
    seen, uniq = set(), []
    for t, u in out:
        if u not in seen:
            seen.add(u)
            uniq.append((t, u))
    return uniq[:3]


def read_board(F, url, tgt, today, res, from_site=False):
    st, html, final, _ = F.get(url)
    res['opened'].append({'url': url, 'status': st})
    if st != 'ok':
        return False
    soup = soup_of(html)
    links = link_items(soup, final)
    picks = pick_notices(links, tgt.get('title', []))
    if not picks:
        F.debug.append({'id': tgt['id'], 'what': 'board-no-pick', 'url': final, 'anchors': anchor_dump(soup, (tgt.get('title') or []) + ['모집', '선발', '공지']),
                        'raw2027': [str(a)[:400] for a in soup.find_all('a') if '2027' in a.get_text()][:3],
                        'scripts': script_dump(html, ['fncDetailView', 'fn_view', 'fnView', 'goView', 'fn_egov_inqire_notice', 'view'])})
    if not picks and from_site:   # front page: follow the notice board link once
        boards = [(t, u) for t, u in links if any(w in t for w in ('공지사항', '공지', '모집안내', '입학안내', '알림'))][:2]
        for t, u in boards:
            st2, h2, f2, _ = F.get(u)
            res['opened'].append({'url': u, 'status': st2})
            if st2 == 'ok':
                picks = pick_notices(link_items(soup_of(h2), f2), tgt.get('title', []))
                if picks:
                    break
    for t, u in picks:
        res['notices'].append({'title': t[:120], 'url': u})
        read_post(F, u, tgt, today, res)
    return bool(picks)


GED_CACHE = {}


def ged_rows(F):
    """Every row of the GED notice list (title text + raw row html kept only for the debug artifact)."""
    if 'rows' in GED_CACHE:
        return GED_CACHE['rows']
    rows = []
    for p in range(1, GED_PAGES + 1):
        st, html, final, _ = F.get(GED_LIST.format(p))
        if st != 'ok':
            GED_CACHE['status'] = st
            break
        if p == 1:
            F.debug.append({'id': 'ged', 'what': 'ged-script', 'scripts': script_dump(html, ['fn_down', 'fn_search', 'fn_link_page']),
                            'forms': [str(fm)[:600] for fm in soup_of(html).find_all('form')][:3]})
        soup = soup_of(html)
        trs = soup.select('table tbody tr') or soup.find_all('tr')
        for tr in trs:
            t = re.sub(r'\s+', ' ', tr.get_text(' ', strip=True))
            if t:
                rows.append({'text': t[:200], 'page': p, 'links': link_items(tr, final), 'html': str(tr)[:1200]})
        if not trs:
            break
    GED_CACHE['rows'] = rows
    return rows


def read_ged(F, find, tgt, today, res):
    rows = ged_rows(F)
    res['opened'].append({'url': GED_LIST.format('1..%d' % GED_PAGES), 'status': GED_CACHE.get('status', 'ok'), 'rows': len(rows)})
    hits = [r for r in rows if all(w in r['text'] for w in find) and all(w in r['text'] for w in tgt.get('title', []))]
    if not GED_CACHE.get('sampled'):
        GED_CACHE['sampled'] = True
        F.debug.append({'id': 'ged', 'what': 'ged-sample', 'rows': [r['html'] for r in rows[:2]], 'n': len(rows)})
    if not hits:
        F.debug.append({'id': tgt['id'], 'what': 'ged-no-hit', 'find': find, 'near': [r['text'] for r in rows if any(w in r['text'] for w in find)][:5]})
    for r in hits[:3]:
        res['ged'].append({'row': r['text'], 'page': r['page'], 'url': GED_LIST.format(r['page'])})
        F.debug.append({'id': tgt['id'], 'what': 'ged-hit', 'find': find, 'html': r['html']})
        res['lines'] += date_lines(r['text'], [], today, GED_LIST.format(r['page']))
        m = re.search(r"fn_down\('(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'\)", r['html'])
        if m:
            pop = 'https://ged.kedi.re.kr/inno/slctNotiInnoPop.do?pop1=Y'
            st, html, final, _ = F.post(pop, {'stndYear': m.group(1), 'orgId': m.group(2), 'seq': m.group(3)})
            res['opened'].append({'url': pop + '&orgId=' + m.group(2) + '&seq=' + m.group(3), 'status': st})
            if st == 'ok':
                res['srcPage'] = GED_LIST.format(r['page'])
                read_post_html(F, html, GED_LIST.format(r['page']), tgt, today, res)
    return bool(hits)


def run(targets_path, out_dir):
    cfg = json.load(open(targets_path, encoding='utf-8'))
    today = kst_now().date()
    os.makedirs(out_dir, exist_ok=True)
    F = Fetcher()
    summary = []
    for tgt in cfg['targets']:
        res = {'id': tgt['id'], 'org': tgt['org'], 'what': tgt.get('what', ''), 'checkedAt': kst_now().strftime('%Y-%m-%dT%H:%M:%S+09:00'),
               'opened': [], 'notices': [], 'ged': [], 'titles': [], 'files': [], 'lines': []}
        for pg in tgt['pages']:
            k = pg['kind']
            try:
                if k == 'post':
                    read_post(F, pg['url'], tgt, today, res)
                elif k in ('board', 'site'):
                    read_board(F, pg['url'], tgt, today, res, from_site=(k == 'site'))
                elif k == 'ged':
                    read_ged(F, pg['find'], tgt, today, res)
            except Exception as e:
                res['opened'].append({'url': pg.get('url', 'ged'), 'status': 'error', 'error': str(e)[:160]})
        sts = [o['status'] for o in res['opened']]
        if res['lines']:
            res['status'] = 'dates'
        elif res['notices'] or res['ged'] or res['titles']:
            res['status'] = 'found-no-dates'
        elif sts and all(s == 'robots' for s in sts):
            res['status'] = 'robots'
        elif any(s == 'ok' for s in sts):
            res['status'] = 'not-found'
        else:
            res['status'] = 'error'
        res['lines'] = res['lines'][:60]
        json.dump(res, open(os.path.join(out_dir, tgt['id'] + '.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        summary.append({'id': tgt['id'], 'org': tgt['org'], 'status': res['status'], 'lines': len(res['lines']), 'files': len(res['files']),
                        'opened': len(res['opened'])})
        print(tgt['id'], tgt['org'], res['status'], 'lines', len(res['lines']), 'files', len(res['files']), sts)
    run_info = {'at': kst_now().strftime('%Y-%m-%dT%H:%M:%S+09:00'), 'targets': len(summary),
                'withDates': sum(1 for s in summary if s['status'] == 'dates'), 'items': summary}
    json.dump(run_info, open(os.path.join(os.path.dirname(out_dir.rstrip('/')) or '.', 'official_run.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    os.makedirs('debug', exist_ok=True)
    json.dump(F.debug, open('debug/debug.json', 'w', encoding='utf-8', errors='replace'), ensure_ascii=False, indent=1)
    return run_info


if __name__ == '__main__':
    tp = sys.argv[1] if len(sys.argv) > 1 else 'watch/targets.json'
    od = sys.argv[2] if len(sys.argv) > 2 else 'data/official'
    info = run(tp, od)
    print('날짜 줄을 찾은 대상 %d / %d' % (info['withDates'], info['targets']))
