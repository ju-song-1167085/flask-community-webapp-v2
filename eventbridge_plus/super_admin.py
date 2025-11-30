"""
Includes (A-1 scope):
  - /admin/users                      : list + search + filters
  - /admin/users/<id>                 : profile view
  - /admin/users/<id>/ban   (POST)    : ban user
  - /admin/users/<id>/unban (POST)    : unban user
  - /admin/manage-users               : redirect helper

"""

from eventbridge_plus import app, db, noti
import os, re
from flask import render_template, request, redirect, url_for, flash
from datetime import datetime, timedelta
from eventbridge_plus.util import get_pagination_params, create_pagination_info
from eventbridge_plus.util import AVAILABLE_LOCATIONS, PROFILE_ALLOWED_EXTENSIONS, save_uploaded_file, remove_uploaded_file
from werkzeug.utils import secure_filename
from eventbridge_plus.auth import (
    require_login,
    require_platform_role,
    get_current_user_id,
    is_super_admin
)


# ===============================
# 1) User list with search
# ===============================
@app.route('/admin/users', endpoint='admin_users_list')
@require_platform_role('super_admin', 'support_technician')
def admin_users_list():
    """
    User list page with simple search & filters.
    Query params:
      - search : matches first_name, last_name, email, username
      - role   : participant | support_technician | super_admin
      - status : active | banned
    """
    search = (request.args.get('search') or '').strip()
    role = (request.args.get('role') or '').strip()
    status = (request.args.get('status') or '').strip()

    # Pagination params
    from eventbridge_plus.util import create_pagination_info, create_pagination_links
    page, per_page = get_pagination_params(request, default_per_page=20)

    try:
        # Build WHERE conditions
        where = []
        params = []

        if search:
            where.append("(LOWER(first_name) LIKE LOWER(%s) OR LOWER(last_name) LIKE LOWER(%s) OR LOWER(email) LIKE LOWER(%s) OR LOWER(username) LIKE LOWER(%s))")
            like = f"%{search}%"
            params.extend([like, like, like, like])

        if role in ('participant', 'support_technician', 'super_admin'):
            where.append("platform_role = %s")
            params.append(role)

        if status in ('active', 'banned'):
            where.append("status = %s")
            params.append(status)

        where_clause = " WHERE " + " AND ".join(where) if where else ""

        # Get total count
        count_sql = "SELECT COUNT(*) AS total FROM users" + where_clause
        with db.get_cursor() as cur:
            cur.execute(count_sql, params)
            total_count = cur.fetchone()['total']

        # Get paginated users
        sql = """
            SELECT
              user_id, username, email, first_name, last_name,
              location, platform_role, status, notifications_enabled,
              banned_reason, banned_at
            FROM users
        """ + where_clause + " ORDER BY first_name, last_name LIMIT %s OFFSET %s"
        
        params_with_pagination = params + [per_page, (page - 1) * per_page]
        
        with db.get_cursor() as cur:
            cur.execute(sql, params_with_pagination)
            users = cur.fetchall() or []

        # Get stats for all users (not just current page)
        with db.get_cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM users", [])
            total_all = cur.fetchone()['total']
            cur.execute("SELECT COUNT(*) AS total FROM users WHERE status = 'active'", [])
            active_users = cur.fetchone()['total']
            banned_users = total_all - active_users

        # Create pagination info
        base_url = url_for('admin_users_list')
        pagination = create_pagination_info(
            page=page,
            per_page=per_page,
            total=total_count,
            base_url=base_url,
            search=search or None,
            role=role or None,
            status=status or None
        )
        pagination_links = create_pagination_links(pagination)

        return render_template(
            'admin/users_list.html',
            users=users,
            total_users=total_all,
            active_users=active_users,
            banned_users=banned_users,
            search_query=search,
            role_filter=role,
            status_filter=status,
            pagination=pagination,
            pagination_links=pagination_links
        )

    except Exception as e:
        flash("Failed to load users.", "danger")
        return redirect(url_for('admin_users_list'))


