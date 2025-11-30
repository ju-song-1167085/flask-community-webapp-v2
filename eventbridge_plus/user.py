# eventbridge_plus/user.py
from eventbridge_plus import app, db, noti
from datetime import datetime
from flask import (
    render_template, request, redirect, url_for,
    flash, make_response, session, jsonify
)

# ---- Session & Permission: Use auth.py ----
from eventbridge_plus.auth import (
    create_user_session, clear_user_session,
    get_user_home_url, redirect_to_user_home,
    require_login, get_user_group_info, is_super_admin,
    get_current_user_id, get_current_platform_role, save_intended_url, get_intended_url, has_intended_url,
    require_platform_role
)

# ---- Validation & Password Hashing: Use validation.py ----
from eventbridge_plus.validation import (
    flask_bcrypt,
    check_username, check_email, check_password, check_password_match,
    check_name, check_location
)

# ---- Constants: Use constants.py ----
from eventbridge_plus.util import (
    AVAILABLE_LOCATIONS,
    DEFAULT_USER_ROLE,        # Default platform role
    USER_STATUS_BANNED        # Banned status
)

DEFAULT_PLATFORM_ROLE = DEFAULT_USER_ROLE

from eventbridge_plus import noti

# ---------------------- Page Routes ----------------------
@app.route('/')
def main_home():
    """Home page: optionally show 3 upcoming events"""
    try:
        upcoming_events = []
        try:
            # If your database uses different tables/fields (like event_info/location), adjust here
            with db.get_cursor() as cursor:
                # Get current user ID for privacy filtering
                user_id = session.get("user_id")
                
                if user_id:
                    # Logged in users: show public events + private group events they're members of
                    cursor.execute("""
                        SELECT e.event_id, e.event_title, e.event_date, e.event_time, e.location, e.event_type
                        FROM event_info e
                        JOIN group_info g ON e.group_id = g.group_id
                        WHERE e.status='scheduled' 
                        AND CONCAT(e.event_date, ' ', e.event_time) > NOW()
                        AND g.status = 'approved'
                        AND (g.is_public = 1 OR EXISTS (
                            SELECT 1 FROM group_members gm 
                            WHERE gm.group_id = g.group_id 
                            AND gm.user_id = %s 
                            AND gm.status = 'active'
                        ))
                        ORDER BY e.event_date ASC, e.event_time ASC
                        LIMIT 3;
                    """, (user_id,))
                else:
                    # Visitors: only show public group events
                    cursor.execute("""
                        SELECT e.event_id, e.event_title, e.event_date, e.event_time, e.location, e.event_type
                        FROM event_info e
                        JOIN group_info g ON e.group_id = g.group_id
                        WHERE e.status='scheduled' 
                        AND CONCAT(e.event_date, ' ', e.event_time) > NOW()
                        AND g.status = 'approved'
                        AND g.is_public = 1
                        ORDER BY e.event_date ASC, e.event_time ASC
                        LIMIT 3;
                    """)
                upcoming_events = cursor.fetchall()
        except Exception:
            # Return empty list on query failure, page won't error but won't show events
            pass
        return render_template('main_home.html', upcoming_events=upcoming_events)
    except Exception:
        return render_template("main_home.html", active_page="home")


@app.route('/home')
@require_login
def home():
    """Unified redirect to role-based homepage"""
    return redirect(get_user_home_url())



