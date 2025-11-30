# profile.py - User profile management routes
"""
Routes for user profile viewing and management.
Used by admin dashboard and user profile features.
"""

from eventbridge_plus import app
from flask import render_template, request, redirect, url_for, flash, session, jsonify
from eventbridge_plus import db, noti
from eventbridge_plus.auth import require_login, get_current_user_id
from eventbridge_plus.util import AVAILABLE_LOCATIONS, PROFILE_ALLOWED_EXTENSIONS, save_uploaded_file, remove_uploaded_file
from eventbridge_plus.validation import check_password, check_current_password, check_new_password_different
import re

@app.route('/profile/<int:user_id>')
@require_login
def profile_view(user_id):
    """
    View another user's profile (limited information)
    Used when admin clicks on a user in the user list
    """
    try:
        current_user_id = get_current_user_id()
        
        with db.get_cursor() as cursor:
            # Get basic profile information only (exclude sensitive data)
            cursor.execute('''
                SELECT 
                    user_id, username, email, first_name, last_name,
                    location, biography, user_image, platform_role,
                    status, notifications_enabled
                FROM users 
                WHERE user_id = %s AND status = 'active'
            ''', (user_id,))
            user = cursor.fetchone()
            
            if not user:
                flash('User not found.', 'error')
                return redirect(url_for('main_home'))
            
            # Show only public group memberships
            cursor.execute('''
                SELECT 
                    g.group_id, g.name as group_name, g.description,
                    gm.group_role, gm.join_date
                FROM group_members gm
                JOIN group_info g ON gm.group_id = g.group_id
                WHERE gm.user_id = %s AND gm.status = 'active' 
                  AND g.is_public = TRUE
                ORDER BY gm.join_date DESC
            ''', (user_id,))
            user_groups = cursor.fetchall()
            
            # Show public event participation history
            cursor.execute('''
                SELECT 
                    e.event_id, e.event_title, e.event_type, e.event_date,
                    g.name as group_name,
                    em.participation_status
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                JOIN group_info g ON e.group_id = g.group_id
                WHERE em.user_id = %s AND g.is_public = TRUE
                  AND em.participation_status = 'attended'
                ORDER BY e.event_date DESC
                LIMIT 5
            ''', (user_id,))
            user_events = cursor.fetchall()
            
            # Basic statistics
            cursor.execute('''
                SELECT 
                    COUNT(DISTINCT CASE WHEN gm.status = 'active' AND g.is_public = TRUE THEN gm.group_id END) as public_groups,
                    COUNT(DISTINCT CASE WHEN em.participation_status = 'attended' AND g.is_public = TRUE THEN em.event_id END) as attended_events
                FROM users u
                LEFT JOIN group_members gm ON u.user_id = gm.user_id
                LEFT JOIN group_info g ON gm.group_id = g.group_id
                LEFT JOIN event_members em ON u.user_id = em.user_id
                LEFT JOIN event_info e ON em.event_id = e.event_id
                LEFT JOIN group_info g2 ON e.group_id = g2.group_id
                WHERE u.user_id = %s
            ''', (user_id,))
            user_stats = cursor.fetchone()
            
            # Check if viewing own profile
            is_own_profile = (current_user_id == user_id)
            
        # Check if current user is admin
        current_user = _fetch_profile_row(current_user_id)
        is_admin = current_user and current_user.get('platform_role') in ['super_admin', 'support_technician']
        
        return render_template('profile.html',
                             profile=user,
                             user_groups=user_groups,
                             user_events=user_events,
                             user_stats=user_stats,
                             is_own_profile=is_own_profile,
                             is_admin=is_admin,
                             available_locations=AVAILABLE_LOCATIONS,
                             notifications_enabled=user['notifications_enabled'])
    
    except Exception as e:
        print(f"Error in profile_view: {e}")
        flash('An error occurred while loading the profile.', 'error')
        return redirect(url_for('main_home'))

