"""
Helpdesk system implementation for ActiveLoop Plus

This module provides:
1. Help request submission and management for participants
2. Support queue management for support technicians and super admins
3. Conversation history and reply system
4. Status tracking and filtering capabilities
"""

from eventbridge_plus import app, db
from flask import render_template, request, redirect, url_for, flash, jsonify
from .auth import (
    require_login, require_platform_role, 
    get_current_user_id, get_current_platform_role, is_participant, is_support_technician, is_super_admin, has_platform_permission
)
from .util import (
    get_pagination_params, create_pagination_info, create_pagination_links,
    HELP_REQUEST_CATEGORIES, HELP_REQUEST_PRIORITIES, HELP_REQUEST_STATUSES,
    MAX_HELP_TITLE_LENGTH, MIN_HELP_TITLE_LENGTH,
    MAX_HELP_DESCRIPTION_LENGTH, MIN_HELP_DESCRIPTION_LENGTH,
    MAX_HELP_REPLY_LENGTH
)
from .noti import create_noti
from datetime import datetime
import json

# =============================================================================
# VALIDATION FUNCTIONS
# =============================================================================

def validate_help_request_title(title):
    """Validate help request title using constants from util"""
    if not title or not title.strip():
        return 'Title is required'
    
    title = title.strip()
    if len(title) > MAX_HELP_TITLE_LENGTH:
        return f'Title must be {MAX_HELP_TITLE_LENGTH} characters or less'
    
    if len(title) < MIN_HELP_TITLE_LENGTH:
        return f'Title must be at least {MIN_HELP_TITLE_LENGTH} characters long'
    
    return None

def validate_help_request_description(description):
    """Validate help request description using constants from util"""
    if not description or not description.strip():
        return 'Description is required'
    
    description = description.strip()
    if len(description) < MIN_HELP_DESCRIPTION_LENGTH:
        return f'Description must be at least {MIN_HELP_DESCRIPTION_LENGTH} characters long'
    
    if len(description) > MAX_HELP_DESCRIPTION_LENGTH:
        return f'Description must be {MAX_HELP_DESCRIPTION_LENGTH} characters or less'
    
    return None

def validate_help_request_category(category):
    """Validate help request category using constants from util"""
    if not category:
        return 'Category is required'
    
    if category not in HELP_REQUEST_CATEGORIES:
        return 'Please select a valid category'
    
    return None

def validate_help_request_priority(priority):
    """Validate help request priority using constants from util"""
    if not priority:
        return 'Priority is required'
    
    if priority not in HELP_REQUEST_PRIORITIES:
        return 'Please select a valid priority'
    
    return None

def validate_reply_content(reply_content):
    """Validate reply content using constants from util"""
    if not reply_content or not reply_content.strip():
        return 'Reply content cannot be empty'
    
    reply_content = reply_content.strip()
    if len(reply_content) < 1:
        return 'Reply content cannot be empty'
    
    if len(reply_content) > MAX_HELP_REPLY_LENGTH:
        return f'Reply content must be {MAX_HELP_REPLY_LENGTH} characters or less'
    
    return None

def clean_reply_content(reply_content):
    """Remove visibility and response requirement markers from reply content for display"""
    if not reply_content:
        return reply_content
    
    # Remove markers
    content = reply_content
    content = content.replace('[INTERNAL_ONLY]', '').replace('[/INTERNAL_ONLY]', '')
    content = content.replace('[REQUIRES_USER_RESPONSE]', '').replace('[/REQUIRES_USER_RESPONSE]', '')
    
    return content.strip()

def is_internal_reply(reply_content):
    """Check if a reply is marked as internal-only"""
    return '[INTERNAL_ONLY]' in reply_content if reply_content else False

def requires_user_response(reply_content):
    """Check if a reply requires user response"""
    return '[REQUIRES_USER_RESPONSE]' in reply_content if reply_content else False

def get_valid_status_transitions(current_status):
    """Get valid status transitions based on current status"""
    # Define valid status flow rules
    status_transitions = {
        'new': ['assigned', 'solved'],  # New can only go to Assigned or Solved
        'assigned': ['new', 'blocked', 'solved'],  # Assigned can go to New (unassign), Blocked or Solved
        'blocked': ['new', 'assigned', 'solved'],  # Blocked can go to New (unassign), Assigned or Solved
        'solved': []  # Solved is final state - no transitions allowed
    }
    
    return status_transitions.get(current_status, [])

def is_valid_status_transition(current_status, new_status):
    """Check if a status transition is valid"""
    valid_transitions = get_valid_status_transitions(current_status)
    return new_status in valid_transitions

def validate_status_transition(current_status, new_status):
    """Validate status transition and return error message if invalid"""
    if current_status == new_status:
        return None  # No change is always valid
    
    if not is_valid_status_transition(current_status, new_status):
        if current_status == 'solved':
            return f"Cannot change status from '{current_status}'. This request is already solved."
        else:
            valid_transitions = get_valid_status_transitions(current_status)
            return f"Cannot change status from '{current_status}' to '{new_status}'. Valid transitions: {', '.join(valid_transitions)}"
    
    return None

# =============================================================================
# DATABASE FUNCTIONS
# =============================================================================