# ---------------------- Login / Register / Logout ----------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    # If already logged in, redirect based on role
    from eventbridge_plus.auth import is_user_logged_in
    if is_user_logged_in():
        return redirect_to_user_home()

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        with db.get_cursor() as cursor:
            cursor.execute("""
                SELECT user_id, username, password_hash, platform_role, status
                FROM users
                WHERE username = %s;
            """, (username,))
            account = cursor.fetchone()

        if account:
            if account.get('status') == USER_STATUS_BANNED:
                flash('Your account has been deactivated. Please contact administrator.', 'warning')
                return render_template('login.html', username=username)

            if flask_bcrypt.check_password_hash(account['password_hash'], password):
                # Save intended URL before creating new session
                saved_intended_url = session.get('intended_url')
                
                # Prepare session data (platform role + possible group role)
                user_data = {
                    'user_id': account['user_id'],
                    'username': account['username'],
                    'platform_role': account['platform_role'],
                }
                group_data = None
                if account['platform_role'] == 'participant':
                    group_data = get_user_group_info(account['user_id'])  # May return None

                create_user_session(user_data, group_data)
                
                # Restore intended URL after session creation
                if saved_intended_url:
                    session['intended_url'] = saved_intended_url

                # Check for intended URL first
                next_url = get_intended_url()
                if not next_url:
                    # Fallback to 'next' parameter
                    next_url = request.args.get("next") or request.form.get("next")
                
                # Only allow relative paths within the site as next (avoid Open Redirect)
                if next_url and next_url.startswith("/"):
                    return redirect(next_url)
                return redirect_to_user_home()

        flash('Username or password is incorrect.', 'danger')
        return render_template('login.html', username=username)

    # GET: Check if there's an intended URL
    next_url = request.args.get('next')
    return render_template('login.html', next_url=next_url)


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    from eventbridge_plus.auth import is_user_logged_in
    if is_user_logged_in():
        return redirect_to_user_home()

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        location = request.form.get('location', '').strip()

        # Use validation.py functions for all validation
        errors = {}

        err = check_username(username, check_db=True)
        if err:
            errors['username_error'] = err

        err = check_email(email, check_db=True)
        if err:
            errors['email_error'] = err

        err = check_password(password)
        if err:
            errors['password_error'] = err

        err = check_password_match(password, password_confirm)
        if err:
            errors['password_confirm_error'] = err

        err = check_name(first_name, 'First name')
        if err:
            errors['first_name_error'] = err

        err = check_name(last_name, 'Last name')
        if err:
            errors['last_name_error'] = err

        err = check_location(location)
        if err:
            errors['location_error'] = err

        if errors:
            try:
                next_url = request.form.get('next')
                return render_template(
                    'signup.html',
                    username=username, email=email,
                    first_name=first_name, last_name=last_name, location=location,
                    available_locations=AVAILABLE_LOCATIONS,
                    next_url=next_url,
                    **errors
                )
            except Exception:
                return f"Signup errors: {errors}", 400
            
        # Save intended URL before creating new session
        saved_intended_url = session.get('intended_url')

        # Create user (use validation.flask_bcrypt consistently)
        password_hash = flask_bcrypt.generate_password_hash(password).decode('utf-8')
        with db.get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO users (username, password_hash, email, first_name, last_name, location, platform_role)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
            """, (username, password_hash, email, first_name, last_name, location, DEFAULT_PLATFORM_ROLE))
            new_id = cursor.lastrowid
            cursor.connection.commit()

        # Auto-login as "participant platform role" (newly registered, no group role yet)
        create_user_session({
            'user_id': new_id,
            'username': username,
            'platform_role': DEFAULT_PLATFORM_ROLE
        })

        # Restore intended URL after session creation
        if saved_intended_url:
            session['intended_url'] = saved_intended_url
        
        # Send welcome notification
        noti.create_noti(
            user_id=new_id,
            title='Welcome to EventBridge+!',
            message=f'Hi {first_name}! Your account has been created successfully. You can now explore events, join groups, and participate in community activities.',
            category='system'
        )
        
        # Send welcome email
        noti.send_welcome_email(email, f"{first_name} {last_name}")
        
        flash('Registration successful! Welcome!', 'success')

        # Check for intended URL first
        next_url = get_intended_url()

        if not next_url:
            # Fallback to 'next' parameter
            next_url = request.form.get("next")
                
        # Only allow relative paths within the site as next (avoid Open Redirect)
        if next_url and next_url.startswith("/"):
            return redirect(next_url)
        return redirect_to_user_home()

    # GET: Preserve next parameter
    next_url = request.args.get('next')
    return render_template('signup.html', 
                         available_locations=AVAILABLE_LOCATIONS,
                         next_url=next_url)


@app.route('/logout')
def logout():
    clear_user_session()
    flash('You have been logged out.', 'info')
    resp = make_response(redirect(url_for('login')))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


# --- Access denied page + 403 handler ---

@app.route("/denied", endpoint="denied")
def denied():
    # This page returns 403, it is convenient to connect directly or redirect
    return render_template("access_denied.html"), 403

@app.errorhandler(403)
def forbidden(e):
    # Any abort(403) to the access_denied
    return render_template("access_denied.html"), 403

# ---------------------- Role-based Dashboards (consistent with auth.py naming) ----------------------
# auth.get_user_home_url() returns these endpoints:
# admin_dashboard / support_dashboard / group_manager_dashboard / group_volunteer_dashboard / participant_dashboard
# Note: participant_dashboard function is accessible via /my/dashboard route

@app.route('/my/dashboard')
@require_login
def participant_dashboard():
    """Participant dashboard with real database data"""
    try:
        user_id = get_current_user_id()
        current_platform_role = get_current_platform_role()
        
        # Get group filter from query parameters
        group_filter = request.args.get('group_filter', type=int)
        
        # Check if this is admin/technician viewing participant dashboard
        is_admin_view = current_platform_role in ['super_admin', 'support_technician']
        
        with db.get_cursor() as cursor:
            # Auto-update volunteer participation status for past events
            cursor.execute("""
                UPDATE event_members em
                JOIN event_info e ON em.event_id = e.event_id
                SET em.participation_status = 'attended'
                WHERE em.user_id = %s
                  AND em.event_role = 'volunteer'
                  AND em.participation_status = 'registered'
                  AND e.event_date < CURDATE()
            """, (user_id,))
            
            # Get upcoming events for this user (with optional group filter)
            events_query = """
                SELECT 
                    e.event_id,
                    e.event_title,
                    e.event_type,
                    e.event_date,
                    e.event_time,
                    e.location,
                    g.name AS group_name,
                    g.group_id,
                    em.event_role,
                    em.participation_status,
                    em.volunteer_status
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                JOIN group_info g ON e.group_id = g.group_id
                WHERE em.user_id = %s
                  AND e.status = 'scheduled'
                  AND CONCAT(e.event_date, ' ', e.event_time) > NOW()
                  AND (em.event_role != 'volunteer' OR em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled')
            """
            events_params = [user_id]
            
            # Add group filter if specified
            if group_filter:
                events_query += " AND g.group_id = %s"
                events_params.append(group_filter)
            
            events_query += " ORDER BY e.event_date ASC, e.event_time ASC LIMIT 10"
            
            cursor.execute(events_query, events_params)
            upcoming_events = cursor.fetchall()
            
            # Get user statistics
            cursor.execute("""
                SELECT COUNT(*) AS count
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                WHERE em.user_id = %s
                  AND em.participation_status = 'attended'
                  AND CONCAT(e.event_date, ' ', e.event_time) < NOW()
            """, (user_id,))
            events_attended = cursor.fetchone()['count']
            
            cursor.execute("""
                SELECT COUNT(*) AS count
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                WHERE em.user_id = %s
                  AND em.event_role = 'volunteer'
                  AND em.participation_status = 'attended'
                  AND CONCAT(e.event_date, ' ', e.event_time) < NOW()
            """, (user_id,))
            volunteer_events = cursor.fetchone()['count']
            
            cursor.execute("""
                SELECT COUNT(*) AS count
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                WHERE em.user_id = %s
                  AND e.status = 'scheduled'
                  AND CONCAT(e.event_date, ' ', e.event_time) > NOW()
                  AND em.participation_status = 'registered'
            """, (user_id,))
            upcoming_registrations = cursor.fetchone()['count']
            
            stats = {
                'events_attended': events_attended,
                'volunteer_events': volunteer_events,
                'upcoming_registrations': upcoming_registrations
            }
            
            # Get user's groups with pending counts (both join requests and volunteer applications)
            cursor.execute("""
                SELECT 
                    g.group_id,
                    g.name,
                    gm.group_role,
                    COALESCE(pending_counts.pending_count, 0) as group_join_pending_count,
                    COALESCE(volunteer_counts.volunteer_count, 0) as volunteer_pending_count,
                    COALESCE(pending_counts.pending_count, 0) + COALESCE(volunteer_counts.volunteer_count, 0) as total_pending_count
                FROM group_members gm
                JOIN group_info g ON gm.group_id = g.group_id
                LEFT JOIN (
                    SELECT group_id, COUNT(*) as pending_count
                    FROM group_members
                    WHERE status = 'pending'
                    GROUP BY group_id
                ) pending_counts ON g.group_id = pending_counts.group_id
                LEFT JOIN (
                    SELECT e.group_id, COUNT(*) as volunteer_count
                    FROM event_members em
                    JOIN event_info e ON em.event_id = e.event_id
                    WHERE em.event_role = 'volunteer' 
                      AND em.volunteer_status = 'assigned'
                      AND e.status = 'scheduled'
                    GROUP BY e.group_id
                ) volunteer_counts ON g.group_id = volunteer_counts.group_id
                WHERE gm.user_id = %s
                  AND gm.status = 'active'
                ORDER BY 
                    CASE WHEN gm.group_role = 'manager' THEN 0 ELSE 1 END,
                    g.name ASC
            """, (user_id,))
            user_groups = cursor.fetchall()
            
            # Check for pending group creation application
            pending_group_application = None
            try:
                cursor.execute("""
                    SELECT 
                        application_id,
                        group_name,
                        status,
                        created_at
                    FROM group_applications
                    WHERE user_id = %s
                      AND status = 'pending'
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (user_id,))
                pending_group_application = cursor.fetchone()
            except Exception:
                pass
            
            # Get pending group join requests
            pending_join_requests = []
            try:
                cursor.execute("""
                    SELECT 
                        gm.membership_id,
                        gm.join_date,
                        g.name as group_name,
                        g.group_id,
                        g.group_type,
                        g.is_public,
                        gm.status
                    FROM group_members gm
                    JOIN group_info g ON gm.group_id = g.group_id
                    WHERE gm.user_id = %s
                      AND gm.status = 'pending'
                    ORDER BY gm.join_date DESC
                """, (user_id,))
                pending_join_requests = cursor.fetchall()
            except Exception:
                pass
        
        return render_template('participant_home.html',
                             upcoming_events=upcoming_events,
                             stats=stats,
                             user_groups=user_groups,
                             pending_group_application=pending_group_application,
                             pending_join_requests=pending_join_requests,
                             is_admin_view=is_admin_view,
                             group_filter=group_filter)
                             
    except Exception as e:
        print(f"Error loading participant dashboard: {e}")
        # In case of error, still check if it's admin view
        current_platform_role = get_current_platform_role()
        is_admin_view = current_platform_role in ['super_admin', 'support_technician']
        return render_template('participant_home.html',
                             upcoming_events=[],
                             stats={'events_attended': 0, 'volunteer_events': 0, 'upcoming_registrations': 0},
                             user_groups=[],
                             pending_group_application=None,
                             pending_join_requests=[],
                             is_admin_view=is_admin_view)


