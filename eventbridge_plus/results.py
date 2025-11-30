from flask import render_template, request, redirect, url_for, flash, session, abort, jsonify, Response
from .auth import require_login, get_current_user_id, get_current_platform_role
from eventbridge_plus import app, db
from datetime import datetime, date, time, timedelta
import traceback
import csv
import re
from io import TextIOWrapper, StringIO
from eventbridge_plus.util import AVAILABLE_EVENT_TYPES, AVAILABLE_LOCATIONS

# Administrator platform role (can be released directly)
ALLOWED_PLATFORM_ROLES_FOR_MANUAL = ['super_admin', 'support_technician']

# ---------- DB helpers ----------
def _q_one(sql, params):
    """Query one row (dict)"""
    with db.get_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()

def _q_all(sql, params):
    """Query all rows (list of dict)"""
    with db.get_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()

def _exec(sql, params):
    """Execute write and commit"""
    with db.get_cursor() as cur:
        cur.execute(sql, params)
        try:
            cur.connection.commit()
        except Exception:
            pass

def _fmt_hms_pair(sec: float | int | None) -> str:
    """ hh:mm:ss format; None -> '—'"""
    if sec is None:
        return "—"
    total = int(round(float(sec)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _format_elapsed(sec: int | None) -> str:
    """Format seconds as H:MM:SS or MM:SS; returns None '-'"""
    if sec is None:
        return "-"
    if sec < 0:
        sec = 0
    total = int(sec)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"

# ---------- Permission: Admin or event's volunteer/manager ----------
def _is_event_volunteer_or_admin(event_id: int, user_id: int) -> bool:
    """The platform administrator will approve it directly; otherwise, must be the volunteer or manager of the group（active）"""
    # Platform administrator
    if session.get('platform_role') in ALLOWED_PLATFORM_ROLES_FOR_MANUAL:
        return True

    # Volunteer/Manager (belongs to the event group and is active)
    with db.get_cursor() as cur:
        cur.execute("""
            SELECT 1
            FROM event_info e
            JOIN group_members gm ON gm.group_id = e.group_id
            WHERE e.event_id = %s
              AND gm.user_id = %s
              AND gm.status = 'active'
              AND gm.group_role IN ('volunteer','manager')
            LIMIT 1
        """, (event_id, user_id))
        return cur.fetchone() is not None

def _fetch_my_result_view_model(event_id: int, user_id: int):
    from datetime import date, datetime, timedelta

    with db.get_cursor() as cur:
        cur.execute("""
            SELECT
                e.event_id, e.event_title, e.event_date, e.event_time, e.location,
                g.name AS group_name, e.status AS event_status,

                em.membership_id, em.event_role, em.participation_status,

                rr.start_time, rr.finish_time, rr.method, rr.recorded_by, rr.recorded_at,
                TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time) AS elapsed_sec
            FROM event_info e
            JOIN group_info g   ON g.group_id   = e.group_id
            JOIN event_members em
                 ON em.event_id = e.event_id AND em.user_id = %s
            LEFT JOIN race_results rr
                 ON rr.membership_id = em.membership_id
            WHERE e.event_id = %s
            LIMIT 1
        """, (user_id, event_id))
        row = cur.fetchone()
        if not row:
            return {'exists': False}

        # —— End judgment (earlier than today; or today and the activity time has passed)——
        ev_time = row.get('event_time')
        if isinstance(ev_time, timedelta):
            secs = int(ev_time.total_seconds())
            ev_time = (datetime.min + timedelta(seconds=secs)).time()

        today = date.today()
        now_t = datetime.now().time()
        is_past = False
        if row['event_date']:
            if row['event_date'] < today:
                is_past = True
            elif row['event_date'] == today and ev_time and ev_time <= now_t:
                is_past = True

        # Grade field
        start_time  = row.get('start_time')
        finish_time = row.get('finish_time')
        elapsed_sec = row.get('elapsed_sec')

        has_start  = start_time  is not None
        has_finish = finish_time is not None
        # Valid results: both start/finish exist and finish > start
        is_valid   = bool(has_start and has_finish and (finish_time > start_time))

        # Page display allows: The activity has ended & there are valid results
        can_show = bool(is_past and is_valid)

        # Recorder
        recorder_name = None
        if row.get('recorded_by'):
            cur.execute("SELECT username FROM users WHERE user_id = %s", (row['recorded_by'],))
            u = cur.fetchone()
            recorder_name = (u or {}).get('username')

        # ===== Rank =====
        rank_overall = None
        total_finishers = None
        rank_in_group = None
        total_in_group = None

        if is_valid:
            # Total finishers (the activity's finish_time is not null)
            cur.execute("""
                SELECT COUNT(*) AS cnt
                FROM event_members em
                JOIN race_results rr ON rr.membership_id = em.membership_id
                WHERE em.event_id = %s
                  AND rr.finish_time IS NOT NULL
                  AND rr.start_time  IS NOT NULL
                  AND rr.finish_time > rr.start_time
            """, (event_id,))
            total_finishers = (cur.fetchone() or {}).get('cnt') or 0

            # My ranking = the number of people faster than me + 1
            cur.execute("""
                SELECT COUNT(*) AS faster
                FROM event_members em
                JOIN race_results rr ON rr.membership_id = em.membership_id
                WHERE em.event_id = %s
                  AND rr.finish_time IS NOT NULL
                  AND rr.start_time  IS NOT NULL
                  AND rr.finish_time > rr.start_time
                  AND TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time) < %s
            """, (event_id, elapsed_sec))
            faster = (cur.fetchone() or {}).get('faster') or 0
            rank_overall = faster + 1

            total_in_group = total_finishers
            rank_in_group = rank_overall

        group_name = row.get('group_name')

        # —— Strings that can be displayed directly in the template —— #
        def _fmt_dt(dt):
            try:
                return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None
            except Exception:
                return None

        start_time_str  = _fmt_dt(start_time)
        finish_time_str = _fmt_dt(finish_time)
        elapsed_str     = _format_elapsed(elapsed_sec) if is_valid and elapsed_sec is not None else "-"

        # ===== Assembly return =====
        return {
            'exists': True,
            'can_show_result': can_show,
            'event': {
                'id': row['event_id'],
                'title': row['event_title'],
                'date': row['event_date'],
                'time': row['event_time'],
                'location': row.get('location'),
                'group_name': group_name,
                'status': row.get('event_status'),
                'is_past': is_past,
            },
            'membership_id': row['membership_id'],
            'event_role': row.get('event_role'),
            'participation_status': row.get('participation_status'),
            'result': {
                'has_start': has_start,
                'has_finish': has_finish,
                'is_valid': is_valid,

                'start_time': start_time,
                'finish_time': finish_time,

                # Directly renderable strings (if you don't want to use filters)
                'start_time_str': start_time_str,
                'finish_time_str': finish_time_str,

                'elapsed_sec': elapsed_sec if is_valid else None,
                'elapsed_str': elapsed_str,

                'method': row.get('method'),
                'recorded_by': row.get('recorded_by'),
                'recorded_by_name': recorder_name,
                'recorded_at': row.get('recorded_at'),
            },
            'rank_overall': rank_overall,
            'total_finishers': total_finishers,
            'rank_in_group': rank_in_group,
            'total_in_group': total_in_group,
            'group_name': group_name,
        }


# ---------- Route ----------
@app.route('/events/<int:event_id>/finish/manual', methods=['GET','POST'])
@require_login
def record_finish_manual(event_id):
    """
    Manually record the finish time (without writing race_results.status):
    - Only membership_id is submitted
    - Use event_date + event_time as start_time (if event_time exists)
    - Use NOW() as finish_time
    - If the record exists, UPDATE; otherwise, INSERT (by membership_id)
    - Block POST when the event is in the future
    - GET renders participants list and event start time
    """
    from datetime import datetime, date, timedelta
    import traceback  

    # Allow: Platform administrators or volunteers/administrators of the activity group
    current_uid = int(get_current_user_id())
    if not _is_event_volunteer_or_admin(event_id, current_uid):
        flash('Permission denied: only Admin or event volunteers/managers can record results.', 'danger')
        return redirect(url_for('main_home'))

    # === Read the event date/time & whether the event has started ===
    evinfo = _q_one("SELECT event_date, event_time FROM event_info WHERE event_id=%s", (event_id,))
    if not evinfo or not evinfo.get('event_date'):
        flash('Event not found or event date missing.', 'danger')
        return redirect(url_for('manage_events'))

    event_date = evinfo['event_date']
    ev_time = evinfo.get('event_time')  # may be TIME or timedelta

    # normalize TIME field if it comes as timedelta
    if isinstance(ev_time, timedelta):
        secs = int(ev_time.total_seconds())
        ev_time = (datetime.min + timedelta(seconds=secs)).time()

    today = date.today()
    now_t = datetime.now().time()

    if event_date < today:
        has_started = True
    elif event_date == today:
        has_started = True if not ev_time else (ev_time <= now_t)
    else:
        has_started = False

    # ---- GET：Render form with participants list & event start time ----
    if request.method == 'GET':
        participants = _q_all("""
            SELECT 
              em.membership_id,
              COALESCE(NULLIF(CONCAT(u.first_name,' ',u.last_name), ' '), u.username) AS full_name
            FROM event_members em
            JOIN users u ON u.user_id = em.user_id
            WHERE em.event_id = %s
              AND em.event_role = 'participant'
              AND em.participation_status IN ('registered','attended')
            ORDER BY full_name
        """, (event_id,))

        recent_records = _q_all("""
            SELECT 
              rr.membership_id,
              COALESCE(NULLIF(CONCAT(u1.first_name,' ',u1.last_name), ' '), u1.username) AS full_name,
              DATE_FORMAT(rr.start_time, '%%d/%%m/%%Y %%H:%%i:%%s')  AS start_dt_fmt,
              DATE_FORMAT(rr.finish_time, '%%d/%%m/%%Y %%H:%%i:%%s') AS finish_dt_fmt,
              CASE 
                WHEN rr.start_time IS NOT NULL AND rr.finish_time IS NOT NULL
                THEN SEC_TO_TIME(TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time))
                ELSE NULL
              END AS elapsed_hms
            FROM race_results rr
            JOIN event_members em ON em.membership_id = rr.membership_id
            JOIN users u1 ON u1.user_id = em.user_id
            WHERE em.event_id = %s
            ORDER BY COALESCE(rr.recorded_at, rr.finish_time) DESC
            LIMIT 10
        """, (event_id,)) or []

        print(f"[record_finish_manual] event_id={event_id} recent_records_count={len(recent_records)}")

        # event_date → DD/MM/YYYY
        start_text = f"{event_date.strftime('%d/%m/%Y')} {ev_time.strftime('%H:%M:%S')}" if ev_time else f"{event_date.strftime('%d/%m/%Y')} (no start time)"

        # The front end is only available when it is "current day and has started" (it can be used to disable buttons/inputs in the template).
        allow_manual = (event_date == today and has_started)  

        return render_template(
            'record_finish.html',
            event_id=event_id,
            event_title=None,
            event_start_text=start_text,
            participants=participants,
            recent_records=recent_records,
            allow_manual=allow_manual,  # Optional, the template is used to disable input/buttons.
        )

    # ---- POST: receive only membership_id; use NOW() as finish_time ----
    if not has_started:
        when_txt = f"{event_date} {(ev_time.strftime('%H:%M:%S') if hasattr(ev_time,'strftime') else ev_time) or ''}".strip()
        flash(f"Event hasn't started yet ({when_txt}). Manual finish is disabled for future events.", 'warning')
        return redirect(url_for('record_finish_manual', event_id=event_id))

    # The day has passed ⇒ Manual (forced) CSV Import is strictly prohibited.
    if event_date < today:  # NEW
        flash("Manual finish entry is closed after the event day. Please use CSV Import to upload finish times.", "warning")
        return redirect(url_for('record_finish_manual', event_id=event_id))

    raw_mid = (request.form.get('membership_id') or '').strip()
    if not raw_mid.isdigit():
        flash('Please input a valid membership_id (integer).', 'warning')
        return redirect(url_for('record_finish_manual', event_id=event_id))
    membership_id = int(raw_mid)

    # Verify membership belongs to this event
    mem_row = _q_one("""
        SELECT membership_id, event_id
        FROM event_members
        WHERE membership_id=%s
    """, (membership_id,))
    if not mem_row:
        flash(f"Membership #{membership_id} does not exist.", 'danger')
        return redirect(url_for('record_finish_manual', event_id=event_id))
    if int(mem_row['event_id']) != int(event_id):
        flash(f"Membership #{membership_id} belongs to event_id={mem_row['event_id']}, not this event (event_id={event_id}).", 'danger')
        return redirect(url_for('record_finish_manual', event_id=event_id))

    # First check if race_results already has records
    rr = _q_one("""
        SELECT start_time, finish_time
        FROM race_results
        WHERE membership_id=%s
    """, (membership_id,))

    try:
        if rr:
            # already recorded
            if rr.get('finish_time'):
                ft = rr['finish_time'].strftime('%d/%m/%Y %H:%M:%S') if hasattr(rr['finish_time'], 'strftime') else rr['finish_time']
                flash(
                    f"The participant's score has been recorded (finish time: {ft}). Repeated entry is not allowed.",
                    "warning"
                )
                return redirect(url_for('record_finish_manual', event_id=event_id))
            else:
                # If there is no finish_time, fill it in
                if ev_time:
                    _exec("""
                        UPDATE race_results
                        SET start_time = COALESCE(start_time, CONCAT(%s,' ',%s)),
                            finish_time = NOW(),
                            method='manual',
                            recorded_by=%s,
                            recorded_at=NOW()
                        WHERE membership_id=%s
                    """, (event_date, ev_time, current_uid, membership_id))
                else:
                    _exec("""
                        UPDATE race_results
                        SET finish_time = NOW(),
                            method='manual',
                            recorded_by=%s,
                            recorded_at=NOW()
                        WHERE membership_id=%s
                    """, (current_uid, membership_id))
        else:
            # No record exists → Add
            if ev_time:
                _exec("""
                    INSERT INTO race_results
                        (membership_id, start_time, finish_time, method, recorded_by, recorded_at)
                    VALUES
                        (%s, CONCAT(%s,' ',%s), NOW(), 'manual', %s, NOW())
                """, (membership_id, event_date, ev_time, current_uid))
            else:
                _exec("""
                    INSERT INTO race_results
                        (membership_id, finish_time, method, recorded_by, recorded_at)
                    VALUES
                        (%s, NOW(), 'manual', %s, NOW())
                """, (membership_id, current_uid))

        # Update participation_status to 'attended' after recording race result
        _exec("""
            UPDATE event_members
            SET participation_status = 'attended'
            WHERE membership_id = %s
              AND event_role = 'participant'
              AND participation_status = 'registered'
        """, (membership_id,))

        # After success, prompt time
        rr2 = _q_one("""
            SELECT start_time, finish_time, TIMESTAMPDIFF(SECOND, start_time, finish_time) AS sec
            FROM race_results WHERE membership_id=%s
        """, (membership_id,))

        if rr2 and rr2.get('start_time') and rr2.get('finish_time') and rr2.get('sec') is not None:
            # Format using the retrieved seconds.
            elapsed_sec = int(rr2['sec'])
            hours = elapsed_sec // 3600
            minutes = (elapsed_sec % 3600) // 60
            seconds = elapsed_sec % 60
            elapsed_str = f"{hours:02}:{minutes:02}:{seconds:02}"
            flash(f"Finish recorded. Elapsed: {elapsed_str}", "success")
        else:
            flash("Finish recorded. Elapsed: N/A (missing start_time).", "success")

    except Exception as e:
        print("[record_finish_manual] ERROR:", repr(e)); traceback.print_exc()
        flash(f"Failed to record finish: {e}", "error")  

    return redirect(url_for('record_finish_manual', event_id=event_id))



# ---------- Helpers for "My Results" ----------
def _coerce_time(v):
    if v is None:
        return None
    if isinstance(v, time):
        return v
    if isinstance(v, timedelta):
        secs = int(v.total_seconds())
        return (datetime.min + timedelta(seconds=secs)).time()
    if isinstance(v, str):
        try:
            hh, mm, *rest = [int(p) for p in v.split(":")]
            ss = rest[0] if rest else 0
            return time(hh, mm, ss)
        except Exception:
            return None
    return None

def _is_event_in_future(ev_date, ev_time):
    """Today's future time or any future date -> future; past date or earlier today -> not future"""
    try:
        today = date.today()
        now_t = datetime.now().time()
        et = _coerce_time(ev_time)
        if ev_date is None:
            return False
        if ev_date > today:
            return True
        if ev_date < today:
            return False
        # same day
        if et is None:
            # No specific time: treat as future until end of day
            return True
        return et > now_t
    except Exception:
        return False

# ---------- My Results list ----------
@app.route('/my/results')
@require_login
def my_results():
    """My Race Record List: strictly use valid race_results to decide the Finished badge"""
    uid = int(get_current_user_id())

    rows = _q_all("""
        SELECT
            e.event_id,
            e.event_title,
            e.event_date,
            e.event_time,
            e.location,
            COALESCE(g.name, '') AS group_name,
            em.membership_id,

            -- valid result exists?
            EXISTS(
                SELECT 1 FROM race_results rr
                WHERE rr.membership_id = em.membership_id
                  AND rr.start_time  IS NOT NULL
                  AND rr.finish_time IS NOT NULL
                  AND rr.finish_time > rr.start_time
                  AND rr.finish_time <= NOW()
            ) AS has_valid_result,

            -- elapsed seconds of the latest valid result
            (
              SELECT TIMESTAMPDIFF(SECOND, rr2.start_time, rr2.finish_time)
              FROM race_results rr2
              WHERE rr2.membership_id = em.membership_id
                AND rr2.start_time  IS NOT NULL
                AND rr2.finish_time IS NOT NULL
                AND rr2.finish_time > rr2.start_time
                AND rr2.finish_time <= NOW()
              ORDER BY rr2.finish_time DESC
              LIMIT 1
            ) AS elapsed_sec

        FROM event_members em
        JOIN event_info   e  ON e.event_id   = em.event_id
        LEFT JOIN group_info g ON g.group_id = e.group_id
        WHERE em.user_id = %s
        ORDER BY e.event_date ASC, e.event_time ASC, e.event_id ASC
    """, [uid])

    def fmt(sec):
        if sec is None: return None
        sec = int(sec)
        return f"{sec//3600:01d}:{(sec%3600)//60:02d}:{sec%60:02d}"

    for r in rows:
        r["is_future"] = _is_event_in_future(r.get("event_date"), r.get("event_time"))
        r["elapsed_hms"] = fmt(r.get("elapsed_sec"))
        if r.get("has_valid_result"):
            r["status_badge"] = ("Finished", "success")
        else:
            if r["is_future"]:
                r["status_badge"] = ("Upcoming", "secondary")
            else:
                r["status_badge"] = ("Pending", "warning")

    return render_template("my_results.html", rows=rows)

# ---------- My Result detail ----------
@app.route('/my/results/<int:event_id>')
@require_login
def my_result_detail(event_id: int):
    """Participants view their performance details for the event"""
    user_id = int(get_current_user_id())
    data = _fetch_my_result_view_model(event_id, user_id)
    if not data['exists']:
        abort(403)
    return render_template('event_result.html', data=data, event_id=event_id)


# ---------------- CSV Import (NEW) ----------------

REQUIRED_HEADERS = {'participation_id', 'id'}
OPTIONAL_HEADERS = {'start_time', 'end_time', 'finish_time'}

def _get_event_date(event_id: int):
    """Fetch event_date for combining with time-only values"""
    row = _q_one("SELECT event_date FROM event_info WHERE event_id=%s", (event_id,))
    return row.get('event_date') if row else None

def _parse_to_datetime(s: str | None, event_date: date | None):
    """
    Supports:
    - YYYY-MM-DD HH:MM[:SS] / YYYY/MM/DD HH:MM[:SS] (month/day/hour, 1-2 digits)
    - Chinese date: YYYY year MM month DD day HH:MM[:SS]
    - Time only: HH:MM[:SS] (combined with event_date)
    - Excel/WPS sequence: 45623 or 45623.375
    Input "sanitization":
    - Full-width slashes and colons: Convert to half-width characters
    - Various whitespace types (NBSP, thin spaces, full-width spaces, etc.): Convert to standard spaces
    """
    s_raw = (s or "").strip()
    if not s_raw:
        return None

    # 0) Excel/WPS serial value (pure number/decimal)
    if re.fullmatch(r"\d+(\.\d+)?", s_raw):
        try:
            serial = float(s_raw)
            base = datetime(1899, 12, 30)  
            days = int(serial)
            frac = serial - days
            return base + timedelta(days=days, seconds=round(frac * 86400))
        except Exception:
            pass

    repl = (
        s_raw
        .replace('：', ':')
        .replace('／', '/')
        .replace('年', '-').replace('月', '-').replace('日', '')
        .replace('T', ' ')
    )

    repl = repl.replace('\u00A0', ' ').replace('\u3000', ' ').replace('\u2009', ' ')
    repl = repl.replace('\u202F', ' ').replace('\u2002', ' ').replace('\u2003', ' ')
    repl = re.sub(r'\s+', ' ', repl).strip()

    norm = repl.replace('/', '-')

    # Pattern A: YYYY-MM-DD HH:MM[:SS]
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$', norm)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hh, mm, ss = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
            return datetime(y, mo, d, hh, mm, ss)
        except Exception:
            return None

    # Pattern B: DD-MM-YYYY HH:MM[:SS] (common in CSVs like 17/09/2025 10:38)
    m_b = re.match(r'^(\d{1,2})-(\d{1,2})-(\d{4})\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$', norm)
    if m_b:
        try:
            d, mo, y = int(m_b.group(1)), int(m_b.group(2)), int(m_b.group(3))
            hh, mm, ss = int(m_b.group(4)), int(m_b.group(5)), int(m_b.group(6) or 0)
            return datetime(y, mo, d, hh, mm, ss)
        except Exception:
            return None

    m2 = re.match(r'^(\d{1,2}):(\d{2})(?::(\d{1,2}))?$', repl)
    if m2 and event_date:
        try:
            hh, mm, ss = int(m2.group(1)), int(m2.group(2)), int(m2.group(3) or 0)
            return datetime(event_date.year, event_date.month, event_date.day, hh, mm, ss)
        except Exception:
            return None

    return None



@app.route('/events/<int:event_id>/results/import', methods=['GET'])
@require_login
def results_import_form(event_id):
    uid = int(get_current_user_id())
    if not _is_event_volunteer_or_admin(event_id, uid):
        abort(403)
    
    # Get event info for display and check if event is completed
    event_info = None
    is_event_completed = False
    has_existing_results = False
    try:
        row = _q_one("SELECT event_title, event_date FROM event_info WHERE event_id=%s", (event_id,))
        if row:
            event_info = row
            # Check if event date has passed
            event_date = row.get('event_date')
            if event_date:
                from datetime import date
                today = date.today()
                if isinstance(event_date, date):
                    is_event_completed = event_date <= today
                else:
                    # If event_date is datetime, compare dates only
                    event_date_only = event_date.date() if hasattr(event_date, 'date') else event_date
                    is_event_completed = event_date_only <= today
        # Check if event already has any results (robust COUNT-based check)
        _row_exist = _q_one(
            """
            SELECT COUNT(1) AS cnt
            FROM race_results rr
            JOIN event_members em ON em.membership_id = rr.membership_id
            WHERE em.event_id = %s
            """,
            (event_id,)
        )
        has_existing_results = bool(_row_exist and int(_row_exist.get('cnt', 0)) > 0)
    except Exception:
        pass
    
    return render_template('results_import.html', event_id=event_id, event_info=event_info, is_event_completed=is_event_completed, has_existing_results=has_existing_results)

@app.route('/events/<int:event_id>/results/import', methods=['POST'])
@require_login
def results_import_post(event_id):
    uid = int(get_current_user_id())
    if not _is_event_volunteer_or_admin(event_id, uid):
        abort(403)

    update_policy = request.form.get('update_policy', 'skip')  # 'skip' | 'overwrite'
    validate_only = request.form.get('validate_only') == '1'  # '1' if checked
    file = request.files.get('csv_file')
    if not file or file.filename == '':
        flash('Please choose a CSV file.', 'warning')
        return redirect(url_for('results_import_form', event_id=event_id))

    event_date = _get_event_date(event_id)
    if not event_date:
        flash('Event date not found. Cannot parse time-only values.', 'danger')
        return redirect(url_for('results_import_form', event_id=event_id))

    stream = TextIOWrapper(file.stream, encoding='utf-8', errors='replace', newline='')
    reader = csv.DictReader(stream)

    # headers
    headers = {h.strip().lower() for h in (reader.fieldnames or [])}
    
    # Check if at least one of the required ID columns is present
    has_participation_id = 'participation_id' in headers or 'id' in headers
    if not has_participation_id:
        flash(f'Missing required header: must include either "id" or "participation_id"', 'danger')
        return redirect(url_for('results_import_form', event_id=event_id))
    
    allowed = REQUIRED_HEADERS | OPTIONAL_HEADERS | {'username'}  # username is allowed but not required
    unknown = headers - allowed
    if unknown:
        flash(f'Unknown headers ignored: {", ".join(sorted(unknown))}', 'info')

    report_rows = []
    seen_in_file = set()
    success_count = 0
    fail_count = 0

    sql_select_exist = "SELECT 1 FROM race_results WHERE membership_id=%s LIMIT 1"
    sql_insert = """
        INSERT INTO race_results (membership_id, start_time, finish_time, method, recorded_by)
        VALUES (%s, %s, %s, 'manual', %s)
    """
    sql_update = """
        UPDATE race_results
        SET start_time=%s, finish_time=%s, method='manual', recorded_by=%s
        WHERE membership_id=%s
    """

    def _fmt_hms(total_sec: int) -> str:
        try:
            total_sec = int(total_sec)
        except Exception:
            return "00:00:00"
        h = total_sec // 3600
        m = (total_sec % 3600) // 60
        s = total_sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    with db.get_cursor() as cur:
        line_no = 1
        for row in reader:
            line_no += 1
            # Support both 'participation_id' and 'id' column names
            pid_raw = (row.get('participation_id') or row.get('id') or '').strip()
            st_raw  = (row.get('start_time') or '').strip()
            et_raw  = (row.get('end_time') or row.get('finish_time') or '').strip()

            # participation_id
            if not pid_raw.isdigit() or int(pid_raw) <= 0:
                fail_count += 1
                report_rows.append({
                    'line': line_no, 'participation_id': pid_raw,
                    'status': 'failed', 'message': 'participation_id must be a positive integer'
                })
                continue
            membership_id = int(pid_raw)

            # duplicate in CSV
            if membership_id in seen_in_file:
                fail_count += 1
                report_rows.append({
                    'line': line_no, 'participation_id': membership_id,
                    'status': 'failed', 'message': 'Duplicate participation_id in CSV'
                })
                continue
            seen_in_file.add(membership_id)

            # NEW: First do the existence + belonging activity check to avoid foreign key errors
            cur.execute("""
                SELECT membership_id, event_id
                FROM event_members
                WHERE membership_id = %s
                LIMIT 1
            """, (membership_id,))
            mem = cur.fetchone()
            if not mem:
                fail_count += 1
                report_rows.append({
                    'line': line_no, 'participation_id': membership_id,
                    'status': 'failed', 'message': 'membership_id not found in event_members'
                })
                continue
            if int(mem['event_id']) != int(event_id):
                fail_count += 1
                report_rows.append({
                    'line': line_no, 'participation_id': membership_id,
                    'status': 'failed',
                    'message': f'membership_id belongs to event_id={mem["event_id"]}, not current event_id={event_id}'
                })
                continue

            # both times required to be a "success"
            start_dt = _parse_to_datetime(st_raw, event_date) if st_raw else None
            end_dt   = _parse_to_datetime(et_raw, event_date) if et_raw else None
            if not start_dt or not end_dt:
                fail_count += 1
                report_rows.append({
                    'line': line_no, 'participation_id': membership_id,
                    'status': 'failed',
                    'message': 'start_time and end_time must both be valid (HH:MM[:SS] or YYYY/MM/DD HH:MM[:SS])'
                })
                continue

            elapsed_sec = int((end_dt - start_dt).total_seconds())
            if elapsed_sec <= 0:
                fail_count += 1
                report_rows.append({
                    'line': line_no, 'participation_id': membership_id,
                    'status': 'failed', 'message': 'Non-positive duration (end_time must be after start_time)'
                })
                continue

            # exists?
            cur.execute(sql_select_exist, (membership_id,))
            exists = cur.fetchone() is not None

            try:
                if validate_only:
                    # Validation only mode - simulate what would happen
                    if exists:
                        if update_policy == 'overwrite':
                            report_rows.append({
                                'line': line_no, 'participation_id': membership_id,
                                'status': 'would_update', 'message': f"Would update (elapsed time={_fmt_hms(elapsed_sec)})"
                            })
                        else:
                            report_rows.append({
                                'line': line_no, 'participation_id': membership_id,
                                'status': 'would_skip', 'message': 'Existing result found; would skip per policy'
                            })
                    else:
                        report_rows.append({
                            'line': line_no, 'participation_id': membership_id,
                            'status': 'would_insert', 'message': f"Would insert (elapsed time={_fmt_hms(elapsed_sec)})"
                        })
                    success_count += 1
                else:
                    # Normal import mode - actually write to database
                    if exists:
                        if update_policy == 'overwrite':
                            cur.execute(sql_update, (start_dt, end_dt, uid, membership_id))
                            report_rows.append({
                                'line': line_no, 'participation_id': membership_id,
                                'status': 'updated', 'message': f"Updated (elapsed time={_fmt_hms(elapsed_sec)})"
                            })
                            success_count += 1
                        else:
                            report_rows.append({
                                'line': line_no, 'participation_id': membership_id,
                                'status': 'skipped', 'message': 'Existing result found; skipped per policy'
                            })
                    else:
                        cur.execute(sql_insert, (membership_id, start_dt, end_dt, uid))
                        report_rows.append({
                            'line': line_no, 'participation_id': membership_id,
                            'status': 'inserted', 'message': f"Inserted (elapsed time={_fmt_hms(elapsed_sec)})"
                        })
                        success_count += 1
            except Exception as e:
                fail_count += 1
                report_rows.append({
                    'line': line_no, 'participation_id': membership_id,
                    'status': 'failed', 'message': f'DB error: {e}'
                })

        # Only commit if not in validation mode
        if not validate_only:
            try:
                cur.connection.commit()
            except Exception:
                pass

    summary = {'total': success_count + fail_count, 'success_count': success_count, 'fail_count': fail_count}
    event_info = None
    try:
        row = _q_one("SELECT event_title, event_date FROM event_info WHERE event_id=%s", (event_id,))
        if row:
            event_info = row
    except Exception:
        pass
    
    # Show appropriate message based on mode
    if validate_only and success_count > 0:
        flash(f'✓ Validation passed for {success_count} row(s). No data was imported.', 'success')
    elif not validate_only and success_count > 0:
        flash(f'✓ Successfully imported {success_count} row(s)!', 'success')
    elif fail_count > 0:
        flash(f'⚠ Found {fail_count} error(s). Please check the report below.', 'warning')
    
    # Recompute flags for rendering page after POST
    is_event_completed = False
    has_existing_results = False
    try:
        info_row = _q_one("SELECT event_date FROM event_info WHERE event_id=%s", (event_id,))
        if info_row:
            from datetime import date as _d
            evd = info_row.get('event_date')
            if evd:
                evd_only = evd.date() if hasattr(evd, 'date') else evd
                is_event_completed = evd_only <= _d.today()
        _row_exist2 = _q_one(
            """
            SELECT COUNT(1) AS cnt
            FROM race_results rr
            JOIN event_members em ON em.membership_id = rr.membership_id
            WHERE em.event_id = %s
            """,
            (event_id,)
        )
        has_existing_results = bool(_row_exist2 and int(_row_exist2.get('cnt', 0)) > 0)
    except Exception:
        pass

    return render_template('results_import.html',
                          event_id=event_id,
                          event_info=event_info,
                          summary=summary,
                          report_rows=report_rows,
                          update_policy=update_policy,
                          validate_only=validate_only,
                          is_event_completed=is_event_completed,
                          has_existing_results=has_existing_results)


@app.route('/results/import/template.csv')
@require_login
def results_import_template():
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['participation_id', 'start_time', 'end_time'])
    writer.writerow([12345, '00:25:13', '00:50:02'])                         # time-only -> combined with event_date
    writer.writerow([23456, '2025-11-02 09:00:00', '2025-11-02 09:42:33'])   # full datetime
    data = output.getvalue()
    output.close()
    return Response(data, mimetype='text/csv',
                    headers={'Content-Disposition':'attachment; filename="results_import_template.csv"'})

# ============ Cross-Event Comparison ============

def _parse_date_str(s: str | None):
    """Fault-tolerant YYYY/MM/DD parsing; None/empty string returns None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def _parse_event_ids(csv: str | None):
    """
    Parse comma-separated event_id: '1, 5,9' -> [1,5,9]
    (Note: Multiple-select drop-down submission will use ?event_id=1&event_id=5, which we handle with getlist in compare_view)
    """
    ids = []
    if not csv:
        return ids
    for part in csv.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids

@app.route('/events/<int:event_id>/results/template.csv')
@require_login
def results_template_for_event(event_id):
    """
    Export the current event's list of participants：
    - Only include event_members.role='participant' and status in the allowed set.
    - If grades are available, start_time/finish_time are also exported (full DATETIME format).
    - An additional participant column (name/username) is included for easier identification; this is ignored during import.
    """
    uid = int(get_current_user_id())
    if not _is_event_volunteer_or_admin(event_id, uid):
        abort(403)

    # Allows exported entry status (same as when you import/manually register)
    allowed_status = ('registered', 'attended')

    # Get event_date so that it can be converted to time-only format if needed (here we directly output the full DATETIME for best compatibility)
    evrow = _q_one("SELECT event_date FROM event_info WHERE event_id=%s", (event_id,))
    event_date = evrow.get('event_date') if evrow else None

    rows = _q_all("""
        SELECT
          em.membership_id,
          u.username,
          rr.start_time,
          rr.finish_time
        FROM event_members em
        JOIN users u ON u.user_id = em.user_id
        LEFT JOIN race_results rr ON rr.membership_id = em.membership_id
        WHERE em.event_id = %s
          AND em.event_role = 'participant'
          AND em.participation_status IN %s
        ORDER BY em.membership_id ASC
    """, (event_id, allowed_status))

    def _fmt_dt(dt):
        # Export as a complete DATETIME string; if empty, return an empty string
        try:
            return dt.strftime("%Y/%m/%d %H:%M:%S") if dt else ""
        except Exception:
            return str(dt or "")

    output = StringIO()
    writer = csv.writer(output)

    # New format: id, username, start_time, finish_time
    writer.writerow(['id', 'username', 'start_time', 'finish_time'])

    for r in rows:
        writer.writerow([
            r['membership_id'],
            r['username'] or '',
            _fmt_dt(r.get('start_time')),
            _fmt_dt(r.get('finish_time'))
        ])

    data = output.getvalue()
    output.close()
    
    # Generate safe filename
    event_name = ""
    try:
        ev_info = _q_one("SELECT event_title FROM event_info WHERE event_id=%s", (event_id,))
        if ev_info and ev_info.get('event_title'):
            event_name = ev_info['event_title'][:30].replace(" ", "_")
    except Exception:
        pass
    
    if event_name:
        filename = f"event_{event_id}_{event_name}_template.csv"
    else:
        filename = f"event_{event_id}_template.csv"
    return Response(
        data,
        mimetype='text/csv; charset=utf-8',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': 'text/csv; charset=utf-8'
        }
    )


@app.route('/compare', methods=['GET'])
@require_login
def compare_view():
    """
    Cross-Event comparison
    Filters (GET):
      - event_id (multi): ?event_id=1&event_id=5... ; compatible with comma strings ?event_id=1,5,9
      - date_from / date_to : YYYY/MM/DD
      - location   : 'All Locations' or specific location
      - event_type : 'All' or concrete type
    Only valid scores are counted: rr.start_time & rr.finish_time are not empty, and finish_time > start_time, and finish_time <= NOW()
    """
    uid = int(get_current_user_id())

    # ---------- Event IDs: Prioritize reading the multi-select drop-down ----------
    event_ids_raw = request.args.getlist('event_id')
    if event_ids_raw:
        event_ids = [int(x) for x in event_ids_raw if str(x).isdigit()]
        event_ids_csv = ",".join(map(str, event_ids))  
    else:
        # Compatible with comma format
        event_ids_csv = (request.args.get('event_id') or '').strip()
        event_ids = _parse_event_ids(event_ids_csv)

    # ---------- Date ----------
    date_from = _parse_date_str(request.args.get('date_from'))
    date_to   = _parse_date_str(request.args.get('date_to'))

    # ---------- Other filters ----------
    location   = (request.args.get('location') or '').strip() or None
    event_type = (request.args.get('event_type') or '').strip() or None

    # ---------- Personal record (current user), only valid results are taken ----------
    conds = [
        "em.user_id = %s",
        "rr.start_time IS NOT NULL",
        "rr.finish_time IS NOT NULL",
        "rr.finish_time > rr.start_time",
        "rr.finish_time <= NOW()"
    ]
    params = [uid]

    if event_ids:
        conds.append("em.event_id IN (" + ",".join(["%s"] * len(event_ids)) + ")")
        params += event_ids
    if date_from:
        conds.append("e.event_date >= %s"); params.append(date_from)
    if date_to:
        conds.append("e.event_date <= %s"); params.append(date_to)
    if event_type and event_type != "All":
        conds.append("e.event_type = %s"); params.append(event_type)
    if location and location != "All Locations":
        conds.append("e.location = %s"); params.append(location)

    where_user = " AND ".join(conds)
    rows_user = _q_all(f"""
    SELECT
      e.event_id,
      e.event_title,
      e.event_date,
      e.event_time,
      e.location,
      e.event_type,
      COALESCE(g.name, '') AS group_name,
      TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time) AS finish_seconds,

      /* My ranking: Number of people faster than me + 1 */
      (
        SELECT COUNT(*)
        FROM event_members em3
        JOIN race_results rr3 ON rr3.membership_id = em3.membership_id
        WHERE em3.event_id = e.event_id
          AND rr3.start_time  IS NOT NULL
          AND rr3.finish_time IS NOT NULL
          AND rr3.finish_time > rr3.start_time
          AND rr3.finish_time <= NOW()
          AND TIMESTAMPDIFF(SECOND, rr3.start_time, rr3.finish_time)
                < TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time)
      ) + 1 AS my_rank,

      /* Total number of finishers */
      (
        SELECT COUNT(*)
        FROM event_members em4
        JOIN race_results rr4 ON rr4.membership_id = em4.membership_id
        WHERE em4.event_id = e.event_id
          AND rr4.start_time  IS NOT NULL
          AND rr4.finish_time IS NOT NULL
          AND rr4.finish_time > rr4.start_time
          AND rr4.finish_time <= NOW()
      ) AS total_finishers

    FROM event_members em
    JOIN event_info   e  ON e.event_id   = em.event_id
    LEFT JOIN group_info g ON g.group_id = e.group_id
    JOIN users        u  ON u.user_id    = em.user_id
    JOIN race_results rr ON rr.membership_id = em.membership_id
    WHERE {where_user}
    ORDER BY e.event_date ASC, e.event_time ASC, e.event_id ASC