# ===============================
# 2) User profile
# ===============================
@app.route('/admin/users/<int:user_id>', endpoint='admin_user_profile')
@require_platform_role('super_admin', 'support_technician')
def admin_user_profile(user_id: int):
    """
    Simple user profile page:
      - basic user fields
      - groups list
      - last 10 event participations (with role/status/hours)
      - quick totals
    """
    try:
        with db.get_cursor() as cur:
            # user
            cur.execute("""
                SELECT
                  user_id, username, email, first_name, last_name, location,
                  gender, birth_date, biography, user_image,
                  platform_role, status, notifications_enabled,
                  banned_reason, banned_by, banned_at
                FROM users
                WHERE user_id = %s
            """, (user_id,))
            user = cur.fetchone()
            if not user:
                flash("User not found.", "warning")
                return redirect(url_for('admin_users_list'))

            # groups
            cur.execute("""
                SELECT
                  g.group_id, g.name AS group_name, g.description, g.group_type,
                  gm.group_role, gm.status AS membership_status, gm.join_date
                FROM group_members gm
                JOIN group_info g ON gm.group_id = g.group_id
                WHERE gm.user_id = %s
                ORDER BY gm.join_date DESC
            """, (user_id,))
            groups = cur.fetchall() or []

            # recent events (limit 10)
            cur.execute("""
                SELECT
                  e.event_id, e.event_title, e.event_type, e.event_date, e.event_time,
                  g.name AS group_name,
                  em.event_role, em.participation_status, em.volunteer_hours
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                JOIN group_info g ON e.group_id = g.group_id
                WHERE em.user_id = %s
                ORDER BY e.event_date DESC
                LIMIT 10
            """, (user_id,))
            events = cur.fetchall() or []

            # quick totals (simple)
            total_events = len(events)
            attended_events = sum(1 for r in events if r.get('participation_status') == 'attended')
            volunteer_events = sum(1 for r in events if r.get('event_role') == 'volunteer')
            total_vol_hours = sum((r.get('volunteer_hours') or 0) for r in events)

            banned_by_admin = None
            if user.get('banned_by'):
                cur.execute("SELECT username, first_name, last_name FROM users WHERE user_id = %s", (user['banned_by'],))
                banned_by_admin = cur.fetchone()

        return render_template(
            'admin/user_detail.html',
            user=user,
            user_groups=groups,
            user_events=events,
            user_stats={
                # match template keys (some templates show active_groups from another query; we keep simple)
                "active_groups": sum(1 for g in groups if g.get('membership_status') == 'active'),
                "total_events": total_events,
                "attended_events": attended_events,
                "total_volunteer_hours": total_vol_hours
            },
            banned_by_admin=banned_by_admin
        )

    except Exception as e:
        flash("Failed to load profile.", "danger")
        return redirect(url_for('admin_users_list'))

@app.route("/admin/users/<int:user_id>/profile", methods=["GET"], endpoint="admin_profile_edit_page")
@require_platform_role('super_admin')
def admin_profile_edit_page(user_id: int):
    """
    Admin-only profile editing page (left: form, right: recent activity).
    """
    try:
        profile, event_recent, num_part, num_vol = _load_profile_bundle(user_id)
        if not profile:
            flash("User not found.", "danger")
            return redirect(url_for("admin_users_list"))

        return render_template(
            "profile.html",
            profile=profile,
            event_recent=event_recent,
            num_participation=num_part,
            num_volunteer=num_vol,
            available_locations=AVAILABLE_LOCATIONS,
            is_admin=True,
            is_own_profile=False
        )
    except Exception as e:
        flash("Failed to load profile.", "danger")
        return redirect(url_for("admin_users_list"))

