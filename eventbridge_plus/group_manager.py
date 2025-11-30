# eventbridge_plus/group_manager.py
from eventbridge_plus import app, db, noti
from flask import render_template, request, redirect, url_for, flash, session, jsonify
from .auth import (
    require_login, 
    get_current_user_id, 
    is_super_admin, 
    is_support_technician,
    is_group_manager,
    can_change_group_roles_in_specific_group
)
from .util import get_pagination_params, create_pagination_info, create_pagination_links

# =============================================================================
# MEMBERSHIP MANAGEMENT ROUTES
# =============================================================================

@app.route('/membership')
@require_login
def membership_management():
    """Membership management page - different views for super_admin vs group_manager"""
    user_id = get_current_user_id()
    user_role = session.get('platform_role')
    
    # Get group_id, sort, pagination, and search from query params
    group_id = request.args.get('group_id', type=int)
    group_sort = request.args.get('group_sort', 'newest').strip()
    member_sort = request.args.get('member_sort', 'role').strip()
    group_search = request.args.get('group_search', '').strip()
    page, per_page = get_pagination_params(request, default_per_page=10)
    
    if is_super_admin() or is_support_technician():
        # Super admin and support technician can access any group
        if not group_id:
            # Show group selection page with sorting, pagination, and search
            with db.get_cursor() as cur:
                # Build search condition
                search_condition = ""
                search_params = []
                if group_search:
                    search_condition = "AND LOWER(g.name) LIKE LOWER(%s)"
                    search_params.append(f"%{group_search}%")
                
                # First get total count with search
                cur.execute(f"""
                    SELECT COUNT(DISTINCT g.group_id) as total
                    FROM group_info g
                    WHERE g.status = 'approved' {search_condition}
                """, search_params)
                total_groups = cur.fetchone()['total']
                
                # Build order clause
                order_clause = "g.created_at DESC"  # Default to newest
                if group_sort == 'oldest':
                    order_clause = "g.created_at ASC"
                elif group_sort == 'name_asc':
                    order_clause = "g.name ASC"
                elif group_sort == 'name_desc':
                    order_clause = "g.name DESC"
                
                # Calculate offset for pagination
                offset = (page - 1) * per_page
                
                cur.execute(f"""
                    SELECT g.group_id, g.name, g.group_type, g.status, g.max_members,
                           COUNT(gm.membership_id) as current_members, g.created_at, g.is_public
                    FROM group_info g
                    LEFT JOIN group_members gm ON g.group_id = gm.group_id AND gm.status = 'active'
                    WHERE g.status = 'approved' {search_condition}
                    GROUP BY g.group_id, g.name, g.group_type, g.status, g.max_members, g.created_at, g.is_public
                    ORDER BY {order_clause}
                    LIMIT %s OFFSET %s
                """, search_params + [per_page, offset])
                available_groups = cur.fetchall()
                
                # Create pagination info
                base_url = url_for('membership_management')
                pagination = create_pagination_info(
                    page=page,
                    per_page=per_page,
                    total=total_groups,
                    base_url=base_url,
                    group_sort=group_sort,
                    group_search=group_search or None
                )
                pagination_links = create_pagination_links(pagination)
            
            return render_template('group_manager/membership.html',
                                 user_role=user_role,
                                 available_groups=available_groups,
                                 selected_group=None,
                                 selected_group_id=None,
                                 group_members=[],
                                 current_user_id=user_id,
                                 group_sort=group_sort,
                                 member_sort=member_sort,
                                 group_search=group_search,
                                 pagination=pagination,
                                 pagination_links=pagination_links,
                                 pending_count=0)
        
        # Get selected group info
        with db.get_cursor() as cur:
            cur.execute("""
                SELECT g.group_id, g.name, g.group_type, g.status, g.max_members,
                       COUNT(gm.membership_id) as current_members, g.created_at, g.is_public
                FROM group_info g
                LEFT JOIN group_members gm ON g.group_id = gm.group_id AND gm.status = 'active'
                WHERE g.group_id = %s
                GROUP BY g.group_id, g.name, g.group_type, g.status, g.max_members, g.created_at, g.is_public
            """, (group_id,))
            selected_group = cur.fetchone()
            
            if not selected_group:
                flash('Group not found.', 'error')
                return redirect(url_for('membership_management'))
            
            # Get all groups for dropdown
            cur.execute("""
                SELECT g.group_id, g.name, g.group_type, g.status, g.max_members,
                       COUNT(gm.membership_id) as current_members, g.created_at, g.is_public
                FROM group_info g
                LEFT JOIN group_members gm ON g.group_id = gm.group_id AND gm.status = 'active'
                WHERE g.status = 'approved'
                GROUP BY g.group_id, g.name, g.group_type, g.status, g.max_members, g.created_at, g.is_public
                ORDER BY g.name
            """)
            available_groups = cur.fetchall()
    
    elif is_group_manager():
        # Group manager can manage groups they are manager of
        with db.get_cursor() as cur:
            # Build search condition for group managers
            search_condition = ""
            search_params = [user_id]
            if group_search:
                search_condition = "AND LOWER(g.name) LIKE LOWER(%s)"
                search_params.append(f"%{group_search}%")
            
            # Get all groups they manage with search
            cur.execute(f"""
                SELECT g.group_id, g.name, g.group_type, g.status, g.max_members,
                       COUNT(gm.membership_id) as current_members, g.created_at, g.is_public
                FROM group_info g
                LEFT JOIN group_members gm ON g.group_id = gm.group_id AND gm.status = 'active'
                JOIN group_members user_gm ON g.group_id = user_gm.group_id
                WHERE user_gm.user_id = %s AND user_gm.group_role = 'manager' AND user_gm.status = 'active' {search_condition}
                GROUP BY g.group_id, g.name, g.group_type, g.status, g.max_members, g.created_at, g.is_public
                ORDER BY g.name
            """, search_params)
            available_groups = cur.fetchall()
            
            if not available_groups:
                flash('You are not a manager of any group.', 'error')
                return redirect(url_for('participant_dashboard'))
            
            # If group_id is provided, verify it's a group they manage
            if group_id:
                selected_group = None
                for group in available_groups:
                    if group['group_id'] == group_id:
                        selected_group = group
                        break
                
                if not selected_group:
                    flash('You can only manage groups you are a manager of.', 'error')
                    return redirect(url_for('membership_management'))
            else:
                # No group_id provided, show first group or let user select
                if len(available_groups) == 1:
                    # Only one group, auto-select it
                    selected_group = available_groups[0]
                    group_id = selected_group['group_id']
                else:
                    # Multiple groups, show selection interface
                    selected_group = None
                    group_id = None
    
    else:
        flash('Access denied. You need manager or admin privileges.', 'error')
        return redirect(url_for('participant_dashboard'))
    
    # Get group members with sorting
    with db.get_cursor() as cur:
        # Build order clause for members
        member_order_clause = "gm.group_role DESC, u.first_name, u.last_name"  # Default
        if member_sort == 'name_asc':
            member_order_clause = "u.first_name ASC, u.last_name ASC"
        elif member_sort == 'name_desc':
            member_order_clause = "u.first_name DESC, u.last_name DESC"
        elif member_sort == 'join_date_asc':
            member_order_clause = "gm.join_date ASC"
        elif member_sort == 'join_date_desc':
            member_order_clause = "gm.join_date DESC"
        elif member_sort == 'role':
            member_order_clause = "gm.group_role DESC, u.first_name, u.last_name"
        elif member_sort == 'status':
            member_order_clause = "gm.status ASC, u.first_name, u.last_name"
        
        cur.execute(f"""
            SELECT gm.membership_id, gm.user_id, gm.group_role, gm.status, gm.join_date,
                   u.username, u.first_name, u.last_name, u.email, u.user_image
            FROM group_members gm
            JOIN users u ON gm.user_id = u.user_id
            WHERE gm.group_id = %s
            ORDER BY {member_order_clause}
        """, (group_id,))
        group_members = cur.fetchall()
        
        # Get activity stats for each member separately
        for member in group_members:
            cur.execute("""
                SELECT 
                    COUNT(*) as total_events_attended,
                    SUM(CASE WHEN em.event_role = 'volunteer' AND em.volunteer_status = 'confirmed' THEN 1 ELSE 0 END) as volunteer_events_count,
                    MAX(e.event_date) as last_event_date
                FROM event_members em 
                JOIN event_info e ON em.event_id = e.event_id 
                WHERE em.user_id = %s 
                  AND em.participation_status IN ('registered', 'attended')
                  AND e.event_date <= CURDATE()
            """, (member['user_id'],))
            stats = cur.fetchone()
            
            member['total_events_attended'] = stats['total_events_attended'] or 0
            member['volunteer_events_count'] = stats['volunteer_events_count'] or 0
            member['last_event_date'] = stats['last_event_date']
        
        # Get pending requests count for display
        cur.execute("""
            SELECT COUNT(*) as pending_count
            FROM group_members
            WHERE group_id = %s AND status = 'pending'
        """, (group_id,))
        pending_count = cur.fetchone()['pending_count']
        
        # Add group statistics data (US-GM-08)
        group_stats = {}
        if selected_group:
            # Event count (total events created by this group)
            cur.execute("""
                SELECT COUNT(*) as count
                FROM event_info e
                WHERE e.group_id = %s
            """, (group_id,))
            event_registration_count = cur.fetchone()['count'] or 0
            
            # Past participants count (actual attendees - including those with race results)
            cur.execute("""
                SELECT COUNT(DISTINCT em.membership_id) as count
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                LEFT JOIN race_results rr ON em.membership_id = rr.membership_id
                WHERE e.group_id = %s 
                  AND e.event_date < CURDATE()
                  AND (em.participation_status = 'attended' 
                       OR (rr.start_time IS NOT NULL 
                           AND rr.finish_time IS NOT NULL 
                           AND rr.finish_time > rr.start_time))
            """, (group_id,))
            past_participants_count = cur.fetchone()['count'] or 0
            
            # Upcoming participants count (registered for future events)
            cur.execute("""
                SELECT COUNT(DISTINCT em.user_id) as count
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                WHERE e.group_id = %s AND em.participation_status = 'registered'
                  AND e.event_date >= CURDATE() AND e.status = 'scheduled'
            """, (group_id,))
            upcoming_participants_count = cur.fetchone()['count'] or 0
            
            # Attendance count - Count users with valid race results instead of just 'attended' status
            cur.execute("""
                SELECT COUNT(DISTINCT em.membership_id) as count
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                LEFT JOIN race_results rr ON em.membership_id = rr.membership_id
                WHERE e.group_id = %s 
                  AND (em.participation_status = 'attended' 
                       OR (rr.start_time IS NOT NULL 
                           AND rr.finish_time IS NOT NULL 
                           AND rr.finish_time > rr.start_time))
            """, (group_id,))
            attendance_count = cur.fetchone()['count'] or 0
            
            # Volunteer count - Only count confirmed volunteers from past events
            cur.execute("""
                SELECT COUNT(*) as count
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                WHERE e.group_id = %s AND em.event_role = 'volunteer' 
                  AND em.volunteer_status = 'confirmed'
                  AND e.event_date < CURDATE()
            """, (group_id,))
            volunteer_count = cur.fetchone()['count'] or 0
            
            # Pending volunteer count
            cur.execute("""
                SELECT COUNT(*) as count
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                WHERE e.group_id = %s AND em.event_role = 'volunteer' 
                  AND em.volunteer_status = 'assigned'
                  AND e.status = 'scheduled'
                  AND e.event_date >= CURDATE()
            """, (group_id,))
            pending_volunteer_count = cur.fetchone()['count'] or 0
            
            # Calculate participation rate
            cur.execute("""
                SELECT 
                    COUNT(DISTINCT gm.user_id) as total_members,
                    COUNT(DISTINCT em.user_id) as participating_members
                FROM group_members gm
                LEFT JOIN event_members em ON gm.user_id = em.user_id
                LEFT JOIN event_info e ON em.event_id = e.event_id AND e.group_id = gm.group_id
                WHERE gm.group_id = %s AND gm.status = 'active'
            """, (group_id,))
            participation_data = cur.fetchone()
            if participation_data['total_members'] > 0:
                participation_rate = round(
                    (participation_data['participating_members'] / participation_data['total_members']) * 100
                )
            else:
                participation_rate = 0
            
            # Upcoming event count
            cur.execute("""
                SELECT COUNT(*) as count
                FROM event_info e
                WHERE e.group_id = %s AND e.status = 'scheduled'
                  AND e.event_date >= CURDATE()
            """, (group_id,))
            upcoming_event_count = cur.fetchone()['count'] or 0
            
            group_stats = {
                'event_registration_count': event_registration_count,
                'upcoming_event_count': upcoming_event_count,
                'past_participants_count': past_participants_count,
                'upcoming_participants_count': upcoming_participants_count,
                'attendance_count': attendance_count,
                'volunteer_count': volunteer_count,
                'pending_volunteer_count': pending_volunteer_count,
                'participation_rate': participation_rate
            }
    
    return render_template('group_manager/membership.html',
                         user_role=user_role,
                         available_groups=available_groups if (is_super_admin() or is_support_technician() or is_group_manager()) else [],
                         selected_group=selected_group,
                         selected_group_id=group_id,
                         group_members=group_members,
                         current_user_id=user_id,
                         group_sort=group_sort,
                         member_sort=member_sort,
                         group_search=group_search,
                         pending_count=pending_count,
                         group_stats=group_stats)


