"""공식 공고 자동 읽기 — 영재원·학교·교육청 누리집의 모집 공고에서 '날짜가 든 줄'만 뽑아 둡니다.

왜: 입시노트의 자료 점검 예약(Claude)은 웹 읽기 도구가 https만 열고, 첨부 PDF·HWP 내려받기가 막혀
    '자동 확인 불가'가 쌓였습니다(2026-10-19 기준 32건). GitHub Actions는 http 누리집·첨부 파일을 직접 받을 수 있어,
    여기서 날짜 줄을 뽑아 두면 자료 점검이 이 결과를 원문 근거로 씁니다(대표의 수동 확인을 줄이기 위해).

지키는 것
- robots.txt를 먼저 읽고, 막힌 주소는 열지 않습니다(403·401이면 막힌 것으로 봄). 우회 주소·캐시·미러를 쓰지 않습니다.
- 정직한 이름(User-Agent)으로, 사이트마다 1초 넘게 쉬며, 한 번 실행에 사이트당 최대 15번만 엽니다.
- 로그인·회원 전용 페이지는 열지 않습니다. 제목에 '명단'·'합격자'가 든 글·파일은 열지 않습니다(개인정보).
- 원문 전체를 저장하지 않습니다. 날짜가 든 짧은 줄(160자 안)과 주소만 남깁니다(사실만 옮김, 저작권).
- (2026-10-22) 일정을 그림으로만 올린 공고·스캔 PDF는 글자 인식(tesseract)으로 읽고, 그 줄에는 via: 'ocr'을 붙입니다.
  글자 인식은 틀릴 수 있어, 페이지에 이미 있는 날짜와 같은 줄만 lines(확인 근거)에 넣고, 다른 날짜는 ocr_lines에만 남깁니다.

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
FILE_EXT = ('.pdf', '.hwp', '.hwpx', '.jpg', '.jpeg', '.png')
OCR_BUDGET = 40          # 2026-10-22: pictures read per run (글자 인식은 느려서 한 번 실행에 그림 40장까지)
OCR_PDF_PAGES = 4        # a scanned PDF: first pages only (모집 일정은 보통 앞쪽에 있음)
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
        self.ocr_left = OCR_BUDGET

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

    def get(self, url, binary=False, referer=None):
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
        # 2026-10-25: 경기 미래과학교육원 첨부(n15)가 'Connection broken: IncompleteRead'로 내려받다 끊김 — 끊긴 받기만 쉬었다가 2번 더(같은 주소, robots·속도 규칙 그대로)
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True, headers={'Referer': referer} if referer else None)
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
            except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError) as e:
                self.last[host] = time.time()
                if attempt < 2:
                    time.sleep(PAUSE * 3 * (attempt + 1))
                    continue
                return 'error', ('3번 받다 끊김: ' + str(e))[:200], url, ''
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


def text_pdf_layout(data):
    """Second reader for PDFs whose tables come out scrambled: poppler's pdftotext keeps each table row on one line."""
    import shutil
    import subprocess
    import tempfile
    if not shutil.which('pdftotext'):
        return ''
    with tempfile.NamedTemporaryFile(suffix='.pdf') as tf:
        tf.write(data)
        tf.flush()
        r = subprocess.run(['pdftotext', '-layout', '-enc', 'UTF-8', tf.name, '-'], capture_output=True, timeout=60)
        return r.stdout.decode('utf-8', 'ignore')


def ocr_ready():
    import shutil
    return bool(shutil.which('tesseract'))


def text_ocr_image(data):
    """글자 인식(2026-10-22): 모집 일정을 그림으로만 올린 공고·첨부. 한국어+영어, 표의 한 줄이 한 줄로 나오게(psm 6)."""
    import subprocess
    import tempfile
    if not ocr_ready():
        return ''
    with tempfile.NamedTemporaryFile(suffix='.img') as tf:
        tf.write(data)
        tf.flush()
        r = subprocess.run(['tesseract', tf.name, '-', '-l', os.environ.get('OCR_LANG', 'kor+eng'), '--psm', '6'], capture_output=True, timeout=120)
        return r.stdout.decode('utf-8', 'ignore')


