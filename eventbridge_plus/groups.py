from eventbridge_plus import app, noti, db
from flask import render_template, request, jsonify, session, flash, redirect, url_for
from .auth import require_login, require_platform_role, get_current_user_id
from datetime import datetime

# ---- Configuration and constants ----
try:
    from .validation import AVAILABLE_LOCATIONS
except Exception:
    AVAILABLE_LOCATIONS = ['Christchurch', 'Dunedin', 'Nelson', 'Queenstown', 'Tekapo']

GROUP_TYPES = ['activity', 'social', 'mixed']


# ---- Utility function ----
def _current_user_id():
    """Get user_id from session (or use username as a fallback)"""
    uid = session.get('user_id')
    if uid:
        return uid
    username = session.get('username')
    if not username:
        return None
    from eventbridge_plus.util import create_pagination_info, create_pagination_links
    # Enforce 10 per page regardless of URL param
    try:
        page = max(1, int(request.args.get('page', 1)))
    except Exception:
        page = 1
    per_page = 10

    with db.get_cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        return row['user_id'] if row else None


def _active_member_count(group_id: int) -> int:
    """Count the current number of active members in the group"""
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM group_members WHERE group_id=%s AND status='active'",
            (group_id,),
        )
        row = cur.fetchone() or {}
        return row.get('c', 0)


def _has_column(table: str, column: str) -> bool:
    """Check if a column exists in the database (to avoid 500 errors caused by schema inconsistency)"""
    try:
        with db.get_cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM {table} LIKE %s", (column,))
            return cur.fetchone() is not None
    except Exception:
        return False


HAS_LOCATION = True  # group_location column exists in group_info table
LOC_SELECT = "group_location" if HAS_LOCATION else "NULL"

# ===== [NEW] helpers for notifications (minimal addition) =====
def _get_admin_user_ids():
    """Return all admin user_ids (super_admin / support_technician) to notify."""
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE platform_role IN ('super_admin','support_technician')")
            rows = cur.fetchall() or []
            if rows:
                return [r['user_id'] for r in rows]
    except Exception:
        pass
    # Fallback if column name differs
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE role IN ('super_admin','support_technician')")
            rows = cur.fetchall() or []
            return [r['user_id'] for r in rows]
    except Exception:
        return []
    return []

def _get_username(user_id: int) -> str:
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT username FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            return row['username'] if row else ''
    except Exception:
        return ''


# =============================================================================
# PARTICIPANT ROUTES (Public Group Join/Leave)
# =============================================================================

@app.route('/groups/<int:group_id>')
def group_detail(group_id):
    """Group detail page (public access for public groups)"""
    try:
        user_id = get_current_user_id()
        
        with db.get_cursor() as cursor:
            # Get group info with manager
            cursor.execute(f"""
                SELECT 
                    g.group_id, g.name, g.description, {LOC_SELECT} AS location,
                    g.group_type, g.is_public, g.max_members, g.status,
                    g.created_at,
                    COUNT(DISTINCT gm.membership_id) AS member_count,
                    COUNT(DISTINCT CASE 
                        WHEN e.status = 'scheduled' AND e.event_date >= CURDATE() 
                        THEN e.event_id 
                    END) AS upcoming_events_count,
                    manager.username as manager_username,
                    manager.platform_role as manager_platform_role
                FROM group_info g
                LEFT JOIN group_members gm ON g.group_id = gm.group_id AND gm.status = 'active'
                LEFT JOIN event_info e ON g.group_id = e.group_id
                LEFT JOIN group_members gm_manager ON g.group_id = gm_manager.group_id 
                    AND gm_manager.group_role = 'manager' AND gm_manager.status = 'active'
                LEFT JOIN users manager ON gm_manager.user_id = manager.user_id
                WHERE g.group_id = %s
                GROUP BY g.group_id, g.name, g.description, g.group_type, 
                         g.is_public, g.max_members, g.status, g.created_at,
                         manager.username, manager.platform_role
            """, (group_id,))
            group = cursor.fetchone()
            
            if not group:
                flash('Group not found.', 'error')
                return redirect(url_for('explore', tab='groups'))
            
            # Check if group is approved (allow platform admins to see non-approved groups)
            from .auth import is_super_admin, is_support_technician
            if group['status'] != 'approved' and not (is_super_admin() or is_support_technician()):
                flash('This group is not accessible.', 'error')
                return redirect(url_for('explore', tab='groups'))
            
            # For private groups, require login unless platform admin
            if not group['is_public'] and (not user_id) and not (is_super_admin() or is_support_technician()):
                flash('You must be logged in to view this group.', 'error')
                return redirect(url_for('login'))
            
            # Check user's membership status
            user_membership = None
            if user_id:
                cursor.execute("""
                    SELECT group_role, status
                    FROM group_members
                    WHERE user_id = %s AND group_id = %s
                """, (user_id, group_id))
                user_membership = cursor.fetchone()
            
            # Get upcoming events for this group WITH user registration info
            # For private groups, only show events to group members (except admins)
            if user_id:
                if group['is_public'] or (is_super_admin() or is_support_technician()):
                    # Public group OR admin user: show all events to logged-in users
                    cursor.execute("""
                        SELECT 
                            e.event_id, e.event_title, e.event_type,
                            e.event_date, e.event_time, e.location,
                            e.max_participants,
                            COUNT(DISTINCT em.membership_id) AS registered_count,
                            MAX(CASE WHEN em.user_id = %s THEN em.event_role END) AS user_event_role,
                            MAX(CASE WHEN em.user_id = %s THEN em.participation_status END) AS user_participation_status
                        FROM event_info e
                        LEFT JOIN event_members em ON e.event_id = em.event_id
                            AND em.participation_status IN ('registered', 'attended')
                            AND (em.event_role != 'volunteer' OR em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled')
                        WHERE e.group_id = %s
                            AND e.status = 'scheduled'
                            AND e.event_date >= CURDATE()
                        GROUP BY e.event_id, e.event_title, e.event_type,
                                 e.event_date, e.event_time, e.location, e.max_participants
                        ORDER BY e.event_date ASC, e.event_time ASC
                        LIMIT 10
                    """, (user_id, user_id, group_id))
                else:
                    # Private group: only show events to group members
                    cursor.execute("""
                        SELECT 
                            e.event_id, e.event_title, e.event_type,
                            e.event_date, e.event_time, e.location,
                            e.max_participants,
                            COUNT(DISTINCT em.membership_id) AS registered_count,
                            MAX(CASE WHEN em.user_id = %s THEN em.event_role END) AS user_event_role,
                            MAX(CASE WHEN em.user_id = %s THEN em.participation_status END) AS user_participation_status
                        FROM event_info e
                        LEFT JOIN event_members em ON e.event_id = em.event_id
                            AND em.participation_status IN ('registered', 'attended')
                            AND (em.event_role != 'volunteer' OR em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled')
                        WHERE e.group_id = %s
                            AND e.status = 'scheduled'
                            AND e.event_date >= CURDATE()
                            AND EXISTS (
                                SELECT 1 FROM group_members gm 
                                WHERE gm.group_id = e.group_id 
                                AND gm.user_id = %s 
                                AND gm.status = 'active'
                            )
                        GROUP BY e.event_id, e.event_title, e.event_type,
                                 e.event_date, e.event_time, e.location, e.max_participants
                        ORDER BY e.event_date ASC, e.event_time ASC
                        LIMIT 10
                    """, (user_id, user_id, group_id, user_id))
            else:
                # Not logged in: only show events from public groups
                if group['is_public']:
                    cursor.execute("""
                        SELECT 
                            e.event_id, e.event_title, e.event_type,
                            e.event_date, e.event_time, e.location,
                            e.max_participants,
                            COUNT(DISTINCT em.membership_id) AS registered_count,
                            NULL AS user_event_role,
                            NULL AS user_participation_status
                        FROM event_info e
                        LEFT JOIN event_members em ON e.event_id = em.event_id
                            AND em.participation_status IN ('registered', 'attended')
                            AND (em.event_role != 'volunteer' OR em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled')
                        WHERE e.group_id = %s
                            AND e.status = 'scheduled'
                            AND e.event_date >= CURDATE()
                        GROUP BY e.event_id, e.event_title, e.event_type,
                                 e.event_date, e.event_time, e.location, e.max_participants
                        ORDER BY e.event_date ASC, e.event_time ASC
                        LIMIT 10
                    """, (group_id,))
                else:
                    # Private group and not logged in: show no events
                    upcoming_events = []
            
            # Only fetch events if we executed a query
            if 'upcoming_events' not in locals():
                upcoming_events = cursor.fetchall()
            
            # Calculate availability for events
            for ev in upcoming_events:
                reg = ev['registered_count'] or 0
                maxp = ev['max_participants']
                if maxp is None:
                    ev['spots_available'] = None
                    ev['is_full'] = False
                else:
                    ev['spots_available'] = maxp - reg
                    ev['is_full'] = ev['spots_available'] <= 0
                
                # Add is_registered flag
                ev['is_registered'] = ev['user_participation_status'] in ('registered', 'attended') if ev.get('user_participation_status') else False
            
            # Determine if events are hidden due to privacy restrictions
            events_hidden_due_to_privacy = False
            if not group['is_public'] and user_id and (not user_membership or user_membership.get('status') != 'active') and not (is_super_admin() or is_support_technician()):
                # Private group, logged in user, but not an active member (no membership or pending) and not admin
                events_hidden_due_to_privacy = True
            elif not group['is_public'] and not user_id:
                # Private group and not logged in
                events_hidden_due_to_privacy = True
            
            # Get group members list (only for public groups and logged-in users)
            group_members = []
            if group['is_public'] and user_id:
                cursor.execute("""
                    SELECT u.username
                    FROM group_members gm
                    JOIN users u ON gm.user_id = u.user_id
                    WHERE gm.group_id = %s AND gm.status = 'active'
                    ORDER BY u.username ASC
                """, (group_id,))
                group_members = cursor.fetchall()
        
        # Get user's group role for analytics button
        user_group_role = None
        if user_membership:
            user_group_role = user_membership['group_role']
        
        return render_template('search/group_detail.html',
                             group=group,
                             user_membership=user_membership,
                             user_group_role=user_group_role,
                             upcoming_events=upcoming_events,
                             events_hidden_due_to_privacy=events_hidden_due_to_privacy,
                             group_members=group_members)
    
    except Exception as e:
        print(f"Error loading group detail: {e}")
        flash('Error loading group details.', 'error')
        return redirect(url_for('explore', tab='groups'))