# === My Stats Index (participants) ===========================================
@app.route('/my/stats', methods=['GET'], endpoint='my_stats_index')
@require_login
def my_stats_index():
    """
    List events the current user participated in, with links to each event's stats.
    The page is only indexed. The actual permission verification has been done in the /events/<id>/stats route (viewable by administrators/group leaders/participants).
    """
    uid = int(get_current_user_id())

    with db.get_cursor() as cursor:
        cursor.execute("""
            SELECT 
                e.event_id,
                e.event_title AS title,
                e.event_date,
                g.name AS group_name
            FROM event_members em
            JOIN event_info  e ON e.event_id  = em.event_id
            JOIN group_info  g ON g.group_id  = e.group_id
            WHERE em.user_id = %s
              AND em.event_role = 'participant'
              AND em.participation_status IN ('registered','attended','completed')
            ORDER BY e.event_date DESC, e.event_id DESC
        """, (uid,))
        events = cursor.fetchall() or []

    return render_template('my_stats.html', events=events)
# ============================================================================ 


@app.route('/group/volunteer/dashboard')
@require_login
def group_volunteer_dashboard():
    try:
        return render_template('volunteer_home.html')
    except Exception:
        return "Volunteer dashboard (create templates/volunteer_home.html)", 200


@app.route('/group/manager/dashboard')
@require_login
def group_manager_dashboard():
    try:
        return redirect(url_for('participant_dashboard'))
    except Exception:
        try:
            return redirect(url_for('participant_home'))
        except Exception:
            try:
                return redirect(url_for('main_home'))
            except Exception:
                return redirect(url_for('explore', tab='events'))