@app.route('/search-users')
@require_login
def search_users():
    """Search users for adding to groups"""
    query = request.args.get('q', '').strip()
    
    if len(query) < 2:
        return jsonify({'users': []})
    
    try:
        with db.get_cursor() as cur:
            cur.execute("""
                SELECT user_id, username, first_name, last_name, email
                FROM users
                WHERE (LOWER(username) LIKE LOWER(%s) OR LOWER(first_name) LIKE LOWER(%s) OR LOWER(last_name) LIKE LOWER(%s) OR LOWER(email) LIKE LOWER(%s))
                  AND status = 'active'
                ORDER BY first_name, last_name
                LIMIT 10
            """, (f'%{query}%', f'%{query}%', f'%{query}%', f'%{query}%'))
            users = cur.fetchall()
        
        return jsonify({'users': users})
    
    except Exception as e:
        print(f"Search users error: {e}")
        return jsonify({'users': []})


@app.route('/add-group-member', methods=['POST'])
@require_login
def add_group_member():
    """Add a new member to a group"""
    user_id = get_current_user_id()
    user_role = session.get('platform_role')
    group_id = request.form.get('group_id', type=int)
    member_user_id = request.form.get('user_id', type=int)
    member_role = request.form.get('member_role', '').strip()
    
    if not group_id or not member_user_id or not member_role:
        flash('Missing required information.', 'error')
        return redirect(url_for('membership_management', group_id=group_id))
    
    # Check permissions
    if is_super_admin() or is_support_technician():
        # Super admin and support technician can add to any group
        pass
    elif is_group_manager():
        # Group manager can only add to their own group
        if not can_change_group_roles_in_specific_group(group_id):
            flash('You can only manage members in your own group.', 'error')
            return redirect(url_for('membership_management'))
    else:
        flash('Access denied.', 'error')
        return redirect(url_for('membership_management'))
    
    try:
        with db.get_cursor() as cur:
            # Check if group exists and get info
            cur.execute("""
                SELECT name, max_members, status
                FROM group_info
                WHERE group_id = %s
            """, (group_id,))
            group_info = cur.fetchone()
            
            if not group_info:
                flash('Group not found.', 'error')
                return redirect(url_for('membership_management'))
            
            if group_info['status'] != 'approved':
                flash('Cannot add members to inactive groups.', 'error')
                return redirect(url_for('membership_management', group_id=group_id))
            
            # Check if user exists and is active
            cur.execute("""
                SELECT username, first_name, last_name, status
                FROM users
                WHERE user_id = %s
            """, (member_user_id,))
            user_info = cur.fetchone()
            
            if not user_info:
                flash('User not found.', 'error')
                return redirect(url_for('membership_management', group_id=group_id))
            
            if user_info['status'] != 'active':
                flash('Cannot add inactive users to groups.', 'error')
                return redirect(url_for('membership_management', group_id=group_id))
            
            # Check if user is already a member
            cur.execute("""
                SELECT status
                FROM group_members
                WHERE user_id = %s AND group_id = %s
            """, (member_user_id, group_id))
            existing_membership = cur.fetchone()
            
            if existing_membership:
                if existing_membership['status'] == 'active':
                    flash(f'{user_info["first_name"]} {user_info["last_name"]} is already a member of this group.', 'warning')
                else:
                    flash(f'{user_info["first_name"]} {user_info["last_name"]} has a pending membership request.', 'info')
                return redirect(url_for('membership_management', group_id=group_id))
            
            # Check group capacity
            cur.execute("""
                SELECT COUNT(*) as current_count
                FROM group_members
                WHERE group_id = %s AND status = 'active'
            """, (group_id,))
            current_count = cur.fetchone()['current_count']
            
            if current_count >= group_info['max_members']:
                flash(f'Group has reached maximum capacity ({group_info["max_members"]} members).', 'error')
                return redirect(url_for('membership_management', group_id=group_id))
            
            # Add member
            cur.execute("""
                INSERT INTO group_members (user_id, group_id, group_role, status)
                VALUES (%s, %s, %s, 'active')
            """, (member_user_id, group_id, member_role))
            
            # Send notification to the new member
            noti.create_noti(
                user_id=member_user_id,
                title='Added to Group',
                message=f'You have been added to the group "{group_info["name"]}" as a {member_role}.',
                category='group',
                related_id=group_id
            )
            
            # Send notification to the person who added the member (admin/manager)
            noti.create_noti(
                user_id=user_id,
                title='Member Added Successfully',
                message=f'Successfully added {user_info["first_name"]} {user_info["last_name"]} to "{group_info["name"]}" as {member_role}.',
                category='group',
                related_id=group_id
            )
            
            flash(f'Successfully added {user_info["first_name"]} {user_info["last_name"]} to the group as {member_role}.', 'success')
    
    except Exception as e:
        print(f"Add group member error: {e}")
        flash('An error occurred while adding the member.', 'error')
    
    return redirect(url_for('membership_management', group_id=group_id))