@app.route('/groups/<int:group_id>/join')
@require_login
def group_join(group_id):
    """Join a public group (automatic approval)"""
    try:
        user_id = get_current_user_id()
        
        with db.get_cursor() as cursor:
            # Check if group exists and is public
            cursor.execute("""
                SELECT name, is_public, status, max_members
                FROM group_info
                WHERE group_id = %s
            """, (group_id,))
            group = cursor.fetchone()
            
            if not group:
                flash('Group not found.', 'error')
                return redirect(url_for('explore', tab='groups'))
            
            if group['status'] != 'approved':
                flash('This group is not available.', 'error')
                return redirect(url_for('group_detail', group_id=group_id))
            
            # Check if already a member
            cursor.execute("""
                SELECT status
                FROM group_members
                WHERE user_id = %s AND group_id = %s
            """, (user_id, group_id))
            existing = cursor.fetchone()
            
            if existing:
                if existing['status'] == 'active':
                    flash('You are already a member of this group.', 'info')
                else:
                    flash('You have a pending request for this group.', 'info')
                return redirect(url_for('group_detail', group_id=group_id))
            
            # Check the number of groups the user has joined (up to 10)
            cursor.execute("""
                SELECT COUNT(*) AS group_count
                FROM group_members
                WHERE user_id = %s AND status = 'active'
""", (user_id,))
            user_group_count = cursor.fetchone()['group_count']

            if user_group_count >= 10:
                flash('You have reached the maximum limit of 10 groups. Please leave a group before joining a new one.', 'warning')
                return redirect(url_for('group_detail', group_id=group_id))
                        
            # Check capacity
            current_count = _active_member_count(group_id)
            if current_count >= group['max_members']:
                flash('This group has reached maximum capacity.', 'warning')
                return redirect(url_for('group_detail', group_id=group_id))
            
            # Handle joining based on group type
            if group['is_public']:
                # Auto-join for public groups
                cursor.execute("""
                    INSERT INTO group_members (user_id, group_id, group_role, status)
                    VALUES (%s, %s, 'member', 'active')
                """, (user_id, group_id))
                
                # Send notification
                noti.create_noti(
                    user_id=user_id,
                    title='Successfully Joined Group',
                    message=f'You have joined "{group["name"]}"! You can now participate in group events and activities.',
                    category='group',
                    related_id=group_id
                )
                
                flash(f'Successfully joined "{group["name"]}"!', 'success')
            else:
                # Request to join private groups
                # Store pending event info in session for later use during approval
                from flask import session
                pending_event_info = session.get('pending_event_registration')
                
                if pending_event_info and isinstance(pending_event_info, dict):
                    event_id = pending_event_info.get('event_id')
                    event_group_id = pending_event_info.get('group_id')
                    
                    # Verify this event belongs to the current group
                    if event_id and event_group_id == group_id:
                        # Store event info in notifications table for group approval process
                        try:
                            noti.create_noti(
                                user_id=user_id,
                                title='PENDING_EVENT_REGISTRATION',
                                message=f'event_id:{event_id}|event_title:{pending_event_info.get("event_title", "")}',
                                category='system',
                                related_id=group_id
                            )
                        except Exception:
                            pass  # If notification fails, continue without auto-registration
                
                # Insert without pending_event_id (using session instead)
                cursor.execute("""
                    INSERT INTO group_members (user_id, group_id, group_role, status)
                    VALUES (%s, %s, 'member', 'pending')
                """, (user_id, group_id))
                
                # Send notification to user
                noti.create_noti(
                    user_id=user_id,
                    title='Join Request Submitted',
                    message=f'Your request to join "{group["name"]}" has been submitted and is awaiting approval.',
                    category='group',
                    related_id=group_id
                )
                
                # Send notification to group managers
                cursor.execute("""
                    SELECT gm.user_id, u.username
                    FROM group_members gm
                    JOIN users u ON gm.user_id = u.user_id
                    WHERE gm.group_id = %s AND gm.group_role = 'manager' AND gm.status = 'active'
                """, (group_id,))
                managers = cursor.fetchall()
                
                for manager in managers:
                    noti.create_noti(
                        user_id=manager['user_id'],
                        title='New Group Join Request',
                        message=f'A new request to join "{group["name"]}" has been submitted and is awaiting your approval.',
                        category='group',
                        related_id=group_id
                    )
                
                flash(f'Your request to join "{group["name"]}" has been submitted for approval.', 'info')
            
            return redirect(url_for('group_detail', group_id=group_id))
    
    except Exception as e:
        print(f"Error joining group: {e}")
        flash('An error occurred while joining the group.', 'error')
        return redirect(url_for('explore', tab='groups'))