@app.route('/admin/dashboard')
@require_platform_role('super_admin')
def admin_dashboard():
    try:
        # Import analytics functions
        from eventbridge_plus.analytics import get_system_user_statistics, get_event_participation_insights, get_platform_monitoring_data, get_personal_activity_stats
        from flask import request
        
        # Get time periods from request (default values)
        event_period = request.args.get('event_period', 'all')
        growth_period = request.args.get('growth_period', 'last_6_months')  # Default to last 6 months
        
        # Map period to label
        period_labels = {
            'all': 'All Time',
            'last_month': 'Last Month',
            'last_3_months': 'Last 3 Months',
            'last_6_months': 'Last 6 Months',
            'last_year': 'Last Year'
        }
        event_period_label = period_labels.get(event_period, 'All Time')
        growth_period_label = period_labels.get(growth_period, 'Last 6 Months')
        
        # Get all analytics data with period filter
        user_stats = get_system_user_statistics()
        event_insights = get_event_participation_insights(period=event_period)
        platform_data = get_platform_monitoring_data(period=growth_period)
        
        # Get helpdesk statistics for the overview card
        helpdesk_stats = get_helpdesk_overview_stats()
        
        return render_template('admin_home.html',
                             user_stats=user_stats,
                             event_insights=event_insights,
                             platform_data=platform_data,
                             helpdesk_stats=helpdesk_stats,
                             event_period=event_period,
                             growth_period=growth_period,
                             event_period_label=event_period_label,
                             growth_period_label=growth_period_label)
    except Exception as e:
        print(f"Error loading admin dashboard: {e}")
        import traceback
        traceback.print_exc()
        return render_template('admin_home.html')