@app.route('/change-member-role', methods=['POST'])
@require_login
def change_member_role():
    """Change a member's role in a group"""
    user_id = get_current_user_id()
    membership_id = request.form.get('membership_id', type=int)
    group_id = request.form.get('group_id', type=int)
    new_role = request.form.get('new_role', '').strip()
    
    if not membership_id or not group_id or not new_role:
        flash('Missing required information.', 'error')
        return redirect(url_for('membership_management', group_id=group_id))
    
    # Check permissions
    if is_super_admin():
        # Super admin can change roles in any group
        pass
    elif is_support_technician():
        # Support technician cannot change group roles
        flash('You do not have permission to change group roles.', 'warning')
        return redirect(url_for('membership_management', group_id=group_id))
    elif is_group_manager():
        # Group manager can only change roles in their own group
        if not can_change_group_roles_in_specific_group(group_id):
            flash('You can only manage members in your own group.', 'error')
            return redirect(url_for('membership_management'))
    else:
        flash('Access denied.', 'error')
        return redirect(url_for('membership_management'))
    
    try:
        with db.get_cursor() as cur:
            # Get current membership info
            cur.execute("""
                SELECT gm.user_id, gm.group_role, u.username, u.first_name, u.last_name, g.name as group_name
                FROM group_members gm
                JOIN users u ON gm.user_id = u.user_id
                JOIN group_info g ON gm.group_id = g.group_id
                WHERE gm.membership_id = %s AND gm.group_id = %s
            """, (membership_id, group_id))
            membership = cur.fetchone()
            
            if not membership:
                flash('Membership not found.', 'error')
                return redirect(url_for('membership_management', group_id=group_id))
            
            # Check if this is the last manager and handle auto-promotion
            cur.execute("""
                SELECT COUNT(*) as manager_count
                FROM group_members
                WHERE group_id = %s AND group_role = 'manager' AND status = 'active'
            """, (group_id,))
            manager_count = cur.fetchone()['manager_count']
            
            # If changing the last manager to non-manager role, handle auto-promotion
            if membership['group_role'] == 'manager' and manager_count <= 1 and new_role != 'manager':
                if is_super_admin():
                    # Super admin can change last manager role, but auto-promote another member
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
                    """, (group_id,))
                    next_manager = cur.fetchone()
                    
                    if next_manager:
                        # Promote the next member to manager
                        cur.execute("""
                            UPDATE group_members
                            SET group_role = 'manager'
                            WHERE membership_id = %s
                        """, (next_manager['membership_id'],))
                        
                        # Send notification to the new manager
                        noti.create_noti(
                            user_id=next_manager['user_id'],
                            title='Promoted to Group Manager',
                            message=f'You have been automatically promoted to manager of "{membership["group_name"]}" as the previous manager\'s role was changed.',
                            category='group',
                            related_id=group_id
                        )
                        
                        flash(f'Last manager role changed. {next_manager["first_name"]} {next_manager["last_name"]} has been automatically promoted to manager.', 'info')
                    else:
                        flash('Cannot change the last manager\'s role as there are no other members to promote.', 'warning')
                        return redirect(url_for('membership_management', group_id=group_id))
                else:
                    # Group manager cannot change the last manager's role
                    flash('Cannot change the role of the last manager in this group.', 'warning')
                    return redirect(url_for('membership_management', group_id=group_id))
            
            # Update role
            cur.execute("""
                UPDATE group_members
                SET group_role = %s
                WHERE membership_id = %s
            """, (new_role, membership_id))
            
            # Send notification to the member whose role was changed
            noti.create_noti(
                user_id=membership['user_id'],
                title='Role Changed',
                message=f'Your role in "{membership["group_name"]}" has been changed to {new_role}.',
                category='group',
                related_id=group_id
            )
            
            # Send notification to the person who changed the role (admin/manager)
            noti.create_noti(
                user_id=user_id,
                title='Member Role Changed Successfully',
                message=f'Successfully changed {membership["first_name"]} {membership["last_name"]}\'s role to {new_role} in "{membership["group_name"]}".',
                category='group',
                related_id=group_id
            )
            
            flash(f'Successfully changed {membership["first_name"]} {membership["last_name"]}\'s role to {new_role}.', 'success')
    
    except Exception as e:
        print(f"Change member role error: {e}")
        flash('An error occurred while changing the role.', 'error')
    
    return redirect(url_for('membership_management', group_id=group_id))


@app.route('/remove-group-member', methods=['POST'])
@require_login
def remove_group_member():
    """Remove a member from a group"""
    user_id = get_current_user_id()
    membership_id = request.form.get('membership_id', type=int)
    group_id = request.form.get('group_id', type=int)
    
    if not membership_id or not group_id:
        flash('Missing required information.', 'error')
        return redirect(url_for('membership_management', group_id=group_id))
    
    # Check permissions
    if is_super_admin() or is_support_technician():
        # Super admin and support technician can remove from any group
        pass
    elif is_group_manager():
        # Group manager can only remove from their own group
        if not can_change_group_roles_in_specific_group(group_id):
            flash('You can only manage members in your own group.', 'error')
            return redirect(url_for('membership_management'))
    else:
        flash('Access denied.', 'error')
        return redirect(url_for('membership_management'))
    
    try:
        with db.get_cursor() as cur:
            # Get membership info
            cur.execute("""
                SELECT gm.user_id, gm.group_role, u.username, u.first_name, u.last_name, g.name as group_name
                FROM group_members gm
                JOIN users u ON gm.user_id = u.user_id
                JOIN group_info g ON gm.group_id = g.group_id
                WHERE gm.membership_id = %s AND gm.group_id = %s
            """, (membership_id, group_id))
            membership = cur.fetchone()
            
            if not membership:
                flash('Membership not found.', 'error')
                return redirect(url_for('membership_management', group_id=group_id))
            
            # Check if this is the last manager
            cur.execute("""
                SELECT COUNT(*) as manager_count
                FROM group_members
                WHERE group_id = %s AND group_role = 'manager' AND status = 'active'
            """, (group_id,))
            manager_count = cur.fetchone()['manager_count']
            
            # If removing the last manager, handle auto-promotion
            if membership['group_role'] == 'manager' and manager_count <= 1:
                if is_super_admin():
                    # Super admin can remove last manager, but auto-promote another member
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
                    """, (group_id,))
                    next_manager = cur.fetchone()
                    
                    if next_manager:
                        # Promote the next member to manager
                        cur.execute("""
                            UPDATE group_members
                            SET group_role = 'manager'
                            WHERE membership_id = %s
                        """, (next_manager['membership_id'],))
                        
                        # Send notification to the new manager
                        noti.create_noti(
                            user_id=next_manager['user_id'],
                            title='Promoted to Group Manager',
                            message=f'You have been automatically promoted to manager of "{membership["group_name"]}" as the previous manager was removed.',
                            category='group',
                            related_id=group_id
                        )
                        
                        flash(f'Last manager removed. {next_manager["first_name"]} {next_manager["last_name"]} has been automatically promoted to manager.', 'info')
                    else:
                        flash('Cannot remove the last manager as there are no other members to promote.', 'warning')
                        return redirect(url_for('membership_management', group_id=group_id))
                else:
                    # Group manager cannot remove the last manager
                    flash('Cannot remove the last manager of this group.', 'warning')
                    return redirect(url_for('membership_management', group_id=group_id))
            
            # Remove membership
            cur.execute("""
                DELETE FROM group_members
                WHERE membership_id = %s
            """, (membership_id,))
            
            # Also remove the user from all events of this group
            cur.execute("""
                DELETE em FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                WHERE em.user_id = %s AND e.group_id = %s
            """, (membership['user_id'], group_id))
            
            # Send notification to the removed member
            noti.create_noti(
                user_id=membership['user_id'],
                title='Removed from Group',
                message=f'You have been removed from the group "{membership["group_name"]}". You are also automatically unregistered from all events of this group.',
                category='group',
                related_id=group_id
            )
            
            # Send notification to the person who removed the member (admin/manager)
            noti.create_noti(
                user_id=user_id,
                title='Member Removed Successfully',
                message=f'Successfully removed {membership["first_name"]} {membership["last_name"]} from "{membership["group_name"]}".',
                category='group',
                related_id=group_id
            )
            
            flash(f'Successfully removed {membership["first_name"]} {membership["last_name"]} from the group.', 'success')
    
    except Exception as e:
        print(f"Remove group member error: {e}")
        flash('An error occurred while removing the member.', 'error')
    
    return redirect(url_for('membership_management', group_id=group_id))