def text_pdf_ocr(data, F=None):
    """A scanned PDF (no text layer): first pages as pictures → 글자 인식."""
    import glob
    import shutil
    import subprocess
    import tempfile
    if not (ocr_ready() and shutil.which('pdftoppm')):
        return ''
    out = []
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'f.pdf')
        with open(p, 'wb') as fh:
            fh.write(data)
        subprocess.run(['pdftoppm', '-r', '200', '-l', str(OCR_PDF_PAGES), '-png', p, os.path.join(d, 'pg')], capture_output=True, timeout=180)
        for f in sorted(glob.glob(os.path.join(d, 'pg*.png'))):
            if F is not None:
                if F.ocr_left <= 0:
                    break
                F.ocr_left -= 1
            with open(f, 'rb') as fh:
                out.append(text_ocr_image(fh.read()))
    return '\n'.join(out)


def is_image(data):
    return data[:3] == b'\xff\xd8\xff' or data[:8] == b'\x89PNG\r\n\x1a\n'


def text_hwp(data):
    """HWP 5 (OLE): BodyText/Section* streams, raw-deflate records, PARA_TEXT (tag 67) as UTF-16LE."""
    import olefile
    ole = olefile.OleFileIO(io.BytesIO(data))
    header = ole.openstream('FileHeader').read()
    compressed = bool(header[36] & 1)
    if header[36] & 4:   # 2026-10-25: 배포용 문서 — 본문(ViewText)이 암호화돼 BodyText가 없음. 빈 글로 넘기지 않고 까닭을 남김(사람이 열어 보거나 PDF 첨부를 찾음)
        raise ValueError('배포용 HWP(본문 암호화)라 자동으로 못 읽음')
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
    if is_image(data):
        return 'image', ''   # read by 글자 인식 in read_post_html (budget-limited)
    return 'unknown', ''


# ---------- dates ----------
FULL = re.compile(r'(20\d\d)\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?')
MD_KO = re.compile(r'(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일(?:\s*\(\s*([월화수목금토일])\s*\))?')
MD_DOT = re.compile(r'(?<![\d.])(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*\(\s*([월화수목금토일])\s*\)')
WD = '월화수목금토일'


def cycle_window(text):
    """2026-10-25: 해가 안 적힌 날짜를 문서의 학년도로 맞춘다. 'N학년도' 선발·전형은 N-1년 3월 ~ N년 2월에 열린다.
    (전북대 '2026학년도 … 신입생 모집'(2025-10-23 글)의 '10월 27일'을 오늘에 가까운 2026년으로 읽던 오류 — 작년 날짜가 올해 공식 날짜처럼 보일 수 있었음)
    뽑는 해를 가리키는 말('N학년도 … 신입생·대상자·입학·모집·선발·전형')에 붙은 학년도만 센다 — 영재원 요강은 지원 자격을
    '2026학년도 초3 재학생'처럼 지금 학년도로 적어서 그냥 세면 틀림(경인교대 2027 요강). 가장 많이 나온 것(같으면 큰 것), 없으면 None(예전처럼 오늘에 가까운 해)."""
    cnt = {}
    for m in re.finditer(r'(20\d\d)\s*학년도[^\n]{0,30}?(신입생|대상자|입학|모집|선발|전형)', text or ''):
        n = int(m.group(1)); cnt[n] = cnt.get(n, 0) + 1
    if not cnt:
        return None
    n = max(cnt, key=lambda k: (cnt[k], k))
    end = datetime.date(n, 3, 1) - datetime.timedelta(days=1)
    return (datetime.date(n - 1, 3, 1), end)