def create_help_request(user_id, category, title, description, priority='medium'):
    """Create a new help request"""
    try:
        with db.get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO help_requests (user_id, category, title, description, priority, status)
                VALUES (%s, %s, %s, %s, %s, 'new')
            """, (user_id, category, title, description, priority))
            
            request_id = cursor.lastrowid
            
            # Send notification to user about successful submission
            create_noti(
                user_id=user_id,
                title="Help Request Submitted",
                message=f"Your help request #" f"{request_id}: '{title}' has been submitted successfully. We will get back to you soon.",
                category="help_request",
                related_id=request_id
            )
            
            # Auto-assign new requests immediately
            try:
                from .assign_request import simple_auto_assign
                if priority != 'high':  # Auto-assign all except high priority (super_admin only)
                    success, assigned_to, message = simple_auto_assign(request_id, priority)
                    pass  # Auto-assignment completed
            except Exception as auto_error:
                pass  # Auto-assignment error
            
            return request_id
    except Exception as e:
        return None

def get_help_request(request_id, user_id=None):
    """Get help request details with user information"""
    try:
        with db.get_cursor() as cursor:
            if user_id:
                # For participants - only their own requests
                cursor.execute("""
                    SELECT hr.*, u.username, u.first_name, u.last_name, u.email,
                           assigned.username as assigned_username,
                           assigned.first_name as assigned_first_name,
                           assigned.last_name as assigned_last_name
                    FROM help_requests hr
                    JOIN users u ON hr.user_id = u.user_id
                    LEFT JOIN users assigned ON hr.assigned_to = assigned.user_id
                    WHERE hr.request_id = %s AND hr.user_id = %s
                """, (request_id, user_id))
            else:
                # For support staff - all requests
                cursor.execute("""
                    SELECT hr.*, u.username, u.first_name, u.last_name, u.email,
                           assigned.username as assigned_username,
                           assigned.first_name as assigned_first_name,
                           assigned.last_name as assigned_last_name
                    FROM help_requests hr
                    JOIN users u ON hr.user_id = u.user_id
                    LEFT JOIN users assigned ON hr.assigned_to = assigned.user_id
                    WHERE hr.request_id = %s
                """, (request_id,))
            return cursor.fetchone()
    except Exception as e:
        return None

def get_user_help_requests_filtered(user_id, status_filter=None, sort_by='last_activity_desc'):
    """Get help requests for a specific user with filtering and sorting"""
    try:
        with db.get_cursor() as cursor:
            # Build WHERE clause
            where_clause = "WHERE hr.user_id = %s"
            params = [user_id]
            
            if status_filter and status_filter != 'all':
                where_clause += " AND hr.status = %s"
                params.append(status_filter)
            
            # Build ORDER BY clause
            if sort_by == 'last_activity_desc':
                order_clause = "ORDER BY last_activity DESC"
            elif sort_by == 'last_activity_asc':
                order_clause = "ORDER BY last_activity ASC"
            elif sort_by == 'priority_desc':
                # Priority order: urgent > high > medium > low
                order_clause = "ORDER BY CASE hr.priority WHEN 'urgent' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 WHEN 'low' THEN 4 ELSE 5 END ASC"
            elif sort_by == 'priority_asc':
                # Priority order: low > medium > high > urgent
                order_clause = "ORDER BY CASE hr.priority WHEN 'urgent' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 WHEN 'low' THEN 4 ELSE 5 END DESC"
            elif sort_by == 'title_asc':
                order_clause = "ORDER BY hr.title ASC"
            elif sort_by == 'title_desc':
                order_clause = "ORDER BY hr.title DESC"
            else:
                order_clause = "ORDER BY last_activity DESC"
            
            # Get all requests with last activity time
            cursor.execute(f"""
                SELECT hr.*, u.username, u.first_name, u.last_name,
                       GREATEST(hr.updated_at, COALESCE(hr.last_staff_reply_at, hr.created_at)) as last_activity
                FROM help_requests hr
                JOIN users u ON hr.user_id = u.user_id
                {where_clause}
                {order_clause}
            """, params)
            requests = cursor.fetchall()
            
            return requests
    except Exception as e:
        return []

def get_support_manage_requests(filters=None, page=1, per_page=20, sort_by='priority', sort_order='asc'):
    """Get help requests for support queue with filtering and pagination"""
    try:
        with db.get_cursor() as cursor:
            # Build WHERE clause based on filters
            where_conditions = []
            params = []
            
            if filters:
                if filters.get('status'):
                    where_conditions.append("hr.status = %s")
                    params.append(filters['status'])
                
                if filters.get('category'):
                    where_conditions.append("hr.category = %s")
                    params.append(filters['category'])
                
                if filters.get('priority'):
                    where_conditions.append("hr.priority = %s")
                    params.append(filters['priority'])
                
                if filters.get('assigned_to'):
                    if filters['assigned_to'] == 'unassigned':
                        where_conditions.append("hr.assigned_to IS NULL")
                    else:
                        where_conditions.append("hr.assigned_to = %s")
                        params.append(filters['assigned_to'])
                
                if filters.get('username'):
                    where_conditions.append("u.username LIKE %s")
                    params.append(f"%{filters['username']}%")
            
            where_clause = "WHERE " + " AND ".join(where_conditions) if where_conditions else ""
            
            # Build ORDER BY clause
            valid_sort_columns = {
                'request_id': 'hr.request_id',
                'title': 'hr.title',
                'username': 'u.username',
                'category': 'hr.category',
                'priority': 'hr.priority',
                'status': 'hr.status',
                'assigned_to': 'assigned.username',
                'created_at': 'hr.created_at',
                'last_staff_reply_at': 'hr.last_staff_reply_at'
            }
            
            # Default sorting by priority and created_at
            if sort_by in valid_sort_columns:
                if sort_by == 'priority':
                    # Special handling for priority to maintain logical order
                    order_clause = f"""
                        ORDER BY 
                            CASE hr.priority
                                WHEN 'urgent' THEN 1
                                WHEN 'high' THEN 2
                                WHEN 'medium' THEN 3
                                WHEN 'low' THEN 4
                            END {sort_order.upper()},
                            hr.created_at ASC
                    """
                else:
                    order_clause = f"ORDER BY {valid_sort_columns[sort_by]} {sort_order.upper()}"
            else:
                # Default sorting
                order_clause = """
                    ORDER BY 
                        CASE hr.priority
                            WHEN 'urgent' THEN 1
                            WHEN 'high' THEN 2
                            WHEN 'medium' THEN 3
                            WHEN 'low' THEN 4
                        END,
                        hr.created_at ASC
                """
            
            # Get total count
            count_query = f"""
                SELECT COUNT(*) as total
                FROM help_requests hr
                JOIN users u ON hr.user_id = u.user_id
                {where_clause}
            """
            cursor.execute(count_query, params)
            total = cursor.fetchone()['total']
            
            # Get paginated results
            offset = (page - 1) * per_page
            query = f"""
                SELECT hr.*, u.username, u.first_name, u.last_name, u.email,
                       assigned.username as assigned_username,
                       assigned.first_name as assigned_first_name,
                       assigned.last_name as assigned_last_name
                FROM help_requests hr
                JOIN users u ON hr.user_id = u.user_id
                LEFT JOIN users assigned ON hr.assigned_to = assigned.user_id
                {where_clause}
                {order_clause}
                LIMIT %s OFFSET %s
            """
            cursor.execute(query, params + [per_page, offset])
            requests = cursor.fetchall()
            
            return requests, total
    except Exception as e:
        return [], 0

def get_help_replies(request_id, user_role=None):
    """Get all replies for a help request, filtering by visibility based on user role"""
    try:
        with db.get_cursor() as cursor:
            cursor.execute("""
                SELECT hr.*, u.username, u.first_name, u.last_name, u.platform_role
                FROM help_replies hr
                JOIN users u ON hr.sender_id = u.user_id
                WHERE hr.request_id = %s
                ORDER BY hr.created_at ASC
            """, (request_id,))
            all_replies = cursor.fetchall()
            
            # Filter replies based on user role and visibility markers
            filtered_replies = []
            for reply in all_replies:
                # For support staff, show all replies
                if user_role in ['super_admin', 'support_technician']:
                    filtered_replies.append(reply)
                else:
                    # For participants, only show user-visible replies (no INTERNAL_ONLY marker)
                    if '[INTERNAL_ONLY]' not in reply['reply_content']:
                        filtered_replies.append(reply)
            
            return filtered_replies
    except Exception as e:
        return []

def add_help_reply(request_id, sender_id, reply_content, visibility='user_visible', requires_user_response=False, mark_as_solved=False):
    """Add a reply to a help request"""
    try:
        with db.get_cursor() as cursor:
            # First, verify the request exists
            cursor.execute("""
                SELECT request_id FROM help_requests WHERE request_id = %s
            """, (request_id,))
            if not cursor.fetchone():
                return None
            
            # Enforce permission constraint at insertion point:
            # Only the request owner (participant) or the assigned staff (support/super_admin) may reply.
            cursor.execute("""
                SELECT hr.user_id AS owner_id, hr.assigned_to
                FROM help_requests hr
                WHERE hr.request_id = %s
            """, (request_id,))
            req_info = cursor.fetchone()
            if not req_info:
                return None

            cursor.execute("""
                SELECT platform_role FROM users WHERE user_id = %s
            """, (sender_id,))
            sender_row = cursor.fetchone()
            if not sender_row:
                return None
            sender_role = sender_row['platform_role']

            owner_id = req_info['owner_id']
            assigned_to = req_info['assigned_to']

            is_staff = sender_role in ['super_admin', 'support_technician']
            if is_staff:
                # Staff must be the assigned user
                if not assigned_to or assigned_to != sender_id:
                    return None
            else:
                # Non-staff must be the request owner
                if owner_id != sender_id:
                    return None

            # Add visibility marker to reply content for internal-only messages
            if visibility == 'internal_only':
                reply_content = f"[INTERNAL_ONLY]{reply_content}[/INTERNAL_ONLY]"
            
            # Add user response requirement marker
            if requires_user_response:
                reply_content = f"[REQUIRES_USER_RESPONSE]{reply_content}[/REQUIRES_USER_RESPONSE]"
            
            # Insert the reply
            cursor.execute("""
                INSERT INTO help_replies (request_id, sender_id, reply_content)
                VALUES (%s, %s, %s)
            """, (request_id, sender_id, reply_content))
            
            # Get the inserted reply ID
            reply_id = cursor.lastrowid
            if not reply_id:
                return None
            
            # Get sender role and request owner info
            cursor.execute("""
                SELECT platform_role FROM users WHERE user_id = %s
            """, (sender_id,))
            user_result = cursor.fetchone()
            if not user_result:
                return None
            user_role = user_result['platform_role']
            
            # Get request owner info for notifications
            cursor.execute("""
                SELECT hr.user_id, hr.title, u.first_name, u.last_name
                FROM help_requests hr
                JOIN users u ON hr.user_id = u.user_id
                WHERE hr.request_id = %s
            """, (request_id,))
            request_info = cursor.fetchone()
            
            if user_role in ['super_admin', 'support_technician']:
                # Update last staff reply timestamp
                cursor.execute("""
                    UPDATE help_requests 
                    SET last_staff_reply_at = NOW(), updated_at = NOW()
                    WHERE request_id = %s
                """, (request_id,))
                
                # Handle status changes based on reply action
                if requires_user_response:
                    cursor.execute("""
                        UPDATE help_requests 
                        SET status = 'blocked', updated_at = NOW()
                        WHERE request_id = %s
                    """, (request_id,))
                elif mark_as_solved:
                    cursor.execute("""
                        UPDATE help_requests 
                        SET status = 'solved', resolved_at = NOW(), updated_at = NOW()
                        WHERE request_id = %s
                    """, (request_id,))
                
                # Send notification to request owner about staff reply (only for user-visible replies)
                if visibility == 'user_visible' and request_info and request_info['user_id'] != sender_id:
                    try:
                        create_noti(
                            user_id=request_info['user_id'],
                            title="New Reply to Your Help Request",
                            message=f"Support staff has replied to your help request #" f"{request_id}: {request_info['title']}",
                            category="system",
                            related_id=request_id
                        )
                    except Exception as noti_error:
                        pass  # Notification failed
            else:
                # User reply - check if we need to unblock the request
                cursor.execute("""
                    SELECT status FROM help_requests WHERE request_id = %s
                """, (request_id,))
                status_result = cursor.fetchone()
                current_status = status_result['status'] if status_result else None
                
                # If request is blocked and user replied, change status to assigned
                if current_status == 'blocked':
                    cursor.execute("""
                        UPDATE help_requests 
                        SET status = 'assigned', updated_at = NOW()
                        WHERE request_id = %s
                    """, (request_id,))
                
                # Send notification to assigned staff about user reply
                cursor.execute("""
                    SELECT assigned_to FROM help_requests WHERE request_id = %s
                """, (request_id,))
                assigned_result = cursor.fetchone()
                assigned_to = assigned_result['assigned_to'] if assigned_result else None
                
                if assigned_to and assigned_to != sender_id:
                    try:
                        create_noti(
                            user_id=assigned_to,
                            title="New Reply to Assigned Request",
                            message=f"User has replied to help request #" f"{request_id}: {request_info['title']}",
                            category="system",
                            related_id=request_id
                        )
                    except Exception as noti_error:
                        pass  # Notification failed
            
            return reply_id
    except Exception as e:
        return None

def update_help_request_status(request_id, status, assigned_to=None, priority=None, bypass_validation=False):
    """Update help request status, assignment and priority"""
    try:
        with db.get_cursor() as cursor:
            # Get current request info before updating
            cursor.execute("""
                SELECT hr.user_id, hr.title, hr.status as old_status, hr.assigned_to as old_assigned_to,
                       hr.priority as old_priority,
                       u.first_name, u.last_name
                FROM help_requests hr
                JOIN users u ON hr.user_id = u.user_id
                WHERE hr.request_id = %s
            """, (request_id,))
            request_info = cursor.fetchone()
            
            if not request_info:
                return False
            
            # Validate status transition (unless bypassed for super admin operations)
            if not bypass_validation:
                current_status = request_info['old_status']
                transition_error = validate_status_transition(current_status, status)
                if transition_error:
                    return False
            
            update_fields = ["status = %s", "updated_at = NOW()"]
            params = [status]
            
            # Always update assigned_to (either to a value or NULL)
            if assigned_to is not None:
                update_fields.append("assigned_to = %s")
                params.append(assigned_to)
            else:
                update_fields.append("assigned_to = NULL")
            
            if priority is not None:
                update_fields.append("priority = %s")
                params.append(priority)
            
            if status == 'resolved':
                update_fields.append("resolved_at = NOW()")
            
            params.append(request_id)
            
            cursor.execute(f"""
                UPDATE help_requests 
                SET {', '.join(update_fields)}
                WHERE request_id = %s
            """, params)
            
            # Send notifications for status changes
            if request_info['old_status'] != status:
                # Status changed - notify request owner
                try:
                    create_noti(
                        user_id=request_info['user_id'],
                        title="Help Request Status Updated",
                        message=f"Your help request #" f"{request_id}: '{request_info['title']}' status has been changed to {status.title()}",
                        category="help_request",
                        related_id=request_id
                    )
                except Exception as noti_error:
                    pass  # Notification failed
            
            # Send notifications for assignment changes
            if request_info['old_assigned_to'] != assigned_to:
                if assigned_to is not None:
                    # New assignment - notify the assigned staff member
                    cursor.execute("""
                        SELECT first_name, last_name FROM users WHERE user_id = %s
                    """, (assigned_to,))
                    staff_info = cursor.fetchone()
                    
                    try:
                        create_noti(
                            user_id=assigned_to,
                            title="New Task Assignment",
                            message=f"You have been assigned to help request #{request_id}: '{request_info['title']}' from {request_info['first_name']} {request_info['last_name']}",
                            category="help_request",
                            related_id=request_id,
                            force=True
                        )
                    except Exception as noti_error:
                        pass  # Notification failed
                    
                    # Also notify request owner about assignment
                    staff_name = f"{staff_info['first_name']} {staff_info['last_name']}" if staff_info else "a support staff member"
                    try:
                        create_noti(
                            user_id=request_info['user_id'],
                            title="Help Request Assigned",
                            message=f"Your help request '{request_info['title']}' has been assigned to {staff_name}",
                            category="help_request",
                            related_id=request_id
                        )
                    except Exception as noti_error:
                        pass  # Notification failed
                else:
                    # Request was unassigned - notify both the previously assigned staff and request owner
                    if request_info['old_assigned_to']:
                        # Notify the previously assigned staff member
                        try:
                            create_noti(
                                user_id=request_info['old_assigned_to'],
                                title="Request Unassigned",
                                message=f"Help request #{request_id}: '{request_info['title']}' has been unassigned from you",
                                category="help_request",
                                related_id=request_id,
                                force=True
                            )
                        except Exception as noti_error:
                            pass  # Notification failed
                    
                    # Notify request owner about unassignment
                    try:
                        create_noti(
                            user_id=request_info['user_id'],
                            title="Help Request Unassigned",
                            message=f"Your help request '{request_info['title']}' has been unassigned and is back in the queue",
                            category="help_request",
                            related_id=request_id
                        )
                    except Exception as noti_error:
                        pass  # Notification failed
            
            # Send escalation notifications on priority raise to HIGH
            if priority is not None and request_info.get('old_priority') != priority and priority == 'high':
                # Notify current assignee (if any)
                if assigned_to is not None:
                    try:
                        create_noti(
                            user_id=assigned_to,
                            title="Escalated Help Request",
                            message=f"High priority help request #{request_id}: '{request_info['title']}' has been escalated to you",
                            category="help_request",
                            related_id=request_id,
                            force=True
                        )
                    except Exception:
                        pass
                # If reassigned on escalation, notify previous assignee as well
                if request_info.get('old_assigned_to') and request_info.get('old_assigned_to') != assigned_to:
                    try:
                        create_noti(
                            user_id=request_info['old_assigned_to'],
                            title="Request Escalated and Reassigned",
                            message=f"Help request #{request_id}: '{request_info['title']}' was escalated to HIGH and reassigned.",
                            category="help_request",
                            related_id=request_id,
                            force=True
                        )
                    except Exception:
                        pass

            return True
    except Exception as e:
        return False

def get_support_staff(priority=None):
    """Get list of support staff for assignment dropdown"""
    try:
        with db.get_cursor() as cursor:
            # For high priority, only show super admins
            if priority == 'high':
                cursor.execute("""
                    SELECT user_id, username, first_name, last_name, platform_role
                    FROM users
                    WHERE platform_role = 'super_admin'
                    AND status = 'active'
                    ORDER BY first_name, last_name
                """)
            else:
                # For normal/low/urgent priority, show all support staff
                cursor.execute("""
                    SELECT user_id, username, first_name, last_name, platform_role
                    FROM users
                    WHERE platform_role IN ('super_admin', 'support_technician')
                    AND status = 'active'
                    ORDER BY 
                        CASE platform_role
                            WHEN 'super_admin' THEN 1
                            WHEN 'support_technician' THEN 2
                        END,
                        first_name, last_name
                """)
            return cursor.fetchall()
    except Exception as e:
        return []

# =============================================================================
# ROUTES - PARTICIPANT FUNCTIONS
# =============================================================================

@app.route('/helpdesk')
@require_login
def helpdesk_home():
    """Helpdesk home page - shows different views based on user role"""
    user_id = get_current_user_id()
    
    if is_participant():
        # Show participant's help requests with filtering and sorting
        status_filter = request.args.get('status', 'all')
        sort_by = request.args.get('sort_by', 'last_activity_desc')
        
        requests = get_user_help_requests_filtered(user_id, status_filter, sort_by)
        
        return render_template('helpdesk/my_help_requests.html',
                             requests=requests,
                             current_status=status_filter,
                             current_sort=sort_by)
    
    elif is_support_technician() or is_super_admin():
        # Show support queue
        return redirect(url_for('support_manage'))
    
    else:
        flash('Access denied', 'error')
        return redirect(url_for('login'))

@app.route('/helpdesk/faq')
def faq():
    """FAQ page - shows frequently asked questions (accessible to all users)"""
    return render_template('helpdesk/faq.html')

@app.route('/helpdesk/submit', methods=['GET', 'POST'])
@require_login
def submit_help_request():
    """Submit a new help request"""
    # All logged-in users can submit help requests
    
    if request.method == 'POST':
        category = request.form.get('category')
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        priority = 'medium'  # Always set to medium for user submissions
        
        # Validation using dedicated validation functions
        validation_errors = []
        
        title_error = validate_help_request_title(title)
        if title_error:
            validation_errors.append(title_error)
        
        description_error = validate_help_request_description(description)
        if description_error:
            validation_errors.append(description_error)
        
        category_error = validate_help_request_category(category)
        if category_error:
            validation_errors.append(category_error)
        
        if validation_errors:
            for error in validation_errors:
                flash(error, 'error')
            return render_template('helpdesk/submit_request.html')
        
        user_id = get_current_user_id()
        request_id = create_help_request(user_id, category, title, description, priority)
        
        if request_id:
            flash(f'Help request #{request_id} submitted successfully! We will get back to you soon.', 'success')
            return redirect(url_for('view_help_request', request_id=request_id))
        else:
            flash('Failed to submit help request. Please try again.', 'error')
    
    return render_template('helpdesk/submit_request.html')

@app.route('/helpdesk/request/<int:request_id>')
@require_login
def view_help_request(request_id):
    """View help request details and conversation"""
    user_id = get_current_user_id()
    
    # Get request details
    if is_participant():
        request_data = get_help_request(request_id, user_id)
    else:
        request_data = get_help_request(request_id)
    
    if not request_data:
        flash('Help request not found', 'error')
        return redirect(url_for('helpdesk_home'))
    
    # Get conversation history (filtered by user role)
    user_role = get_current_platform_role()
    replies = get_help_replies(request_id, user_role)
    
    # Get support staff list for assignment (for staff only)
    support_staff = []
    if is_support_technician() or is_super_admin():
        support_staff = get_support_staff(request_data['priority'])
    
    return render_template('helpdesk/view_request.html',
                         request=request_data,
                         replies=replies,
                         support_staff=support_staff)

@app.route('/helpdesk/request/<int:request_id>/reply', methods=['POST'])
@require_login
def reply_to_help_request(request_id):
    """Add a reply to a help request"""
    user_id = get_current_user_id()
    reply_content = request.form.get('reply_content', '').strip()
    
    # Get visibility and reply action for support staff
    visibility = request.form.get('visibility', 'user_visible')
    reply_action = request.form.get('reply_action', 'normal')
    
    # Determine if user response is required and if should mark as solved
    # Internal Only replies don't affect status changes
    if visibility == 'internal_only':
        requires_user_response = False
        mark_as_solved = False
    else:
        requires_user_response = reply_action == 'requires_response'
        mark_as_solved = reply_action == 'mark_solved'
    
    # Validate reply content (skip validation if marking as solved)
    if not mark_as_solved:
        reply_error = validate_reply_content(reply_content)
        if reply_error:
            flash(reply_error, 'error')
            return redirect(url_for('view_help_request', request_id=request_id))
    
    # Validate visibility (only support staff can set internal-only)
    if visibility == 'internal_only' and not (is_support_technician() or is_super_admin()):
        visibility = 'user_visible'
    
    # Validate reply actions (only support staff can set these)
    if not (is_support_technician() or is_super_admin()):
        requires_user_response = False
        mark_as_solved = False
    
    # Check if user can reply to this request
    if is_participant():
        # Participants can only see and reply to their own requests
        request_data = get_help_request(request_id, user_id)
    else:
        request_data = get_help_request(request_id)
    
    if not request_data:
        flash('Help request not found', 'error')
        return redirect(url_for('helpdesk_home'))

    # Enforce reply permissions: only the assigned support staff (or the request owner) may reply
    if is_support_technician() or is_super_admin():
        assigned_to = request_data.get('assigned_to') if isinstance(request_data, dict) else request_data['assigned_to']
        if not assigned_to or assigned_to != user_id:
            flash('Only the assigned support staff can reply to this request. Assign it to yourself first.', 'error')
            return redirect(url_for('view_help_request', request_id=request_id))
    
    # Add reply
    reply_id = add_help_reply(request_id, user_id, reply_content, visibility, requires_user_response, mark_as_solved)
    
    if reply_id:
        flash('Reply added successfully', 'success')
    else:
        flash('Failed to add reply. Please try again or contact support if the problem persists.', 'error')
    
    return redirect(url_for('view_help_request', request_id=request_id))

# =============================================================================
# ROUTES - SUPPORT STAFF FUNCTIONS
# =============================================================================

@app.route('/helpdesk/support-manage')
@require_platform_role('super_admin', 'support_technician')
def support_manage():
    """Support queue management page"""
    # Get filters from query parameters
    filters = {}
    if request.args.get('status'):
        filters['status'] = request.args.get('status')
    if request.args.get('category'):
        filters['category'] = request.args.get('category')
    if request.args.get('priority'):
        filters['priority'] = request.args.get('priority')
    if request.args.get('assigned_to'):
        filters['assigned_to'] = request.args.get('assigned_to')
    if request.args.get('username'):
        filters['username'] = request.args.get('username')
    
    # Get sorting parameters
    sort_by = request.args.get('sort_by', 'priority')
    sort_order = request.args.get('sort_order', 'asc')
    
    page, per_page = get_pagination_params(request)
    requests, total = get_support_manage_requests(filters, page, per_page, sort_by, sort_order)
    
    # Remove pagination params from request.args to avoid duplication
    pagination_params = {k: v for k, v in request.args.items() 
                        if k not in ['page', 'per_page']}
    
    pagination_info = create_pagination_info(
        page, per_page, total,
        url_for('support_manage'),
        **pagination_params
    )
    # Build pagination links for template component consistency
    pagination_links = create_pagination_links(pagination_info)
    
    # Get support staff for assignment dropdown (no priority filter for manage page)
    support_staff = get_support_staff()
    
    return render_template('helpdesk/support_manage.html',
                         requests=requests,
                         pagination=pagination_info,
                         pagination_links=pagination_links,
                         support_staff=support_staff,
                         current_filters=filters,
                         current_sort={'sort_by': sort_by, 'sort_order': sort_order})

@app.route('/helpdesk/request/<int:request_id>/take', methods=['POST'])
@require_platform_role('super_admin', 'support_technician')
def take_request(request_id):
    """Take ownership of a help request"""
    current_user_id = get_current_user_id()
    current_user_role = get_current_platform_role()
    
    try:
        with db.get_cursor() as cursor:
            # Check if request exists and get priority information
            cursor.execute("""
                SELECT status, assigned_to, priority FROM help_requests 
                WHERE request_id = %s
            """, (request_id,))
            request = cursor.fetchone()
            
            if not request:
                flash('Request not found', 'error')
                return redirect(url_for('support_manage'))
            
            if request['assigned_to'] and request['assigned_to'] != current_user_id:
                flash('Request is already assigned to another staff member', 'warning')
                return redirect(url_for('view_help_request', request_id=request_id))
            
            # Check if high priority and user is not super_admin
            if request['priority'] == 'high' and current_user_role != 'super_admin':
                flash('Only super administrators can assign high priority requests', 'error')
                return redirect(url_for('support_manage'))
            
            # Take the request - only if current status allows transition to assigned
            current_status = request['status']
            if not is_valid_status_transition(current_status, 'assigned'):
                valid_transitions = get_valid_status_transitions(current_status)
                flash(f'Cannot assign request with status "{current_status}". Valid transitions: {", ".join(valid_transitions)}', 'error')
                return redirect(url_for('view_help_request', request_id=request_id))
            
            success = update_help_request_status(
                request_id, 
                'assigned',  # Change status to assigned when taken
                current_user_id,  # Assign to current user
                None  # Keep existing priority
            )
            
            if success:
                flash('Request assigned to you', 'success')
            else:
                flash('Failed to assign request', 'error')
                
    except Exception as e:
        flash('An error occurred while assigning the request', 'error')
    
    return redirect(url_for('view_help_request', request_id=request_id))

@app.route('/helpdesk/request/<int:request_id>/drop', methods=['POST'])
@require_platform_role('super_admin', 'support_technician')
def drop_request(request_id):
    """Unassign a help request (can be done by assigned user or super admin)"""
    current_user_id = get_current_user_id()
    
    try:
        with db.get_cursor() as cursor:
            # Check if request exists and get assignment info
            cursor.execute("""
                SELECT status, assigned_to FROM help_requests 
                WHERE request_id = %s
            """, (request_id,))
            request = cursor.fetchone()
            
            if not request:
                flash('Request not found', 'error')
                return redirect(url_for('support_manage'))
            
            # Check if user can unassign this request
            if not request['assigned_to']:
                flash('Request is not assigned to anyone', 'warning')
                return redirect(url_for('view_help_request', request_id=request_id))
            
            # Only the assigned user or super admin can unassign
            if request['assigned_to'] != current_user_id and not has_platform_permission('super_admin'):
                flash('You can only unassign requests assigned to you', 'warning')
                return redirect(url_for('view_help_request', request_id=request_id))
            
            # Check if status allows unassignment
            current_status = request['status']
            if current_status == 'solved':
                flash('Cannot unassign a solved request', 'error')
                return redirect(url_for('view_help_request', request_id=request_id))
            
            # For unassignment, we can either:
            # 1. Change status to 'new' if it's not already 'new'
            # 2. Just unassign (set assigned_to to NULL) if it's already 'new'
            if current_status == 'new':
                # Already 'new' status, just unassign
                success = update_help_request_status(
                    request_id, 
                    current_status,  # Keep current status
                    None,           # Unassign
                    None,           # Keep existing priority
                    bypass_validation=True  # No status change needed
                )
            else:
                # Change status to 'new' and unassign
                if not is_valid_status_transition(current_status, 'new'):
                    valid_transitions = get_valid_status_transitions(current_status)
                    flash(f'Cannot unassign request with status "{current_status}". Valid transitions: {", ".join(valid_transitions)}', 'error')
                    return redirect(url_for('view_help_request', request_id=request_id))
                
                success = update_help_request_status(
                    request_id, 
                    'new',  # Change status back to new when unassigned
                    None,   # Unassign
                    None,   # Keep existing priority
                    bypass_validation=has_platform_permission('super_admin')  # Super admin can bypass validation
                )
            
            if success:
                flash('Request unassigned successfully', 'success')
            else:
                flash('Failed to unassign request', 'error')
                
    except Exception as e:
        flash('An error occurred while unassigning the request', 'error')
    
    return redirect(url_for('view_help_request', request_id=request_id))

@app.route('/helpdesk/request/<int:request_id>/unassign', methods=['POST'])
@require_platform_role('super_admin')
def unassign_request(request_id):
    """Super admin unassign any request assigned by others"""
    current_user_id = get_current_user_id()
    
    try:
        with db.get_cursor() as cursor:
            # Check if request exists and is assigned
            cursor.execute("""
                SELECT status, assigned_to FROM help_requests 
                WHERE request_id = %s
            """, (request_id,))
            request = cursor.fetchone()
            
            if not request:
                flash('Request not found', 'error')
                return redirect(url_for('support_manage'))
            
            if not request['assigned_to']:
                flash('Request is not assigned to anyone', 'warning')
                return redirect(url_for('support_manage'))
            
            # Only allow unassigning if status is not 'solved' (solved is final state)
            if request['status'] == 'solved':
                flash('Cannot unassign a solved request', 'error')
                return redirect(url_for('support_manage'))
            
            # Use update_help_request_status with bypass_validation=True for super admin
            success = update_help_request_status(
                request_id, 
                'new',  # Change status back to new when unassigned
                None,   # Unassign
                None,   # Keep existing priority
                bypass_validation=True  # Bypass status transition validation for super admin
            )
            
            if success:
                flash('Request unassigned successfully', 'success')
            else:
                flash('Failed to unassign request', 'error')
                
    except Exception as e:
        flash('An error occurred while unassigning the request', 'error')
    
    return redirect(url_for('support_manage'))

@app.route('/helpdesk/request/<int:request_id>/update-status', methods=['POST'])
@require_platform_role('super_admin', 'support_technician')
def update_request_status(request_id):
    """Update help request status, priority and assignment"""
    status = request.form.get('status')
    priority = request.form.get('priority')
    assigned_to = request.form.get('assigned_to')
    
    if not status:
        flash('Status is required', 'error')
        return redirect(url_for('view_help_request', request_id=request_id))
    
    if not priority:
        flash('Priority is required', 'error')
        return redirect(url_for('view_help_request', request_id=request_id))
    
    # Convert empty string to None for assigned_to
    if assigned_to == '':
        assigned_to = None
    elif assigned_to:
        assigned_to = int(assigned_to)
    
    # Get current status for validation
    try:
        with db.get_cursor() as cursor:
            cursor.execute("""
                SELECT status FROM help_requests WHERE request_id = %s
            """, (request_id,))
            result = cursor.fetchone()
            if not result:
                flash('Request not found', 'error')
                return redirect(url_for('view_help_request', request_id=request_id))
            
            current_status = result['status']
            transition_error = validate_status_transition(current_status, status)
            if transition_error:
                flash(transition_error, 'error')
                return redirect(url_for('view_help_request', request_id=request_id))
    except Exception as e:
        flash('Error validating status change', 'error')
        return redirect(url_for('view_help_request', request_id=request_id))
    
    # Enforce: High priority requests' escalation can only be modified by super_admin
    try:
        current_role = get_current_platform_role()
        with db.get_cursor() as cursor:
            cursor.execute("""
                SELECT priority FROM help_requests WHERE request_id = %s
            """, (request_id,))
            row = cursor.fetchone()
            current_priority = row['priority'] if row else None
        if current_priority == 'high' and current_role != 'super_admin':
            flash('Only Super Admins can modify escalation settings for high priority requests.', 'error')
            return redirect(url_for('view_help_request', request_id=request_id))
    except Exception:
        # If role/priority check fails silently, fallback to safe path of denying
        if get_current_platform_role() != 'super_admin':
            flash('Only Super Admins can modify escalation settings for high priority requests.', 'error')
            return redirect(url_for('view_help_request', request_id=request_id))

    success = update_help_request_status(request_id, status, assigned_to, priority)
    
    if success:
        flash('Request updated successfully', 'success')
    else:
        flash('Failed to update request', 'error')
    
    return redirect(url_for('view_help_request', request_id=request_id))

# =============================================================================
# AJAX ROUTES
# =============================================================================

@app.route('/helpdesk/api/request/<int:request_id>/stats')
@require_platform_role('super_admin', 'support_technician')
def get_request_stats(request_id):
    """Get statistics for a help request (AJAX)"""
    try:
        with db.get_cursor() as cursor:
            # Get reply count
            cursor.execute("""
                SELECT COUNT(*) as reply_count
                FROM help_replies
                WHERE request_id = %s
            """, (request_id,))
            reply_count = cursor.fetchone()['reply_count']
            
            # Get time since creation
            cursor.execute("""
                SELECT created_at, last_staff_reply_at
                FROM help_requests
                WHERE request_id = %s
            """, (request_id,))
            request_data = cursor.fetchone()
            
            return jsonify({
                'reply_count': reply_count,
                'created_at': request_data['created_at'].isoformat() if request_data['created_at'] else None,
                'last_staff_reply_at': request_data['last_staff_reply_at'].isoformat() if request_data['last_staff_reply_at'] else None
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/helpdesk/api/workload-dashboard')
@require_platform_role('super_admin', 'support_technician')
def api_workload_dashboard():
    """Get workload dashboard data for all technicians using simple method (AJAX)"""
    try:
        from .assign_request import get_simple_workload_dashboard
        
        workload_data = get_simple_workload_dashboard()
        
        return jsonify({
            'success': True,
            'technicians': workload_data
        })
        
    except Exception as e:
        print(f"Error in api_workload_dashboard: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/helpdesk/api/bulk-auto-assign', methods=['POST'])
@require_platform_role('super_admin')
def api_bulk_auto_assign():
    """Auto-assign all unassigned requests using simple method (AJAX)"""
    try:
        print("DEBUG: Starting bulk auto-assign")
        from .assign_request import bulk_simple_assign
        
        result = bulk_simple_assign()
        print(f"DEBUG: Bulk auto-assign result: {result}")
        
        return jsonify(result)
        
    except Exception as e:
        print(f"DEBUG: Bulk auto-assign error: {str(e)}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# REJECTION HISTORY FUNCTIONS
# =============================================================================

@app.route('/my/rejection-history')
@require_login
def user_rejection_history():
    """View current user's rejection history"""
    
    user_id = get_current_user_id()
    
    # Get filter parameters
    rejection_type = request.args.get('type', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    
    # Get pagination parameters
    page, per_page = get_pagination_params(request, default_per_page=20)
    
    rejections = []
    total_rejections = 0
    
    try:
        with db.get_cursor() as cursor:
            # Start with empty list
            rejections = []
            
            # Get group creation rejections
            if not rejection_type or rejection_type == 'group_creation':
                group_creation_params = [user_id]
                group_creation_where = ["gi.status = 'rejected'", "gi.created_by = %s"]
                
                if date_from:
                    group_creation_where.append("DATE(gi.updated_at) >= %s")
                    group_creation_params.append(date_from)
                
                if date_to:
                    group_creation_where.append("DATE(gi.updated_at) <= %s")
                    group_creation_params.append(date_to)
                
                group_creation_query = """
                    SELECT 
                        'group_creation' as type,
                        gi.group_id,
                        gi.name as group_name,
                        CASE gi.rejection_reason
                            WHEN 'inappropriate_content' THEN 'Inappropriate Content'
                            WHEN 'duplicate_group' THEN 'Duplicate Group'
                            WHEN 'insufficient_info' THEN 'Insufficient Information'
                            WHEN 'policy_violation' THEN 'Policy Violation'
                            WHEN 'other' THEN 'Other'
                            ELSE gi.rejection_reason
                        END as reason,
                           gi.updated_at as rejected_at,
                           gi.created_by as user_id,
                           u.first_name as user_name,
                           u.username,
                        NULL as rejected_by,
                        NULL as rejected_by_name,
                        NULL as rejected_by_username
                    FROM group_info gi
                    JOIN users u ON gi.created_by = u.user_id
                    WHERE """ + " AND ".join(group_creation_where) + """
                    ORDER BY gi.updated_at DESC
                """
                
                cursor.execute(group_creation_query, group_creation_params)
                group_creation_rejections = cursor.fetchall()
                rejections.extend(group_creation_rejections)
            
            # Get group membership rejections (from group_requests table)
            if not rejection_type or rejection_type == 'group_membership':
                membership_params = [user_id]
                membership_where = ["gr.status = 'rejected'", "gr.user_id = %s", "gr.message = 'Rejected membership request'"]
                
                if date_from:
                    membership_where.append("DATE(gr.requested_at) >= %s")
                    membership_params.append(date_from)
                
                if date_to:
                    membership_where.append("DATE(gr.requested_at) <= %s")
                    membership_params.append(date_to)
                
                membership_query = """
                    SELECT 
                        'group_membership' as type,
                        gr.group_id,
                        gi.name as group_name,
                        CASE gr.rejection_reason
                            WHEN 'group_full' THEN 'Group Full'
                            WHEN 'activity_mismatch' THEN 'Activity Mismatch'
                            WHEN 'insufficient_info' THEN 'Insufficient Information'
                            WHEN 'other' THEN 'Other'
                            ELSE gr.rejection_reason
                        END as reason,
                        gr.requested_at as rejected_at,
                        gr.user_id,
                        u.first_name as user_name,
                        u.username,
                        NULL as rejected_by,
                        NULL as rejected_by_name,
                        NULL as rejected_by_username
                    FROM group_requests gr
                    JOIN users u ON gr.user_id = u.user_id
                    JOIN group_info gi ON gr.group_id = gi.group_id
                    WHERE """ + " AND ".join(membership_where) + """
                    ORDER BY gr.requested_at DESC
                """
                
                cursor.execute(membership_query, membership_params)
                membership_rejections = cursor.fetchall()
                rejections.extend(membership_rejections)
            
            # Get group join request rejections
            if not rejection_type or rejection_type == 'group_request':
                request_params = [user_id]
                request_where = ["gr.status = 'rejected'", "gr.user_id = %s", "(gr.message IS NULL OR gr.message != 'Rejected membership request')"]
                
                if date_from:
                    request_where.append("DATE(gr.requested_at) >= %s")
                    request_params.append(date_from)
                
                if date_to:
                    request_where.append("DATE(gr.requested_at) <= %s")
                    request_params.append(date_to)
                
                request_query = """
                    SELECT 
                        'group_request' as type,
                        gr.group_id,
                        gi.name as group_name,
                        CASE gr.rejection_reason
                            WHEN 'group_full' THEN 'Group Full'
                            WHEN 'activity_mismatch' THEN 'Activity Mismatch'
                            WHEN 'insufficient_info' THEN 'Insufficient Information'
                            WHEN 'other' THEN 'Other'
                            ELSE gr.rejection_reason
                        END as reason,
                        gr.requested_at as rejected_at,
                        gr.user_id,
                        u.first_name as user_name,
                        u.username,
                        NULL as rejected_by,
                        NULL as rejected_by_name,
                        NULL as rejected_by_username
                    FROM group_requests gr
                    JOIN users u ON gr.user_id = u.user_id
                    JOIN group_info gi ON gr.group_id = gi.group_id
                    WHERE """ + " AND ".join(request_where) + """
                    ORDER BY gr.requested_at DESC
                """
                
                cursor.execute(request_query, request_params)
                request_rejections = cursor.fetchall()
                rejections.extend(request_rejections)

            # Get volunteer rejections (via notifications recorded at rejection time)
            if not rejection_type or rejection_type == 'volunteer':
                vol_params = [user_id]
                vol_where = ["n.user_id = %s", "n.category = 'volunteer'", "n.title = 'Volunteer Application Rejected'"]

                if date_from:
                    vol_where.append("DATE(n.created_at) >= %s")
                    vol_params.append(date_from)

                if date_to:
                    vol_where.append("DATE(n.created_at) <= %s")
                    vol_params.append(date_to)

                vol_query = """
                    SELECT 
                        'group_membership' AS type,
                        e.event_id AS group_id,
                        e.event_title AS group_name,
                        n.message AS raw_message,
                        n.created_at AS rejected_at,
                        n.user_id,
                        NULL AS username,
                        NULL AS rejected_by,
                        NULL AS rejected_by_name,
                        NULL AS rejected_by_username
                    FROM notifications n
                    LEFT JOIN event_info e ON e.event_id = n.related_id
                    WHERE {where}
                    ORDER BY n.created_at DESC
                """.format(where=" AND ".join(vol_where))

                cursor.execute(vol_query, vol_params)
                volunteer_rejections = cursor.fetchall() or []

                # Normalize reason from notification message (extract text after 'Reason: ' if present)
                for row in volunteer_rejections:
                    msg = row.get('raw_message') or ''
                    reason_text = msg
                    if 'Reason:' in msg:
                        try:
                            reason_text = msg.split('Reason:', 1)[1].strip()
                        except Exception:
                            reason_text = msg
                    row['reason'] = reason_text
                    # Align keys with table expectations
                    row.pop('raw_message', None)

                rejections.extend(volunteer_rejections)
            
            # Sort all rejections by date (most recent first)
            rejections.sort(key=lambda x: x['rejected_at'], reverse=True)
            total_rejections = len(rejections)
            
            # Apply pagination
            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page
            paginated_rejections = rejections[start_idx:end_idx]
            
    except Exception as e:
        print(f"Error fetching user rejection history: {e}")
        import traceback
        traceback.print_exc()
        flash('Error loading rejection history. Please try again.', 'error')
        paginated_rejections = []
        total_rejections = 0
    
    # Create pagination info
    pagination_info = create_pagination_info(
        page, per_page, total_rejections,
        url_for('user_rejection_history'),
        type=rejection_type,
        date_from=date_from,
        date_to=date_to
    )
    
    # Prepare current filters for template
    current_filters = {
        'type': rejection_type,
        'date_from': date_from,
        'date_to': date_to
    }
    
    return render_template('helpdesk/rejection_reasons.html',
                         rejections=paginated_rejections,
                         pagination=pagination_info,
                         current_filters=current_filters,
                         is_user_view=True)


@app.route('/support/rejection-history')
@require_platform_role('super_admin', 'support_technician')
def support_rejection_history():
    """View all rejection reasons for helpdesk support"""
    
    # Get filter parameters
    rejection_type = request.args.get('type', '')
    user_search = request.args.get('user_search', '').strip()
    group_search = request.args.get('group_search', '').strip()
    
    # Get pagination parameters
    page, per_page = get_pagination_params(request, default_per_page=20)
    
    rejections = []
    total_rejections = 0
    
    try:
        with db.get_cursor() as cursor:
            # Start with empty list
            rejections = []
            
            # Get group creation rejections
            if not rejection_type or rejection_type == 'group_creation':
                group_creation_params = []
                group_creation_where = ["gi.status = 'rejected'"]
                
                if user_search:
                    group_creation_where.append("(u.username LIKE %s OR CONCAT(u.first_name, ' ', u.last_name) LIKE %s)")
                    search_term = f"%{user_search}%"
                    group_creation_params.extend([search_term, search_term])
                
                if group_search:
                    group_creation_where.append("gi.name LIKE %s")
                    group_search_term = f"%{group_search}%"
                    group_creation_params.append(group_search_term)
                
                group_creation_query = """
                    SELECT 
                        'group_creation' as type,
                        gi.group_id,
                        gi.name as group_name,
                        CASE gi.rejection_reason
                            WHEN 'inappropriate_content' THEN 'Inappropriate Content'
                            WHEN 'duplicate_group' THEN 'Duplicate Group'
                            WHEN 'insufficient_info' THEN 'Insufficient Information'
                            WHEN 'guideline_violation' THEN 'Guideline Violation'
                            WHEN 'other' THEN 'Other'
                            ELSE gi.rejection_reason
                        END as reason,
                        gi.updated_at as rejected_at,
                        gi.created_by as user_id,
                        u.username,
                        CONCAT(u.first_name, ' ', u.last_name) as user_name,
                        NULL as rejected_by,
                        NULL as rejected_by_name,
                        NULL as rejected_by_username
                    FROM group_info gi
                    JOIN users u ON gi.created_by = u.user_id
                    WHERE """ + ' AND '.join(group_creation_where) + """
                    ORDER BY gi.updated_at DESC
                """
                
                cursor.execute(group_creation_query, group_creation_params)
                group_creation_results = cursor.fetchall()
                rejections.extend(group_creation_results)
            
            # Get group request rejections (excludes membership rejections)
            if not rejection_type or rejection_type == 'group_request':
                group_request_params = []
                group_request_where = ["gr.status = 'rejected'", "(gr.message IS NULL OR gr.message != 'Rejected membership request')"]
                
                if user_search:
                    group_request_where.append("(u.username LIKE %s OR CONCAT(u.first_name, ' ', u.last_name) LIKE %s)")
                    search_term = f"%{user_search}%"
                    group_request_params.extend([search_term, search_term])
                
                if group_search:
                    group_request_where.append("gi.name LIKE %s")
                    group_search_term = f"%{group_search}%"
                    group_request_params.append(group_search_term)
                
                group_request_query = """
                    SELECT 
                        'group_request' as type,
                        gr.group_id,
                        gi.name as group_name,
                        CASE gr.rejection_reason
                            WHEN 'group_full' THEN 'Group Full'
                            WHEN 'activity_mismatch' THEN 'Activity Mismatch'
                            WHEN 'insufficient_info' THEN 'Insufficient Information'
                            WHEN 'other' THEN 'Other'
                            ELSE gr.rejection_reason
                        END as reason,
                        gr.requested_at as rejected_at,
                        gr.user_id,
                        u.username,
                        CONCAT(u.first_name, ' ', u.last_name) as user_name,
                        NULL as rejected_by,
                        NULL as rejected_by_name,
                        NULL as rejected_by_username
                    FROM group_requests gr
                    JOIN users u ON gr.user_id = u.user_id
                    JOIN group_info gi ON gr.group_id = gi.group_id
                    WHERE """ + ' AND '.join(group_request_where) + """
                    ORDER BY gr.requested_at DESC
                """
                
                cursor.execute(group_request_query, group_request_params)
                group_request_results = cursor.fetchall()
                rejections.extend(group_request_results)
            
            # Get group membership rejections (stored in group_requests table)
            if not rejection_type or rejection_type == 'group_membership':
                group_membership_params = []
                group_membership_where = ["gr.status = 'rejected'", "gr.message = 'Rejected membership request'"]
                
                if user_search:
                    group_membership_where.append("(u.username LIKE %s OR CONCAT(u.first_name, ' ', u.last_name) LIKE %s)")
                    search_term = f"%{user_search}%"
                    group_membership_params.extend([search_term, search_term])
                
                if group_search:
                    group_membership_where.append("gi.name LIKE %s")
                    group_search_term = f"%{group_search}%"
                    group_membership_params.append(group_search_term)
                
                group_membership_query = """
                    SELECT 
                        'group_membership' as type,
                        gr.group_id,
                        gi.name as group_name,
                        CASE gr.rejection_reason
                            WHEN 'group_full' THEN 'Group Full'
                            WHEN 'activity_mismatch' THEN 'Activity Mismatch'
                            WHEN 'insufficient_info' THEN 'Insufficient Information'
                            WHEN 'other' THEN 'Other'
                            ELSE gr.rejection_reason
                        END as reason,
                        gr.requested_at as rejected_at,
                        gr.user_id,
                        u.username,
                        CONCAT(u.first_name, ' ', u.last_name) as user_name,
                        NULL as rejected_by,
                        NULL as rejected_by_name,
                        NULL as rejected_by_username
                    FROM group_requests gr
                    JOIN users u ON gr.user_id = u.user_id
                    JOIN group_info gi ON gr.group_id = gi.group_id
                    WHERE """ + ' AND '.join(group_membership_where) + """
                    ORDER BY gr.requested_at DESC
                """
                
                cursor.execute(group_membership_query, group_membership_params)
                group_membership_results = cursor.fetchall()
                rejections.extend(group_membership_results)
            
            # Sort all results by date (most recent first)
            rejections.sort(key=lambda x: x['rejected_at'], reverse=True)
            
            # Apply pagination
            total_rejections = len(rejections)
            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page
            paginated_rejections = rejections[start_idx:end_idx]
            
    except Exception as e:
        flash(f'Error loading rejection reasons: {str(e)}', 'error')
        paginated_rejections = []
        total_rejections = 0
    
    # Create pagination info
    pagination = create_pagination_info(
        page=page,
        per_page=per_page,
        total=total_rejections,
        base_url=url_for('support_rejection_history'),
        type=rejection_type,
        user_search=user_search,
        group_search=group_search
    )
    
    # Prepare filter context for template
    current_filters = {
        'type': rejection_type,
        'user_search': user_search,
        'group_search': group_search
    }
    
    return render_template('helpdesk/rejection_reasons.html', 
                         rejections=paginated_rejections,
                         pagination=pagination,
                         current_filters=current_filters)