@app.route('/approve-group-request', methods=['POST'])
@require_login
def approve_group_request():
    """Approve a pending group membership request"""
    user_id = get_current_user_id()
    membership_id = request.form.get('membership_id', type=int)
    group_id = request.form.get('group_id', type=int)
    
    if not membership_id or not group_id:
        flash('Missing required information.', 'error')
        return redirect(url_for('membership_management', group_id=group_id))
    
    # Check permissions
    if is_super_admin() or is_support_technician():
        # Super admin and support technician can approve requests for any group
        pass
    elif is_group_manager():
        # Group manager can only approve requests for their own group
        if not can_change_group_roles_in_specific_group(group_id):
            flash('You can only manage requests for your own group.', 'error')
            return redirect(url_for('membership_management'))
    else:
        flash('Access denied.', 'error')
        return redirect(url_for('membership_management'))
    
    try:
        with db.get_cursor() as cur:
            # Get membership info (without pending_event_id from database)
            cur.execute("""
                SELECT gm.user_id, gm.status, 
                       u.username, u.first_name, u.last_name, 
                       g.name as group_name, g.max_members
                FROM group_members gm
                JOIN users u ON gm.user_id = u.user_id
                JOIN group_info g ON gm.group_id = g.group_id
                WHERE gm.membership_id = %s AND gm.group_id = %s
            """, (membership_id, group_id))
            membership = cur.fetchone()
            
            if not membership:
                flash('Membership request not found.', 'error')
                return redirect(url_for('membership_management', group_id=group_id))
            
            if membership['status'] != 'pending':
                flash('This request is not pending.', 'warning')
                return redirect(url_for('membership_management', group_id=group_id))
            
            # Check group capacity
            cur.execute("""
                SELECT COUNT(*) as current_count
                FROM group_members
                WHERE group_id = %s AND status = 'active'
            """, (group_id,))
            current_count = cur.fetchone()['current_count']
            
            if current_count >= membership['max_members']:
                flash(f'Group has reached maximum capacity ({membership["max_members"]} members).', 'error')
                return redirect(url_for('membership_management', group_id=group_id))
            
            # Approve the request
            cur.execute("""
                UPDATE group_members
                SET status = 'active'
                WHERE membership_id = %s
            """, (membership_id,))
            
            # Send notification to the approved member
            noti.create_noti(
                user_id=membership['user_id'],
                title='Group Request Approved',
                message=f'Your request to join "{membership["group_name"]}" has been approved! You are now a member of the group.',
                category='group',
                related_id=group_id
            )
            
            # Send notification to the approver
            noti.create_noti(
                user_id=user_id,
                title='Request Approved',
                message=f'Successfully approved {membership["first_name"]} {membership["last_name"]}\'s request to join "{membership["group_name"]}".',
                category='group',
                related_id=group_id
            )
            
            # Check if the approved user has a pending event registration for this group
            approved_user_id = membership['user_id']
            
            # Get pending event info from notifications table
            cur.execute("""
                SELECT message
                FROM notifications
                WHERE user_id = %s AND title = 'PENDING_EVENT_REGISTRATION' 
                  AND category = 'system' AND related_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (approved_user_id, group_id))
            pending_notification = cur.fetchone()
            
            pending_event_id = None
            if pending_notification:
                message = pending_notification['message']
                if 'event_id:' in message:
                    try:
                        event_id_part = message.split('event_id:')[1].split('|')[0]
                        pending_event_id = int(event_id_part)
                    except (ValueError, IndexError):
                        pass
            
            target_event = None
            
            # First, try to use the pending_event_id if it exists
            if pending_event_id:
                cur.execute("""
                    SELECT e.event_id, e.event_title, e.max_participants,
                           COUNT(em.membership_id) as registered_count
                    FROM event_info e
                    LEFT JOIN event_members em ON e.event_id = em.event_id 
                        AND em.participation_status IN ('registered', 'attended')
                    WHERE e.event_id = %s AND e.group_id = %s AND e.status = 'scheduled'
                        AND e.event_date >= CURDATE()
                    GROUP BY e.event_id, e.event_title, e.max_participants, e.event_date
                """, (pending_event_id, group_id))
                target_event = cur.fetchone()
            
            # If no pending event or it's not valid, fall back to the most recent upcoming event
            if not target_event:
                cur.execute("""
                    SELECT e.event_id, e.event_title, e.max_participants,
                           COUNT(em.membership_id) as registered_count
                    FROM event_info e
                    LEFT JOIN event_members em ON e.event_id = em.event_id 
                        AND em.participation_status IN ('registered', 'attended')
                    WHERE e.group_id = %s AND e.status = 'scheduled'
                        AND e.event_date >= CURDATE()
                    GROUP BY e.event_id, e.event_title, e.max_participants, e.event_date
                    ORDER BY e.event_date ASC
                    LIMIT 1
                """, (group_id,))
                target_event = cur.fetchone()
            
            # Auto-register for the target event if available and not full
            if target_event and target_event['registered_count'] < target_event['max_participants']:
                # Check if user is already registered for this event
                cur.execute("""
                    SELECT 1 FROM event_members 
                    WHERE user_id = %s AND event_id = %s
                """, (approved_user_id, target_event['event_id']))
                
                existing_registration = cur.fetchone()
                if not existing_registration:
                    # Register user for the event
                    cur.execute("""
                        INSERT INTO event_members (event_id, user_id, event_role, participation_status)
                        VALUES (%s, %s, 'participant', 'registered')
                    """, (target_event['event_id'], approved_user_id))
                    
                    # Clear the pending event notification
                    cur.execute("""
                        DELETE FROM notifications
                        WHERE user_id = %s AND title = 'PENDING_EVENT_REGISTRATION' 
                          AND category = 'system' AND related_id = %s
                    """, (approved_user_id, group_id))
                    
                    # Send notification about auto event registration
                    noti.create_noti(
                        user_id=approved_user_id,
                        title='Auto-Registered for Event',
                        message=f'You have been automatically registered for "{target_event["event_title"]}" after joining "{membership["group_name"]}".',
                        category='event',
                        related_id=target_event['event_id']
                    )
            
            flash(f'Successfully approved {membership["first_name"]} {membership["last_name"]}\'s request to join the group.', 'success')
    
    except Exception as e:
        flash('An error occurred while approving the request.', 'error')
    
    return redirect(url_for('membership_management', group_id=group_id))


@app.route('/reject-group-request', methods=['POST'])
@require_login
def reject_group_request():
    """Reject a pending group membership request"""
    user_id = get_current_user_id()
    membership_id = request.form.get('membership_id', type=int)
    group_id = request.form.get('group_id', type=int)
    reason = request.form.get('reason', '').strip()
    
    if not membership_id or not group_id:
        flash('Missing required information.', 'error')
        return redirect(url_for('membership_management', group_id=group_id))
    
    # Check permissions
    if is_super_admin() or is_support_technician():
        # Super admin and support technician can reject requests for any group
        pass
    elif is_group_manager():
        # Group manager can only reject requests for their own group
        if not can_change_group_roles_in_specific_group(group_id):
            flash('You can only manage requests for your own group.', 'error')
            return redirect(url_for('membership_management'))
    else:
        flash('Access denied.', 'error')
        return redirect(url_for('membership_management'))
    
    try:
        with db.get_cursor() as cur:
            # Get membership info
            cur.execute("""
                SELECT gm.user_id, gm.status, u.username, u.first_name, u.last_name, g.name as group_name
                FROM group_members gm
                JOIN users u ON gm.user_id = u.user_id
                JOIN group_info g ON gm.group_id = g.group_id
                WHERE gm.membership_id = %s AND gm.group_id = %s
            """, (membership_id, group_id))
            membership = cur.fetchone()
            
            if not membership:
                flash('Membership request not found.', 'error')
                return redirect(url_for('membership_management', group_id=group_id))
            
            if membership['status'] != 'pending':
                flash('This request is not pending.', 'warning')
                return redirect(url_for('membership_management', group_id=group_id))
            
            # Store rejection reason before removing the membership request
            if reason:
                # Validate the rejection reason is a valid ENUM value
                valid_reasons = ['group_full', 'activity_mismatch', 'insufficient_info', 'other']
                enum_reason = reason if reason in valid_reasons else 'other'
                
                # Store the rejection reason in group_requests table for tracking
                cur.execute("""
                    INSERT INTO group_requests (group_id, user_id, message, status, rejection_reason, requested_at)
                    VALUES (%s, %s, %s, 'rejected', %s, NOW())
                    ON DUPLICATE KEY UPDATE 
                    status = 'rejected', 
                    rejection_reason = %s,
                    message = %s,
                    requested_at = NOW()
                """, (group_id, membership['user_id'], f"Rejected membership request", enum_reason, enum_reason, f"Rejected membership request"))
            
            # Remove the membership request
            cur.execute("""
                DELETE FROM group_members
                WHERE membership_id = %s
            """, (membership_id,))
            
            # Send notification to the rejected member without reason
            noti.create_noti(
                user_id=membership['user_id'],
                title='Group Request Rejected',
                message=f'Your request to join "{membership["group_name"]}" has been rejected. Please contact support for more information.',
                category='group',
                related_id=group_id
            )
            
            # Send notification to the rejector
            noti.create_noti(
                user_id=user_id,
                title='Request Rejected',
                message=f'Successfully rejected {membership["first_name"]} {membership["last_name"]}\'s request to join "{membership["group_name"]}".',
                category='group',
                related_id=group_id
            )
            
            flash(f'Successfully rejected {membership["first_name"]} {membership["last_name"]}\'s request to join the group.', 'success')
    
    except Exception as e:
        print(f"Reject group request error: {e}")
        flash('An error occurred while rejecting the request.', 'error')
    
    return redirect(url_for('membership_management', group_id=group_id))