@app.route("/admin/users/<int:user_id>/profile/edit", methods=["POST"], endpoint="admin_profile_save")
@require_platform_role('super_admin')
def admin_profile_save(user_id: int):
    """
    Process admin edits:
      - action=update_info     : update text fields
      - action=update_avatar   : upload/replace/remove profile image
    """
    action = (request.form.get("action") or "").strip()

    current = _fetch_profile_row(user_id)
    if not current:
        flash("User not found.", "danger")
        return redirect(url_for("admin_users_list"))

    # A) Update core info
    if action == "update_info":
        first_name = (request.form.get("first_name") or "").strip()
        last_name  = (request.form.get("last_name")  or "").strip()
        username   = (request.form.get("username")   or "").strip()
        email      = (request.form.get("email")      or "").strip().lower()
        location   = (request.form.get("location")   or "").strip()
        platform_role = (request.form.get("platform_role") or current["platform_role"] or "").strip()
        status     = (request.form.get("status")     or current["status"] or "").strip()
        ban_reason = (request.form.get("ban_reason") or "").strip()
        biography  = (request.form.get("biography")  or "").strip()
        notifications_enabled = 1 if request.form.get("notifications_enabled") == "on" else 0

        # Minimal validation: mirror Project 1 feel
        errs = {}
        if not first_name:
            errs["first_name_error"] = "First name is required."
        if not last_name:
            errs["last_name_error"]  = "Last name is required."
        if not username or not re.match(r"^[A-Za-z0-9_.]+$", username):
            errs["username_error"] = "Username must contain only letters, numbers, underscores, and dots."
        else:
            # Check if username actually changed
            current_username = current.get("username", "").strip()
            new_username = username.strip()
            if new_username != current_username:
                with db.get_cursor() as cursor:
                    cursor.execute('SELECT user_id FROM users WHERE username = %s AND user_id != %s', (username, user_id))
                    if cursor.fetchone():
                        errs["username_error"] = "This username is already taken. Please choose another one."
        
        if not email or not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}$', email):
            errs["email_error"] = "Please enter a valid email address."
        else:
            # Check if email actually changed (case insensitive comparison)
            current_email = current.get("email", "").lower().strip()
            new_email = email.lower().strip()
            if new_email != current_email:
                with db.get_cursor() as cursor:
                    cursor.execute('SELECT user_id FROM users WHERE LOWER(email) = LOWER(%s) AND user_id != %s', (email, user_id))
                    if cursor.fetchone():
                        errs["email_error"] = "This email is already taken. Please choose another one."

        if errs:
            for v in errs.values():
                flash(v, "warning")
            # Reload profile data and render template with errors
            profile, event_recent, num_part, num_vol = _load_profile_bundle(user_id)
            return render_template(
                "profile.html",
                profile=profile,
                event_recent=event_recent,
                num_participation=num_part,
                num_volunteer=num_vol,
                available_locations=AVAILABLE_LOCATIONS,
                is_admin=True,
                is_own_profile=False,
                form=request.form,
                **errs
            )

        try:
            with db.get_cursor() as cur:
                cur.execute("""
                    UPDATE users
                       SET first_name=%s, last_name=%s, username=%s, email=%s,
                           location=%s, platform_role=%s, status=%s,
                           banned_reason=%s, biography=%s, notifications_enabled=%s
                     WHERE user_id=%s
                """, (first_name, last_name, username, email, location, platform_role,
                      status, (ban_reason or None), (biography or None), notifications_enabled, user_id))
                cur.connection.commit()
            flash("Profile updated.", "success")
        except Exception as e:
            flash("Failed to update profile.", "danger")

        return redirect(url_for("admin_profile_edit_page", user_id=user_id))

    # B) Avatar upload/remove
    if action == "update_avatar":
        # Remove existing image
        if request.form.get("remove_avatar") == "1":
            try:
                old_rel = current.get("user_image")
                if old_rel:
                    remove_uploaded_file(old_rel)  # best-effort removal
                with db.get_cursor() as cur:
                    cur.execute("UPDATE users SET user_image=NULL WHERE user_id=%s", (user_id,))
                    cur.connection.commit()
                flash("Profile image removed.", "success")
            except Exception as e:
                flash("Failed to remove image.", "danger")
            return redirect(url_for("admin_profile_edit_page", user_id=user_id))

        # Upload new image
        file = request.files.get("avatar")
        if not file or not file.filename:
            flash("No file selected.", "warning")
            return redirect(url_for("admin_profile_edit_page", user_id=user_id))

        try:
            rel_path = save_uploaded_file(
                file_storage=file,
                subdir="profiles",
                allowed_exts=PROFILE_ALLOWED_EXTENSIONS,
                filename_prefix=f"user{user_id}"
            )

            old_rel = current.get("user_image")
            if old_rel and old_rel != rel_path:
                remove_uploaded_file(old_rel)

            with db.get_cursor() as cur:
                cur.execute("UPDATE users SET user_image=%s WHERE user_id=%s", (rel_path, user_id))
                cur.connection.commit()
            flash("Profile image updated.", "success")
        except ValueError as ve:
            flash(str(ve), "danger")
        except Exception as e:
            flash("Failed to upload image.", "danger")

        return redirect(url_for("admin_profile_edit_page", user_id=user_id))

    flash("Unknown action.", "warning")
    return redirect(url_for("admin_profile_edit_page", user_id=user_id))