def pick_year(mo, d, wd, today, after=None, window=None):
    """No year written. 1) an earlier full date on the same line sets the year ('2026. 12. 28. ~ 1. 3.' → next year);
    2) the weekday in brackets, if exactly one candidate (within 270 days of today or inside the 학년도 window) has it;
    3) the document's 학년도 window (cycle_window); 4) else the year nearest to today."""
    if after:
        try:
            c = datetime.date(after.year, mo, d)
            return after.year if c >= after else after.year + 1
        except ValueError:
            pass
    cands = []
    years = {today.year - 1, today.year, today.year + 1}
    if window:
        years |= {window[0].year, window[1].year}
    for y in sorted(years):
        try:
            cands.append(datetime.date(y, mo, d))
        except ValueError:
            pass
    if wd:   # 요일이 적혀 있으면 요일이 가장 강한 근거(학년도보다 먼저) — 학년도 창 안이거나 오늘에서 270일 안
        ok = [c for c in cands if WD[c.weekday()] == wd and (abs((c - today).days) <= 270 or (window and window[0] <= c <= window[1]))]
        if len(ok) == 1:
            return ok[0].year
    if window:
        inw = [c for c in cands if window[0] <= c <= window[1]]
        if len(inw) == 1:
            return inw[0].year
    if not cands:
        return today.year
    return min(cands, key=lambda c: abs((c - today).days)).year


RANGE_END = re.compile(r'~\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?(?!\d)')


def tidy(line):
    """PDF text often moves the weekday out of its brackets: '2026.9.16.( ) 수' → '2026.9.16.(수)'."""
    return re.sub(r'\(\s*\)\s*([월화수목금토일])(?![가-힣])', r'(\1)', line)


def dates_in(line, today, window=None):
    line = tidy(line)
    found, spans = [], []
    for m in FULL.finditer(line):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        spans.append(m.span())
        try:
            datetime.date(y, mo, d)
            found.append((m.start(), y, mo, d, True))
        except ValueError:
            pass
    for m in RANGE_END.finditer(line):   # '2026. 10. 16. ~ 10. 25.' (end of a range, no weekday)
        if any(a <= m.start(1) < b for a, b in spans):
            continue
        before = [x for x in found if x[0] < m.start() and x[4]]
        if before:
            mo, d = int(m.group(1)), int(m.group(2))
            try:
                found.append((m.start(1), pick_year(mo, d, None, today, datetime.date(before[-1][1], before[-1][2], before[-1][3]), window), mo, d, False))
                spans.append(m.span())
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
                found.append((m.start(), pick_year(mo, d, m.group(3), today, after, window), mo, d, False))
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
    win = cycle_window(text)
    for raw in re.split(r'[\r\n]+', text):
        line = re.sub(r'\s+', ' ', raw).strip()
        if not line:
            continue
        ds = dates_in(line, today, win) if len(line) >= 5 else []
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
        if href and not href.lower().startswith(('javascript', '#', 'mailto:', 'tel:')):   # 2026-10-24: 'javaScript:'도 링크가 아님(인천 학교 누리집)
            url = urllib.parse.urljoin(base.replace('post:', ''), href)
        else:
            m = re.search(r"['\"](\d{5,})['\"]", on + ' ' + href) or re.search(r"\(\s*(\d{5,})\s*[,)]", on + ' ' + href)
            if not m and re.fullmatch(r'\d{5,}', a.get('data-id') or ''):
                m = re.match(r'(\d+)', a.get('data-id'))
            fk = re.search(r"fn_fileDownload\('([0-9a-f]{16,})'\)", on + ' ' + href)
            if fk and '/na/ntt/' in base:
                out.append((t, urllib.parse.urljoin(base, '/common/nttFileDownload.do?fileKey=' + fk.group(1))))
                continue
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
            elif '/boardCnts/list.do' in base and re.search(r'\d{6,9}', on + ' ' + href):
                # 2026-10-24: 강원 등 boardCnts 목록은 글 주소를 스크립트로 연다 — 숫자 중 boardID가 아닌 것을 글 번호(boardSeq)로 본다
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(base).query))
                seqs = [x for x in re.findall(r'(\d{6,9})', on + ' ' + href) if x != q.get('boardID', '')]
                if seqs:
                    url = urllib.parse.urljoin(base, 'view.do?' + urllib.parse.urlencode({'boardID': q.get('boardID', ''), 'boardSeq': seqs[0], 'lev': '0', 'm': q.get('m', ''), 's': q.get('s', '')}))
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
        st, data, final, ctype = F.get(url, binary=True)
        if st == 'ok' and looks_file(data, ctype):   # 2026-10-24: 'post'가 첨부 파일 주소(down.php·downpost 등)를 바로 가리킬 때
            res['opened'].append({'url': url, 'status': st})
            read_direct_file(F, data, ctype, url, tgt, today, res)
            return
        html = decode_html(data, _NoEnc()) if st == 'ok' else data
    res['opened'].append({'url': url, 'status': st})
    if st != 'ok':
        return
    read_post_html(F, html, final, tgt, today, res, depth_files)


