#!/usr/bin/env python3
"""
Short Interest Squeeze Scanner
===============================
Detects stocks with rapid short interest INCREASES on small cap exchanges.
Backtest finding: 30%+ SI increase on SC exchange = SQUEEZE signal (long).
The signal is INVERTED -- rapid shorts INCREASE means they get squeezed, stock goes UP.

Backtest results (Fintel data, 2018-2026, 4-week hold):
  ALL exchanges: +2.62% 4w (inverted signal)
  Small caps (SC): +10.29% 4w, t=30.47***  <-- deploy target
  Exclude OTC (penny stock noise destroys signal)

Data source: FINRA Consolidated Short Interest API (free, no auth)
  URL: https://api.finra.org/data/group/otcmarket/name/consolidatedShortInterest
  Published: Twice monthly (~1st and ~15th of each month)

Scanner runs nightly on PythonAnywhere. Detects new settlement dates
and fires signals only on fresh data (dedup by settlement_date).

Usage:
  python3 si_scanner.py              # Normal nightly run
  python3 si_scanner.py --test-email # Send test email
  python3 si_scanner.py --status     # Show DB stats
  python3 si_scanner.py --force      # Force re-run on latest date (ignore dedup)
  python3 si_scanner.py --dry-run    # Detect signals, skip email
"""

import os
import sys
import json
import sqlite3
import smtplib
import argparse
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

import config

# ============================================================
# CONSTANTS
# ============================================================
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH      = os.path.join(SCRIPT_DIR, config.DB_NAME)

FINRA_URL    = 'https://api.finra.org/data/group/otcmarket/name/consolidatedShortInterest'
FINRA_HEADERS = {'Content-Type': 'application/json', 'Accept': 'application/json'}
ROWS_PER_REQ = 5000

CHANGE_THRESHOLD = config.CHANGE_THRESHOLD   # % SI increase -> signal
MIN_PRICE        = config.MIN_PRICE          # Filter penny stocks
TARGET_CLASSES   = set(config.TARGET_MARKET_CLASSES)
EXCLUDE_CLASSES  = set(config.EXCLUDE_MARKET_CLASSES)

# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS si_signals (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker           TEXT NOT NULL,
            settlement_date  TEXT NOT NULL,
            change_percent   REAL NOT NULL,
            short_position   INTEGER,
            prev_position    INTEGER,
            days_to_cover    REAL,
            market_class     TEXT,
            entry_price      REAL,
            detected_date    TEXT NOT NULL,
            emailed          INTEGER DEFAULT 0,
            UNIQUE(ticker, settlement_date)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS processed_dates (
            settlement_date  TEXT PRIMARY KEY,
            signals_found    INTEGER DEFAULT 0,
            processed_at     TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_date       TEXT,
            settlement_date TEXT,
            tickers_checked INTEGER,
            signals_found   INTEGER,
            new_signals     INTEGER,
            email_sent      INTEGER,
            errors          TEXT
        )
    """)
    conn.commit()
    return conn

# ============================================================
# FINRA API
# ============================================================

def get_latest_settlement_date():
    """Get the most recent settlement date from FINRA using AAPL as probe."""
    body = {
        'limit': 5000,
        'compareFilters': [
            {'compareType': 'equal', 'fieldName': 'symbolCode', 'fieldValue': 'AAPL'}
        ]
    }
    try:
        r = requests.post(FINRA_URL, headers=FINRA_HEADERS, json=body, timeout=30)
        if r.status_code == 200:
            data = r.json()
            dates = sorted(set(d['settlementDate'] for d in data if d.get('settlementDate')))
            return dates[-1] if dates else None
    except Exception as e:
        print(f'  FINRA probe failed: {e}')
    return None

def fetch_signals_for_date(settlement_date):
    """
    Fetch all tickers from FINRA for a settlement date where change_percent >= threshold
    and market_class is in TARGET_CLASSES.
    Paginates automatically.
    """
    all_records = []
    offset = 0
    while True:
        body = {
            'limit': ROWS_PER_REQ,
            'offset': offset,
            'dateRangeFilters': [
                {'fieldName': 'settlementDate', 'startDate': settlement_date, 'endDate': settlement_date}
            ],
        }
        try:
            r = requests.post(FINRA_URL, headers=FINRA_HEADERS, json=body, timeout=120)
            if r.status_code == 200:
                data = r.json()
                if not data:
                    break
                all_records.extend(data)
                if len(data) < ROWS_PER_REQ:
                    break
                offset += ROWS_PER_REQ
                time.sleep(0.5)
            elif r.status_code == 204:
                break
            else:
                print(f'  FINRA HTTP {r.status_code}')
                break
        except Exception as e:
            print(f'  FINRA fetch error: {e}')
            break
    # Filter for target market classes + change_percent threshold (client-side)
    filtered = [
        r for r in all_records
        if r.get('marketClassCode', '') in TARGET_CLASSES
        and r.get('marketClassCode', '') not in EXCLUDE_CLASSES
        and float(r.get('changePercent', 0) or 0) >= CHANGE_THRESHOLD
    ]
    print(f'  FINRA: {len(all_records)} total records, {len(filtered)} signals (>={CHANGE_THRESHOLD}% change, target exchanges)')
    return filtered

# ============================================================
# PRICE FILTER
# ============================================================

def get_price(ticker):
    """Fetch current price via yfinance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period='1d')
        if not hist.empty:
            return float(hist['Close'].iloc[-1])
    except Exception:
        pass
    return None