""", params)

    # ---------- Average of all users (regardless of user), using the same filters & only counting valid scores ----------
    conds_avg, params_avg = [
        "rr.start_time IS NOT NULL",
        "rr.finish_time IS NOT NULL",
        "rr.finish_time > rr.start_time",
        "rr.finish_time <= NOW()"
    ], []

    if event_ids:
        conds_avg.append("em.event_id IN (" + ",".join(["%s"] * len(event_ids)) + ")")
        params_avg += event_ids
    if date_from:
        conds_avg.append("e.event_date >= %s"); params_avg.append(date_from)
    if date_to:
        conds_avg.append("e.event_date <= %s"); params_avg.append(date_to)
    if event_type and event_type != "All":
        conds_avg.append("e.event_type = %s"); params_avg.append(event_type)
    if location and location != "All Locations":
        conds_avg.append("e.location = %s"); params_avg.append(location)

    where_avg = " AND ".join(conds_avg)
    rows_avg = _q_all(f"""
        SELECT
          e.event_id,
          e.event_title,
          e.event_date,
          AVG(TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time)) AS avg_finish,
          COUNT(*) AS cnt
        FROM event_members em
        JOIN event_info   e  ON e.event_id   = em.event_id
        JOIN users        u  ON u.user_id    = em.user_id
        JOIN race_results rr ON rr.membership_id = em.membership_id
        WHERE {where_avg}
        GROUP BY e.event_id, e.event_title, e.event_date
    """, params_avg)

    # ---------- Highlights ----------
    my_secs = [r['finish_seconds'] for r in rows_user if r.get('finish_seconds') is not None]
    pb = min(my_secs) if my_secs else None

    # Overall trend improvement: average of the first half vs. average of the second half (query sorted by date in ascending order)
    pct_improve = None
    n = len(my_secs)
    if n >= 2:
        mid = n // 2                         
        avg_first_half  = sum(my_secs[:mid]) / mid if mid > 0 else None
        avg_second_half = sum(my_secs[mid:]) / (n - mid) if (n - mid) > 0 else None
        if avg_first_half and avg_first_half > 0 and avg_second_half is not None:
            pct_improve = round((avg_first_half - avg_second_half) / avg_first_half * 100, 1)

    avg_map = {r['event_id']: r for r in rows_avg}
    diffs = []
    for r in rows_user:
        ev_id = r['event_id']
        me = r.get('finish_seconds')
        avg_row = avg_map.get(ev_id)
        if me is not None and avg_row and avg_row.get('avg_finish') is not None:
            diffs.append(me - avg_row['avg_finish'])
    avg_diff = round(sum(diffs)/len(diffs), 1) if diffs else None

    # ---------- Drop-down data source: only list "activities for which the current user has [valid scores] under the current filter" ----------
    conds_list = [
        "em.user_id = %s",
        "rr.start_time IS NOT NULL",
        "rr.finish_time IS NOT NULL",
        "rr.finish_time > rr.start_time",
        "rr.finish_time <= NOW()"
    ]
    params_list = [uid]

    # Date/Location/Type
    if date_from:
        conds_list.append("e.event_date >= %s"); params_list.append(date_from)
    if date_to:
        conds_list.append("e.event_date <= %s"); params_list.append(date_to)
    if event_type and event_type != "All":
        conds_list.append("e.event_type = %s"); params_list.append(event_type)
    if location and location != "All Locations":
        conds_list.append("e.location = %s"); params_list.append(location)

    where_list = " AND ".join(conds_list)
    event_list = _q_all(f"""
        SELECT DISTINCT e.event_id, e.event_title, e.event_date
        FROM event_members em
        JOIN event_info   e  ON e.event_id = em.event_id
        JOIN race_results rr ON rr.membership_id = em.membership_id
        WHERE {where_list}
        ORDER BY e.event_date DESC
    """, params_list)

    # ---------- Location/Type options: dynamic fallback if constants are not available ----------
    if AVAILABLE_LOCATIONS is None:
        locs = [r['location'] for r in _q_all(
            "SELECT DISTINCT location FROM event_info WHERE location IS NOT NULL AND location<>'' ORDER BY location", []
        )]
    else:
        locs = AVAILABLE_LOCATIONS

    if AVAILABLE_EVENT_TYPES is None:
        types = [r['event_type'] for r in _q_all(
            "SELECT DISTINCT event_type FROM event_info WHERE event_type IS NOT NULL AND event_type<>'' ORDER BY event_type", []
        )]
    else:
        types = AVAILABLE_EVENT_TYPES

    # Prepare hh:mm:ss display string for the table
    for r in rows_user:
        r['finish_hms_pair'] = _fmt_hms_pair(r.get('finish_seconds'))

        avg_row = avg_map.get(r['event_id'])
        if avg_row and avg_row.get('avg_finish') is not None:
            avg_val = avg_row['avg_finish']
            r['avg_hms_pair'] = _fmt_hms_pair(avg_val)

            diff_sec = None
            if r.get('finish_seconds') is not None:
                diff_sec = float(r['finish_seconds']) - float(avg_val)

            if diff_sec is None:
                r['diff_hms_pair'] = "—"
            else:
                diff_abs = abs(diff_sec)
                # Format as hh:mm:ss without sign (we'll use fast/slow in the template)
                total = int(round(diff_abs))
                h, rem = divmod(total, 3600)
                m, s = divmod(rem, 60)
                r['diff_hms_pair'] = f"{h:02d}:{m:02d}:{s:02d}"
                r['diff_seconds'] = diff_sec  # Store original signed value for color coding
        else:
            r['avg_hms_pair']  = "—"
            r['diff_hms_pair'] = "—"

    # ---------- rendering ----------
    return render_template(
        'compare.html',
        user_rows=rows_user,
        avg_rows=rows_avg,
        pb=pb,
        pct_improve=pct_improve,
        avg_diff=avg_diff,

        # Filter echo
        event_ids_csv=event_ids_csv,
        selected_event_ids=[str(x) for x in event_ids],  
        date_from=date_from,
        date_to=date_to,
        selected_location=(location or "All Locations"),
        selected_event_type=(event_type or "All"),

        # Drop-down options (only includes activities for which I have valid scores)
        event_list=event_list,
        locations=locs,
        event_types=types,
    )

# === per-event full results JSON ===
@app.route('/events/<int:event_id>/results/all.json')
@require_login
def event_results_all_json(event_id: int):
    rows = _q_all("""
        SELECT em.membership_id,
               COALESCE(NULLIF(CONCAT(u.first_name,' ',u.last_name), ' '), u.username) AS username,
               rr.start_time,
               rr.finish_time,
               TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time) AS elapsed_sec,
               TIME_FORMAT(SEC_TO_TIME(TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time)), '%i:%s') AS elapsed_mmss
        FROM event_members em
        JOIN users u         ON u.user_id = em.user_id
        JOIN race_results rr ON rr.membership_id = em.membership_id
        WHERE em.event_id = %s
          AND rr.start_time  IS NOT NULL
          AND rr.finish_time IS NOT NULL
          AND rr.finish_time > rr.start_time
          AND rr.finish_time <= NOW()
    """, [event_id])

    vals = [r['elapsed_sec'] for r in rows if r.get('elapsed_sec') is not None]
    avg_finish = round(sum(vals)/len(vals), 1) if vals else None

    sorted_rows = sorted([r for r in rows if r.get('elapsed_sec') is not None],
                         key=lambda x: x['elapsed_sec'])
    rank = 0
    prev = None
    for i, r in enumerate(sorted_rows, start=1):
        if prev is None or r['elapsed_sec'] != prev:
            rank = i
            prev = r['elapsed_sec']
        r['rank'] = rank

    return jsonify({'avg_finish': avg_finish, 'items': sorted_rows})


@app.route('/events/<int:event_id>/results/all')
@require_login
def view_event_results(event_id: int):
    """Display all valid results of an activity (user name, start and end time, score, ranking, average, best score)"""

    # 1) Event info 
    ev = _q_one(
        "SELECT event_title, event_date, group_id FROM event_info WHERE event_id=%s",
        [event_id]
    ) or {}

    if not ev:
        abort(404)

    # ===== Access control begins =====
    user_id = get_current_user_id()
    role = get_current_platform_role()

    allowed = False

    # A) super Admin/technician：pass
    if role in ('super_admin', 'support_technician'):
        allowed = True
    else:
        # B) The manager of the group to which the activity belongs: Release
        is_group_manager = _q_one("""
            SELECT 1
            FROM group_members
            WHERE group_id = %s
              AND user_id  = %s
              AND group_role = 'manager'
            LIMIT 1
        """, (ev.get('group_id'), user_id))

        if is_group_manager:
            allowed = True
        else:
            # C) Only participants of this event (registered or present) can view it
            participated = _q_one("""
                SELECT 1
                FROM event_members
                WHERE event_id = %s
                  AND user_id  = %s
                  AND event_role = 'participant'
                  AND participation_status IN ('registered','attended')
                LIMIT 1
            """, (event_id, user_id))

            if participated:
                allowed = True

    if not allowed:
        # Jump to the unauthorized page
        return render_template("access_denied.html",
                               message="You must be a participant of this event or a manager of its group to view the results."), 403
    # ===== Access control ends =====

    # 2) Query score data
    rows = _q_all("""
        SELECT em.membership_id,
               COALESCE(NULLIF(CONCAT(u.first_name,' ',u.last_name), ' '), u.username) AS username,
               rr.start_time,
               rr.finish_time,
               TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time) AS elapsed_sec
        FROM event_members em
        JOIN users u         ON u.user_id = em.user_id
        JOIN race_results rr ON rr.membership_id = em.membership_id
        WHERE em.event_id = %s
          AND rr.start_time  IS NOT NULL
          AND rr.finish_time IS NOT NULL
          AND rr.finish_time > rr.start_time
          AND rr.finish_time <= NOW()
    """, [event_id])

    # 3) average value
    vals = [r['elapsed_sec'] for r in rows if r.get('elapsed_sec') is not None]
    avg_finish_sec = int(sum(vals) / len(vals)) if vals else None
    if avg_finish_sec is not None:
        h, rem = divmod(avg_finish_sec, 3600)
        m, s = divmod(rem, 60)
        avg_finish_display = f"{h:02d}:{m:02d}:{s:02d}"
    else:
        avg_finish_display = None

    # 4) Ranking and display fields
    ranked = sorted(
        [r for r in rows if r.get('elapsed_sec') is not None],
        key=lambda x: x['elapsed_sec']
    )

    rank = 0
    prev = None
    best_user = None
    best_sec = None
    best_display = None

    for i, r in enumerate(ranked, start=1):
        if prev is None or r['elapsed_sec'] != prev:
            rank = i
            prev = r['elapsed_sec']
        r['rank'] = rank

        total = int(r['elapsed_sec'])
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        r['elapsed_mmss'] = f"{h:02d}:{m:02d}:{s:02d}"
        r['elapsed_display'] = f"{h:02d}:{m:02d}:{s:02d}"

        if best_sec is None or total < best_sec:
            best_sec = total
            best_user = r['username']
            best_display = r['elapsed_display']

    return render_template(
        'event_all_results.html',
        event_id=event_id,
        event_title=ev.get('event_title'),
        event_date=ev.get('event_date'),
        avg_finish=avg_finish_display,
        best_user=best_user,
        best_time=best_display,
        rows=ranked
    )


@app.route('/events/<int:event_id>/results/export.csv')
@require_login
def export_event_results_csv(event_id: int):
    """Export event results as CSV"""
    rows = _q_all("""
        SELECT em.membership_id,
               COALESCE(NULLIF(CONCAT(u.first_name,' ',u.last_name), ' '), u.username) AS username,
               rr.start_time,
               rr.finish_time,
               TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time) AS elapsed_sec
        FROM event_members em
        JOIN users u         ON u.user_id = em.user_id
        JOIN race_results rr ON rr.membership_id = em.membership_id
        WHERE em.event_id = %s
          AND rr.start_time  IS NOT NULL
          AND rr.finish_time IS NOT NULL
          AND rr.finish_time > rr.start_time
          AND rr.finish_time <= NOW()
        ORDER BY elapsed_sec ASC
    """, [event_id])

    return _generate_results_csv(rows, event_id)


@app.route('/events/<int:event_id>/results/export.pdf')
@require_login
def export_event_results_pdf(event_id: int):
    """Export event results as PDF (placeholder for now)"""
    flash('PDF export feature is coming soon. Please use CSV export for now.', 'info')
    return redirect(url_for('view_event_results', event_id=event_id))


@app.route('/events/<int:event_id>/results/import/export')
@require_login
def export_import_results(event_id: int):
    """Export event results (for import page)"""
    rows = _q_all("""
        SELECT em.membership_id,
               COALESCE(NULLIF(CONCAT(u.first_name,' ',u.last_name), ' '), u.username) AS username,
               rr.start_time,
               rr.finish_time,
               TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time) AS elapsed_sec
        FROM event_members em
        JOIN users u ON u.user_id = em.user_id
        LEFT JOIN race_results rr ON rr.membership_id = em.membership_id
        WHERE em.event_id = %s
          AND em.event_role = 'participant'
          AND rr.start_time IS NOT NULL
          AND rr.finish_time IS NOT NULL
          AND rr.finish_time > rr.start_time
        ORDER BY rr.finish_time ASC
    """, [event_id])

    return _generate_results_csv(rows, event_id)


def _generate_results_csv(rows, event_id):
    """Helper function to generate CSV from results"""
    output = StringIO()
    writer = csv.writer(output)
    
    writer.writerow(['Rank', 'Membership ID', 'Username', 'Start Time', 'Finish Time', 'Elapsed Time (HH:MM:SS)'])
    
    prev_sec = None
    rank = 0
    
    from eventbridge_plus.util import nz_time24
    
    for i, r in enumerate(rows, start=1):
        if prev_sec is None or r['elapsed_sec'] != prev_sec:
            rank = i
            prev_sec = r['elapsed_sec']
        
        total = int(r['elapsed_sec']) if r['elapsed_sec'] else 0
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        elapsed_display = f"{h:02d}:{m:02d}:{s:02d}"
        
        start_str = nz_time24(r['start_time']) if r['start_time'] else ''
        finish_str = nz_time24(r['finish_time']) if r['finish_time'] else ''
        
        writer.writerow([
            rank,
            r['membership_id'],
            r['username'],
            start_str,
            finish_str,
            elapsed_display
        ])
    
    data = output.getvalue()
    output.close()
    
    nz_date_str = datetime.now().strftime('%d%m%Y')
    filename = f"event_{event_id}_results_{nz_date_str}.csv"
    return Response(
        data,
        mimetype='text/csv; charset=utf-8',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': 'text/csv; charset=utf-8'
        }
    )