@app.route('/groups/<int:group_id>/leave')
@require_login
def group_leave(group_id):
    """Leave a group"""
    try:
        user_id = get_current_user_id()
        
        with db.get_cursor() as cursor:
            # Check membership
            cursor.execute("""
                SELECT gm.group_role, g.name, gm.status
                FROM group_members gm
                JOIN group_info g ON gm.group_id = g.group_id
                WHERE gm.user_id = %s AND gm.group_id = %s
            """, (user_id, group_id))
            membership = cursor.fetchone()
            
            if not membership:
                flash('You are not a member of this group.', 'error')
                return redirect(url_for('group_detail', group_id=group_id))
            
            # Remove membership
            cursor.execute("""
                DELETE FROM group_members
                WHERE user_id = %s AND group_id = %s
            """, (user_id, group_id))

            cursor.execute("""
                DELETE em FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                WHERE em.user_id = %s AND e.group_id = %s
            """, (user_id, group_id))
            
            # Send notification
            if membership['status'] == 'pending':
                noti.create_noti(
                    user_id=user_id,
                    title='Join Request Cancelled',
                    message=f'You have cancelled your request to join "{membership["name"]}". You can reapply anytime if you change your mind.',
                    category='group',
                    related_id=group_id
                )
                flash(f'Your request to join "{membership["name"]}" has been cancelled.', 'success')
            else:
                noti.create_noti(
                    user_id=user_id,
                    title='Left Group',
                    message=f'You have left "{membership["name"]}". You can rejoin anytime if you change your mind.',
                    category='group',
                    related_id=group_id
                )
                flash(f'You have left "{membership["name"]}".', 'success')
            
            return redirect(url_for('group_detail', group_id=group_id))
    
    except Exception as e:
        print(f"Error leaving group: {e}")
        flash('An error occurred while leaving the group.', 'error')
        return redirect(url_for('group_detail', group_id=group_id))


@app.route('/groups/<int:group_id>/cancel-request', methods=['POST'])
@require_login
def cancel_group_request(group_id):
    """Cancel a pending group join request"""
    try:
        user_id = get_current_user_id()
        
        with db.get_cursor() as cursor:
            # Check if user has a pending request for this group
            cursor.execute("""
                SELECT gm.membership_id, g.name
                FROM group_members gm
                JOIN group_info g ON gm.group_id = g.group_id
                WHERE gm.user_id = %s AND gm.group_id = %s AND gm.status = 'pending'
            """, (user_id, group_id))
            request = cursor.fetchone()
            
            if not request:
                flash('No pending request found for this group.', 'error')
                return redirect(url_for('group_detail', group_id=group_id))
            
            # Remove the pending request
            cursor.execute("""
                DELETE FROM group_members
                WHERE membership_id = %s
            """, (request['membership_id'],))
            
            # Send notification to user
            noti.create_noti(
                user_id=user_id,
                title='Join Request Cancelled',
                message=f'You have cancelled your request to join "{request["name"]}". You can reapply anytime if you change your mind.',
                category='group',
                related_id=group_id
            )
            
            flash(f'Your request to join "{request["name"]}" has been cancelled.', 'success')
            return redirect(url_for('group_detail', group_id=group_id))
    
    except Exception as e:
        print(f"Error cancelling group request: {e}")
        flash('An error occurred while cancelling the request.', 'error')
        return redirect(url_for('group_detail', group_id=group_id))