# ============================================================
# SIGNAL DETECTION + DEDUP
# ============================================================

def is_date_processed(conn, settlement_date):
    c = conn.cursor()
    c.execute('SELECT settlement_date FROM processed_dates WHERE settlement_date = ?', (settlement_date,))
    return c.fetchone() is not None

def mark_date_processed(conn, settlement_date, signals_found):
    c = conn.cursor()
    c.execute(
        'INSERT OR REPLACE INTO processed_dates (settlement_date, signals_found, processed_at) VALUES (?,?,?)',
        (settlement_date, signals_found, datetime.utcnow().strftime('%Y-%m-%d %H:%M'))
    )
    conn.commit()

def store_signal(conn, sig):
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO si_signals
            (ticker, settlement_date, change_percent, short_position, prev_position,
             days_to_cover, market_class, entry_price, detected_date, emailed)
            VALUES (?,?,?,?,?,?,?,?,?,0)
        """, (
            sig['ticker'], sig['settlement_date'], sig['change_percent'],
            sig.get('short_position'), sig.get('prev_position'),
            sig.get('days_to_cover'), sig.get('market_class'),
            sig.get('entry_price'), datetime.utcnow().strftime('%Y-%m-%d')
        ))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def mark_emailed(conn, ticker, settlement_date):
    c = conn.cursor()
    c.execute(
        'UPDATE si_signals SET emailed=1 WHERE ticker=? AND settlement_date=?',
        (ticker, settlement_date)
    )
    conn.commit()

# ============================================================
# EMAIL
# ============================================================

def build_email_subject(signals):
    tickers = ', '.join(s['ticker'] for s in signals[:10])
    if len(signals) > 10:
        tickers += f' +{len(signals)-10} more'
    return f'SI SQUEEZE: {tickers}'

def build_email_html(signals, settlement_date, recent):
    today = datetime.utcnow().strftime('%Y-%m-%d')
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <style>
        body {{ font-family: 'Segoe UI', Arial, sans-serif; background:#1a1a2e; color:#e0e0e0; margin:0; padding:0; }}
        .wrap {{ max-width:700px; margin:0 auto; padding:20px; }}
        h1 {{ color:#ff9800; font-size:22px; border-bottom:2px solid #333; padding-bottom:10px; margin-top:0; }}
        .summary {{ background:#16213e; border-radius:8px; padding:14px; margin:14px 0; font-size:14px; }}
        .card {{ background:#16213e; border-left:4px solid #ff9800; border-radius:8px; padding:14px; margin:10px 0; }}
        .ticker {{ font-size:20px; font-weight:bold; color:#fff; }}
        .badge {{ display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px;
                  font-weight:bold; background:#ff9800; color:#000; margin-left:10px; vertical-align:middle; }}
        .metrics {{ display:flex; gap:18px; margin:10px 0; flex-wrap:wrap; }}
        .metric {{ text-align:center; }}
        .mv {{ font-size:16px; font-weight:bold; }}
        .ml {{ font-size:11px; color:#888; }}
        .meta {{ font-size:12px; color:#aaa; margin-top:6px; }}
        .backtest {{ background:#0d2137; border:1px solid #1a5276; border-radius:8px;
                     padding:12px; margin:20px 0; font-size:12px; color:#7fb3d8; }}
        table {{ width:100%; border-collapse:collapse; margin-top:8px; }}
        th {{ background:#0f3460; color:#e0e0e0; padding:7px; text-align:left; font-size:12px; }}
        td {{ padding:7px; border-bottom:1px solid #333; font-size:12px; }}
        .footer {{ color:#555; font-size:11px; margin-top:28px; border-top:1px solid #333; padding-top:10px; }}
      </style>
    </head>
    <body><div class='wrap'>
    <h1>SHORT INTEREST SQUEEZE SCANNER &mdash; {today}</h1>
    <div class='summary'>
      <strong>{len(signals)} squeeze signal(s)</strong> &nbsp;|&nbsp;
      Settlement date: {settlement_date} &nbsp;|&nbsp;
      SI increase &ge;{CHANGE_THRESHOLD}% on SC/exchange tickers
    </div>
    """

    for s in sorted(signals, key=lambda x: -x['change_percent'])[:20]:
        price_str = f"${s['entry_price']:.2f}" if s.get('entry_price') else 'N/A'
        dtc_str   = f"{s['days_to_cover']:.1f}d" if s.get('days_to_cover') else 'N/A'
        html += f"""
    <div class='card'>
      <span class='ticker'>{s['ticker']}</span>
      <span class='badge'>SQUEEZE &bull; LONG</span>
      <div class='metrics'>
        <div class='metric'>
          <div class='mv' style='color:#ff9800;'>+{s['change_percent']:.1f}%</div>
          <div class='ml'>SI CHANGE</div>
        </div>
        <div class='metric'>
          <div class='mv'>{price_str}</div>
          <div class='ml'>PRICE</div>
        </div>
        <div class='metric'>
          <div class='mv'>{dtc_str}</div>
          <div class='ml'>DAYS TO COVER</div>
        </div>
        <div class='metric'>
          <div class='mv'>28d</div>
          <div class='ml'>HOLD</div>
        </div>
      </div>
      <div class='meta'>{s.get('market_class','?')} &bull; Short pos: {s.get('short_position',0):,} &bull; Prev: {s.get('prev_position',0):,}</div>
    </div>
        """

    html += """
    <div class='backtest'>
      <strong>Backtest Reference (Fintel/FINRA data, 2018-2026, 4-week hold):</strong><br>
      SC exchange, SI increase &ge;30%: <strong>+10.29% alpha/trade, t=30.47***</strong><br>
      Signal is INVERTED: rapid short increase = squeeze setup, go LONG<br>
      Exclude OTC (penny stock noise). Published ~1st and ~15th each month.
    </div>
    """

    if recent:
        html += """
    <h2 style='color:#64b5f6;'>Recent Signal History</h2>
    <table>
      <tr><th>Date</th><th>Ticker</th><th>SI Change</th><th>Class</th><th>Price</th></tr>
        """
        for r in recent[:15]:
            html += f"""
      <tr>
        <td>{r[0]}</td><td><strong>{r[1]}</strong></td>
        <td style='color:#ff9800;'>+{r[2]:.1f}%</td>
        <td>{r[3]}</td><td>{'$'+str(round(r[4],2)) if r[4] else 'N/A'}</td>
      </tr>
            """
        html += '</table>'

    html += f"""
    <div class='footer'>
      SI Squeeze Scanner v1.0 &nbsp;|&nbsp; FINRA Consolidated Short Interest (free) &nbsp;|&nbsp;
      IB AutoTrader parses subject: 'SI SQUEEZE: ...' &rarr; BUY orders<br>
      Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
    </div>
    </div></body></html>
    """
    return html