def get_helpdesk_overview_stats():
    """Get helpdesk statistics for admin dashboard overview card"""
    try:
        with db.get_cursor() as cursor:
            # Get active requests (new + assigned + blocked, excluding solved)
            cursor.execute("""
                SELECT 
                    COUNT(CASE WHEN status = 'new' THEN 1 END) as new_requests,
                    COUNT(CASE WHEN status = 'assigned' THEN 1 END) as assigned_requests,
                    COUNT(CASE WHEN status = 'blocked' THEN 1 END) as blocked_requests,
                    COUNT(CASE WHEN status = 'solved' THEN 1 END) as solved_requests
                FROM help_requests
            """)
            
            stats = cursor.fetchone()
            
            if stats:
                active_requests = (stats['new_requests'] or 0) + (stats['assigned_requests'] or 0) + (stats['blocked_requests'] or 0)
                solved_requests = stats['solved_requests'] or 0
                
                return {
                    'active_requests': active_requests,
                    'solved_requests': solved_requests,
                    'new_requests': stats['new_requests'] or 0,
                    'assigned_requests': stats['assigned_requests'] or 0,
                    'blocked_requests': stats['blocked_requests'] or 0
                }
            else:
                return {
                    'active_requests': 0,
                    'solved_requests': 0,
                    'new_requests': 0,
                    'assigned_requests': 0,
                    'blocked_requests': 0
                }
                
    except Exception as e:
        print(f"Error getting helpdesk overview stats: {e}")
        return {
            'active_requests': 0,
            'solved_requests': 0,
            'new_requests': 0,
            'assigned_requests': 0,
            'blocked_requests': 0
        }