# =====================  Participant applies to create a group  =====================
@app.route('/groups/new', methods=['GET', 'POST'])
@require_login
def group_apply_for_participant():
    """
    Participant uses the same group_form.html to submit a "group creation application" (not directly creating a group).
    Admin will approve/reject on the review page.
    """
    # If you are an administrator, go directly to the administrator entrance
    if session.get('platform_role') in ['super_admin']:
        return redirect(url_for('group_create'))

    uid = get_current_user_id()

    # ---------- POST：Submit for review or Save draft ----------
    if request.method == 'POST':
        action = (request.form.get('action') or 'submit').strip()   # 'submit' | 'save_draft'
        resume_id = request.form.get('resume_id', type=int)

        name = (request.form.get('name') or '').strip()
        description = (request.form.get('description') or '').strip()
        location = (request.form.get('location') or '').strip() or None
        group_type = (request.form.get('group_type') or 'mixed').strip()
        visibility = (request.form.get('visibility') or 'public').strip()
        try:
            max_members = int(request.form.get('max_members') or 100)
        except ValueError:
            max_members = 100

        if not name:
            flash('Group name cannot be empty', 'danger');  return redirect(request.url)
        if group_type not in GROUP_TYPES:
            flash('Group type illegal', 'danger');           return redirect(request.url)
        
        # Check for duplicate group name
        if check_group_name_duplicate(name):
            flash(f'A group with the name "{name}" already exists. Please choose a different name.', 'danger')
            return redirect(request.url)

        status_to_set = 'draft' if action == 'save_draft' else 'pending'
        gid = None
        with db.get_cursor() as cur:
            # If there is a resume_id and it belongs to the user and the status is rejected/draft/pending, update the original record
            if resume_id:
                cur.execute("""
                    SELECT group_id FROM group_info
                    WHERE group_id=%s AND created_by=%s AND status IN ('rejected','draft','pending')
                """, (resume_id, uid))
                own = cur.fetchone()
                if own:
                    clear_rej = ", rejection_reason=NULL" if status_to_set == 'pending' else ""
                    if HAS_LOCATION:
                        try:
                            cur.execute(f"""
                                UPDATE group_info
                                   SET name=%s, description=%s, group_location=%s, group_type=%s,
                                       is_public=%s, max_members=%s, status=%s{clear_rej}
                                 WHERE group_id=%s
                            """, (name, description, location, group_type,
                                  1 if visibility == 'public' else 0, max_members, status_to_set, resume_id))
                        except Exception:
                            cur.execute("""
                                UPDATE group_info
                                   SET name=%s, description=%s, group_location=%s, group_type=%s,
                                       is_public=%s, max_members=%s, status=%s
                                 WHERE group_id=%s
                            """, (name, description, location, group_type,
                                  1 if visibility == 'public' else 0, max_members, status_to_set, resume_id))
                    else:
                        try:
                            cur.execute(f"""
                                UPDATE group_info
                                   SET name=%s, description=%s, group_type=%s,
                                       is_public=%s, max_members=%s, status=%s{clear_rej}
                                 WHERE group_id=%s
                            """, (name, description, group_type,
                                  1 if visibility == 'public' else 0, max_members, status_to_set, resume_id))
                        except Exception:
                            cur.execute("""
                                UPDATE group_info
                                   SET name=%s, description=%s, group_type=%s,
                                       is_public=%s, max_members=%s, status=%s
                                 WHERE group_id=%s
                            """, (name, description, group_type,
                                  1 if visibility == 'public' else 0, max_members, status_to_set, resume_id))
                    gid = resume_id

            # Otherwise insert a new record
            if gid is None:
                if HAS_LOCATION:
                    cur.execute("""
                        INSERT INTO group_info
                          (name, description, group_location, group_type, is_public, max_members, status, created_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (name, description, location, group_type,
                          1 if visibility == 'public' else 0, max_members, status_to_set, uid))
                else:
                    cur.execute("""
                        INSERT INTO group_info
                          (name, description, group_type, is_public, max_members, status, created_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (name, description, group_type,
                          1 if visibility == 'public' else 0, max_members, status_to_set, uid))
                gid = cur.lastrowid

        # Draft: Prompt and return only
        if action == 'save_draft':
            flash('Draft saved. You can return and submit it later.', 'success')
            return redirect(url_for('group_apply_for_participant'))

        # Submit for review: send notification (applicant + administrator)
        try:
            noti.create_noti(
                user_id=uid,
                title='Group application submitted',
                message=f'Your application for "{name}" has been submitted and is pending review.',
                category='group',
                related_id=gid
            )
        except Exception:
            pass

        try:
            applicant = _get_username(uid)
            vis_label = 'Public' if visibility == 'public' else 'Private'
            for aid in _get_admin_user_ids():
                noti.create_noti(
                    user_id=aid,
                    title='New Group Application',
                    message=(f'Applicant: {applicant or "N/A"} | '
                             f'Name: "{name}" | Type: {group_type} | Visibility: {vis_label} | '
                             f'Max: {max_members} | Location: {location or "-"}'),
                    category='group',
                    related_id=gid
                )
        except Exception:
            pass

        flash('Submitted for review. A Super Admin will approve or reject it soon.', 'success')
        return redirect(url_for('explore', tab='groups'))

    # ---------- GET: Pre-fill the most recent "rejected/draft" record (rejected first) ----------
    prefill, resume_id = None, None
    with db.get_cursor() as cur:
        cur.execute(f"""
            SELECT group_id, name, description, {LOC_SELECT} AS location,
                   group_type, is_public, max_members, status, rejection_reason,
                   COALESCE(updated_at, created_at) AS ts
              FROM group_info
             WHERE created_by=%s AND status IN ('rejected','draft')
             ORDER BY (status='rejected') DESC, ts DESC
             LIMIT 1
        """, (uid,))
        row = cur.fetchone()
        if row:
            prefill = {
                'name': row.get('name'),
                'description': row.get('description'),
                'location': row.get('location'),
                'group_type': row.get('group_type'),
                'is_public': row.get('is_public'),
                'max_members': row.get('max_members'),
                'status': row.get('status'),
                'rejection_reason': row.get('rejection_reason'),
            }
            resume_id = row.get('group_id')

    # GET: Renders the same form, but with different buttons/titles in participant mode
    return render_template(
        'admin/group_form.html',
        mode='create',
        group=prefill,
        locations=AVAILABLE_LOCATIONS,
        group_types=GROUP_TYPES,
        resume_id=resume_id,
        can_edit=True  # Regular users can also apply to create groups
    )



# ============== 1) Listing Page ==============
@app.route('/super-admin/groups', methods=['GET'])
@app.route('/admin/groups', methods=['GET'])
@require_platform_role('super_admin', 'support_technician')
def groups_index():
    # Get sort parameter, tab, search query, and approver filter
    sort_by = request.args.get('sort', 'newest').strip()
    tab = request.args.get('tab', 'approved').strip()
    search_query = request.args.get('search', '').strip()
    approver_filter = request.args.get('approver', '').strip()
    
    # Pagination params via util.py
    from eventbridge_plus.util import get_pagination_params, create_pagination_info, create_pagination_links
    page, per_page = get_pagination_params(request, default_per_page=10)

    # Build order clause
    order_clause = "created_at DESC"  # Default to newest
    if sort_by == 'oldest':
        order_clause = "created_at ASC"
    elif sort_by == 'name_asc':
        order_clause = "name ASC"
    elif sort_by == 'name_desc':
        order_clause = "name DESC"
    elif sort_by == 'type_asc':
        order_clause = "group_type ASC, name ASC"
    elif sort_by == 'type_desc':
        order_clause = "group_type DESC, name ASC"
    elif sort_by == 'status_asc':
        order_clause = "status ASC, name ASC"
    elif sort_by == 'status_desc':
        order_clause = "status DESC, name ASC"
    
    with db.get_cursor() as cur:
        # Build search condition
        search_condition = ""
        search_params = []
        if search_query:
            search_condition = "AND LOWER(g.name) LIKE LOWER(%s)"
            search_params.append(f"%{search_query}%")
        
        if tab == 'pending':
            # Show only pending groups
            cur.execute(f"""
                SELECT
                    g.group_id, g.name, g.description, {LOC_SELECT} AS location,
                    g.group_type, g.is_public, g.max_members, g.status, g.created_at, g.updated_at,
                    u.username, u.first_name, u.last_name, u.email
                FROM group_info g
                JOIN users u ON g.created_by = u.user_id
                WHERE g.status = 'pending' {search_condition}
                ORDER BY {order_clause}
            """, search_params)
        else:
            # Show all groups except pending (default)
            cur.execute(f"""
                SELECT
                    g.group_id, g.name, g.description, {LOC_SELECT} AS location,
                    g.group_type, g.is_public, g.max_members, g.status, g.created_at, g.updated_at,
                    NULL as username, NULL as first_name, NULL as last_name, NULL as email
                FROM group_info g
                WHERE g.status != 'pending' {search_condition}
                ORDER BY {order_clause}
            """, search_params)
        groups_all = cur.fetchall()

        # total count for selected tab
        if tab == 'pending':
            cur.execute(f"""
                SELECT COUNT(*) AS total
                FROM group_info g
                WHERE g.status = 'pending' {search_condition}
            """, search_params)
        else:
            cur.execute(f"""
                SELECT COUNT(*) AS total
                FROM group_info g
                WHERE g.status != 'pending' {search_condition}
            """, search_params)
        total_groups = cur.fetchone()['total']

        # Get total pending count (regardless of search)
        cur.execute("""
            SELECT COUNT(*) AS total
            FROM group_info
            WHERE status = 'pending'
        """)
        total_pending_count = cur.fetchone()['total']

    # slice current page
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    groups = groups_all[start_idx:end_idx]
    
    # Get approver information for each group
    for group in groups:
        group_id = group['group_id']
        approved_by = None
        
        # Find approver information from approval records
        with db.get_cursor() as cur:
            cur.execute("""
                SELECT n.user_id, u.username, n.created_at
                FROM notifications n
                JOIN users u ON n.user_id = u.user_id
                WHERE n.category = 'system' 
                AND n.message LIKE %s
                ORDER BY n.created_at DESC
                LIMIT 1
            """, (f'APPROVED_GROUP:{group_id}:%',))
            
            approval_record = cur.fetchone()
            if approval_record:
                approved_by = {
                    'user_id': approval_record['user_id'],
                    'username': approval_record['username'],
                    'approved_at': approval_record['created_at']
                }
        
        group['approved_by'] = approved_by
    
    # Apply approver filter if specified
    if approver_filter:
        groups = [group for group in groups 
                 if group['approved_by'] and group['approved_by']['user_id'] == int(approver_filter)]

    base_url = url_for('groups_index')
    pagination = create_pagination_info(
        page=page,
        per_page=per_page,
        total=total_groups,
        base_url=base_url,
        sort=sort_by,
        tab=tab,
        search=search_query or None
    )
    pagination_links = create_pagination_links(pagination)
    
    # Get list of approvers for the dropdown filter (only those who actually approved groups)
    with db.get_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT u.user_id, u.username
            FROM notifications n
            JOIN users u ON n.user_id = u.user_id
            WHERE n.category = 'system' 
            AND n.message LIKE 'APPROVED_GROUP:%'
            ORDER BY u.username
        """)
        approvers = cur.fetchall()

    return render_template('admin/groups_list.html', 
                         groups=groups, 
                         current_sort=sort_by, 
                         current_tab=tab, 
                         search_query=search_query,
                         approver_filter=approver_filter,
                         approvers=approvers,
                         total_pending_count=total_pending_count,
                         pagination=pagination, 
                         pagination_links=pagination_links)


# ============== 2) Create ==============
@app.route('/super-admin/groups/new', methods=['GET', 'POST'])
@app.route('/admin/groups/new', methods=['GET', 'POST'])
@require_platform_role('super_admin')
def group_create():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        description = (request.form.get('description') or '').strip()
        location = (request.form.get('location') or '').strip() or None
        group_type = (request.form.get('group_type') or 'mixed').strip()
        is_public = 1 if (request.form.get('visibility') or 'public') == 'public' else 0
        try:
            max_members = int(request.form.get('max_members') or 500)
        except ValueError:
            max_members = 500
        members_csv = (request.form.get('members') or '').strip()

        # Basic verification
        if not name:
            flash('Group name cannot be empty', 'danger');  return redirect(request.url)
        if group_type not in GROUP_TYPES:
            flash('Group type illegal', 'danger');     return redirect(request.url)
        if max_members <= 0:
            flash('max_members Must be greater than 0', 'danger'); return redirect(request.url)
        if HAS_LOCATION and location and location not in AVAILABLE_LOCATIONS:
            flash('Location Not allowed', 'danger'); return redirect(request.url)
        
        # Check for duplicate group name
        if check_group_name_duplicate(name):
            flash(f'A group with the name "{name}" already exists. Please choose a different name.', 'danger')
            return redirect(request.url)

        creator_id = _current_user_id()
        if not creator_id:
            flash('Unable to identify the current user, please log in again', 'danger')
            return redirect(url_for('groups_index'))

        # Verify before creation: The initial number of members does not exceed the upper limit
        usernames = []
        if members_csv:
            usernames = [x.strip() for x in members_csv.split(',') if x.strip()]
            with db.get_cursor() as cur:
                placeholders = ",".join(["%s"] * len(usernames))
                cur.execute(
                    f"SELECT user_id, username FROM users WHERE username IN ({placeholders})",
                    tuple(usernames),
                )
                cand_users = cur.fetchall() or []
                if len(cand_users) > max_members:
                    flash(
                        f'Initial number of members（{len(cand_users)}）Exceed the upper limit（{max_members}），Please reduce it and try again.',
                        'danger'
                    )
                    return redirect(request.url)

        # Insert group (super administrator directly approved)
        with db.get_cursor() as cur:
            if HAS_LOCATION:
                cur.execute("""
                    INSERT INTO group_info
                        (name, description, group_location, group_type, is_public, max_members, status, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, 'approved', %s)
                """, (name, description, location, group_type, is_public, max_members, creator_id))
            else:
                cur.execute("""
                    INSERT INTO group_info
                        (name, description, group_type, is_public, max_members, status, created_by)
                    VALUES (%s, %s, %s, %s, %s, 'approved', %s)
                """, (name, description, group_type, is_public, max_members, creator_id))
            gid = cur.lastrowid

            if members_csv:
                cur.execute("UPDATE group_info SET first_members=%s WHERE group_id=%s",
                            (members_csv, gid))

            if members_csv and usernames:
                placeholders = ",".join(["%s"] * len(usernames))
                cur.execute(
                    f"SELECT user_id FROM users WHERE username IN ({placeholders})",
                    tuple(usernames),
                )
                rows = cur.fetchall() or []
                if rows:
                    cur.executemany("""
                        INSERT IGNORE INTO group_members (user_id, group_id, group_role, status)
                        VALUES (%s, %s, 'member', 'active')
                    """, [(r['user_id'], gid) for r in rows])

            # Add the creator (admin) as group manager
            cur.execute("""
                INSERT IGNORE INTO group_members (user_id, group_id, group_role, status)
                VALUES (%s, %s, 'manager', 'active')
            """, (creator_id, gid))

        flash('Group created successfully', 'success')
        return redirect(url_for('groups_index'))

    # GET render
    return render_template(
        'admin/group_form.html',
        mode='create',
        group=None,
        locations=AVAILABLE_LOCATIONS,
        group_types=GROUP_TYPES,
        can_edit=True  # Super admin can always edit
    )


# ============== 3) Edit (information modification + member list + pending approval) ==============
@app.route('/super-admin/groups/<int:group_id>/edit', methods=['GET', 'POST'])
@app.route('/admin/groups/<int:group_id>/edit', methods=['GET', 'POST'])
@require_platform_role('super_admin', 'support_technician')
def group_edit(group_id):
    # Check if user can edit (only super_admin can POST/edit)
    from eventbridge_plus.auth import is_super_admin
    can_edit = is_super_admin()
    
    if request.method == 'POST':
        if not can_edit:
            flash('Only Super Admins can edit group information.', 'error')
            return redirect(url_for('groups_index'))
        name = (request.form.get('name') or '').strip()
        description = (request.form.get('description') or '').strip()
        location = (request.form.get('location') or '').strip() or None
        group_type = (request.form.get('group_type') or 'mixed').strip()
        is_public = 1 if (request.form.get('visibility') or 'public') == 'public' else 0
        try:
            max_members = int(request.form.get('max_members') or 500)
        except ValueError:
            max_members = 500

        if not name:
            flash('Group name cannot be empty', 'danger');  return redirect(request.url)
        if group_type not in GROUP_TYPES:
            flash('Group type illegal', 'danger');     return redirect(request.url)
        if max_members <= 0:
            flash('max_members Must be greater than 0', 'danger'); return redirect(request.url)
        if HAS_LOCATION and location and location not in AVAILABLE_LOCATIONS:
            flash('Location not allowed', 'danger'); return redirect(request.url)
        
        # Check for duplicate group name (exclude current group)
        if check_group_name_duplicate(name, exclude_group_id=group_id):
            flash(f'A group with the name "{name}" already exists. Please choose a different name.', 'danger')
            return redirect(request.url)

        with db.get_cursor() as cur:
            if HAS_LOCATION:
                cur.execute("""
                    UPDATE group_info
                    SET name=%s, description=%s, group_location=%s, group_type=%s,
                        is_public=%s, max_members=%s
                    WHERE group_id=%s
                """, (name, description, location, group_type, is_public, max_members, group_id))
            else:
                cur.execute("""
                    UPDATE group_info
                    SET name=%s, description=%s, group_type=%s,
                        is_public=%s, max_members=%s
                    WHERE group_id=%s
                """, (name, description, group_type, is_public, max_members, group_id))

        flash('Group information updated', 'success')
        return redirect(url_for('group_edit', group_id=group_id))

    # GET: Query groups, members, and pending approval requests
    with db.get_cursor() as cur:
        if HAS_LOCATION:
            cur.execute("SELECT * FROM group_info WHERE group_id=%s", (group_id,))
        else:
            cur.execute("""
                SELECT
                    group_id, name, description, NULL AS location,
                    group_type, is_public, max_members, status,
                    first_members, created_by, created_at, updated_at
                FROM group_info
                WHERE group_id=%s
            """, (group_id,))
        group = cur.fetchone()

        cur.execute("""
            SELECT gm.membership_id, gm.user_id, u.username, gm.group_role, gm.status, gm.join_date
            FROM group_members gm
            JOIN users u ON gm.user_id = u.user_id
            WHERE gm.group_id=%s
            ORDER BY u.username
        """, (group_id,))
        members = cur.fetchall()

        cur.execute("""
            SELECT r.request_id, r.user_id, u.username, r.message, r.requested_at
            FROM group_requests r
            JOIN users u ON u.user_id = r.user_id
            WHERE r.group_id=%s AND r.status='pending'
            ORDER BY r.requested_at ASC
        """, (group_id,))
        pending_requests = cur.fetchall()

    return render_template(
        'admin/group_form.html',
        mode='edit',
        group=group,
        members=members,
        pending_requests=pending_requests,
        locations=AVAILABLE_LOCATIONS,
        group_types=GROUP_TYPES,
        can_edit=can_edit
    )

# ================== [ADDED] members add/remove for Edit page ==================
@app.route('/admin/groups/<int:group_id>/members/add', methods=['POST'])
@app.route('/super-admin/groups/<int:group_id>/members/add', methods=['POST'])
@require_platform_role('super_admin')
def group_member_add(group_id):  # [ADDED]
    members_csv = (request.form.get('members') or '').strip()
    if not members_csv:
        flash('Please enter usernames (comma-separated).', 'warning')
        return redirect(url_for('group_edit', group_id=group_id))

    usernames = [x.strip() for x in members_csv.split(',') if x.strip()]
    if not usernames:
        flash('No valid usernames found.', 'warning')
        return redirect(url_for('group_edit', group_id=group_id))

    with db.get_cursor() as cur:
        placeholders = ",".join(["%s"] * len(usernames))
        cur.execute(
            f"SELECT user_id, username FROM users WHERE username IN ({placeholders})",
            tuple(usernames)
        )
        rows = cur.fetchall() or []

        if not rows:
            flash('No matching users.', 'warning')
            return redirect(url_for('group_edit', group_id=group_id))

        # capacity check
        cur.execute("SELECT max_members FROM group_info WHERE group_id=%s", (group_id,))
        grp = cur.fetchone() or {}
        max_members = grp.get('max_members') or 0

        cur.execute("SELECT COUNT(*) AS c FROM group_members WHERE group_id=%s AND status='active'", (group_id,))
        current = (cur.fetchone() or {}).get('c', 0)

        can_add = max(0, max_members - current) if max_members else len(rows)
        rows_to_add = rows[:can_add]

        if rows_to_add:
            cur.executemany("""
                INSERT IGNORE INTO group_members (user_id, group_id, group_role, status)
                VALUES (%s, %s, 'member', 'active')
            """, [(r['user_id'], group_id) for r in rows_to_add])

        if len(rows_to_add) < len(rows):
            flash(f'Only added {len(rows_to_add)} (capacity reached).', 'warning')
        else:
            flash(f'Added {len(rows_to_add)} member(s).', 'success')

    return redirect(url_for('group_edit', group_id=group_id))


@app.route('/admin/groups/<int:group_id>/members/<int:user_id>/remove', methods=['POST'])
@app.route('/super-admin/groups/<int:group_id>/members/<int:user_id>/remove', methods=['POST'])
@require_platform_role('super_admin')
def group_member_remove(group_id, user_id):  # [ADDED]
    with db.get_cursor() as cur:
        # Remove from group
        cur.execute("DELETE FROM group_members WHERE group_id=%s AND user_id=%s", (group_id, user_id))
        
        # Also remove from all events of this group
        cur.execute("""
            DELETE em FROM event_members em
            JOIN event_info e ON em.event_id = e.event_id
            WHERE em.user_id = %s AND e.group_id = %s
        """, (user_id, group_id))
    
    flash('Member removed.', 'success')
    return redirect(url_for('group_edit', group_id=group_id))
# ================== [ADDED] end ==================

# ========== Participant: Submit application (insert a row into group_info with status=pending) ==========
@app.route('/groups/apply', methods=['POST'])
@require_login
def group_apply_submit_sql():
    uid = int(get_current_user_id())

    # Get form
    name = (request.form.get('group_name') or '').strip()
    desc = (request.form.get('description') or '').strip()
    loc  = (request.form.get('location') or '').strip()
    maxm = request.form.get('expected_size')
    try:
        maxm = int(maxm) if maxm else None
    except Exception:
        maxm = None

    if not name or not desc or not loc:
        return jsonify({"ok": False, "msg": "name/description/location required"}), 400

    esc = lambda s: s.replace("'", "''")
    sql = f"""
        INSERT INTO group_info (name, description, group_location, max_members, status, created_by, created_at, updated_at)
        VALUES ('{esc(name)}', '{esc(desc)}', '{esc(loc)}', {maxm if maxm is not None else 'NULL'},
                'pending', {uid}, NOW(), NOW());
    """
    db.session.execute(sql)
    new_id = db.session.execute("SELECT LAST_INSERT_ID();").scalar()
    db.session.commit()

    try:
        noti.send(uid, f'Submitted application for \"{name}\" (status: pending).')
    except Exception:
        pass

    return jsonify({"ok": True, "group_id": int(new_id)})

# =============================================================================
# VALIDATION FUNCTIONS
# =============================================================================

def check_group_name_duplicate(name, exclude_group_id=None):
    """
    Check if a group with the same name already exists (regardless of location).
    
    Args:
        name (str): Group name to check
        exclude_group_id (int, optional): Group ID to exclude from check (for updates)
    
    Returns:
        bool: True if duplicate exists, False otherwise
    """
    try:
        with db.get_cursor() as cur:
            if exclude_group_id:
                cur.execute("""
                    SELECT COUNT(*) as count
                    FROM group_info
                    WHERE LOWER(name) = LOWER(%s)
                    AND group_id != %s
                    AND status IN ('approved', 'pending')
                """, (name, exclude_group_id))
            else:
                cur.execute("""
                    SELECT COUNT(*) as count
                    FROM group_info
                    WHERE LOWER(name) = LOWER(%s)
                    AND status IN ('approved', 'pending')
                """, (name,))
            
            result = cur.fetchone()
            return result['count'] > 0 if result else False
            
    except Exception as e:
        print(f"Error checking group name duplicate: {e}")
        return False

# helpers
def _pick(r, key, idx):
    try: return r[key]
    except Exception: return r[idx]
# ========== Participant: My application ==========
@app.route('/my/applications', methods=['GET'])
@require_login
def my_applications_page():
    uid = int(get_current_user_id())

    # —— Read filter conditions —— #
    loc    = (request.args.get('location') or '').strip()
    status = (request.args.get('status') or '').strip()
    sort   = request.args.get('sort', 'desc')  # 'asc' or 'desc'

    ALLOWED_STATUS = {'pending', 'approved', 'rejected', 'draft'}
    sort = 'asc' if sort == 'asc' else 'desc'

    # —— First check the list of Locations available to the current user (de-duplication, for drop-down) —— #
    rows_locs = db.session.execute(
        """
        SELECT DISTINCT group_location
        FROM group_info
        WHERE created_by = %s
          AND group_location IS NOT NULL
          AND group_location <> ''
        ORDER BY group_location ASC
        """,
        (uid,)
    ).fetchall()

    def _pick(r, key, idx):
        """Compatible with dict/Row/tuple row results"""
        try:
            return r[key]
        except Exception:
            return r[idx]

    locations = [_pick(r, 'group_location', 0) for r in rows_locs]

    where = ["created_by = %s"]
    params = [uid]
    if loc:
        where.append("group_location = %s")
        params.append(loc)
    if status in ALLOWED_STATUS:
        where.append("status = %s")
        params.append(status)

    # —— Query and sort the application list —— #
    sql = f"""
      SELECT group_id, name, group_location, status, rejection_reason, updated_at
      FROM group_info
      WHERE {" AND ".join(where)}
      ORDER BY updated_at {sort}
    """
    rows = db.session.execute(sql, tuple(params)).fetchall()

    apps = [{
        "group_id":         _pick(r, 'group_id', 0),
        "name":             _pick(r, 'name', 1),
        "group_location":   _pick(r, 'group_location', 2),
        "status":           _pick(r, 'status', 3),
        "rejection_reason": _pick(r, 'rejection_reason', 4) or "",
        "updated_at":       _pick(r, 'updated_at', 5),
    } for r in rows]

    # Pass to template: data + current filter echo + drop-down candidates
    return render_template(
        'my_applications.html',
        apps=apps,
        q_location=loc, q_status=status, q_sort=sort,
        locations=locations
    )

# ========== Participant: Real-time status polling (JSON, front-end calls every 10 seconds) ==========
@app.route('/api/my/applications/status', methods=['GET'])
@require_login
def my_applications_status_api():
    uid = int(get_current_user_id())
    loc    = (request.args.get('location') or '').strip()
    status = (request.args.get('status') or '').strip()
    sort   = request.args.get('sort', 'desc')
    ALLOWED_STATUS = {'pending','approved','rejected','draft'}
    sort = 'asc' if sort == 'asc' else 'desc'

    where = ["created_by = %s"]
    params = [uid]
    if loc:
        where.append("group_location LIKE %s")
        params.append(f"%{loc}%")
    if status in ALLOWED_STATUS:
        where.append("status = %s")
        params.append(status)

    sql = f"""
      SELECT group_id, name, group_location,
             status,
             COALESCE(rejection_reason,'') AS feedback,
             DATE_FORMAT(updated_at, '%%d/%%m/%%Y %%h:%%i %%p') AS updated_fmt
      FROM group_info
      WHERE {" AND ".join(where)}
      ORDER BY updated_at {sort}
    """
    rows = db.session.execute(sql, tuple(params)).fetchall()

    def p(r, k, i): 
        try: return r[k]
        except Exception: return r[i]

    items = [{
        "group_id":  p(r,'group_id',0),
        "name":      p(r,'name',1),
        "location":  p(r,'group_location',2),
        "status":    p(r,'status',3),
        "feedback":  p(r,'feedback',4),
        "updated_at":p(r,'updated_fmt',5),
    } for r in rows]
    return jsonify({"items": items})


# ============== 3.5) Approve / Reject application (NEW) ==============
@app.route('/admin/groups/<int:group_id>/approve', methods=['POST'])
@app.route('/super-admin/groups/<int:group_id>/approve', methods=['POST'])
@require_platform_role('super_admin')
def group_approve(group_id):  # [NEW]
    # Get the ID of the admin who is approving
    admin_id = get_current_user_id()
    
    with db.get_cursor() as cur:

        cur.execute("SELECT created_by, name, group_type, is_public, max_members FROM group_info WHERE group_id=%s", (group_id,))
        row = cur.fetchone()
        if not row:
            flash('Group not found.', 'danger')
            return redirect(url_for('groups_index'))

        cur.execute("UPDATE group_info SET status='approved', rejection_reason=NULL WHERE group_id=%s", (group_id,))

        # Helper function to check and add user to group with 10-group limit
        def add_user_to_group_if_under_limit(user_id, group_id, role, user_description):
            cur.execute("""
                SELECT COUNT(*) AS group_count
                FROM group_members
                WHERE user_id = %s AND status = 'active'
            """, (user_id,))
            user_group_count = cur.fetchone()['group_count']
            
            if user_group_count < 10:
                cur.execute("""
                    INSERT IGNORE INTO group_members (user_id, group_id, group_role, status)
                    VALUES (%s, %s, %s, 'active')
                """, (user_id, group_id, role))
                return True
            else:
                flash(f'{user_description} has reached the maximum limit of 10 groups. Group approved but {user_description.lower()} not added as manager.', 'warning')
                return False

        # The creator joins and is set as manager (check 10 group limit)
        if row.get('created_by'):
            add_user_to_group_if_under_limit(row['created_by'], group_id, 'manager', 'Group creator')

        # Register the approving admin as group manager (check 10 group limit)
        add_user_to_group_if_under_limit(admin_id, group_id, 'manager', 'Approving admin')

    try:
        if row and row.get('created_by'):
            vis_label = 'Public' if row.get('is_public') else 'Private'
            msg = (f'Your group "{row.get("name")}" has been approved. '
                   f'Type: {row.get("group_type")}, Visibility: {vis_label}, '
                   f'Max members: {row.get("max_members")}. '
                   'You are now the group manager.')
            noti.create_noti(
                user_id=row['created_by'],
                title='Group Approved',
                message=msg,
                category='group', related_id=group_id
            )
        
        # Send notification to the approving admin
        admin_msg = (f'You have been assigned as manager for group "{row.get("name")}". '
                    f'Type: {row.get("group_type")}, Visibility: {vis_label}, '
                    f'Max members: {row.get("max_members")}.')
        noti.create_noti(
            user_id=admin_id,
            title='Group Management Assigned',
            message=admin_msg,
            category='group', 
            related_id=group_id
        )
        
        # Store approval record as system notification (for approver tracking)
        approval_record_msg = f'APPROVED_GROUP:{group_id}:{row.get("name")}:{admin_id}'
        noti.create_noti(
            user_id=admin_id,
            title='Group Approval Record',
            message=approval_record_msg,
            category='system',
            related_id=group_id
        )
    except Exception:
        pass

    flash('Application approved and group activated.', 'success')
    return redirect(url_for('groups_index', tab='pending'))


@app.route('/admin/groups/<int:group_id>/reject', methods=['POST'])
@app.route('/super-admin/groups/<int:group_id>/reject', methods=['POST'])
@require_platform_role('super_admin')
def group_reject(group_id):  # [CHANGED]
    reason_full = (request.form.get('reason') or '').strip()

    with db.get_cursor() as cur:
        # Read creator & name
        cur.execute("SELECT created_by, name FROM group_info WHERE group_id=%s", (group_id,))
        row = cur.fetchone() or {}

        # Validate the rejection reason is a valid ENUM value
        valid_reasons = ['inappropriate_content', 'duplicate_group', 'insufficient_info', 'guideline_violation', 'other']
        enum_reason = reason_full if reason_full in valid_reasons else 'other'
        
        try:
            cur.execute(
                "UPDATE group_info SET status='rejected', rejection_reason=%s WHERE group_id=%s",
                (enum_reason, group_id)
            )
        except Exception as e:
            print(f"Error updating rejection reason: {e}")
            cur.execute(
                "UPDATE group_info SET status='rejected', rejection_reason='other' WHERE group_id=%s",
                (group_id,)
            )

    # Send notification without reason - users should contact helpdesk for details
    try:
        if row.get('created_by'):
            noti.create_noti(
                user_id=row['created_by'],
                title='Group Rejected',
                message=f'Your group "{row.get("name")}" was rejected. Please contact support for more information.',
                category='group', related_id=group_id
            )
    except Exception:
        pass

    flash('Application rejected.', 'warning')
    return redirect(url_for('groups_index', tab='pending'))

@app.route('/groups/<int:group_id>/edit', methods=['GET'])
@require_login
def groups_edit_compat(group_id):
    return redirect(url_for('group_apply_for_participant', group_id=group_id))
# ============== 4) Activate / Deactivate ==============
@app.route('/admin/groups/<int:group_id>/deactivate', methods=['POST'])
@app.route('/super-admin/groups/<int:group_id>/deactivate', methods=['POST'])
@require_platform_role('super_admin')
def group_deactivate(group_id):
    with db.get_cursor() as cur:
        cur.execute("UPDATE group_info SET status='inactive' WHERE group_id=%s", (group_id,))
    flash('Group deactivated.', 'warning')
    return redirect(url_for('groups_index'))

@app.route('/admin/groups/<int:group_id>/activate', methods=['POST'])
@app.route('/super-admin/groups/<int:group_id>/activate', methods=['POST'])
@require_platform_role('super_admin')
def group_activate(group_id):
    with db.get_cursor() as cur:
        cur.execute("UPDATE group_info SET status='approved' WHERE group_id=%s", (group_id,))
    flash('Group activated.', 'success')
    return redirect(url_for('groups_index'))

# ============== 5) Delete group ==============
@app.route('/admin/groups/<int:group_id>/delete', methods=['POST'])
@app.route('/super-admin/groups/<int:group_id>/delete', methods=['POST'])
@require_platform_role('super_admin')
def group_delete(group_id):
    with db.get_cursor() as cur:
        try:
            cur.execute("""
                DELETE em FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                WHERE e.group_id=%s
            """, (group_id,))
        except Exception:
            pass
        try:
            cur.execute("DELETE FROM event_info WHERE group_id=%s", (group_id,))
        except Exception:
            pass
        try:
            cur.execute("DELETE FROM group_requests WHERE group_id=%s", (group_id,))
        except Exception:
            pass
        try:
            cur.execute("DELETE FROM group_members WHERE group_id=%s", (group_id,))
        except Exception:
            pass

        cur.execute("DELETE FROM group_info WHERE group_id=%s", (group_id,))

    flash('Group deleted.', 'success')
    return redirect(url_for('groups_index'))




@app.route('/admin/groups/<int:group_id>/application')
@require_platform_role('super_admin')
def admin_group_application_detail(group_id):
    """View group application details (super_admin only)"""
    try:
        with db.get_cursor() as cur:
            # Get group application details with applicant info
            cur.execute(f"""
                SELECT 
                    g.group_id, g.name, g.description, {LOC_SELECT} AS location,
                    g.group_type, g.is_public, g.max_members, g.status, 
                    g.created_at, g.updated_at,
                    u.username, u.first_name, u.last_name, u.email, u.user_image
                FROM group_info g
                JOIN users u ON g.created_by = u.user_id
                WHERE g.group_id = %s
            """, (group_id,))
            application = cur.fetchone()
            
            if not application:
                flash('Application not found', 'error')
                return redirect(url_for('groups_index'))
            
            # Get proposed initial members if any
            initial_members = []
            if application.get('first_members'):
                member_usernames = [name.strip() for name in application['first_members'].split(',') if name.strip()]
                if member_usernames:
                    placeholders = ','.join(['%s'] * len(member_usernames))
                    cur.execute(f"""
                        SELECT user_id, username, first_name, last_name, email
                        FROM users 
                        WHERE username IN ({placeholders})
                    """, member_usernames)
                    initial_members = cur.fetchall()
            
        return render_template('admin/group_form.html', 
                             group=application, 
                             mode='view',
                             group_types=['activity', 'social', 'mixed'],
                             locations=['Auckland', 'Wellington', 'Christchurch', 'Hamilton', 'Tauranga', 'Napier', 'Dunedin', 'Palmerston North', 'Nelson', 'Rotorua'])
        
    except Exception as e:
        print(f"Error loading application details: {e}")
        flash('Error loading application details', 'error')
        return redirect(url_for('groups_index'))
    
# ======================  EVENT MANAGEMENT (minimal, no conflicts)  ===========

from datetime import date as _date_cls, time as _time_cls

def _require_group_manager_of(group_id: int):
    """Only group managers of this group can manage its events."""
    uid = get_current_user_id()
    if not uid:
        flash("Please log in to manage events.", "warning")
        return redirect(url_for('login', next=request.url))

    with db.get_cursor() as cur:
        cur.execute("""
            SELECT 1
            FROM group_members
            WHERE group_id=%s AND user_id=%s
              AND group_role='manager' AND status='active'
            LIMIT 1
        """, (group_id, uid))
        ok = cur.fetchone()

    if not ok:
        flash("You are not authorized to manage this group's events.", "danger")
        return redirect(url_for('group_detail', group_id=group_id))

    return uid

def _parse_dt_fields(form):
    """Combine HTML date + time → Python date/time."""
    d_str = (form.get('event_date') or '').strip()      # YYYY/MM/DD
    t_str = (form.get('event_time') or '').strip()      # HH:MM
    try:
        d = _date_cls.fromisoformat(d_str)
        t = _time_cls.fromisoformat(t_str)
        return d, t, None
    except Exception:
        return None, None, "Invalid date/time format."

def _is_past(d: _date_cls, t: _time_cls) -> bool:
    now = datetime.now()
    return datetime.combine(d, t) < now

def _notify_event_participants(event_id: int, text: str):
    """Notify all participants of an event (best-effort)."""
    try:
        with db.get_cursor() as cur:
            cur.execute("""
                SELECT DISTINCT user_id
                FROM event_members
                WHERE event_id=%s
            """, (event_id,))
            users = cur.fetchall() or []
        for u in users:
            try:
                noti.create_noti(user_id=u['user_id'], title='Event Update',
                                 message=text, category='event', related_id=event_id)
            except Exception:
                pass
    except Exception:
        pass

def _load_event(group_id, event_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM event_info WHERE event_id=%s AND group_id=%s",
                    (event_id, group_id))
        return cur.fetchone()

# ---- Cancel / Delete ----
@app.route('/groups/<int:group_id>/events/<int:event_id>/cancel', methods=['POST'])
@require_login
def events_cancel(group_id, event_id):
    # Allow platform admins to bypass group manager requirement
    from .auth import is_super_admin, is_support_technician
    if not (is_super_admin() or is_support_technician()):
        uid = _require_group_manager_of(group_id)
        if not isinstance(uid, int):
            return uid
    ev = _load_event(group_id, event_id)
    if not ev:
        flash('Event not found.', 'warning')
        return redirect(url_for('group_detail', group_id=group_id))

    with db.get_cursor() as cur:
        cur.execute("UPDATE event_info SET status='cancelled' WHERE event_id=%s AND group_id=%s",
                    (event_id, group_id))
    _notify_event_participants(event_id, f"Event '{ev['event_title']}' has been cancelled.")
    flash('Event cancelled and notifications sent.', 'success')
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/groups/<int:group_id>/events/<int:event_id>/delete', methods=['POST'])
@require_login
def events_delete(group_id, event_id):
    # Allow platform admins to bypass group manager requirement
    from .auth import is_super_admin, is_support_technician
    if not (is_super_admin() or is_support_technician()):
        uid = _require_group_manager_of(group_id)
        if not isinstance(uid, int):
            return uid
    ev = _load_event(group_id, event_id)
    if not ev:
        flash('Event not found.', 'warning')
        return redirect(url_for('group_detail', group_id=group_id))

    with db.get_cursor() as cur:
        try:
            cur.execute("DELETE FROM event_members WHERE event_id=%s", (event_id,))
        except Exception:
            pass
        cur.execute("DELETE FROM event_info WHERE event_id=%s AND group_id=%s",
                    (event_id, group_id))

    _notify_event_participants(event_id, f"Event '{ev['event_title']}' has been deleted.")
    flash('Event deleted.', 'success')
    return redirect(url_for('group_detail', group_id=group_id))