class _NoEnc:
    encoding = ''


def looks_file(data, ctype):
    if not data:
        return False
    head = data[:8]
    if head[:4] == b'%PDF' or head == bytes.fromhex('d0cf11e0a1b11ae1') or (head[:2] == b'PK' and b'<html' not in data[:200].lower()):
        return True
    c = (ctype or '').lower()
    return bool(c) and not any(w in c for w in ('html', 'text', 'xml', 'json'))


def read_direct_file(F, data, ctype, url, tgt, today, res):
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1])[:80] or '첨부'
    rec = {'url': url, 'name': name, 'status': 'ok', 'direct': True}
    try:
        kind, text = file_text(data, name, ctype)
        rec.update({'type': kind, 'bytes': len(data), 'sha1': hashlib.sha1(data).hexdigest()[:12]})
        got = date_lines(text, tgt.get('near', []), today, url)
        if not got and kind == 'pdf':
            got = date_lines(text_pdf_layout(data), tgt.get('near', []), today, url)
            if got:
                rec['reader'] = 'pdftotext'
        if not got and F.ocr_left > 0 and ((kind == 'pdf' and len(text.strip()) < 200) or kind == 'image'):
            if kind == 'image':
                F.ocr_left -= 1
                text = text_ocr_image(data)
            else:
                text = text_pdf_ocr(data, F)
            got = ocr_route(date_lines(text, tgt.get('near', []), today, url), tgt, res)
            rec['reader'] = 'ocr'
        res['lines'] += got
        rec['lines'] = len(got)
    except Exception as e:
        rec.update({'status': 'parse-error', 'error': str(e)[:120]})
    res['files'].append(rec)
    res['titles'].append({'url': url, 'title': '첨부: ' + name})


def ocr_route(lines, tgt, res):
    """글자 인식 줄은 틀릴 수 있어 '확인'에만 씁니다(2026-10-22).
    페이지에 이미 있는 날짜(tgt['expect'], build_targets가 넣음)와 시작·끝이 같은 줄만 lines(자료 점검이 근거로 씀)에 넣고,
    다른 날짜·새 날짜는 ocr_lines에만 남깁니다 — 자료 점검은 이것만으로 날짜를 바꾸자고 제안하지 않습니다."""
    keep = []
    for x in lines:
        x['via'] = 'ocr'
        if any(s in x['dates'] and e in x['dates'] for s, e in tgt.get('expect', [])):
            keep.append(x)
        else:
            res.setdefault('ocr_lines', []).append(x)
    return keep


SKIP_IMG = re.compile(r'(icon|logo|btn|bullet|common|layout|banner|blank|spacer|arrow|quick|footer|header|gnb|sns|img_?bg|/images?/(?:sub|main)/)', re.I)