def send_email(subject, html_body):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = config.EMAIL_SENDER
    msg['To']      = config.EMAIL_RECIPIENT
    msg.attach(MIMEText(html_body, 'html'))
    try:
        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as srv:
            srv.starttls()
            srv.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
            srv.sendmail(config.EMAIL_SENDER, config.EMAIL_RECIPIENT, msg.as_string())
        print('  Email sent successfully')
        return True
    except Exception as e:
        print(f'  ERROR sending email: {e}')
        return False

def get_recent_signals(conn, n=30):
    c = conn.cursor()
    c.execute("""
        SELECT settlement_date, ticker, change_percent, market_class, entry_price
        FROM si_signals ORDER BY detected_date DESC, change_percent DESC LIMIT ?
    """, (n,))
    return c.fetchall()

def log_scan(conn, settlement_date, tickers_checked, signals_found, new_signals, email_sent, errors=''):
    c = conn.cursor()
    c.execute("""
        INSERT INTO scan_log (scan_date, settlement_date, tickers_checked, signals_found,
                              new_signals, email_sent, errors)
        VALUES (?,?,?,?,?,?,?)
    """, (
        datetime.utcnow().strftime('%Y-%m-%d %H:%M'),
        settlement_date, tickers_checked, signals_found, new_signals,
        1 if email_sent else 0, errors
    ))
    conn.commit()

