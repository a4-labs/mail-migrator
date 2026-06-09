#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migrator poczty IMAP -> IMAP, wersja SERWEROWA z interaktywnym Web GUI.
"""

import os
import re
import time
import base64
import imaplib
import email
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template_string

imaplib._MAXLINE = 10_000_000

# ===================== STAN GLOBALNY =====================

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
        self.started_at = None
        self.finished = False
        self.log_lines = []
        
        # Zmienne konfiguracyjne z formularza
        self.src_host = ""
        self.src_port = 993
        self.src_user = ""
        self.src_pass = ""
        self.dst_host = ""
        self.dst_port = 993
        self.dst_user = ""
        self.dst_pass = ""
        self.folders = []
        self.workers = 2
        self.retry_hours = 2.0

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
                "folders": self.folders, "workers": self.workers,
                "retry_hours": self.retry_hours,
                "log": self.log_lines[-200:],
            }

STATE = State()
_started = threading.Event()


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

# ===================== LOGIKA MIGRACJI =====================

def is_limit_error(exc):
    s = str(exc).lower()
    keys = ['eof', 'limit', 'too many', 'try again', 'unavailable',
            'bandwidth', 'connection reset', 'timed out', 'broken pipe',
            'socket error']
    return any(k in s for k in keys)

def existing_index(dst, target, st):
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
        # Chunkowanie zapytania FETCH (podział na mniejsze paczki po 500)
        chunk_size = 500
        for i in range(0, len(nums), chunk_size):
            chunk = nums[i:i + chunk_size]
            seq = b','.join(chunk).decode()
            typ, fetched = dst.fetch(seq, '(FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            if typ != 'OK' or not fetched:
                continue
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
        to_mark, cur_num = [], None
        chunk_size = 500
        for i in range(0, len(nums), chunk_size):
            chunk = nums[i:i + chunk_size]
            seq = b','.join(chunk).decode()
            typ, fetched = dst.fetch(seq, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            if typ != 'OK' or not fetched:
                continue
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
            try: dst.logout()
            except Exception: pass
    return marked

def worker(wid, src_cfg, dst_cfg, folder, target, job_ids, existing, ex_lock, counters, c_lock, unseen, seen_msgids, s_lock, st):
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
                        if not blob: continue
                        try:
                            fl = imaplib.ParseFlags(blob)
                            flags = ' '.join(x.decode() if isinstance(x, bytes) else x for x in fl if (x.decode() if isinstance(x, bytes) else x) != '\\Recent')
                        except Exception: pass
                        try: internaldate = imaplib.Internaldate2tuple(blob)
                        except Exception: pass

                typ, md = src.fetch(mid, '(RFC822)')
                if typ != 'OK' or not md: continue
                raw = None
                for part in md:
                    if isinstance(part, tuple) and part[1]:
                        raw = part[1]; break
                if raw is None: continue

                msgid = None
                try:
                    # Optymalizacja pamięci: nie parsujemy całego pliku strukturalnie, co dla 50MB załącznika zjada mnóstwo RAMu.
                    # Przeszukujemy tylko pierwsze 50 KB w poszukiwaniu Message-ID przy pomocy regexu.
                    head_chunk = raw[:50000]
                    match = re.search(br'(?mi)^Message-ID:\s*([^\r\n]+)', head_chunk)
                    if match:
                        msgid = norm_msgid(match.group(1).decode('utf-8', 'ignore'))
                except Exception:
                    msgid = None

                was_seen = '\\Seen' in flags

                if msgid:
                    with ex_lock:
                        already = msgid in existing
                        if not already: existing.add(msgid)
                    if already:
                        if was_seen:
                            num = None
                            with ex_lock: num = unseen.pop(msgid, None)
                            if num:
                                try:
                                    dst.select(f'"{target}"')
                                    dst.store(num, '+FLAGS', '(\\Seen)')
                                except Exception: pass
                        with c_lock: counters['skipped'] += 1
                        with st.lock: st.skipped_total += 1
                        continue

                ta, _ = dst.append(f'"{target}"', f'({flags})' if flags else None, internaldate, raw)
                if was_seen and ta == 'OK' and msgid:
                    with s_lock: seen_msgids.append(msgid)
                with c_lock: counters['done'] += 1
                with st.lock:
                    st.copied_total += 1
                    st.folder_done += 1
                
                # Agresywne uwalnianie pamięci: usuwamy potężną zmienną raw
                del raw
                if counters['done'] % 20 == 0:
                    import gc; gc.collect()
            except Exception as e:
                if is_limit_error(e):
                    st.log(f"   [watek {wid}] LIMIT/zerwanie: {e}")
                    with c_lock: counters['limit'] = True
                    break
                st.log(f"   [watek {wid}] blad maila: {e}")
                continue
    finally:
        for c in (src, dst):
            try: c.logout()
            except Exception: pass

def migrate_once(st):
    src_cfg = {"host": st.src_host, "port": st.src_port, "user": st.src_user, "password": st.src_pass}
    dst_cfg = {"host": st.dst_host, "port": st.dst_port, "user": st.dst_user, "password": st.dst_pass}

    try:
        src = connect(**src_cfg)
        dst = connect(**dst_cfg)
    except Exception as e:
        st.log(f"Polaczenie nieudane: {e}")
        return 'limit' if is_limit_error(e) else 'error'

    any_new = False
    hit_limit = False
    try:
        for folder in st.folders:
            target = target_name(folder)
            with st.lock:
                st.current_folder = f"{folder} -> {target}"
                st.folder_done = 0
            st.log(f"=== Folder '{folder}' -> '{target}' ===")

            opened = False
            for attempt in range(2):
                try:
                    typ, _ = src.select(f'"{folder}"', readonly=True)
                    if typ == 'OK': opened = True; break
                except Exception as e:
                    st.log(f"   otwarcie: {e}")
                    if attempt == 0:
                        try: src.logout()
                        except Exception: pass
                        try: dst.logout()
                        except Exception: pass
                        try: src = connect(**src_cfg); dst = connect(**dst_cfg)
                        except Exception as e2:
                            st.log(f"   wznowienie polaczen: {e2}")
                            if is_limit_error(e2): return 'limit'
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

            n = max(1, min(st.workers, len(ids)))
            buckets = [[] for _ in range(n)]
            for idx, mid in enumerate(ids):
                buckets[idx % n].append(mid)

            ex_lock = threading.Lock(); c_lock = threading.Lock()
            s_lock = threading.Lock(); seen_msgids = []
            counters = {'done': 0, 'skipped': 0, 'limit': False}

            threads = []
            for wid in range(n):
                if not buckets[wid]: continue
                t = threading.Thread(target=worker, args=(wid + 1, src_cfg, dst_cfg, folder, target, buckets[wid], existing, ex_lock, counters, c_lock, unseen, seen_msgids, s_lock, st), daemon=True)
                threads.append(t); t.start()
            for t in threads: t.join()

            if counters['done'] > 0: any_new = True
            st.log(f"   skopiowano {counters['done']}, pominieto {counters['skipped']}")

            if seen_msgids:
                m = mark_seen(dst_cfg, target, seen_msgids, st)
                with st.lock: st.seen_marked_total += m
                st.log(f"   oznaczono przeczytane: {m}")

            if counters['limit']:
                hit_limit = True
                st.log("   >>> trafiono limit/zerwanie - przerywam przebieg")
                break
    finally:
        for c in (src, dst):
            try: c.logout()
            except Exception: pass

    if hit_limit: return 'limit'
    if any_new: return 'progress'
    return 'done'

def run_loop():
    STATE.log(f"Start migracji: {STATE.src_user} -> {STATE.dst_user}")
    STATE.log(f"Foldery: {', '.join(STATE.folders)} | watki: {STATE.workers} | retry: {STATE.retry_hours}h")

    while True:
        with STATE.lock:
            STATE.cycle += 1
            STATE.status = "pracuje"
            STATE.next_retry_ts = None
        STATE.log(f"--- Przebieg #{STATE.cycle} ---")
        try:
            result = migrate_once(STATE)
        except Exception as e:
            STATE.log(f"Blad przebiegu: {e}")
            result = 'limit' if is_limit_error(e) else 'error'

        if result == 'done':
            with STATE.lock:
                STATE.status = "zakonczono"
                STATE.finished = True
            STATE.log(">>> GOTOWE. Caly material przeniesiony, brak nowych maili.")
            return
        elif result == 'error':
            wait = STATE.retry_hours * 3600
            nxt = time.time() + wait
            with STATE.lock:
                STATE.status = "czeka_limit"
                STATE.next_retry_ts = nxt
            STATE.log(f"Blad. Ponawiam za {STATE.retry_hours}h.")
            time.sleep(wait)
        elif result == 'limit':
            wait = STATE.retry_hours * 3600
            nxt = time.time() + wait
            with STATE.lock:
                STATE.status = "czeka_limit"
                STATE.next_retry_ts = nxt
            STATE.log(f"Limit Gmaila. Czekam {STATE.retry_hours}h i probuje dalej.")
            time.sleep(wait)
        else:
            STATE.log("Przebieg dograł nowe maile. Kontynuuje po 60s.")
            time.sleep(60)

# ===================== WEB GUI =====================

app = Flask(__name__)

PAGE = """<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Migrator poczty</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg-color: #0b0f19;
  --panel-bg: rgba(22, 27, 34, 0.7);
  --border: rgba(255, 255, 255, 0.1);
  --text-main: #e6edf3;
  --text-muted: #8b949e;
  --accent: #3b82f6;
  --accent-hover: #2563eb;
  --success: #10b981;
  --warning: #f59e0b;
  --error: #ef4444;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg-color);
  background-image: radial-gradient(circle at top right, rgba(59, 130, 246, 0.15), transparent 40%),
                    radial-gradient(circle at bottom left, rgba(16, 185, 129, 0.1), transparent 40%);
  background-attachment: fixed;
  color: var(--text-main);
  font-family: 'Inter', sans-serif;
  padding: 2rem;
  line-height: 1.6;
  min-height: 100vh;
}
.wrap { max-width: 900px; margin: 0 auto; }
.glass-panel {
  background: var(--panel-bg);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 24px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.3);
}
h1 { font-size: 24px; font-weight: 700; margin-bottom: 8px; background: linear-gradient(to right, #60a5fa, #a78bfa); -webkit-background-clip: text; color: transparent; }
.sub { color: var(--text-muted); font-size: 14px; margin-bottom: 30px; }

/* Form styles */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }
.form-group { display: flex; flex-direction: column; gap: 8px; }
.form-group label { font-size: 13px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
.form-control {
  background: rgba(0,0,0,0.2);
  border: 1px solid var(--border);
  color: #fff;
  padding: 12px 16px;
  border-radius: 8px;
  font-family: 'Inter', sans-serif;
  font-size: 15px;
  transition: all 0.2s;
}
.form-control:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2); }
.btn {
  background: var(--accent);
  color: white;
  border: none;
  padding: 14px 24px;
  border-radius: 8px;
  font-size: 16px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
  width: 100%;
}
.btn-secondary {
  background: transparent;
  border: 1px solid var(--accent);
  color: var(--accent);
}
.btn:hover { transform: translateY(-1px); }
.btn:disabled { opacity: 0.7; cursor: not-allowed; transform: none; }
.btn-secondary:hover { background: rgba(59, 130, 246, 0.1); }