@app.route('/profile/edit', methods=['GET', 'POST'])
@require_login
def edit_profile():
    """
    Simple handler for:
      - action=update_info   : update profile fields
      - action=update_avatar : upload/remove avatar
    """
    user_id = get_current_user_id()
    if not user_id:
        return redirect(url_for('login'))

    # GET: Form is on /profile, same-page operation, redirect back
    if request.method == 'GET':
        return redirect(url_for('profile_view', user_id=user_id))

    action = (request.form.get('action') or '').strip()

    # -----------------------------------------------------------
    # 1) Update profile details                                      
    # -----------------------------------------------------------
    if action == 'update_info':
        # Read current row and calculate is_admin (backend as single source of truth)
        current = _fetch_profile_row(user_id)
        if not current:
            flash('Profile not found.', 'error')
            return redirect(url_for('profile_view', user_id=user_id))
        is_admin = (current.get('platform_role') == 'super_admin')

        # Read form data (non-admins won't submit email/role as frontend disables them)
        first_name = (request.form.get('first_name') or '').strip()
        last_name  = (request.form.get('last_name')  or '').strip()
        username   = (request.form.get('username')   or '').strip()
        location   = (request.form.get('location')   or '').strip()
        biography  = (request.form.get('bio')        or '').strip()
        
        # Only read/validate email and role when form actually submits them and current user is admin
        email_submitted = ('email' in request.form)
        role_submitted  = ('role'  in request.form)
        username_submitted = ('username'  in request.form)
        email = (request.form.get('email') or '').strip() if (is_admin and email_submitted) else current['email']
        role  = (request.form.get('role')  or '').strip() if (is_admin and role_submitted)  else current['platform_role']
        username = (request.form.get('username')  or '').strip() if (is_admin and username_submitted)  else current['username']

        # ---------- Validation ----------
        first_name_error = last_name_error = username_error = email_error = location_error = None

        # Relax name validation, only check for non-empty + length to avoid false positives
        if not first_name:
            first_name_error = 'First name is required.'
        elif len(first_name) > 50:
            first_name_error = 'First name cannot exceed 50 characters.'

        if not last_name:
            last_name_error = 'Last name is required.'
        elif len(last_name) > 50:
            last_name_error = 'Last name cannot exceed 50 characters.'

        # Username uniqueness
        if is_admin and username_submitted:
            if not username:
                username_error = 'Username is required.'
            elif len(username) > 50 or not re.match(r'^[A-Za-z0-9_.]+$', username):
                username_error = 'Username can only contain letters, numbers, underscores, and dots.'
            elif username != current['username']:  # Only check for duplicates if username changed
                with db.get_cursor() as cursor:
                    cursor.execute('''
                        SELECT user_id FROM users 
                        WHERE username = %s AND user_id != %s
                    ''', (username, user_id))
                    if cursor.fetchone():
                        username_error = 'This username is already taken. Please choose another one.'

        # Location validation
        if not location:
            location_error = 'Please select your location.'
        elif location not in AVAILABLE_LOCATIONS:
            location_error = 'Please select a valid location from the list.'

        # Only validate email and check uniqueness (excluding self) if admin and email was actually submitted
        if is_admin and email_submitted:
            if not email:
                email_error = 'Email address is required.'
            elif len(email) > 100 or not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
                email_error = 'Please enter a valid email address (e.g., user@example.com).'
            elif email != current['email']:  # Only check for duplicates if email changed
                with db.get_cursor() as cursor:
                    cursor.execute('SELECT user_id FROM users WHERE email=%s AND user_id<>%s', (email, user_id))
                    if cursor.fetchone():
                        email_error = 'An account already exists with this email address.'

        # Return with errors if any (and pass available_locations to prevent dropdown disappearing)
        if any([first_name_error, last_name_error, username_error, email_error, location_error]):
            profile_row = _fetch_profile_row(user_id)
            return render_template(
                'profile.html',
                profile=profile_row,
                available_locations=AVAILABLE_LOCATIONS,  # Ensure dropdown exists
                is_admin=is_admin,                         # Synchronize template logic
                form=request.form,
                first_name_error=first_name_error,
                last_name_error=last_name_error,
                username_error=username_error,
                email_error=email_error,
                location_error=location_error,
            )

        # ---------- Only update changed fields ----------
        # Dynamically build UPDATE statement to avoid column/parameter mismatch
        sets, params = [], []

        def add(col, val):
            sets.append(f"{col}=%s")
            params.append(val)

        if first_name != (current.get('first_name') or ''):
            add('first_name', first_name)
        if last_name  != (current.get('last_name')  or ''):
            add('last_name', last_name)
        if username   != (current.get('username')   or ''):
            add('username', username)
        if location   != (current.get('location')   or ''):
            add('location', location)
        if biography  != (current.get('biography')  or ''):
            add('biography', biography)

        # Only consider updating email/role if admin and form submitted them
        if is_admin and email_submitted and email != current.get('email'):
            add('email', email)
        if is_admin and role_submitted and role in ('participant','super_admin','support_technician') and role != current.get('platform_role'):
            add('platform_role', role)

        if not sets:
            flash('No changes detected.', 'info')
            return redirect(url_for('profile_view', user_id=user_id))

        sql = f"UPDATE users SET {', '.join(sets)} WHERE user_id=%s"
        params.append(user_id)

        try:
            with db.get_cursor() as c:
                c.execute(sql, params)
                c.connection.commit()

            # Sync session username/role if changed
            session['username'] = username
            if is_admin and role_submitted:
                session['platform_role'] = role

            flash('Profile updated successfully!', 'success')
        except Exception as e:
            print('update_info failed:', e)
            flash('An error occurred while updating your profile. Please try again.', 'error')

        return redirect(url_for('profile_view', user_id=user_id))

    # -----------------------------------------------------------
    # 2) Upload/remove avatar                                    
    # -----------------------------------------------------------
    elif action == 'update_avatar':
        remove_image = request.form.get('remove_image')  # optional: '1' means remove

        # First get the current DB path
        with db.get_cursor() as c:
            c.execute("SELECT user_image FROM users WHERE user_id=%s", (user_id,))
            row = c.fetchone()
            current_rel = row['user_image'] if row else None

        # Remove avatar process
        if remove_image and current_rel:
            if remove_uploaded_file(current_rel):
                with db.get_cursor() as c:
                    c.execute(
                        "UPDATE users SET user_image=NULL WHERE user_id=%s",
                        (user_id,)
                    )
                    c.connection.commit()
                flash('Profile image removed.', 'success')
            else:
                flash('Failed to remove profile image.', 'error')
            return redirect(url_for('profile_view', user_id=user_id))

        # Upload avatar process
        file = request.files.get('profile_picture')
        if not file or file.filename == '':
            flash('Please choose an image file.', 'error')
            return redirect(url_for('profile_view', user_id=user_id))

        try:
            # Use the utility function for safe file upload
            rel_path = save_uploaded_file(
                file_storage=file,
                subdir='profiles',
                allowed_exts=PROFILE_ALLOWED_EXTENSIONS,
                filename_prefix=f'user_{user_id}'
            )

            # Remove old image if exists
            if current_rel:
                remove_uploaded_file(current_rel)

            with db.get_cursor() as c:
                c.execute("""
                    UPDATE users
                       SET user_image=%s
                     WHERE user_id=%s
                """, (rel_path, user_id))
                c.connection.commit()

            flash('Profile image updated.', 'success')
        except Exception as e:
            print('update_avatar failed:', e)
            flash('Failed to upload profile picture. Please try again.', 'error')

        return redirect(url_for('profile_view', user_id=user_id))

    # -----------------------------------------------------------
    # 3) Update preferences (not yet stored)                                   
    # -----------------------------------------------------------
    elif action == 'update_prefs':
        flash('Preferences saved (not persisted yet — no DB columns).', 'info')
        return redirect(url_for('profile_view', user_id=user_id))

    # Unknown action
    flash('Unsupported action.', 'error')
    return redirect(url_for('profile_view', user_id=user_id))