# ============================================================
# MAIN SCAN
# ============================================================

def run_scan(force=False, dry_run=False):
    print(f"{'='*60}")
    print(f'SI SQUEEZE SCANNER -- {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}')
    print(f"{'='*60}")

    conn = init_db()

    # Step 1: Get latest settlement date
    print('Probing FINRA for latest settlement date...')
    settlement_date = get_latest_settlement_date()
    if not settlement_date:
        print('ERROR: Could not retrieve settlement date from FINRA')
        log_scan(conn, 'UNKNOWN', 0, 0, 0, False, 'FINRA probe failed')
        conn.close()
        return
    print(f'Latest FINRA settlement date: {settlement_date}')

    # Step 2: Check if already processed
    if not force and is_date_processed(conn, settlement_date):
        print(f'Settlement date {settlement_date} already processed. No new data.')
        log_scan(conn, settlement_date, 0, 0, 0, False)
        conn.close()
        return

    # Step 3: Fetch qualifying records from FINRA
    print(f'Fetching SI squeeze candidates for {settlement_date}...')
    records = fetch_signals_for_date(settlement_date)
    if not records:
        print('No qualifying signals found.')
        mark_date_processed(conn, settlement_date, 0)
        recent = get_recent_signals(conn)
        subj = f'SI Scanner -- No signals ({settlement_date})'
        html = build_email_html([], settlement_date, recent)
        if not dry_run:
            send_email(subj, html)
        log_scan(conn, settlement_date, 0, 0, 0, not dry_run)
        conn.close()
        return

    # Step 4: Enrich + filter by price
    print(f'Enriching {len(records)} candidates with live prices...')
    signals = []
    for rec in records:
        ticker     = rec.get('symbolCode', '')
        chg_pct    = float(rec.get('changePercent', 0) or 0)
        mkt_class  = rec.get('marketClassCode', '')
        short_pos  = int(rec.get('currentShortPositionQuantity', 0) or 0)
        prev_pos   = int(rec.get('previousShortPositionQuantity', 0) or 0)
        dtc        = rec.get('daysToCoverQuantity')

        if not ticker or chg_pct < CHANGE_THRESHOLD:
            continue
        if mkt_class not in TARGET_CLASSES:
            continue

        # Price filter
        price = get_price(ticker)
        if price is not None and price < MIN_PRICE:
            print(f'  {ticker}: price ${price:.2f} < ${MIN_PRICE} -- skipped')
            continue

        signals.append({
            'ticker':          ticker,
            'settlement_date': settlement_date,
            'change_percent':  chg_pct,
            'short_position':  short_pos,
            'prev_position':   prev_pos,
            'days_to_cover':   float(dtc) if dtc else None,
            'market_class':    mkt_class,
            'entry_price':     price,
        })
        time.sleep(0.1)  # rate limit yfinance

    print(f'Signals after price filter: {len(signals)}')

    # Step 5: Dedup + store
    new_signals = []
    for s in signals:
        if store_signal(conn, s):
            new_signals.append(s)
    print(f'New signals (not previously seen): {len(new_signals)}')

    # Step 6: Email
    recent = get_recent_signals(conn)
    email_sent = False
    if new_signals:
        subject = build_email_subject(new_signals)
        html    = build_email_html(new_signals, settlement_date, recent)
        if dry_run:
            print(f'  DRY RUN: subject would be: {subject}')
        else:
            print(f'  Subject: {subject}')
            email_sent = send_email(subject, html)
            if email_sent:
                for s in new_signals:
                    mark_emailed(conn, s['ticker'], s['settlement_date'])
    else:
        subj = f'SI Scanner -- {len(signals)} signals (all previously sent) ({settlement_date})'
        html = build_email_html([], settlement_date, recent)
        if not dry_run:
            send_email(subj, html)

    mark_date_processed(conn, settlement_date, len(new_signals))
    log_scan(conn, settlement_date, len(records), len(signals), len(new_signals), email_sent)

    print(f"{'='*60}")
    print(f'SCAN COMPLETE: {len(new_signals)} new squeeze signals')
    for s in new_signals[:10]:
        price_str = f"${s['entry_price']:.2f}" if s.get('entry_price') else 'N/A'
        print(f"  {s['ticker']:<6}  +{s['change_percent']:.1f}%  {s['market_class']}  {price_str}")
    print(f"{'='*60}")
    conn.close()

