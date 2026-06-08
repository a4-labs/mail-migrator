#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migrator poczty IMAP -> IMAP, wersja SERWEROWA (Koyeb) z web GUI.

- Dziala bez przerwy w tle (osobny watek), bez klikania.
- Przenosi INBOX + Wyslane (konfigurowalne nizej).
- Logika 1:1 z wersja desktopowa: dedup po Message-ID, przenoszenie
  statusu przeczytania, mapowanie folderow na dpoczte, dekodowanie
  IMAP UTF-7 (polskie nazwy), odpornosc na zerwane polaczenia.
- PETLA: gdy trafi limit Gmaila / zerwanie, czeka RETRY_HOURS i probuje
  dalej. Konczy, gdy caly material przeszedl (przebieg bez nowych maili).
- Web GUI: wejdz na adres aplikacji, zobaczysz status, log i postep.

Konfiguracja przez zmienne srodowiskowe (Koyeb -> Environment) lub .env:
  SRC_HOST, SRC_PORT, DST_HOST, DST_PORT
  MAILBOX_1_LABEL, MAILBOX_1_SRC_USER, MAILBOX_1_SRC_PASS,
  MAILBOX_1_DST_USER, MAILBOX_1_DST_PASS   (obsluga wielu: _2, _3, ...)
Opcjonalnie:
  FOLDERS=INBOX,[Gmail]/Wyslane   (domyslnie wlasnie te dwa)
  WORKERS=2
  RETRY_HOURS=6
  AUTOSTART=1   (1 = migracja rusza od razu po starcie serwera)