def body_images(soup, base):
    """Pictures inside the notice body (공고 내용을 그림 한 장으로 올린 경우). Site decoration is skipped."""
    out = []
    for im in soup.find_all('img'):
        src = (im.get('src') or im.get('data-src') or '').strip()
        if not src or src.startswith('data:'):
            continue
        u = urllib.parse.urljoin(base.replace('post:', ''), src)
        if SKIP_IMG.search(u) or denied(im.get('alt') or '') or denied(urllib.parse.unquote(u)):
            continue
        if not re.search(r'\.(jpe?g|png)(\?|$)', u, re.I) and not re.search(r'(download|file|atch|upload|editor|board)', u, re.I):
            continue
        if u not in out:
            out.append(u)
    return out[:3]


def read_post_html(F, html, final, tgt, today, res, depth_files=True):
    soup = soup_of(html)
    title = re.sub(r'\s+', ' ', (soup.title.get_text() if soup.title else '')).strip()[:120]
    links = link_items(soup, final)
    imgs = body_images(soup, final)
    n_lines = len(res['lines'])
    res['lines'] += date_lines(visible_text(soup), tgt.get('near', []), today, final)
    if not depth_files:
        return
    n_files = len(res['files'])
    consent = re.search(r'function\s+file_download\s*\([^)]*\)\s*\{[^}]*file_download_popup\(', html or '')
    for t, u in links:
        if consent and 'file_download.php' in u:
            continue   # the site asks people to agree before downloading: do not skip that step automatically
        low = (t + ' ' + u).lower()
        is_file = any(e in low for e in FILE_EXT) or re.search(r'(download|filedown|fileDown|atchFile|file_down|attach|down\.php|downpost)', u, re.I)   # 2026-10-24: down.php·downpost(학교 게시판 첨부)
        if not is_file or denied(t):
            continue
        FW = ('모집', '요강', '선발', '공고', '안내', '일정', '계획', '전형', '입학')   # 2026-10-24: '입학전형 다운로드'(하나고)처럼 '전형·입학'만 있는 첨부도
        uu = urllib.parse.unquote(u)
        if not any(w in t for w in FW) and not any(e in t.lower() for e in FILE_EXT) and not (any(w in uu for w in FW) and any(e in uu.lower() for e in FILE_EXT)):
            continue
        if u.startswith('post:'):   # a download the site's own form sends (GED)
            pu, _, qs = u[5:].partition('?')
            st2, data, f2, ctype = F.post(pu, dict(urllib.parse.parse_qsl(qs)), binary=True)
        else:
            st2, data, f2, ctype = F.get(u, binary=True, referer=final)   # 2026-10-24: 일부 교육청 첨부는 글 주소(Referer)가 있어야 열림
        rec = {'url': u if not u.startswith('post:') else u[5:].partition('?')[0] + '?' + 'file=' + urllib.parse.quote(t[:60]), 'name': t[:80], 'status': st2}
        if st2 == 'error' and isinstance(data, str):   # 2026-10-25: 내려받기 오류의 까닭을 남김(경기 미래과학교육원 첨부 3개가 '오류'만 남아 원인을 몰랐던 일)
            rec['error'] = data[:160]
        if st2 == 'ok':
            try:
                kind, text = file_text(data, t, ctype)
                rec.update({'type': kind, 'bytes': len(data), 'sha1': hashlib.sha1(data).hexdigest()[:12]})
                src_url = rec['url'] if not u.startswith('post:') else (res.get('srcPage') or rec['url'])
                got = date_lines(text, tgt.get('near', []), today, src_url)
                if not got and kind == 'pdf':
                    got = date_lines(text_pdf_layout(data), tgt.get('near', []), today, src_url)
                    if got:
                        rec['reader'] = 'pdftotext'
                if not got and F.ocr_left > 0 and ((kind == 'pdf' and len(text.strip()) < 200) or kind == 'image'):
                    if kind == 'image':
                        F.ocr_left -= 1
                        text = text_ocr_image(data)
                    else:
                        text = text_pdf_ocr(data, F)
                    got = ocr_route(date_lines(text, tgt.get('near', []), today, src_url), tgt, res)
                    rec['reader'] = 'ocr'
                res['lines'] += got
                if not got:
                    flat = re.sub(r'\s+', ' ', text)
                    near_at = [flat[max(0, m.start() - 60):m.start() + 120] for m in re.finditer('|'.join(map(re.escape, tgt.get('near', [])[:3])) or 'x^', flat)][:3]
                    F.debug.append({'id': tgt['id'], 'what': 'file-no-lines', 'url': u[:120], 'name': t[:80], 'type': kind, 'chars': len(text), 'sample': flat[:200], 'near': near_at})
            except Exception as e:  # a broken file should not stop the others
                rec.update({'status': 'parse-error', 'error': str(e)[:120]})
        res['files'].append(rec)
        if len(res['files']) >= 6:
            break
    # 2026-10-22: nothing with dates in the text or files → the schedule may be a picture in the body (글자 인식, 최대 2장)
    if len(res['lines']) == n_lines and imgs and ocr_ready():
        for u in imgs[:2]:
            if F.ocr_left <= 0:
                break
            st3, data, f3, ctype = F.get(u, binary=True)
            rec = {'url': u, 'name': '본문 그림', 'status': st3, 'reader': 'ocr'}
            if st3 == 'ok' and is_image(data) and len(data) >= 30 * 1024:
                F.ocr_left -= 1
                try:
                    got = ocr_route(date_lines(text_ocr_image(data), tgt.get('near', []), today, final), tgt, res)
                    rec.update({'type': 'image', 'bytes': len(data), 'sha1': hashlib.sha1(data).hexdigest()[:12], 'lines': len(got)})
                    res['lines'] += got
                except Exception as e:
                    rec.update({'status': 'parse-error', 'error': str(e)[:120]})
                res['files'].append(rec)
    if len(res['files']) == n_files:
        F.debug.append({'id': tgt['id'], 'what': 'post-no-files', 'url': final, 'anchors': anchor_dump(soup_of(html), ['첨부', '다운', 'down', 'Down', 'hwp', 'pdf', 'file', 'File']),
                        'scripts': script_dump(html, ['fn_fileDownload', 'file_download', 'fn_download', 'fn_fileDown', 'download', 'fnDown', 'fileDown']),
                        'html': re.sub(r'\s+', ' ', html)[:1500] if 'ged.kedi.re.kr' in final else ''})
    if title:
        res['titles'].append({'url': final, 'title': title})