@app.route('/support/dashboard')
@require_platform_role('support_technician')
def support_dashboard():
    """Support technician dashboard with statistics and recent requests"""
    try:
        from .helpdesk import get_support_manage_requests
        from .util import get_pagination_params
        
        # Get basic statistics
        stats = {
            'new_requests': 0,
            'open_requests': 0,
            'resolved_today': 0,
            'assigned_to_me': 0,
            'total_requests': 0
        }
        
        # Get recent requests assigned to current user (last 10)
        current_user_id = get_current_user_id()
        recent_requests, _ = get_support_manage_requests({'assigned_to': current_user_id}, 1, 10)
        
        # Get statistics from database
        try:
            with db.get_cursor() as cursor:
                # New requests count
                cursor.execute("SELECT COUNT(*) as count FROM help_requests WHERE status = 'new'")
                stats['new_requests'] = cursor.fetchone()['count']
                
                # Open requests count
                cursor.execute("SELECT COUNT(*) as count FROM help_requests WHERE status = 'open'")
                stats['open_requests'] = cursor.fetchone()['count']
                
                # Resolved today count
                cursor.execute("""
                    SELECT COUNT(*) as count FROM help_requests 
                    WHERE status = 'resolved' AND DATE(resolved_at) = CURDATE()
                """)
                stats['resolved_today'] = cursor.fetchone()['count']
                
                # Assigned to current user count
                cursor.execute("""
                    SELECT COUNT(*) as count FROM help_requests 
                    WHERE assigned_to = %s AND status != 'resolved'
                """, (current_user_id,))
                stats['assigned_to_me'] = cursor.fetchone()['count']
                
                # Total requests count
                cursor.execute("SELECT COUNT(*) as count FROM help_requests")
                stats['total_requests'] = cursor.fetchone()['count']
                
        except Exception as e:
            print(f"Error getting support statistics: {e}")
        
        return render_template('support_tech_home.html',
                             stats=stats,
                             recent_requests=recent_requests)
    except Exception as e:
        print(f"Error in support_dashboard: {e}")
        return "Support dashboard error", 500


# ---------------------- Debug/Diagnostic ----------------------
@app.get("/_routes")
def _routes():
    return {"routes": sorted([r.rule for r in app.url_map.iter_rules()])}, 200


@app.get("/_db_check")
def _db_check():
    try:
        with db.get_cursor() as cursor:
            cursor.execute("SELECT DATABASE() AS db, COUNT(*) AS users_cnt FROM users")
            row = cursor.fetchone()
        return {"db": row["db"], "users": row["users_cnt"]}, 200
    except Exception as e:
        return {"error": str(e)}, 500


@app.get("/_db_diag")
def _db_diag():
    try:
        with db.get_cursor() as cur:
            cur.execute("""
                SELECT DATABASE()   AS db,
                       @@port       AS port,
                       @@version    AS version,
                       @@datadir    AS datadir,
                       COUNT(*)     AS users
                FROM users
            """)
            return cur.fetchone(), 200
    except Exception as e:
        return {"error": str(e)}, 500

# ---------------------- Notification ----------------------