"""

import os
import re
import time
import base64
import imaplib
import email
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string

imaplib._MAXLINE = 10_000_000
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


# ===================== KONFIGURACJA =====================

def load_env_file(path):
    data = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip().strip('"').strip("'")
    return data


# zmienne srodowiskowe maja pierwszenstwo, .env jako fallback (lokalnie)
_ENVFILE = load_env_file(ENV_PATH)


def cfg(key, default=""):
    return os.environ.get(key, _ENVFILE.get(key, default))


def parse_mailboxes():
    src_host = cfg("SRC_HOST", "imap.gmail.com")
    src_port = cfg("SRC_PORT", "993")
    dst_host = cfg("DST_HOST", "imap.dpoczta.pl")
    dst_port = cfg("DST_PORT", "993")
    boxes = []
    i = 1
    while True:
        p = f"MAILBOX_{i}_"
        if not cfg(f"{p}SRC_USER"):
            break
        boxes.append({
            "label": cfg(f"{p}LABEL", f"Skrzynka {i}"),
            "src_host": src_host, "src_port": src_port,
            "src_user": cfg(f"{p}SRC_USER"), "src_pass": cfg(f"{p}SRC_PASS"),
            "dst_host": dst_host, "dst_port": dst_port,
            "dst_user": cfg(f"{p}DST_USER"), "dst_pass": cfg(f"{p}DST_PASS"),
        })
        i += 1
    return boxes


FOLDERS = [f.strip() for f in cfg("FOLDERS", "INBOX,[Gmail]/Wyslane").split(",") if f.strip()]
WORKERS = int(cfg("WORKERS", "2"))
RETRY_HOURS = float(cfg("RETRY_HOURS", "6"))
AUTOSTART = cfg("AUTOSTART", "1") == "1"


# ===================== IMAP: pomocnicze =====================

def imap_utf7_decode(s):
    if '&' not in s:
        return s
    res, i = [], 0
    while i < len(s):
        c = s[i]
        if c == '&':
            end = s.find('-', i)
            if end == -1:
                res.append(c); i += 1; continue
            chunk = s[i + 1:end]
            if chunk == '':
                res.append('&')
            else:
                b64 = chunk.replace(',', '/')
                pad = '=' * (-len(b64) % 4)
                try:
                    res.append(base64.b64decode(b64 + pad).decode('utf-16-be'))
                except Exception:
                    res.append(s[i:end + 1])
            i = end + 1
        else:
            res.append(c); i += 1
    return ''.join(res)


def connect(host, port, user, password):
    m = imaplib.IMAP4_SSL(host, int(port))
    m.login(user, password)
    return m


def target_name(src_folder):
    decoded = imap_utf7_decode(src_folder)
    low = decoded.lower()
    mapping = {
        'inbox': 'INBOX',
        '[gmail]/sent mail': 'Sent',
        '[gmail]/wyslane': 'Sent',
        '[gmail]/wysłane': 'Sent',
        '[gmail]/drafts': 'Drafts',
        '[gmail]/wersje robocze': 'Drafts',
        '[gmail]/trash': 'Trash',
        '[gmail]/kosz': 'Trash',
        '[gmail]/spam': 'SPAM',
        '[gmail]/all mail': 'Archives',
        '[gmail]/cala poczta': 'Archives',
        '[gmail]/cała poczta': 'Archives',
    }
    return mapping.get(low, decoded.replace('[Gmail]/', '').replace('/', '.'))


def norm_msgid(value):
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    return value.strip().strip('<>').strip().lower()


# ===================== STAN (dla web GUI) =====================

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "oczekuje"      # oczekuje / pracuje / czeka_limit / zakonczono / blad
        self.phase = ""
        self.current_folder = ""
        self.folder_done = 0
        self.folder_total = 0
        self.copied_total = 0
        self.skipped_total = 0
        self.seen_marked_total = 0
        self.cycle = 0
        self.next_retry_ts = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.finished = False
        self.log_lines = []

    def log(self, msg):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self.lock:
            self.log_lines.append(line)
            if len(self.log_lines) > 500:
                self.log_lines = self.log_lines[-500:]
        print(line, flush=True)

    def snapshot(self):
        with self.lock:
            return {
                "status": self.status, "phase": self.phase,
                "current_folder": self.current_folder,
                "folder_done": self.folder_done,
                "folder_total": self.folder_total,
                "copied_total": self.copied_total,
                "skipped_total": self.skipped_total,
                "seen_marked_total": self.seen_marked_total,
                "cycle": self.cycle,
                "next_retry_ts": self.next_retry_ts,
                "started_at": self.started_at,
                "finished": self.finished,
                "folders": FOLDERS, "workers": WORKERS,
                "retry_hours": RETRY_HOURS,
                "log": self.log_lines[-200:],
            }


STATE = State()
_started = threading.Event()


# ===================== LOGIKA MIGRACJI =====================

def is_limit_error(exc):
    """Czy wyjatek wyglada na limit/zerwanie Gmaila."""
    s = str(exc).lower()
    keys = ['eof', 'limit', 'too many', 'try again', 'unavailable',
            'bandwidth', 'connection reset', 'timed out', 'broken pipe',
            'socket error']
    return any(k in s for k in keys)


def existing_index(dst, target, st):
    """(existing set, unseen_map) z folderu docelowego - dedup + naprawa statusu."""
    existing, unseen = set(), {}
    try:
        typ, _ = dst.select(f'"{target}"')
        if typ != 'OK':
            return existing, unseen
        typ, data = dst.search(None, 'ALL')
        if typ != 'OK' or not data or not data[0]:
            return existing, unseen
        nums = data[0].split()
        if not nums:
            return existing, unseen
        seq = b','.join(nums).decode()
        typ, fetched = dst.fetch(seq, '(FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
        if typ != 'OK' or not fetched:
            return existing, unseen
        cur_num, cur_seen = None, False
        for part in fetched:
            if isinstance(part, tuple):
                meta = part[0].decode('utf-8', 'replace')
                head = meta.strip().split(' ', 1)[0]
                cur_num = head if head.isdigit() else None
                cur_seen = '\\Seen' in meta
                if part[1]:
                    line = part[1].decode('utf-8', 'replace')
                    if ':' in line:
                        n = norm_msgid(line.split(':', 1)[1])
                        if n:
                            existing.add(n)
                            if not cur_seen and cur_num:
                                unseen[n] = cur_num
    except Exception as e:
        st.log(f"   (indeks duplikatow: {e})")
    return existing, unseen


def mark_seen(dst_cfg, target, msgids, st):
    if not msgids:
        return 0
    wanted = set(msgids)
    marked = 0
    dst = None
    try:
        dst = connect(**dst_cfg)
        typ, _ = dst.select(f'"{target}"')
        if typ != 'OK':
            return 0
        typ, data = dst.search(None, 'ALL')
        if typ != 'OK' or not data or not data[0]:
            return 0
        nums = data[0].split()
        seq = b','.join(nums).decode()
        typ, fetched = dst.fetch(seq, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
        if typ != 'OK' or not fetched:
            return 0
        to_mark, cur_num = [], None
        for part in fetched:
            if isinstance(part, tuple):
                meta = part[0].decode('utf-8', 'replace')
                head = meta.strip().split(' ', 1)[0]
                cur_num = head if head.isdigit() else None
                if part[1] and cur_num:
                    line = part[1].decode('utf-8', 'replace')
                    if ':' in line:
                        n = norm_msgid(line.split(':', 1)[1])
                        if n and n in wanted:
                            to_mark.append(cur_num)
        for j in range(0, len(to_mark), 200):
            batch = ','.join(to_mark[j:j + 200])
            dst.store(batch, '+FLAGS', '(\\Seen)')
            marked += len(to_mark[j:j + 200])
    except Exception as e:
        st.log(f"   [seen] blad: {e}")
    finally:
        if dst:
            try:
                dst.logout()
            except Exception:
                pass
    return marked


class LimitHit(Exception):
    pass


def worker(wid, src_cfg, dst_cfg, folder, target, job_ids,
           existing, ex_lock, counters, c_lock, unseen, seen_msgids, s_lock, st):
    try:
        src = connect(**src_cfg)
        dst = connect(**dst_cfg)
        src.select(f'"{folder}"', readonly=True)
    except Exception as e:
        st.log(f"   [watek {wid}] polaczenie: {e}")
        if is_limit_error(e):
            with c_lock:
                counters['limit'] = True
        return
    try:
        for mid in job_ids:
            with c_lock:
                if counters.get('limit'):
                    break
            try:
                flags, internaldate = '', None
                tf, fd = src.fetch(mid, '(FLAGS INTERNALDATE)')
                if tf == 'OK' and fd:
                    for fp in fd:
                        blob = fp if isinstance(fp, bytes) else (fp[0] if isinstance(fp, tuple) else None)
                        if not blob:
                            continue
                        try:
                            fl = imaplib.ParseFlags(blob)
                            flags = ' '.join(x.decode() if isinstance(x, bytes) else x
                                             for x in fl
                                             if (x.decode() if isinstance(x, bytes) else x) != '\\Recent')
                        except Exception:
                            pass
                        try:
                            internaldate = imaplib.Internaldate2tuple(blob)
                        except Exception:
                            pass

                typ, md = src.fetch(mid, '(RFC822)')
                if typ != 'OK' or not md:
                    continue
                raw = None
                for part in md:
                    if isinstance(part, tuple) and part[1]:
                        raw = part[1]; break
                if raw is None:
                    continue

                try:
                    parsed = email.message_from_bytes(raw)
                    msgid = norm_msgid(parsed.get('Message-ID'))
                except Exception:
                    msgid = None

                was_seen = '\\Seen' in flags

                if msgid:
                    with ex_lock:
                        already = msgid in existing
                        if not already:
                            existing.add(msgid)
                    if already:
                        if was_seen:
                            num = None
                            with ex_lock:
                                num = unseen.pop(msgid, None)
                            if num:
                                try:
                                    dst.select(f'"{target}"')
                                    dst.store(num, '+FLAGS', '(\\Seen)')
                                except Exception:
                                    pass
                        with c_lock:
                            counters['skipped'] += 1
                        with st.lock:
                            st.skipped_total += 1
                        continue

                ta, _ = dst.append(f'"{target}"',
                                   f'({flags})' if flags else None,
                                   internaldate, raw)
                if was_seen and ta == 'OK' and msgid:
                    with s_lock:
                        seen_msgids.append(msgid)
                with c_lock:
                    counters['done'] += 1
                with st.lock:
                    st.copied_total += 1
                    st.folder_done += 1
            except Exception as e:
                if is_limit_error(e):
                    st.log(f"   [watek {wid}] LIMIT/zerwanie: {e}")
                    with c_lock:
                        counters['limit'] = True
                    break
                st.log(f"   [watek {wid}] blad maila: {e}")
                continue
    finally:
        for c in (src, dst):
            try:
                c.logout()
            except Exception:
                pass


def migrate_once(box, st):
    """Jeden przebieg po wszystkich folderach. Zwraca:
       'done'  - przeszlo wszystko, brak nowych,
       'limit' - trafiono limit, trzeba czekac,
       'progress' - cos przeszlo, warto isc dalej od razu."""
    src_cfg = {"host": box["src_host"], "port": box["src_port"],
               "user": box["src_user"], "password": box["src_pass"]}
    dst_cfg = {"host": box["dst_host"], "port": box["dst_port"],
               "user": box["dst_user"], "password": box["dst_pass"]}

    try:
        src = connect(**src_cfg)
        dst = connect(**dst_cfg)
    except Exception as e:
        st.log(f"Polaczenie nieudane: {e}")
        return 'limit' if is_limit_error(e) else 'error'

    any_new = False
    hit_limit = False
    try:
        for folder in FOLDERS:
            target = target_name(folder)
            with st.lock:
                st.current_folder = f"{folder} -> {target}"
                st.folder_done = 0
            st.log(f"=== Folder '{folder}' -> '{target}' ===")

            opened = False
            for attempt in range(2):
                try:
                    typ, _ = src.select(f'"{folder}"', readonly=True)
                    if typ == 'OK':
                        opened = True; break
                except Exception as e:
                    st.log(f"   otwarcie: {e}")
                    if attempt == 0:
                        try: src.logout()
                        except Exception: pass
                        try: dst.logout()
                        except Exception: pass
                        try:
                            src = connect(**src_cfg); dst = connect(**dst_cfg)
                        except Exception as e2:
                            st.log(f"   wznowienie polaczen: {e2}")
                            if is_limit_error(e2):
                                return 'limit'
                            break
            if not opened:
                st.log(f"   pomijam '{folder}'")
                continue

            existing, unseen = existing_index(dst, target, st)
            st.log(f"   na dpoczcie juz: {len(existing)} (do naprawy statusu: {len(unseen)})")
            src.select(f'"{folder}"', readonly=True)

            typ, data = src.search(None, 'ALL')
            if typ != 'OK' or not data or not data[0]:
                st.log("   folder zrodlowy pusty"); continue
            ids = data[0].split()
            with st.lock:
                st.folder_total = len(ids)
            st.log(f"   maili w zrodle: {len(ids)}")

            n = max(1, min(WORKERS, len(ids)))
            buckets = [[] for _ in range(n)]
            for idx, mid in enumerate(ids):
                buckets[idx % n].append(mid)

            ex_lock = threading.Lock(); c_lock = threading.Lock()
            s_lock = threading.Lock(); seen_msgids = []
            counters = {'done': 0, 'skipped': 0, 'limit': False}

            threads = []
            for wid in range(n):
                if not buckets[wid]:
                    continue
                t = threading.Thread(target=worker, args=(
                    wid + 1, src_cfg, dst_cfg, folder, target, buckets[wid],
                    existing, ex_lock, counters, c_lock, unseen,
                    seen_msgids, s_lock, st), daemon=True)
                threads.append(t); t.start()
            for t in threads:
                t.join()

            if counters['done'] > 0:
                any_new = True
            st.log(f"   skopiowano {counters['done']}, pominieto {counters['skipped']}")

            if seen_msgids:
                m = mark_seen(dst_cfg, target, seen_msgids, st)
                with st.lock:
                    st.seen_marked_total += m
                st.log(f"   oznaczono przeczytane: {m}")

            if counters['limit']:
                hit_limit = True
                st.log("   >>> trafiono limit/zerwanie - przerywam przebieg")
                break
    finally:
        for c in (src, dst):
            try:
                c.logout()
            except Exception:
                pass

    if hit_limit:
        return 'limit'
    if any_new:
        return 'progress'
    return 'done'


def run_loop():
    boxes = parse_mailboxes()
    if not boxes:
        STATE.status = "blad"
        STATE.log("BRAK KONFIGURACJI: ustaw MAILBOX_1_* w zmiennych srodowiskowych.")
        return
    box = boxes[0]  # wersja serwerowa: jedna skrzynka (pierwsza)
    STATE.log(f"Start migracji: {box['label']} ({box['src_user']} -> {box['dst_user']})")
    STATE.log(f"Foldery: {', '.join(FOLDERS)} | watki: {WORKERS} | retry: {RETRY_HOURS}h")

    while True:
        with STATE.lock:
            STATE.cycle += 1
            STATE.status = "pracuje"
            STATE.next_retry_ts = None
        STATE.log(f"--- Przebieg #{STATE.cycle} ---")
        try:
            result = migrate_once(box, STATE)
        except Exception as e:
            STATE.log(f"Blad przebiegu: {e}")
            result = 'limit' if is_limit_error(e) else 'error'

        if result == 'done':
            with STATE.lock:
                STATE.status = "zakonczono"
                STATE.finished = True
            STATE.log(">>> GOTOWE. Caly material przeniesiony, brak nowych maili.")
            STATE.log(">>> Mozesz przepiac MX na dpoczte. Serwer mozna wylaczyc.")
            return
        elif result == 'error':
            wait = RETRY_HOURS * 3600
            nxt = time.time() + wait
            with STATE.lock:
                STATE.status = "czeka_limit"
                STATE.next_retry_ts = nxt
            STATE.log(f"Blad. Ponawiam za {RETRY_HOURS}h.")
            time.sleep(wait)
        elif result == 'limit':
            wait = RETRY_HOURS * 3600
            nxt = time.time() + wait
            with STATE.lock:
                STATE.status = "czeka_limit"
                STATE.next_retry_ts = nxt
            STATE.log(f"Limit Gmaila. Czekam {RETRY_HOURS}h i probuje dalej.")
            time.sleep(wait)
        else:  # progress - idz dalej od razu, krotka przerwa
            STATE.log("Przebieg dograł nowe maile. Kontynuuje po 60s.")
            time.sleep(60)


def ensure_started():
    if not _started.is_set():
        _started.set()
        threading.Thread(target=run_loop, daemon=True).start()


# ===================== WEB GUI =====================

app = Flask(__name__)

PAGE = """<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Migrator poczty</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--bd:#30363d;--tx:#e6edf3;
--mut:#8b949e;--acc:#2f81f7;--ok:#3fb950;--warn:#d29922;--err:#f85149;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);
font-family:'SF Mono',ui-monospace,'Cascadia Code',Consolas,monospace;
padding:24px;line-height:1.5}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:18px;font-weight:600;margin-bottom:2px;letter-spacing:.3px}
.sub{color:var(--mut);font-size:12px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:10px;margin-bottom:18px}
.card{background:var(--panel);border:1px solid var(--bd);border-radius:8px;
padding:12px 14px}
.card .k{color:var(--mut);font-size:11px;text-transform:uppercase;
letter-spacing:.5px;margin-bottom:4px}
.card .v{font-size:20px;font-weight:600}
.badge{display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;
font-weight:600}
.b-pracuje{background:rgba(47,129,247,.15);color:var(--acc)}
.b-czeka_limit{background:rgba(210,153,34,.15);color:var(--warn)}
.b-zakonczono{background:rgba(63,185,80,.15);color:var(--ok)}
.b-blad{background:rgba(248,81,73,.15);color:var(--err)}
.b-oczekuje{background:rgba(139,148,158,.15);color:var(--mut)}
.bar{height:6px;background:#21262d;border-radius:4px;overflow:hidden;margin-top:10px}
.bar>i{display:block;height:100%;background:var(--acc);width:0;
transition:width .4s ease}
.log{background:#010409;border:1px solid var(--bd);border-radius:8px;
padding:14px;height:380px;overflow-y:auto;font-size:12px;white-space:pre-wrap;
word-break:break-word}
.log div{padding:1px 0;color:#c9d1d9}
.row{display:flex;justify-content:space-between;align-items:center;
margin-bottom:14px;flex-wrap:wrap;gap:8px}
.muted{color:var(--mut);font-size:12px}
.retry{color:var(--warn);font-size:13px;margin-top:6px}
</style></head><body><div class="wrap">
<h1>Migrator poczty &middot; IMAP &rarr; dpoczta</h1>
<div class="sub" id="meta">laduje...</div>
<div class="row">
  <span class="badge b-oczekuje" id="status">...</span>
  <span class="muted" id="folder"></span>
</div>
<div class="grid">
  <div class="card"><div class="k">Skopiowane</div><div class="v" id="copied">0</div></div>
  <div class="card"><div class="k">Pominiete (dubel)</div><div class="v" id="skipped">0</div></div>
  <div class="card"><div class="k">Oznacz. przeczyt.</div><div class="v" id="seen">0</div></div>
  <div class="card"><div class="k">Przebieg #</div><div class="v" id="cycle">0</div></div>
</div>
<div class="card" style="margin-bottom:18px">
  <div class="k">Postep biezacego folderu</div>
  <div class="v" style="font-size:14px"><span id="fd">0</span> / <span id="ft">0</span></div>
  <div class="bar"><i id="barfill"></i></div>
  <div class="retry" id="retry"></div>
</div>
<div class="log" id="log"></div>
</div>
<script>
function fmtRetry(ts){
  if(!ts) return "";
  const left = Math.max(0, ts*1000 - Date.now());
  const h = Math.floor(left/3600000), m = Math.floor((left%3600000)/60000);
  return "Nastepna proba za ~"+h+"h "+m+"m";
}
async function tick(){
  try{
    const r = await fetch('/api/status'); const s = await r.json();
    document.getElementById('meta').textContent =
      'foldery: '+s.folders.join(', ')+'  |  watki: '+s.workers+
      '  |  retry: '+s.retry_hours+'h  |  start: '+s.started_at.replace('T',' ').slice(0,19)+' UTC';
    const st = document.getElementById('status');
    st.textContent = s.status; st.className = 'badge b-'+s.status;
    document.getElementById('folder').textContent = s.current_folder || '';
    document.getElementById('copied').textContent = s.copied_total;
    document.getElementById('skipped').textContent = s.skipped_total;
    document.getElementById('seen').textContent = s.seen_marked_total;
    document.getElementById('cycle').textContent = s.cycle;
    document.getElementById('fd').textContent = s.folder_done;
    document.getElementById('ft').textContent = s.folder_total;
    const pct = s.folder_total ? Math.round(100*s.folder_done/s.folder_total) : 0;
    document.getElementById('barfill').style.width = pct+'%';
    document.getElementById('retry').textContent =
      s.status==='czeka_limit' ? fmtRetry(s.next_retry_ts) : '';
    const log = document.getElementById('log');
    log.innerHTML = s.log.map(l=>'<div>'+l.replace(/</g,'&lt;')+'</div>').join('');
    log.scrollTop = log.scrollHeight;
  }catch(e){}
}
setInterval(tick, 2000); tick();
</script></body></html>"""


@app.route("/")
def index():
    ensure_started()
    return render_template_string(PAGE)


@app.route("/api/status")
def api_status():
    ensure_started()
    return jsonify(STATE.snapshot())


@app.route("/health")
def health():
    return "ok", 200


# autostart migracji przy uruchomieniu serwera (Koyeb)
if AUTOSTART:
    ensure_started()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)

