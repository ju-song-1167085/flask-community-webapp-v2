"""
Session management and authentication helper functions for 2-tier role system

New Role Structure:
- platform_role: 'participant', 'super_admin', 'support_technician'
- group_role (for participants): 'member', 'volunteer', 'manager'

Role Permissions:
- super_admin: All permissions
- support_technician: User management, view profiles/history, ban/unban (no role changes)
- participant: Basic user, group-specific permissions via group_role
- group manager: Create events, change member group_roles
- group volunteer: Apply for volunteer activities
- group member: Basic group member

This module provides functions for:
- Session management (create, get, clear)
- 2-tier role-based authentication
- Platform and group permission checks
- Role-based redirection
"""

from functools import wraps
from flask import session, redirect, url_for, render_template, make_response, request
from eventbridge_plus import db, connect  

# =============================================================================
# SESSION MANAGEMENT
# =============================================================================

def create_user_session(user_data, group_data=None):
    """
    Create user session after successful login.

    Args:
        user_data (dict): keys: user_id, username, platform_role
        group_data (dict, optional): keys: group_id, group_role
    """
    session['loggedin'] = True
    session['user_id'] = user_data['user_id']
    session['username'] = user_data['username']
    session['platform_role'] = user_data['platform_role']

    # Group data for participants
    if group_data:
        session['group_id'] = group_data.get('group_id')
        session['group_role'] = group_data.get('group_role')
    else:
        session.pop('group_id', None)
        session.pop('group_role', None)


def get_current_user_id():
    """Get current logged-in user ID (or None)."""
    return session.get('user_id')


def get_current_user_role():
    """Get current user's platform role (or None)."""
    return session.get('platform_role')


def get_current_platform_role():
    """Get platform role in session (or None)."""
    return session.get('platform_role')


def get_current_group_role():
    """Get group role in session (or None)."""
    return session.get('group_role')


def get_current_group_id():
    """Get group id in session (or None)."""
    return session.get('group_id')


def clear_user_session():
    """Clear all user session data (logout)."""
    session.clear()

# =============================================================================
# LOGIN STATUS CHECKS
# =============================================================================

def is_user_logged_in():
    """True if user is logged in."""
    return 'loggedin' in session


def is_participant():
    return get_current_platform_role() == 'participant'


def is_super_admin():
    return get_current_platform_role() == 'super_admin'


def is_support_technician():
    return get_current_platform_role() == 'support_technician'


def is_group_manager():
    return is_participant() and get_current_group_role() == 'manager'


def is_group_volunteer():
    return is_participant() and get_current_group_role() == 'volunteer'


def is_group_member():
    return is_participant() and get_current_group_role() == 'member'

# =============================================================================
# ROLE-BASED REDIRECTION
# =============================================================================

def get_user_home_url():
    """Return role-specific home URL."""
    if not is_user_logged_in():
        return url_for('login')

    platform_role = get_current_platform_role()

    if platform_role == 'super_admin':
        return url_for('admin_dashboard')
    elif platform_role == 'support_technician':
        return url_for('support_dashboard')
    elif platform_role == 'participant':
        group_role = get_current_group_role()
        if group_role == 'manager':
            return url_for('group_manager_dashboard')
        elif group_role == 'volunteer':
            return url_for('participant_dashboard')  # Changed from group_volunteer_dashboard
        else:
            return url_for('participant_dashboard')
    else:
        return url_for('logout')


def redirect_to_user_home():
    return redirect(get_user_home_url())

# =============================================================================
# INTENDED URL (anonymous user flow helpers)
# =============================================================================

def save_intended_url(url=None):
    """
    Save the URL that anonymous user was trying to access.
    If url is not provided, uses current request.url
    """
    if url is None:
        url = request.url
    # Only save relative paths within the site (avoid Open Redirect)
    if url and url.startswith(request.url_root):
        # Convert to relative path
        url = url[len(request.url_root) - 1:]
    if url and url.startswith('/'):
        session['intended_url'] = url

def get_intended_url():
    """Get and clear the intended URL from session."""
    return session.pop('intended_url', None)

def has_intended_url():
    """Check if there's an intended URL saved."""
    return 'intended_url' in session

# Deprecated event-specific functions (kept for backwards compatibility)
# Note: Use save_intended_url() instead for new code
def save_intended_event(event_id):
    """Deprecated: Save intended event ID. Use save_intended_url() instead."""
    session['intended_event_id'] = event_id


def get_intended_event():
    """Deprecated: Get and clear intended event ID. Use get_intended_url() instead."""
    return session.pop('intended_event_id', None)


def has_intended_event():
    """Deprecated: Check if there's an intended event. Use has_intended_url() instead."""
    return 'intended_event_id' in session

# =============================================================================
# PERMISSION CHECKS
# =============================================================================

def has_platform_permission(required_role):
    """
    Platform hierarchy: super_admin > support_technician > participant
    """
    if not is_user_logged_in():
        return False

    user_role = get_current_platform_role()
    platform_hierarchy = {
        'super_admin': ['super_admin', 'support_technician', 'participant'],
        'support_technician': ['support_technician', 'participant'],
        'participant': ['participant']
    }
    return required_role in platform_hierarchy.get(user_role, [])


def has_group_permission(required_group_role):
    """
    Group hierarchy: manager > volunteer > member
    """
    if not is_participant():
        return False

    user_group_role = get_current_group_role()
    if not user_group_role:
        return False

    group_hierarchy = {
        'manager': ['manager', 'volunteer', 'member'],
        'volunteer': ['volunteer', 'member'],
        'member': ['member']
    }
    return required_group_role in group_hierarchy.get(user_group_role, [])


def can_view_user_profiles():
    return is_super_admin() or is_support_technician()