# ===============================
# 3) Ban / Unban
# ===============================
@app.route('/admin/users/<int:user_id>/ban', methods=['POST'], endpoint='admin_ban_user')
@require_platform_role('super_admin', 'support_technician')
def admin_ban_user(user_id: int):
    """
    Ban a user.
    - cannot ban yourself
    - only super_admin can ban another super_admin
    """
    try:
        me = get_current_user_id()
        if user_id == me:
            flash("You cannot ban yourself.", "warning")
            return redirect(url_for('admin_user_profile', user_id=user_id))

        reason = (request.form.get('reason') or '').strip()
        if not reason:
            flash("Please provide a reason.", "warning")
            return redirect(url_for('admin_user_profile', user_id=user_id))

        with db.get_cursor() as cur:
            cur.execute("SELECT username, status, platform_role FROM users WHERE user_id = %s", (user_id,))
            target = cur.fetchone()
            if not target:
                flash("User not found.", "warning")
                return redirect(url_for('admin_users_list'))

            if target['status'] == 'banned':
                flash(f"{target['username']} is already banned.", "info")
                return redirect(url_for('admin_user_profile', user_id=user_id))

            if target['platform_role'] == 'super_admin' and not is_super_admin():
                flash("Only a Super Admin can ban another Super Admin.", "danger")
                return redirect(url_for('admin_user_profile', user_id=user_id))

            # Check if user is a group manager before banning
            cur.execute("""
                SELECT gm.group_id, g.name as group_name, gm.group_role
                FROM group_members gm
                JOIN group_info g ON gm.group_id = g.group_id
                WHERE gm.user_id = %s AND gm.group_role = 'manager' AND gm.status = 'active'
            """, (user_id,))
            manager_groups = cur.fetchall()
            
            # Handle group manager auto-promotion
            promoted_managers = []
            for group in manager_groups:
                # Find next member to promote to manager
                cur.execute("""
                    SELECT gm.membership_id, gm.user_id, u.username, u.first_name, u.last_name
                    FROM group_members gm
                    JOIN users u ON gm.user_id = u.user_id
                    WHERE gm.group_id = %s AND gm.group_role != 'manager' AND gm.status = 'active'
                    ORDER BY 
                        CASE gm.group_role 
                            WHEN 'volunteer' THEN 1 
                            WHEN 'member' THEN 2 
                            ELSE 3 
                        END,
                        gm.join_date ASC
                    LIMIT 1
                """, (group['group_id'],))
                next_manager = cur.fetchone()
                
                if next_manager:
                    # Promote the next member to manager
                    cur.execute("""
                        UPDATE group_members
                        SET group_role = 'manager'
                        WHERE membership_id = %s
                    """, (next_manager['membership_id'],))
                    
                    promoted_managers.append({
                        'group_name': group['group_name'],
                        'new_manager': next_manager
                    })
                    
                    # Send notification to the new manager
                    noti.create_noti(
                        user_id=next_manager['user_id'],
                        title='Promoted to Group Manager',
                        message=f'You have been automatically promoted to manager of "{group["group_name"]}" as the previous manager was deactivated.',
                        category='group',
                        related_id=group['group_id']
                    )

            # do ban
            cur.execute("""
                UPDATE users
                SET status='banned', banned_reason=%s, banned_by=%s, banned_at=CURRENT_TIMESTAMP
                WHERE user_id=%s
            """, (reason, me, user_id))

            # Send ban notification to the user
            noti.create_noti(
                user_id=user_id,
                title='Account Deactivated',
                message=f'Your account has been deactivated. Reason: {reason}',
                category='system'
            )

            cur.connection.commit()

        # Prepare success message
        success_msg = f"User {target['username']} has been banned."
        if promoted_managers:
            manager_names = [f"{pm['new_manager']['first_name']} {pm['new_manager']['last_name']} ({pm['group_name']})" for pm in promoted_managers]
            success_msg += f" New managers assigned: {', '.join(manager_names)}."
        
        flash(success_msg, "success")
        return redirect(url_for('admin_user_profile', user_id=user_id))

    except Exception as e:
        flash("Failed to ban user.", "danger")
        return redirect(url_for('admin_user_profile', user_id=user_id))


@app.route('/admin/users/<int:user_id>/unban', methods=['POST'], endpoint='admin_unban_user')
@require_platform_role('super_admin', 'support_technician')
def admin_unban_user(user_id: int):
    """Unban a user (reactivate)."""
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT username, status FROM users WHERE user_id=%s", (user_id,))
            target = cur.fetchone()
            if not target:
                flash("User not found.", "warning")
                return redirect(url_for('admin_users_list'))

            if target['status'] == 'active':
                flash(f"{target['username']} is already active.", "info")
                return redirect(url_for('admin_user_profile', user_id=user_id))

            cur.execute("""
                UPDATE users
                SET status='active', banned_reason=NULL, banned_by=NULL, banned_at=NULL
                WHERE user_id=%s
            """, (user_id,))

            # Send unban notification using noti module
            noti.create_noti(
                user_id=user_id,
                title='Account Restored',
                message='Your account has been restored and you can now access all features again. Welcome back!',
                category='system'
            )

            cur.connection.commit()

        flash("User has been unbanned.", "success")
        return redirect(url_for('admin_user_profile', user_id=user_id))

    except Exception as e:
        flash("Failed to unban user.", "danger")
        return redirect(url_for('admin_user_profile', user_id=user_id))