@app.route('/noti')
@require_login
def notifications_page():
    """Notification inbox page"""
    user_id = get_current_user_id()
    selected_category = request.args.get('category', 'all')
    
    all_notis = noti.get_user_notis(user_id, selected_category)
    unread_count = noti.get_unread_count(user_id)
    is_enabled = noti.is_noti_enabled(user_id)
    
    return render_template('notifications.html', 
                         notifications=all_notis,  
                         unread_count=unread_count,
                         is_notifications_enabled=is_enabled,
                         selected_category=selected_category)


@app.route('/noti/toggle', methods=['POST'])
@require_login
def toggle_noti():
    """Toggle notifications on/off (AJAX endpoint)"""
    user_id = get_current_user_id()
    enabled = request.json.get('enabled', True)
    
    success = noti.toggle_noti_setting(user_id, enabled)
    
    return jsonify({
        'success': success,
        'enabled': enabled,
        'message': 'Notifications enabled.' if enabled else 'Notifications disabled.'
    })








@app.route('/noti/mark-read/<int:noti_id>', methods=['POST'])
@require_login
def mark_noti_read(noti_id):
    """Mark a notification as read"""
    user_id = get_current_user_id()
    success = noti.mark_as_read(noti_id, user_id)
    return jsonify({'success': success})


@app.route('/noti/mark-all-read', methods=['POST'])
@require_login
def mark_all_noti_read():
    """Mark all notifications as read"""
    user_id = get_current_user_id()
    count = noti.mark_all_read(user_id)
    return jsonify({'success': True, 'count': count})


@app.route('/noti/delete-all', methods=['POST'])
@require_login
def delete_all_noti():
    """Delete all notifications"""
    user_id = get_current_user_id()
    count = noti.delete_all_notis(user_id)
    return jsonify({'success': True, 'count': count})


@app.route('/noti/delete/<int:noti_id>', methods=['POST'])
@require_login
def delete_noti_route(noti_id):
    """Delete a notification"""
    user_id = get_current_user_id()
    success = noti.delete_noti(noti_id, user_id)
    return jsonify({'success': success})




@app.route('/noti/mark-unread/<int:noti_id>', methods=['POST'])
@require_login
def mark_unread_route(noti_id):
    """Mark notification as unread"""
    user_id = get_current_user_id()
    success = noti.mark_as_unread(noti_id, user_id)
    return jsonify({'success': success})


@app.route('/noti/unread-count')
def get_unread_count_route():
    """Get unread notification count (for badge)"""
    # Check if user is logged in without redirecting
    from eventbridge_plus.auth import is_user_logged_in

    if not is_user_logged_in():
        return jsonify({'count': 0}), 200
    
    user_id = get_current_user_id()
    count = noti.get_unread_count(user_id)
    return jsonify({'count': count})


@app.route('/personal-activity')
@require_login
def personal_activity():
    """Personal activity for participants"""
    try:
        user_id = get_current_user_id()
        current_platform_role = get_current_platform_role()
        
        # Check if this is admin/technician viewing participant dashboard
        is_admin_view = current_platform_role in ['super_admin', 'support_technician']
        
        # Get personal activity statistics
        from eventbridge_plus.analytics import get_personal_activity_stats
        stats = get_personal_activity_stats(user_id)
        
        if not stats:
            flash('Unable to load personal activity data', 'error')
            return redirect(url_for('participant_dashboard'))
        
        return render_template('personal_activity_dashboard.html',
                             stats=stats,
                             is_admin_view=is_admin_view)
                             
    except Exception as e:
        print(f"Error in personal_activity: {e}")
        import traceback
        traceback.print_exc()
        flash('An error occurred while loading the dashboard', 'error')
        return redirect(url_for('participant_dashboard'))


@app.route('/auth/check-session')
def check_session():
    """Check if user session is still valid (for bfcache handling)"""
    from eventbridge_plus.auth import is_user_logged_in
    return jsonify({'logged_in': is_user_logged_in()})