def can_view_user_history():
    return is_super_admin() or is_support_technician()


def can_ban_unban_users():
    return is_super_admin() or is_support_technician()


def can_access_troubleshooting():
    return is_super_admin() or is_support_technician()


def can_manage_users():
    return is_super_admin() or is_support_technician()


def can_change_platform_roles():
    return is_super_admin()  # only super_admin


def can_change_group_roles():
    return is_super_admin() or is_group_manager()


def can_change_group_roles_in_specific_group(target_group_id):
    if is_super_admin():
        return True
    if is_group_manager():
        # Check if user is actually a manager of the target group
        user_id = get_current_user_id()
        if not user_id:
            return False
        
        try:
            with db.get_cursor() as cursor:
                cursor.execute("""
                    SELECT 1 
                    FROM group_members 
                    WHERE user_id = %s AND group_id = %s 
                      AND group_role = 'manager' AND status = 'active'
                """, (user_id, target_group_id))
                return cursor.fetchone() is not None
        except Exception:
            return False
    return False


def can_create_events():
    return is_super_admin() or is_group_manager()


def can_apply_volunteer():
    return is_super_admin() or is_group_volunteer() or is_group_manager()

# =============================================================================
# AUTH DECORATORS
# =============================================================================

def require_login(f):
    """Require login for protected routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_user_logged_in():
            # Save current URL for redirect after login/signup
            save_intended_url()
            return redirect(url_for('login'))
        
        # Prevent browser caching for security
        response = make_response(f(*args, **kwargs))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    return decorated_function

def require_platform_role(*allowed_platform_roles):
    """
    Decorator factory for platform roles.
    Usage:
        @require_platform_role('super_admin')
        @require_platform_role('super_admin', 'support_technician')
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not is_user_logged_in():
                # Save current URL as next parameter for redirect after login/signup
                next_url = request.url
                return redirect(url_for('login', next=next_url))

            user_platform_role = get_current_platform_role()
            if user_platform_role not in allowed_platform_roles:
                return render_template('access_denied.html'), 403

            # Prevent browser caching for security
            response = make_response(f(*args, **kwargs))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response
        return decorated_function
    return decorator


def require_group_role(*allowed_group_roles):
    """
    Decorator factory for group roles (participants only).
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not is_user_logged_in():
                # Save current URL as next parameter for redirect after login/signup
                next_url = request.url
                return redirect(url_for('login', next=next_url))

            # Super admin bypass
            if is_super_admin():
                response = make_response(f(*args, **kwargs))
                response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = '0'
                return response

            if not is_participant():
                return render_template('access_denied.html'), 403

            user_group_role = get_current_group_role()
            if not user_group_role or user_group_role not in allowed_group_roles:
                return render_template('access_denied.html'), 403

            response = make_response(f(*args, **kwargs))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response
        return decorated_function
    return decorator


def require_permission(permission_func):
    """
    Decorator factory for custom permission function.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not is_user_logged_in():
                return redirect(url_for('login'))

            if not permission_func():
                return render_template('access_denied.html'), 403

            response = make_response(f(*args, **kwargs))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response
        return decorated_function
    return decorator

# =============================================================================
# GROUP MANAGEMENT HELPERS
# =============================================================================

def get_user_group_info(user_id):
    """Return {'group_id', 'group_role'} with highest privilege role."""
    try:
        with db.get_cursor() as cursor:
            cursor.execute("""
                SELECT group_id, group_role
                FROM group_members
                WHERE user_id = %s AND status = 'active'
                ORDER BY 
                    CASE group_role
                        WHEN 'manager' THEN 1
                        WHEN 'volunteer' THEN 2
                        WHEN 'member' THEN 3
                    END
                LIMIT 1
            """, (user_id,))
            result = cursor.fetchone()
            return result if result else None
    except Exception as e:
        print(f"Error getting user group info: {e}")
        return None


def refresh_user_group_session(user_id):
    """Refresh group_id & group_role in session from DB."""
    group_info = get_user_group_info(user_id)
    if group_info:
        session['group_id'] = group_info['group_id']
        session['group_role'] = group_info['group_role']
        return True
    else:
        session.pop('group_id', None)
        session.pop('group_role', None)
        return False

# =============================================================================
# COMPATIBILITY ALIASES (for existing code)
# =============================================================================

def role_required(*allowed_platform_roles):
    """
    Compatibility wrapper so existing code can use:
        @role_required('super_admin')
        @role_required('super_admin', 'support_technician')
    Internally delegates to require_platform_role.
    """
    return require_platform_role(*allowed_platform_roles)

def super_admin_required(view_func):
    """Convenience decorator: only super admin."""
    return require_platform_role('super_admin')(view_func)

__all__ = [
    # session helpers
    'create_user_session', 'get_current_user_id', 'get_current_platform_role',
    'get_current_group_role', 'get_current_group_id', 'clear_user_session',
    # role checks
    'is_user_logged_in', 'is_participant', 'is_super_admin', 'is_support_technician',
    'is_group_manager', 'is_group_volunteer', 'is_group_member',
    # permission checks
    'has_platform_permission', 'has_group_permission', 'can_view_user_profiles',
    'can_view_user_history', 'can_ban_unban_users', 'can_access_troubleshooting',
    'can_manage_users', 'can_change_platform_roles', 'can_change_group_roles',
    'can_change_group_roles_in_specific_group', 'can_create_events',
    'can_apply_volunteer',
    # decorators
    'require_login', 'require_platform_role', 'require_group_role', 'require_permission',
    # compatibility / shortcuts
    'role_required', 'super_admin_required',
    # group helpers
    'get_user_group_info', 'refresh_user_group_session',
]