# ============================================================
# CLI
# ============================================================

def show_status():
    conn = init_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM si_signals')
    total = c.fetchone()[0]
    c.execute('SELECT COUNT(DISTINCT settlement_date) FROM si_signals')
    dates = c.fetchone()[0]
    c.execute('SELECT * FROM scan_log ORDER BY id DESC LIMIT 5')
    scans = c.fetchall()
    c.execute("""
        SELECT settlement_date, ticker, change_percent, market_class, entry_price
        FROM si_signals ORDER BY detected_date DESC, change_percent DESC LIMIT 10
    """)
    recent = c.fetchall()
    print(f"{'='*50}")
    print('SI SQUEEZE SCANNER STATUS')
    print(f"{'='*50}")
    print(f'Total signals:     {total}')
    print(f'Settlement dates:  {dates}')
    if scans:
        print('Last 5 scans:')
        for s in scans:
            print(f'  {s[1]} | date:{s[2]} | checked:{s[3]} | signals:{s[4]} | new:{s[5]}')
    if recent:
        print('Recent signals:')
        for r in recent:
            price_str = f'${r[4]:.2f}' if r[4] else 'N/A'
            print(f'  {r[0]}  {r[1]:<6}  +{r[2]:.1f}%  {r[3]}  {price_str}')
    conn.close()

def send_test_email():
    html = f"""
    <html><body style='font-family:Arial; background:#1a1a2e; color:#e0e0e0; padding:20px;'>
      <h1 style='color:#ff9800;'>SI Squeeze Scanner -- Test Email</h1>
      <p>Configuration working correctly.</p>
      <ul>
        <li>Change threshold: {CHANGE_THRESHOLD}%</li>
        <li>Min price: ${MIN_PRICE}</li>
        <li>Target classes: {sorted(TARGET_CLASSES)}</li>
      </ul>
      <p>IB AutoTrader subject format: 'SI SQUEEZE: TICK1, TICK2'</p>
      <p style='color:#666;'>Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
    </body></html>
    """
    send_email('SI Squeeze Scanner -- Test Email', html)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Short Interest Squeeze Scanner')
    parser.add_argument('--test-email', action='store_true')
    parser.add_argument('--status',     action='store_true')
    parser.add_argument('--force',      action='store_true', help='Re-run even if date already processed')
    parser.add_argument('--dry-run',    action='store_true')
    args = parser.parse_args()

    if args.test_email:
        print('Sending test email...')
        send_test_email()
    elif args.status:
        show_status()
    else:
        run_scan(force=args.force, dry_run=args.dry_run)