@app.route('/change-password', methods=['POST'])
@require_login
def changePassword():
    """
    Change user password using validation functions
    """
    current_user_id = get_current_user_id()
    
    current_password = request.form.get('current_password', '').strip()
    new_password = request.form.get('new_password', '').strip()
    confirm_password = request.form.get('confirm_password', '').strip()
    
    # Validate new password format
    password_error = check_password(new_password)
    if password_error:
        flash(password_error, 'error')
        return redirect(url_for('edit_profile', tab='security'))
    
    # Check if passwords match
    if new_password != confirm_password:
        flash('New passwords do not match.', 'error')
        return redirect(url_for('edit_profile', tab='security'))
    
    try:
        with db.get_cursor() as cursor:
            # Get current password hash
            cursor.execute('SELECT password_hash FROM users WHERE user_id = %s', (current_user_id,))
            user = cursor.fetchone()
            
            if not user:
                flash('User not found.', 'error')
                return redirect(url_for('edit_profile', tab='security'))
            
            # Validate current password
            current_password_error = check_current_password(current_password, user['password_hash'])
            if current_password_error:
                flash(current_password_error, 'error')
                return redirect(url_for('edit_profile', tab='security'))
            
            # Check if new password is different from current
            different_password_error = check_new_password_different(new_password, user['password_hash'])
            if different_password_error:
                flash(different_password_error, 'error')
                return redirect(url_for('edit_profile', tab='security'))
            
            # Hash new password
            from flask_bcrypt import generate_password_hash
            new_password_hash = generate_password_hash(new_password)
            
            # Update password
            cursor.execute('UPDATE users SET password_hash = %s WHERE user_id = %s', 
                         (new_password_hash, current_user_id))
            cursor.connection.commit()
            
            # Send security notification
            noti.create_noti(
                user_id=current_user_id,
                title='Password Changed',
                message='Your password has been successfully changed. If you did not make this change, please contact support immediately.',
                category='system'
            )
            
            flash('Password updated successfully.', 'success')
            return redirect(url_for('edit_profile', tab='security'))
    
    except Exception as e:
        print(f"Error changing password: {e}")
        flash('An error occurred while changing password.', 'error')
        return redirect(url_for('edit_profile', tab='security'))
    
@app.route('/noti/toggle', methods=['POST'])
@require_login
def toggle_notifications():
    """Toggle notifications on/off (called from profile page)"""
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'success': False, 'error': 'Login required'}), 401
    
    try:
        data = request.get_json()
        enabled = data.get('enabled', True)
        
        success = noti.toggle_noti_setting(user_id, enabled)
        
        return jsonify({
            'success': success,
            'enabled': enabled,
            'message': 'Notifications enabled.' if enabled else 'Notifications disabled.'
        })
    except Exception as e:
        print(f"Error toggling notifications: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    
def _fetch_profile_row(user_id: int):
    """
    Return a single user row for profile editing page.
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