# 2026-10-31 (대표 22:30 '광양도 고쳐줘' 추천안 승인): 학교·재단 게시판에 같이 올라오는 교직원 채용 글은 입시 공고가 아님 — 포스코교육재단 '교사 임용후보자 선정경쟁시험'
#  공고의 면접·발표 날짜 15줄을 광양제철고 입시 날짜로 읽었음(1/18~19 최종면접 등). 운영실 '공고 찾는 곳'(ipsi-site tools/k1_help.py)과 같은 뜻으로 거름.
STAFF = re.compile(r'임용|채용|선정경쟁시험|기간제|계약제|시간강사|교원\s*(모집|선발|공개)|직원\s*(모집|선발|공개)|강사\s*(모집|선발)|교사\s*(모집|선발|공개전형)')


def pick_notices(links, words):
    out = []
    for t, u in links:
        if not t or denied(t) or STAFF.search(t):
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
        boards = [(t, u) for t, u in links if any(w in t for w in ('공지사항', '공지', '모집안내', '입학안내', '입학공지', '입학', '알림'))][:3]   # 2026-10-24: 과학고 누리집의 '입학' 메뉴
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


def load_targets(targets_path):
    """watch/targets.json(손으로 적은 것) + watch/targets_auto.json(페이지 게시 때 ipsi-site가 만든 것, 2026-10-22).
    같은 id면 손으로 적은 것을 쓴다. ged 설정은 손으로 적은 파일만 따른다."""
    cfg = json.load(open(targets_path, encoding='utf-8'))
    auto_path = os.path.join(os.path.dirname(targets_path) or '.', 'targets_auto.json')
    if os.path.exists(auto_path):
        try:
            auto = json.load(open(auto_path, encoding='utf-8'))
            have = {t['id'] for t in cfg['targets']}
            ax = {t['id']: t for t in auto.get('targets', []) if t.get('id')}
            for t in cfg['targets']:   # 2026-10-24: 손으로 적은 대상도 페이지의 예상 날짜(expect)를 이어받음(글자 인식 확인·달력 대조용)
                if 'expect' not in t and t['id'] in ax and ax[t['id']].get('expect'):
                    t['expect'] = ax[t['id']]['expect']
            extra = [t for t in auto.get('targets', []) if t.get('id') and t['id'] not in have and t.get('pages')]
            cfg['targets'] = cfg['targets'] + extra
            print('자동 대상 %d곳 더함(%s)' % (len(extra), auto.get('made', '')))
        except Exception as e:
            print('자동 대상 파일을 읽지 못함:', e)
    # 2026-10-28 대표 아침 회의 B2 '공식 경로 3단계': watch/routes.json(공식 경로 대장)의 1·2·3순위 경로를 대상에 붙인다.
    #  대장에만 있는 기관(공식 근거가 기사뿐이던 곳 — B1)은 새 대상으로 더한다. 2·3순위도 공식 기관만, GED는 넣지 않는다.
    rpath = os.path.join(os.path.dirname(targets_path) or '.', 'routes.json')
    if os.path.exists(rpath):
        try:
            R = json.load(open(rpath, encoding='utf-8')).get('orgs', {})
            by = {t['id']: t for t in cfg['targets']}
            added = 0
            for oid, o in R.items():
                # 대장의 robots 칸은 조사 때의 간접 판정이라 거르지 않는다 — 여기서 여는 모든 주소는 Fetcher가 robots.txt를 직접 보고(정직한 이름 UA) 막히면 열지 않는다.
                rs = [r for r in o.get('routes', []) if r.get('url') and 'ged.kedi' not in r.get('url', '') and not r.get('login') and r.get('kind') in ('board', 'post', 'site')]
                if not rs: continue
                routes = {}
                for r in sorted(rs, key=lambda x: x.get('rank', 9)):
                    routes.setdefault(int(r.get('rank', 9)), []).append({'kind': r['kind'], 'url': r['url']})
                t = by.get(oid)
                if t is None:
                    t = {'id': oid, 'org': o.get('org', oid), 'what': o.get('what', '공식 공고(경로 대장)'), 'pages': [], 'title': o.get('title', ['2027']),
                         'near': o.get('near', ['원서', '접수', '지원', '모집', '선발', '전형', '면접', '발표', '합격'])}
                    cfg['targets'].append(t); by[oid] = t; added += 1
                t['routes'] = [{'rank': k, 'pages': v} for k, v in sorted(routes.items())]
            print('경로 대장 %d곳 · 새 대상 %d곳' % (len(R), added))
        except Exception as e:
            print('경로 대장을 읽지 못함:', e)
    return cfg