/* Checkboxes */
.folders-list {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  max-height: 200px;
  overflow-y: auto;
  background: rgba(0,0,0,0.2);
  padding: 16px;
  border-radius: 8px;
  border: 1px solid var(--border);
  margin-top: 12px;
}
.folder-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}
.folder-item input[type="checkbox"] {
  accent-color: var(--accent);
  width: 16px;
  height: 16px;
  cursor: pointer;
}

/* Dashboard styles */
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
.card { background: rgba(0,0,0,0.2); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
.card .k { color: var(--text-muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
.card .v { font-size: 24px; font-weight: 700; }
.badge { display: inline-flex; align-items: center; padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 600; }
.b-pracuje { background: rgba(59,130,246,0.15); color: #60a5fa; border: 1px solid rgba(59,130,246,0.3); }
.b-czeka_limit { background: rgba(245,158,11,0.15); color: #fbbf24; border: 1px solid rgba(245,158,11,0.3); }
.b-zakonczono { background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3); }
.b-blad { background: rgba(239,68,68,0.15); color: #f87171; border: 1px solid rgba(239,68,68,0.3); }
.b-oczekuje { background: rgba(139,148,158,0.15); color: var(--text-muted); border: 1px solid rgba(139,148,158,0.3); }
.bar { height: 8px; background: rgba(0,0,0,0.3); border-radius: 4px; overflow: hidden; margin-top: 12px; box-shadow: inset 0 1px 3px rgba(0,0,0,0.2); }
.bar>i { display: block; height: 100%; background: linear-gradient(90deg, #3b82f6, #8b5cf6); width: 0; transition: width 0.4s ease; border-radius: 4px; }
.log { background: #010409; border: 1px solid var(--border); border-radius: 12px; padding: 16px; height: 400px; overflow-y: auto; font-family: ui-monospace, 'Cascadia Code', monospace; font-size: 13px; white-space: pre-wrap; word-break: break-word; }
.log div { padding: 2px 0; color: #c9d1d9; border-bottom: 1px solid rgba(255,255,255,0.03); }
.row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
.hidden { display: none !important; }
@media (max-width: 600px) { .form-grid, .folders-list { grid-template-columns: 1fr; } }
</style></head><body><div class="wrap">

  <div id="form-view" class="glass-panel hidden">
    <h1>Konfiguracja Migracji</h1>
    <div class="sub">Wprowadź dane skrzynek pocztowych, wybierz foldery i rozpocznij proces w tle.</div>
    <form id="setup-form">
      <h3>Skrzynka źródłowa (skąd pobieramy)</h3><br>
      <div class="form-grid">
        <div class="form-group"><label>Host IMAP</label><input type="text" id="s_host" class="form-control" value="imap.gmail.com" required></div>
        <div class="form-group"><label>Port</label><input type="number" id="s_port" class="form-control" value="993" required></div>
        <div class="form-group"><label>Adres E-mail</label><input type="email" id="s_user" class="form-control" placeholder="jan@gmail.com" required></div>
        <div class="form-group"><label>Hasło (lub hasło aplikacji)</label><input type="password" id="s_pass" class="form-control" required></div>
      </div>
      
      <div style="margin-bottom: 24px;">
        <button type="button" class="btn btn-secondary" id="fetch-folders-btn">Pobierz listę folderów ze źródła</button>
        <div id="folders-container" class="hidden">
           <div class="form-group" style="margin-top: 12px;">
             <label>Zaznacz foldery do migracji:</label>
             <div class="folders-list" id="folders-list"></div>
           </div>
        </div>
      </div>

      <h3>Skrzynka docelowa (dokąd kopiujemy)</h3><br>
      <div class="form-grid">
        <div class="form-group"><label>Host IMAP</label><input type="text" id="d_host" class="form-control" value="imap.dpoczta.pl" required></div>
        <div class="form-group"><label>Port</label><input type="number" id="d_port" class="form-control" value="993" required></div>
        <div class="form-group"><label>Adres E-mail</label><input type="email" id="d_user" class="form-control" placeholder="jan@dpoczta.pl" required></div>
        <div class="form-group"><label>Hasło</label><input type="password" id="d_pass" class="form-control" required></div>
      </div>
      
      <button type="submit" class="btn" id="start-btn" style="background: var(--success); margin-top: 16px;">Uruchom Migrację w tle</button>
    </form>
  </div>

  <div id="dash-view" class="hidden">
    <div class="glass-panel" style="margin-bottom: 24px;">
      <h1>Migrator poczty</h1>
      <div class="sub" id="meta">ładuje...</div>
      <div class="row">
        <span class="badge" id="status">...</span>
        <span style="color: var(--text-muted); font-size: 14px; font-weight: 500;" id="folder"></span>
      </div>
      <div class="grid">
        <div class="card"><div class="k">Skopiowane</div><div class="v" style="color: var(--success)" id="copied">0</div></div>
        <div class="card"><div class="k">Pominięte (dubel)</div><div class="v" id="skipped">0</div></div>
        <div class="card"><div class="k">Oznacz. przeczytane</div><div class="v" id="seen">0</div></div>
        <div class="card"><div class="k">Przebieg #</div><div class="v" id="cycle">0</div></div>
      </div>
      <div class="card">
        <div class="row" style="margin-bottom: 0;">
          <div class="k" style="margin: 0;">Postęp bieżącego folderu</div>
          <div style="font-size:14px; font-weight: 600;"><span id="fd">0</span> / <span id="ft">0</span></div>
        </div>
        <div class="bar"><i id="barfill"></i></div>
        <div style="color: var(--warning); font-size: 13px; margin-top: 8px;" id="retry"></div>
      </div>
    </div>
    <div class="log" id="log"></div>
  </div>

</div>

<script>
function fmtRetry(ts){
  if(!ts) return "";
  const left = Math.max(0, ts*1000 - Date.now());
  const h = Math.floor(left/3600000), m = Math.floor((left%3600000)/60000);
  return "Kolejna próba za ~"+h+"h "+m+"m";
}

let dashActive = false;

async function checkStatus(){
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    
    if (s.status === 'oczekuje' && !dashActive) {
      document.getElementById('form-view').classList.remove('hidden');
      document.getElementById('dash-view').classList.add('hidden');
    } else {
      dashActive = true;
      document.getElementById('form-view').classList.add('hidden');
      document.getElementById('dash-view').classList.remove('hidden');
      updateDash(s);
    }
  } catch(e) {}
}

function updateDash(s) {
  let startTxt = s.started_at ? s.started_at.replace('T',' ').slice(0,19)+' UTC' : '-';
  document.getElementById('meta').textContent =
    'Foldery: ' + (s.folders||[]).join(', ') + '  |  Wątki: ' + s.workers +
    '  |  Start: ' + startTxt;
    
  const st = document.getElementById('status');
  st.textContent = s.status.toUpperCase(); 
  st.className = 'badge b-' + s.status;
  
  document.getElementById('folder').textContent = s.current_folder || '';
  document.getElementById('copied').textContent = s.copied_total;
  document.getElementById('skipped').textContent = s.skipped_total;
  document.getElementById('seen').textContent = s.seen_marked_total;
  document.getElementById('cycle').textContent = s.cycle;
  document.getElementById('fd').textContent = s.folder_done;
  document.getElementById('ft').textContent = s.folder_total;
  
  const pct = s.folder_total ? Math.round(100*s.folder_done/s.folder_total) : 0;
  document.getElementById('barfill').style.width = pct+'%';
  
  document.getElementById('retry').textContent = s.status === 'czeka_limit' ? fmtRetry(s.next_retry_ts) : '';
  
  const log = document.getElementById('log');
  log.innerHTML = s.log.map(l=>'<div>'+l.replace(/</g,'&lt;')+'</div>').join('');
  if(log.scrollHeight - log.scrollTop < 600) log.scrollTop = log.scrollHeight;
}

// Obsługa pobierania folderów
document.getElementById('fetch-folders-btn').addEventListener('click', async (e) => {
  const btn = e.target;
  const user = document.getElementById('s_user').value;
  const pass = document.getElementById('s_pass').value;
  const host = document.getElementById('s_host').value;
  const port = document.getElementById('s_port').value;
  
  if(!user || !pass || !host) {
    alert('Wypełnij najpierw dane skrzynki źródłowej (host, email, hasło)!');
    return;
  }
  
  btn.textContent = 'Pobieranie...';
  btn.disabled = true;
  
  try {
    const res = await fetch('/api/folders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({host, port, user, pass})
    });
    
    const data = await res.json();
    if(data.error) {
      alert('Błąd pobierania: ' + data.error);
    } else if(data.folders) {
      const container = document.getElementById('folders-container');
      const list = document.getElementById('folders-list');
      list.innerHTML = '';
      
      data.folders.forEach(f => {
        const div = document.createElement('label');
        div.className = 'folder-item';
        const chk = document.createElement('input');
        chk.type = 'checkbox';
        chk.value = f.name;
        // Domyślnie zaznaczamy znane
        if(f.name === 'INBOX' || f.name.toLowerCase().includes('wyslane') || f.name.toLowerCase().includes('sent')) {
          chk.checked = true;
        }
        div.appendChild(chk);
        div.appendChild(document.createTextNode(`${f.name} (${f.count} maili)`));
        list.appendChild(div);
      });
      container.classList.remove('hidden');
    }
  } catch(e) {
    alert('Błąd sieci.');
  }
  btn.textContent = 'Odśwież listę folderów';
  btn.disabled = false;
});

// Start migracji
document.getElementById('setup-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  
  // Zbieranie zaznaczonych folderów
  const checkboxes = document.querySelectorAll('#folders-list input[type="checkbox"]:checked');
  let selectedFolders = Array.from(checkboxes).map(c => c.value);
  
  if (selectedFolders.length === 0) {
    alert("Proszę pobrać i zaznaczyć przynajmniej jeden folder do migracji!");
    return;
  }

  const btn = document.getElementById('start-btn');
  btn.disabled = true;
  btn.textContent = 'Uruchamianie...';
  
  const payload = {
    src_host: document.getElementById('s_host').value,
    src_port: parseInt(document.getElementById('s_port').value),
    src_user: document.getElementById('s_user').value,
    src_pass: document.getElementById('s_pass').value,
    dst_host: document.getElementById('d_host').value,
    dst_port: parseInt(document.getElementById('d_port').value),
    dst_user: document.getElementById('d_user').value,
    dst_pass: document.getElementById('d_pass').value,
    folders: selectedFolders
  };
  
  await fetch('/api/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  
  checkStatus();
});

setInterval(checkStatus, 2000);
checkStatus();
</script></body></html>"""

@app.route("/")
def index():
    return render_template_string(PAGE)

@app.route("/api/status")
def api_status():
    return jsonify(STATE.snapshot())

@app.route("/api/folders", methods=["POST"])
def api_folders():
    data = request.json
    try:
        m = connect(data.get('host'), data.get('port'), data.get('user'), data.get('pass'))
        typ, folders_data = m.list()
        folders = []
        if typ == 'OK':
            for line in folders_data:
                if not line: continue
                # Dekodowanie IMAP LIST z wyrażenia regularnego
                decoded = line.decode('utf-8', 'ignore')
                # line format: (\HasNoChildren) "/" "INBOX"
                match = re.match(r'\((?P<flags>.*?)\)\s+"?(?P<delimiter>.*?)"?\s+"?(?P<name>.*?)"?$', decoded)
                if match:
                    name = match.group('name')
                    count = "0"
                    try:
                        styp, sdata = m.status(f'"{name}"', '(MESSAGES)')
                        if styp == 'OK' and sdata and sdata[0]:
                            cmatch = re.search(br'MESSAGES\s+(\d+)', sdata[0])
                            if cmatch:
                                count = cmatch.group(1).decode()
                    except:
                        pass
                    folders.append({"name": name, "count": count})
        m.logout()
        # Odślepianie UTF-7 dla czytelności (opcjonalnie, ale w UI lepiej widzieć zdekodowane)
        # UWAGA: formularz wysyła "value" takie samo, co musimy traktować jako czyste nazwy do pobierania.
        # W skrypcie imap_utf7_decode robi tylko wyswietlanie, ale nazwy bazowe w array foldery muszą być "z serwera".
        return jsonify({"folders": folders})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json
    with STATE.lock:
        if STATE.status != "oczekuje":
            return jsonify({"error": "Migracja juz zostala uruchomiona."}), 400
        
        STATE.src_host = data.get("src_host", "imap.gmail.com")
        STATE.src_port = data.get("src_port", 993)
        STATE.src_user = data.get("src_user")
        STATE.src_pass = data.get("src_pass")
        
        STATE.dst_host = data.get("dst_host", "imap.dpoczta.pl")
        STATE.dst_port = data.get("dst_port", 993)
        STATE.dst_user = data.get("dst_user")
        STATE.dst_pass = data.get("dst_pass")
        
        STATE.folders = data.get("folders", ["INBOX"])
        STATE.started_at = datetime.now(timezone.utc).isoformat()
    
    if not _started.is_set():
        _started.set()
        threading.Thread(target=run_loop, daemon=True).start()
        
    return jsonify({"success": True})

@app.route("/health")
def health():
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