# ===============================
# 4) Helper redirect
# ===============================
@app.route('/admin/manage-users', endpoint='admin_manage_users')
@require_platform_role('super_admin', 'support_technician')
def manage_users():
    """Simple redirect to user list."""
    return redirect(url_for('admin_users_list'))

def _fetch_profile_row(user_id: int):
    """
    Return a single user row for admin editing page.
    """
    with db.get_cursor() as c:
        c.execute("""
            SELECT user_id, username, email, first_name, last_name, location,
                   platform_role, status, biography, banned_reason AS ban_reason,
                   user_image, notifications_enabled
              FROM users
             WHERE user_id = %s
        """, (user_id,))
        return c.fetchone()

def _load_profile_bundle(user_id: int):
    """
    Load profile + lightweight recent activity for the right panel.
    Expand/optimize queries later if needed.
    """
    profile = _fetch_profile_row(user_id)
    if not profile:
        return None, [], 0, 0

    with db.get_cursor() as cur:
        cur.execute("""
            SELECT e.event_title, e.event_date, em.event_role, em.participation_status, em.volunteer_status
              FROM event_members em
              JOIN event_info e ON e.event_id = em.event_id
             WHERE em.user_id = %s
             ORDER BY e.event_date DESC
             LIMIT 20
        """, (user_id,))
        recent = cur.fetchall()

    num_part = len([r for r in recent if (r.get('event_role') or '').lower() == 'participant'])
    num_vol  = len([r for r in recent if (r.get('event_role') or '').lower() == 'volunteer'])
    return profile, recent, num_part, num_vol

# ===============================
# (A-2 preview) Role change — kept minimal, endpoint unique
# ===============================
@app.route('/admin/users/<int:user_id>/change-role', methods=['POST'], endpoint='admin_change_user_role')
@require_platform_role('super_admin')  # Only Super Admin can change platform roles
def admin_change_user_role(user_id: int):
    """
    Change a user's platform role (A-2 scope). You can keep this here;
    template may or may not expose it yet.
    """
    try:
        new_role = (request.form.get('new_role') or '').strip()
        me = get_current_user_id()

        if new_role not in ('participant', 'support_technician', 'super_admin'):
            flash('Invalid role.', 'warning')
            return redirect(url_for('admin_user_profile', user_id=user_id))

        if user_id == me:
            flash("You can't change your own role.", 'warning')
            return redirect(url_for('admin_user_profile', user_id=user_id))

        with db.get_cursor() as cur:
            cur.execute("SELECT username, platform_role FROM users WHERE user_id=%s", (user_id,))
            target = cur.fetchone()
            if not target:
                flash('User not found.', 'warning')
                return redirect(url_for('admin_users_list'))

            old_role = target['platform_role']
            if old_role == new_role:
                flash('The user already has this role.', 'info')
                return redirect(url_for('admin_user_profile', user_id=user_id))

            cur.execute("UPDATE users SET platform_role=%s WHERE user_id=%s", (new_role, user_id))
            cur.execute("""
                INSERT INTO notifications (user_id, title, message, category)
                VALUES (%s, 'Role Changed', %s, 'system')
            """, (user_id, f'Your role has been changed from {old_role} to {new_role}.'))

            cur.connection.commit()

        flash(f"Role updated: {target['username']} — {old_role} → {new_role}", 'success')
        return redirect(url_for('admin_user_profile', user_id=user_id))

    except Exception as e:
        flash('An error occurred while changing the role.', 'danger')
        return redirect(url_for('admin_user_profile', user_id=user_id))


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@require_platform_role('super_admin')
def admin_delete_user(user_id):
    """
    Delete a user account (super_admin only)
    """
    try:
        # Get user info before deletion for email
        with db.get_cursor() as cur:
            cur.execute("""
                SELECT email, first_name, last_name, username
                FROM users 
                WHERE user_id = %s
            """, (user_id,))
            user_info = cur.fetchone()
        
        if not user_info:
            flash('User not found', 'error')
            return redirect(url_for('admin_users_list'))
        
        # Send goodbye email before deletion
        noti.send_goodbye_email(user_info['email'], f"{user_info['first_name']} {user_info['last_name']}")
        
        # Delete user account (cascade will handle related records)
        with db.get_cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
            cur.connection.commit()
        
        flash(f"User {user_info['username']} has been successfully deleted.", 'success')
        return redirect(url_for('admin_users_list'))
        
    except Exception as e:
        flash('An error occurred while deleting the user.', 'danger')
        return redirect(url_for('admin_user_profile', user_id=user_id))