def run(targets_path, out_dir):
    cfg = load_targets(targets_path)
    today = kst_now().date()
    os.makedirs(out_dir, exist_ok=True)
    F = Fetcher()
    summary = []
    for tgt in cfg['targets']:
        res = read_target(F, cfg, tgt, today, out_dir)
        res['lines'] = res['lines'][:60]
        if res.get('ocr_lines'):
            res['ocr_lines'] = res['ocr_lines'][:30]
        json.dump(res, open(os.path.join(out_dir, tgt['id'] + '.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        summary.append({'id': tgt['id'], 'org': tgt['org'], 'status': res['status'], 'lines': len(res['lines']), 'files': len(res['files']),
                        'opened': len(res['opened']), 'route': res.get('route'), 'tried': res.get('tried')})
        print(tgt['id'], tgt['org'], res['status'], 'route', res.get('route'), 'lines', len(res['lines']), 'files', len(res['files']))
    run_info = {'at': kst_now().strftime('%Y-%m-%dT%H:%M:%S+09:00'), 'targets': len(summary),
                'withDates': sum(1 for s in summary if s['status'] == 'dates'), 'blockedAll': [s['id'] for s in summary if s['status'] == 'blocked-all'], 'items': summary}
    json.dump(run_info, open(os.path.join(os.path.dirname(out_dir.rstrip('/')) or '.', 'official_run.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    os.makedirs('debug', exist_ok=True)
    json.dump(F.debug, open('debug/debug.json', 'w', encoding='utf-8', errors='replace'), ensure_ascii=False, indent=1)
    return run_info


def read_pages(F, cfg, tgt, pages, today):
    res = {'id': tgt['id'], 'org': tgt['org'], 'what': tgt.get('what', ''), 'checkedAt': kst_now().strftime('%Y-%m-%dT%H:%M:%S+09:00'),
           'opened': [], 'notices': [], 'ged': [], 'titles': [], 'files': [], 'lines': []}
    for pg in pages:
            k = pg['kind']
            try:
                if k == 'post':
                    read_post(F, pg['url'], tgt, today, res)
                elif k in ('board', 'site'):
                    read_board(F, pg['url'], tgt, today, res, from_site=(k == 'site'))
                elif k == 'ged':
                    # 2026-10-21: KEDI(GED) 정책이 '서면 동의 없는 DB 구축·영리 이용 금지, 영리 사이트는 링크도 사전 허락'이라
                    # 법 확인(KEDI 서면 답)을 받을 때까지 자동으로 읽지 않는다. watch/targets.json의 "ged": true로 다시 켠다.
                    if cfg.get('ged') is True:
                        read_ged(F, pg['find'], tgt, today, res)
                    else:
                        res['opened'].append({'url': 'ged', 'status': 'paused', 'error': 'GED 자동 읽기 멈춤(이용 조건 확인 중)'})
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
    return res


GOOD = ('dates', 'found-no-dates')


def read_target(F, cfg, tgt, today, out_dir):
    """2026-10-28 B2: 경로가 1·2·3순위로 있으면 지난번에 찾은 순위부터 읽고, 막히거나(robots·오류) 공고를 못 찾으면 다음 순위로.
    공고를 찾으면 거기서 멈추고 route(몇 순위)를 남긴다. 모두 막히면 status 'blocked-all'(자료 점검이 '대표 주말 직접 확인'으로 올림)."""
    routes = tgt.get('routes') or []
    if not routes:
        r = read_pages(F, cfg, tgt, tgt['pages'], today); r['route'] = 1 if tgt['pages'] else None; return r
    if tgt.get('pages'):   # 손으로 적은 pages는 1순위에 합침(앞에)
        routes = [{'rank': 1, 'pages': list(tgt['pages']) + (routes[0]['pages'] if routes[0]['rank'] == 1 else [])}] + [x for x in routes if x['rank'] != 1]
    last = None
    try:
        prev = json.load(open(os.path.join(out_dir, tgt['id'] + '.json'), encoding='utf-8'))
        if prev.get('status') in GOOD: last = prev.get('route')
    except Exception:
        pass
    order = sorted(routes, key=lambda x: (0 if x['rank'] == last else 1, x['rank']))
    tried, best = [], None
    for rt in order:
        r = read_pages(F, cfg, tgt, rt['pages'], today)
        tried.append({'rank': rt['rank'], 'status': r['status']}); r['route'] = rt['rank']
        if r['status'] in GOOD:
            best = r; break
        if best is None or (best['status'] in ('robots', 'error') and r['status'] == 'not-found'):
            best = r
    if all(x['status'] in ('robots', 'error') for x in tried):
        best['status'] = 'blocked-all'
    best['tried'] = tried
    return best


if __name__ == '__main__':
    tp = sys.argv[1] if len(sys.argv) > 1 else 'watch/targets.json'
    od = sys.argv[2] if len(sys.argv) > 2 else 'data/official'
    info = run(tp, od)
    print('날짜 줄을 찾은 대상 %d / %d' % (info['withDates'], info['targets']))
