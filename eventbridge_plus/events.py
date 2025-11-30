"""
Event management system for ActiveLoop Plus Project 2

Features:
- Group-based event management
- Multi-role access control (Group Managers only)
- Integrated participant/volunteer registration
- Event discovery entry now unified under /search
- Real-time capacity tracking
"""

from datetime import datetime, date, time, timedelta
from flask import render_template, request, redirect, url_for, flash, session, abort, jsonify
from eventbridge_plus import db, noti
from eventbridge_plus.auth import (
    require_login,
    require_platform_role,
    require_group_role,
    get_current_user_id,
    get_current_platform_role,
    get_current_group_role,
    get_current_group_id,
    is_super_admin,
    is_support_technician,
    is_group_manager,
)
from eventbridge_plus.util import AVAILABLE_EVENT_TYPES, AVAILABLE_LOCATIONS, nz_date
# --- NEW: volunteer roles used by form & DB ---


# =============================================================================
# UTILITY FUNCTIONS FOR VOLUNTEER MANAGEMENT
# =============================================================================
def check_time_conflicts(user_id, event_date, event_time, exclude_event_id=None):
    """Check if user has time conflicts (participant + volunteer)"""
    try:
        with db.get_cursor() as cursor:
            sql = """
                SELECT 
                    e.event_id, e.event_title, e.event_date, e.event_time,
                    e.location, em.event_role, em.participation_status
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                WHERE em.user_id = %s
                  AND e.event_date = %s
                  AND e.event_time = %s
                  AND em.participation_status IN ('registered', 'attended')
            """
            params = [user_id, event_date, event_time]
            if exclude_event_id:
                sql += " AND e.event_id != %s"
                params.append(exclude_event_id)

            cursor.execute(sql, params)
            conflicts = cursor.fetchall()
            return {
                "has_conflict": len(conflicts) > 0,
                "conflicting_events": conflicts,
            }
    except Exception as e:
        print(f"Error checking time conflicts: {e}")
        return {"has_conflict": False, "conflicting_events": []}


def can_user_manage_event(user_id, event_id):
    """Check if user can manage a specific event (Group Manager or Admin)"""
    try:
        with db.get_cursor() as cursor:
            # Check if user is admin
            cursor.execute(
                """
                SELECT platform_role FROM users WHERE user_id = %s
                """,
                (user_id,),
            )
            user = cursor.fetchone()
            if user and user['platform_role'] in ('super_admin', 'support_technician'):
                # Admin can manage any event
                return True
            
            # Check if user is group manager
            cursor.execute(
                """
                SELECT 1 
                FROM event_info e
                JOIN group_members gm ON e.group_id = gm.group_id
                WHERE e.event_id = %s 
                  AND gm.user_id = %s 
                  AND gm.group_role = 'manager' 
                  AND gm.status = 'active'
                """,
                (event_id, user_id),
            )
            return cursor.fetchone() is not None
    except Exception as e:
        print(f"Error checking event management permission: {e}")
        return False


_EVENT_ROUTES_REGISTERED = False

def _format_hms(seconds):
    """Format seconds as H:MM:SS (M:SS if <1h). None returned '—'。"""
    if seconds is None:
        return "—"
    try:
        total = int(round(float(seconds)))
    except Exception:
        return "—"
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"

# =============================================================================
# ROUTE REGISTRATION
# =============================================================================
def register_event_routes(app):
    """Register all event routes with the Flask app"""
    global _EVENT_ROUTES_REGISTERED
    if _EVENT_ROUTES_REGISTERED or "event_detail" in app.view_functions:
        return

    # =============================================================================
    # VOLUNTEER MANAGEMENT ROUTES (Group Managers Only)
    # =============================================================================

    @app.route('/events/<int:event_id>/volunteers/pending', endpoint='pending_volunteers')
    @require_login
    def pending_volunteers(event_id):
        # Allow access to platform administrators or group managers
        is_admin = is_super_admin() or is_support_technician()
        is_group_mgr = session.get('group_role') == 'manager'
        
        if not (is_admin or is_group_mgr):
            flash('Access denied. Only admins or group managers can access this page.', 'error')
            return render_template('access_denied.html'), 403
            
        try:
            user_id = get_current_user_id()
            with db.get_cursor() as cursor:
                # Only check event management permission for group managers, not admins
                if not is_admin and not can_user_manage_event(user_id, event_id):
                    flash('You do not have permission to manage this event.', 'error')
                    return redirect(url_for('manage_events'))

                cursor.execute("""
                    SELECT e.event_title, e.event_date, e.group_id, g.name AS group_name
                    FROM event_info e
                    JOIN group_info g ON e.group_id = g.group_id
                    WHERE e.event_id = %s
                """, (event_id,))
                event = cursor.fetchone()
                if not event:
                    flash('Event not found.', 'error')
                    return redirect(url_for('manage_events'))

                # Get group volunteers
                cursor.execute("""
                    SELECT u.user_id, u.username, u.first_name, u.last_name
                    FROM group_members gm
                    JOIN users u ON gm.user_id = u.user_id
                    WHERE gm.group_id = %s AND gm.status = 'active' AND gm.group_role = 'volunteer'
                    ORDER BY u.username
                """, (event['group_id'],))
                group_volunteers = cursor.fetchall() or []
                
                # Get volunteers who applied to this event (only approved volunteers)
                cursor.execute("""
                    SELECT DISTINCT u.user_id, u.username, u.first_name, u.last_name
                    FROM event_members em
                    JOIN users u ON em.user_id = u.user_id
                    WHERE em.event_id = %s AND em.event_role = 'volunteer'
                      AND (em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled')
                    ORDER BY u.username
                """, (event_id,))
                event_volunteers = cursor.fetchall() or []
                
                # Merge and remove duplicates
                volunteer_ids = {v['user_id'] for v in group_volunteers}
                all_volunteers = list(group_volunteers)
                for ev in event_volunteers:
                    if ev['user_id'] not in volunteer_ids:
                        all_volunteers.append(ev)
                        volunteer_ids.add(ev['user_id'])
                
                group_members = all_volunteers

                cursor.execute("""
                    SELECT em.membership_id, em.user_id, u.username,
                        u.first_name, u.last_name, u.user_image,
                        em.registration_date, em.volunteer_status, em.responsibility
                    FROM event_members em
                    JOIN users u ON em.user_id = u.user_id
                    WHERE em.event_id = %s
                    AND em.event_role = 'volunteer'
                    AND em.volunteer_status = 'assigned'
                    ORDER BY em.registration_date ASC
                """, (event_id,))
                pending_vols = cursor.fetchall()

                return render_template(
                    'group_manager/volunteer_management.html',
                    event=event,
                    event_id=event_id,
                    pending_volunteers=pending_vols,
                    group_members=group_members   
                )
        except Exception as e:
            print(f"Error loading pending volunteers: {e}")
            flash('Error loading volunteer applications.', 'error')
            return redirect(url_for('manage_events'))


    @app.route(
        "/events/<int:event_id>/volunteers/<int:membership_id>/approve",
        methods=["POST"],
        endpoint="approve_volunteer",
    )
    @require_login
    def approve_volunteer(event_id, membership_id):
        """Approve volunteer application (Group Managers or Admins)"""
        is_admin = is_super_admin() or is_support_technician()
        is_group_mgr = session.get("group_role") == "manager"
        
        if not (is_admin or is_group_mgr):
            flash(
                "Access denied. Only group managers or administrators can approve volunteers.",
                "error",
            )
            return render_template("access_denied.html"), 403

        try:
            user_id = get_current_user_id()

            with db.get_cursor() as cursor:
                if (not (is_super_admin() or is_support_technician())) and (not can_user_manage_event(user_id, event_id)):
                    flash(
                        "You do not have permission to approve this application.",
                        "error",
                    )
                    return redirect(url_for("manage_events"))

                cursor.execute(
                    """
                    SELECT em.user_id, e.event_title
                    FROM event_members em
                    JOIN event_info e ON em.event_id = e.event_id
                    WHERE em.membership_id = %s AND em.event_id = %s
                    """,
                    (membership_id, event_id),
                )
                volunteer = cursor.fetchone()

                if not volunteer:
                    flash("Application not found.", "error")
                    return redirect(
                        url_for("pending_volunteers", event_id=event_id)
                    )

                cursor.execute(
                    """
                    UPDATE event_members
                    SET volunteer_status = 'confirmed'
                    WHERE membership_id = %s AND volunteer_status = 'assigned'
                    """,
                    (membership_id,),
                )

                noti.create_noti(
                    user_id=volunteer["user_id"],
                    title="Volunteer Application Approved",
                    message=(
                        f'Your volunteer application has been approved for '
                        f'"{volunteer["event_title"]}". Please attend on time!'
                    ),
                    category="volunteer",
                    related_id=event_id,
                )

                flash("Volunteer application approved!", "success")
                return redirect(
                    url_for("pending_volunteers", event_id=event_id)
                )

        except Exception as e:
            print(f"Error approving volunteer: {e}")
            flash("Error approving application.", "error")
            return redirect(
                url_for("pending_volunteers", event_id=event_id)
            )

    @app.route(
        "/events/<int:event_id>/volunteers/<int:membership_id>/reject",
        methods=["POST"],
        endpoint="reject_volunteer",
    )
    @require_login
    def reject_volunteer(event_id, membership_id):
        """Reject volunteer application (Group Managers or Admins)"""
        is_admin = session.get("platform_role") in ("super_admin", "support_technician")
        is_group_mgr = session.get("group_role") == "manager"
        
        if not (is_admin or is_group_mgr):
            flash(
                "Access denied. Only group managers or administrators can reject volunteers.",
                "error",
            )
            return render_template("access_denied.html"), 403

        try:
            user_id = get_current_user_id()
            reason = request.form.get("reason", "No reason provided").strip()

            with db.get_cursor() as cursor:
                if (not (is_super_admin() or is_support_technician())) and (not can_user_manage_event(user_id, event_id)):
                    flash(
                        "You do not have permission to reject this application.",
                        "error",
                    )
                    return redirect(url_for("manage_events"))

                cursor.execute(
                    """
                    SELECT em.user_id, e.event_title
                    FROM event_members em
                    JOIN event_info e ON em.event_id = e.event_id
                    WHERE em.membership_id = %s AND em.event_id = %s
                    """,
                    (membership_id, event_id),
                )
                volunteer = cursor.fetchone()

                if not volunteer:
                    flash("Application not found.", "error")
                    return redirect(
                        url_for("pending_volunteers", event_id=event_id)
                    )

                cursor.execute(
                    """
                    UPDATE event_members
                    SET volunteer_status = 'cancelled'
                    WHERE membership_id = %s AND volunteer_status = 'assigned'
                    """,
                    (membership_id,),
                )

                noti.create_noti(
                    user_id=volunteer["user_id"],
                    title="Volunteer Application Rejected",
                    message=(
                        f'Sorry, your volunteer application was not approved for '
                        f'"{volunteer["event_title"]}". Reason: {reason}'
                    ),
                    category="volunteer",
                    related_id=event_id,
                )

                flash("Volunteer application rejected.", "info")
                return redirect(
                    url_for("pending_volunteers", event_id=event_id)
                )

        except Exception as e:
            print(f"Error rejecting volunteer: {e}")
            flash("Error rejecting application.", "error")
            return redirect(
                url_for("pending_volunteers", event_id=event_id)
            )

    @app.route(
        "/events/<int:event_id>/volunteers/cancel",
        methods=["POST"],
        endpoint="cancel_volunteer",
    )
    @require_platform_role("participant", "super_admin", "support_technician")
    def cancel_volunteer(event_id):
        """Cancel volunteer application (by volunteer)"""
        try:
            user_id = get_current_user_id()

            with db.get_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT em.membership_id, e.event_title, em.volunteer_status
                    FROM event_members em
                    JOIN event_info e ON em.event_id = e.event_id
                    WHERE em.user_id = %s AND em.event_id = %s 
                      AND em.event_role = 'volunteer'
                    """,
                    (user_id, event_id),
                )
                volunteer_record = cursor.fetchone()

                if not volunteer_record:
                    flash(
                        "You have not applied as volunteer for this event.",
                        "error",
                    )
                    return redirect(url_for("event_detail", event_id=event_id))

                if volunteer_record["volunteer_status"] == "completed":
                    flash(
                        "Cannot cancel completed volunteer activity.", "warning"
                    )
                    return redirect(url_for("event_detail", event_id=event_id))

                cursor.execute(
                    """
                    DELETE FROM event_members
                    WHERE membership_id = %s
                    """,
                    (volunteer_record["membership_id"],),
                )

                noti.create_noti(
                    user_id=user_id,
                    title="Volunteer Application Cancelled",
                    message=(
                        f'You have cancelled your volunteer application for '
                        f'"{volunteer_record["event_title"]}".'
                    ),
                    category="volunteer",
                    related_id=event_id,
                )

                flash("Volunteer application cancelled.", "success")
                return redirect(url_for("event_detail", event_id=event_id))

        except Exception as e:
            print(f"Error canceling volunteer: {e}")
            flash("Error cancelling application.", "error")
            return redirect(url_for("event_detail", event_id=event_id))

    @app.route('/events/<int:event_id>/volunteers/assign', methods=['POST'], endpoint='assign_volunteer')
    @require_login
    def assign_volunteer(event_id):
        """Manager assigns a user as volunteer immediately (no approval)."""
        # Allow admin or group managers
        is_admin = session.get("platform_role") in ("super_admin", "support_technician")
        is_group_mgr = session.get('group_role') == 'manager'
        
        if not (is_admin or is_group_mgr):
            flash('Access denied. Only group managers or administrators can assign volunteers.', 'error')
            return render_template('access_denied.html'), 403

        mgr_id = get_current_user_id()

        try:
            with db.get_cursor() as cursor:
                # Permission verification: Can manage this activity
                if (not (is_super_admin() or is_support_technician())) and (not can_user_manage_event(mgr_id, event_id)):
                    flash('You do not have permission to manage this event.', 'error')
                    return redirect(url_for('manage_events'))

                # Read form fields (try to be compatible with different names)
                raw_user_id = (
                    request.form.get('user_id')
                    or request.form.get('member_id')
                    or request.form.get('selected_member')
                )

                try:
                    target_user_id = int(raw_user_id)
                except (TypeError, ValueError):
                    flash('Please select a member to assign.', 'error')
                    return redirect(url_for('pending_volunteers', event_id=event_id))
                
                # Get responsibility from form
                responsibility = request.form.get('responsibility', '').strip()
                if not responsibility or responsibility not in ['event_setup', 'safety_medical', 'participant_support', 'community_outreach', 'photography']:
                    responsibility = None

                # Read activity information (time, group)
                cursor.execute("""
                    SELECT group_id, event_title, event_date, event_time
                    FROM event_info WHERE event_id=%s
                """, (event_id,))
                ev = cursor.fetchone()
                if not ev:
                    flash('Event not found.', 'error')
                    return redirect(url_for('manage_events'))

                # Must be an active member of the group
                cursor.execute("""
                    SELECT 1 FROM group_members
                    WHERE group_id=%s AND user_id=%s AND status='active' LIMIT 1
                """, (ev['group_id'], target_user_id))
                if cursor.fetchone() is None:
                    flash('User is not an active member of this group.', 'error')
                    return redirect(url_for('pending_volunteers', event_id=event_id))

                # Conflict detection: other events on the same day and time
                conflict = check_time_conflicts(
                    user_id=target_user_id,
                    event_date=ev['event_date'],
                    event_time=ev['event_time'],
                    exclude_event_id=event_id,   # Exclude this activity
                )
                if conflict.get('has_conflict'):
                    flash('Unable to assign tasks due to overlapping time', 'warning')
                    return redirect(url_for('pending_volunteers', event_id=event_id))

                # Is there a record of this activity (maybe participant or volunteer (pending)）
                cursor.execute("""
                    SELECT membership_id, event_role
                    FROM event_members
                    WHERE event_id=%s AND user_id=%s
                    LIMIT 1
                """, (event_id, target_user_id))
                existed = cursor.fetchone()

                if existed:
                    # Directly transferred to confirmed volunteers
                    cursor.execute("""
                        UPDATE event_members
                        SET event_role='volunteer',
                            participation_status='registered',
                            volunteer_status='confirmed',
                            responsibility=%s
                        WHERE membership_id=%s
                    """, (responsibility if responsibility else None, existed['membership_id']))
                else:
                    # Create a new volunteer record and directly confirm
                    cursor.execute("""
                        INSERT INTO event_members
                        (event_id, user_id, event_role, participation_status, volunteer_status, responsibility)
                        VALUES (%s, %s, 'volunteer', 'registered', 'confirmed', %s)
                    """, (event_id, target_user_id, responsibility if responsibility else None))

                # Notify the member
                try:
                    noti.create_noti(
                        user_id=target_user_id,
                        title='Volunteer Assignment',
                        message=f'You have been assigned as a volunteer for "{ev["event_title"]}".',
                        category='volunteer',
                        related_id=event_id
                    )
                except Exception:
                    pass

                flash('Volunteer assigned successfully.', 'success')
                return redirect(url_for('pending_volunteers', event_id=event_id))

        except Exception as e:
            print(f"Error assigning volunteer: {e}")
            flash('Error assigning volunteer.', 'error')
            return redirect(url_for('pending_volunteers', event_id=event_id))

    # =============================================================================
    # EVENT DETAIL (Public View)
    # =============================================================================
    @app.route("/events/<int:event_id>", endpoint="event_detail")
    def event_detail(event_id):
        """Event detail page (public access for public/approved groups)"""
        try:
            with db.get_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT 
                        e.event_id, e.group_id, e.event_title, e.description, e.event_type,
                        e.event_date, e.event_time, e.location, e.max_participants,
                        e.status, e.created_at,
                        g.name AS group_name, g.description AS group_description,
                        g.is_public, g.status AS group_status,
                        COUNT(em.membership_id) AS registered_count
                    FROM event_info e
                    JOIN group_info g ON e.group_id = g.group_id
                    LEFT JOIN event_members em ON e.event_id = em.event_id 
                      AND em.participation_status IN ('registered', 'attended')
                      AND (em.event_role != 'volunteer' OR em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled')
                    WHERE e.event_id = %s
                    GROUP BY e.event_id, e.group_id, e.event_title, e.description,
                             e.event_type, e.event_date, e.event_time, e.location,
                             e.max_participants, e.status, e.created_at,
                             g.name, g.description, g.is_public, g.status
                    """,
                    (event_id,),
                )
                event = cursor.fetchone()

                if not event:
                    flash("Event not found.", "error")
                    return redirect(url_for("explore", tab="events"))

                user_id = get_current_user_id()

                # Check if user can view this event
                can_view = False

                if event["group_status"] != "approved":
                    # Group not approved - no one can view
                    can_view = False
                elif event["is_public"]:
                    # Public group - everyone can view
                    can_view = True
                elif user_id:
                    # Private group - check if user is a member
                    cursor.execute(
                        """
                        SELECT 1 
                        FROM group_members 
                        WHERE user_id = %s AND group_id = %s AND status = 'active'
                        """,
                        (user_id, event["group_id"]),
                    )
                    can_view = cursor.fetchone() is not None
                else:
                    # Private group and not logged in - cannot view
                    can_view = False

                if not can_view:
                    if user_id and not event["is_public"]:
                        # Logged in user trying to access private group event
                        # Store the event ID in session for auto-registration after group approval
                        session["pending_event_registration"] = {
                            "event_id": event_id,
                            "group_id": event["group_id"],
                            "event_title": event["event_title"],
                        }
                        flash(
                            "You need to join this group first to view and register for events.",
                            "info",
                        )
                        return redirect(
                            url_for("group_detail", group_id=event["group_id"])
                        )
                    else:
                        # Not logged in or other access issues
                        flash("This event is not accessible.", "error")
                        return redirect(url_for("explore", tab="events"))

                event["spots_available"] = (
                    event["max_participants"] - event["registered_count"]
                )
                event["is_full"] = event["spots_available"] <= 0

                user_registration = None
                user_group_role = None
                if user_id:
                    cursor.execute(
                        """
                        SELECT event_role, participation_status, volunteer_status
                        FROM event_members 
                        WHERE user_id = %s AND event_id = %s
                        """,
                        (user_id, event_id),
                    )
                    user_registration = cursor.fetchone()
                    
                    # Get user's group role for this event's group
                    cursor.execute(
                        """
                        SELECT group_role
                        FROM group_members 
                        WHERE user_id = %s AND group_id = %s AND status = 'active'
                        """,
                        (user_id, event["group_id"]),
                    )
                    group_role_result = cursor.fetchone()
                    user_group_role = group_role_result["group_role"] if group_role_result else None

                can_manage_event = False
                if user_id:
                    can_manage_event = can_user_manage_event(user_id, event_id)

                participants = []
                if can_manage_event:
                    cursor.execute(
                        """
                        SELECT 
                            em.membership_id, em.event_role, em.participation_status,
                            em.volunteer_status, em.responsibility, em.registration_date,
                            u.user_id, u.username, u.first_name, u.last_name,
                            u.email, u.user_image
                        FROM event_members em
                        JOIN users u ON em.user_id = u.user_id
                        WHERE em.event_id = %s
                          AND em.participation_status IN ('registered', 'attended')
                          AND (em.event_role != 'volunteer' OR em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled')
                        ORDER BY em.registration_date ASC
                        """,
                        (event_id,),
                    )
                    participants = cursor.fetchall()

                # ---  load volunteer needs (required + current assigned) ---
                return render_template(
                    "search/event_detail.html",
                    event=event,
                    user_registration=user_registration,
                    user_group_role=user_group_role,
                    can_manage_event=can_manage_event,
                    participants=participants
                )


        except Exception as e:
            print(f"Error loading event detail: {e}")
            import traceback

            traceback.print_exc()
            flash("Error loading event details.", "error")
            return redirect(url_for("explore", tab="events"))

    @app.route("/events/<int:event_id>/volunteers/update-role", methods=["POST"], endpoint="update_volunteer_role")
    @require_login
    def update_volunteer_role(event_id):
        """Update volunteer role/responsibility for an event member"""
        try:
            data = request.get_json()
            if not data:
                return jsonify({'success': False, 'error': 'No JSON data received'})
                
            membership_id = data.get('membership_id')
            responsibility = data.get('responsibility')
            
            if not membership_id or not responsibility:
                return jsonify({'success': False, 'error': 'Missing required fields'})
            
            user_id = get_current_user_id()
            
            if not user_id:
                return jsonify({'success': False, 'error': 'User not logged in'})
            
            # Check if user can manage this event
            can_manage_event = (is_super_admin() or is_support_technician()) or can_user_manage_event(user_id, event_id)
            
            if not can_manage_event:
                return jsonify({'success': False, 'error': 'Access denied - you cannot manage this event'})
            
            with db.get_cursor() as cursor:
                # Verify the membership exists and is a volunteer
                cursor.execute("""
                    SELECT em.membership_id, em.event_role, em.user_id, u.username
                    FROM event_members em
                    JOIN users u ON em.user_id = u.user_id
                    WHERE em.membership_id = %s AND em.event_id = %s
                """, (membership_id, event_id))
                
                member = cursor.fetchone()
                
                if not member:
                    return jsonify({'success': False, 'error': 'Member not found'})
                
                if member['event_role'] != 'volunteer':
                    return jsonify({'success': False, 'error': 'Member is not a volunteer'})
                
                # Update the responsibility
                cursor.execute("""
                    UPDATE event_members 
                    SET responsibility = %s
                    WHERE membership_id = %s AND event_id = %s
                """, (responsibility, membership_id, event_id))
                
                # Get event and group info for notification
                cursor.execute("""
                    SELECT e.event_title, e.event_date, g.name as group_name
                    FROM event_info e
                    JOIN group_info g ON e.group_id = g.group_id
                    WHERE e.event_id = %s
                """, (event_id,))
                event_info = cursor.fetchone()
                
                # Send notification to the volunteer
                if event_info:
                    responsibility_display = responsibility.replace('_', ' ').title()
                    noti.create_noti(
                        user_id=member['user_id'],
                        title='Volunteer Role Assigned',
                        message=f'You have been assigned the role of "{responsibility_display}" for the event "{event_info["event_title"]}" on {event_info["event_date"]} by {event_info["group_name"]}.',
                        category='volunteer',
                        related_id=event_id
                    )
                
                return jsonify({
                    'success': True, 
                    'message': f"Updated {member['username']}'s role to {responsibility.replace('_', ' ').title()}"
                })
                
        except Exception as e:
            print(f"Error updating volunteer role: {e}")
            return jsonify({'success': False, 'error': 'Internal server error'})

    _EVENT_ROUTES_REGISTERED = True
    # =============================================================================
    # EVENT LEVEL STATS (Participant/Admin can view)
    # =============================================================================
    @app.route('/events/<int:event_id>/stats', methods=['GET'], endpoint='event_stats')
    @require_login
    def event_stats(event_id: int):
        """Event statistics (read-only). Allow admins/managers OR participants of this event.
        Still filters out future results (finish_time <= NOW())."""
        try:
            with db.get_cursor() as cursor:
                # 1) Event info + registered count
                cursor.execute("""
                    SELECT 
                        e.event_id, e.group_id, e.event_title, e.event_date, e.event_time,
                        e.location, g.name AS group_name,
                        (
                            SELECT COUNT(*)
                            FROM event_members em
                            WHERE em.event_id = e.event_id
                            AND (em.participation_status IS NULL
                                OR em.participation_status IN ('registered','attended','completed'))
                            AND (em.event_role != 'volunteer' OR em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled')
                        ) AS registered_count
                    FROM event_info e
                    JOIN group_info g ON g.group_id = e.group_id
                    WHERE e.event_id = %s
                    LIMIT 1
                """, (event_id,))
                ev = cursor.fetchone() or {}
                if not ev.get("event_id"):
                    flash("Event not found.", "error")
                    return redirect(url_for("event_detail", event_id=event_id))

                # ---------- access authorization ----------
                # Allowed: Platform administrators; group leaders of the event group; or participants of the event
                try:
                    from eventbridge_plus.auth import get_current_user_id
                except Exception:

                    get_current_user_id = None


                from flask import session
                platform_role = (session.get('platform_role') or '').lower()
                is_admin_like = platform_role in ('super_admin', 'support_technician')

                is_group_manager = False
                if not is_admin_like:
 
                    try:
                        cursor.execute("""
                            SELECT 1
                            FROM group_members
                            WHERE group_id=%s AND user_id=%s AND group_role='manager'
                            LIMIT 1
                        """, (ev["group_id"], int(get_current_user_id() or 0)))
                        is_group_manager = bool(cursor.fetchone())
                    except Exception:
                        is_group_manager = False  

                is_event_participant = False
                if not (is_admin_like or is_group_manager):
                    uid = int(get_current_user_id() or 0)
                    cursor.execute("""
                        SELECT 1
                        FROM event_members
                        WHERE event_id=%s AND user_id=%s
                        AND event_role='participant'
                        AND participation_status IN ('registered','attended','completed')
                        LIMIT 1
                    """, (event_id, uid))
                    is_event_participant = bool(cursor.fetchone())

                if not (is_admin_like or is_group_manager or is_event_participant):

                    flash("You don't have permission to view this event's stats.", "warning")
                    return redirect(url_for("event_detail", event_id=event_id))
                # ---------- end ----------

                # 2) Aggregates (ONLY valid & not in the future)
                cursor.execute("""
                    SELECT 
                        COUNT(*) AS total_valid,
                        AVG(TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time)) AS avg_sec,
                        MIN(TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time)) AS min_sec,
                        MAX(TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time)) AS max_sec
                    FROM race_results rr
                    JOIN event_members em ON em.membership_id = rr.membership_id
                    WHERE em.event_id = %s
                    AND rr.start_time IS NOT NULL
                    AND rr.finish_time IS NOT NULL
                    AND rr.finish_time > rr.start_time
                    AND rr.finish_time <= NOW()              -- Only count the results that have occurred
                """, (event_id,))
                agg = cursor.fetchone() or {}

                def _format_hms(sec):
                    if sec is None:
                        return "—"
                    sec = int(sec)
                    h = sec // 3600
                    m = (sec % 3600) // 60
                    s = sec % 60
                    return f"{h:01d}:{m:02d}:{s:02d}"

                total_valid = int(agg.get("total_valid") or 0)
                avg_hms = _format_hms(agg.get("avg_sec"))
                min_hms = _format_hms(agg.get("min_sec"))
                max_hms = _format_hms(agg.get("max_sec"))

                # 3) Fastest / slowest (Filter the future as well)
                cursor.execute("""
                    SELECT COALESCE(NULLIF(CONCAT(u.first_name,' ',u.last_name),' '), u.username) AS full_name
                    FROM race_results rr
                    JOIN event_members em ON em.membership_id = rr.membership_id
                    JOIN users u ON u.user_id = em.user_id
                    WHERE em.event_id = %s
                    AND rr.start_time IS NOT NULL
                    AND rr.finish_time IS NOT NULL
                    AND rr.finish_time > rr.start_time
                    AND rr.finish_time <= NOW()
                    ORDER BY TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time) ASC
                    LIMIT 1
                """, (event_id,))
                fastest = cursor.fetchone() or {}

                cursor.execute("""
                    SELECT COALESCE(NULLIF(CONCAT(u.first_name,' ',u.last_name),' '), u.username) AS full_name
                    FROM race_results rr
                    JOIN event_members em ON em.membership_id = rr.membership_id
                    JOIN users u ON u.user_id = em.user_id
                    WHERE em.event_id = %s
                    AND rr.start_time IS NOT NULL
                    AND rr.finish_time IS NOT NULL
                    AND rr.finish_time > rr.start_time
                    AND rr.finish_time <= NOW()
                    ORDER BY TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time) DESC
                    LIMIT 1
                """, (event_id,))
                slowest = cursor.fetchone() or {}

                # 4) Ranked list
                cursor.execute("""
                    SELECT
                        COALESCE(NULLIF(CONCAT(u.first_name,' ',u.last_name),' '), u.username) AS full_name,
                        rr.start_time,
                        rr.finish_time,
                        TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time) AS elapsed_sec,
                        TIME(rr.start_time)  AS start_hms,   -- NEW: For display
                        TIME(rr.finish_time) AS finish_hms,
                        SEC_TO_TIME(TIMESTAMPDIFF(SECOND, rr.start_time, rr.finish_time)) AS elapsed_hms
                    FROM race_results rr
                    JOIN event_members em ON em.membership_id = rr.membership_id
                    JOIN users u ON u.user_id = em.user_id
                    WHERE em.event_id = %s
                    AND rr.start_time IS NOT NULL
                    AND rr.finish_time IS NOT NULL
                    AND rr.finish_time > rr.start_time
                    AND rr.finish_time <= NOW()
                    ORDER BY elapsed_sec ASC, rr.finish_time ASC
                """, (event_id,))
                ranked_results = cursor.fetchall() or []


            return render_template(
                "event_stats.html",
                ev=ev,
                total_registered=ev.get("registered_count", 0),
                total_valid=total_valid,
                avg_hms=avg_hms,
                min_hms=min_hms,
                max_hms=max_hms,
                fastest_name=(fastest.get("full_name") or "—"),
                slowest_name=(slowest.get("full_name") or "—"),
                ranked_results=ranked_results,
                readonly=True 
            )

        except Exception as e:
            import traceback; traceback.print_exc()
            flash("Failed to load event statistics.", "error")
            return redirect(url_for("event_detail", event_id=event_id))




    # =============================================================================
    # EVENT MANAGEMENT ROUTES (Group Managers Only)
    # =============================================================================
    @app.route("/events/create", methods=["GET", "POST"])
    @require_login
    def create_event():
        """Create new event (Group Managers or Admins)"""
        is_admin = session.get("platform_role") in ("super_admin", "support_technician")
        is_group_mgr = session.get("group_role") == "manager"
        
        if not (is_admin or is_group_mgr):
            flash(
                "Access denied. Only group managers or administrators can create events.", "error"
            )
            return render_template("access_denied.html"), 403

        if request.method == "GET":
            user_id = get_current_user_id()
            groups = []
            try:
                with db.get_cursor() as cursor:
                    if is_admin:
                        # Admins can see all approved groups
                        cursor.execute(
                            """
                            SELECT g.group_id, g.name 
                            FROM group_info g
                            WHERE g.status = 'approved'
                            ORDER BY g.name
                            """
                        )
                    else:
                        # Group managers see only their groups
                        cursor.execute(
                            """
                            SELECT g.group_id, g.name 
                            FROM group_info g
                            JOIN group_members gm ON g.group_id = gm.group_id
                            WHERE gm.user_id = %s 
                              AND gm.group_role = 'manager'
                              AND gm.status = 'active' 
                              AND g.status = 'approved'
                            ORDER BY g.name
                            """,
                            (user_id,),
                        )
                    groups = cursor.fetchall()
            except Exception as e:
                print(f"Error loading groups: {e}")
                groups = []

            return render_template(
                "group_manager/create_event.html",
                groups=groups,
                event_types=AVAILABLE_EVENT_TYPES,
                locations=AVAILABLE_LOCATIONS,
            )

        try:
            group_id = request.form.get("group_id", "").strip()
            event_title = request.form.get("event_title", "").strip()
            description = request.form.get("description", "").strip()
            event_type = request.form.get("event_type", "").strip()
            event_date = request.form.get("event_date", "").strip()
            event_time = request.form.get("event_time", "").strip()
            location = request.form.get("location", "").strip()
            max_participants = request.form.get("max_participants", "").strip()

            errors = []
            if not group_id or not group_id.isdigit():
                errors.append("Please select a valid group.")
            if not event_title or len(event_title) < 3:
                errors.append("Event title must be at least 3 characters long.")
            if len(event_title) > 200:
                errors.append("Event title cannot exceed 200 characters.")
            if event_type not in AVAILABLE_EVENT_TYPES:
                errors.append("Please select a valid event type.")
            if location not in AVAILABLE_LOCATIONS:
                errors.append("Please select a valid location.")

            try:
                parsed_date = datetime.strptime(event_date, "%Y-%m-%d").date()
                if parsed_date <= date.today():
                    errors.append("Event date must be in the future.")
            except ValueError:
                errors.append("Please enter a valid date.")

            try:
                _ = datetime.strptime(event_time, "%H:%M").time()
            except ValueError:
                errors.append("Please enter a valid time (HH:MM format).")

            try:
                max_participants = int(max_participants)
                if max_participants < 1 or max_participants > 1000:
                    errors.append(
                        "Maximum participants must be between 1 and 1000."
                    )
            except ValueError:
                errors.append(
                    "Please enter a valid number for maximum participants."
                )

            # Prevent global duplicate event names among active events (exclude cancelled/past)
            if not errors:
                try:
                    with db.get_cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT 1
                            FROM event_info
                            WHERE LOWER(TRIM(event_title)) = LOWER(TRIM(%s))
                              AND (status IS NULL OR LOWER(status) <> 'cancelled')
                              AND event_date >= CURDATE()
                            LIMIT 1
                            """,
                            (event_title,),
                        )
                        if cursor.fetchone():
                            errors.append(
                                "An active event with this title already exists. Please choose a different title."
                            )
                except Exception:
                    pass

            # Prevent duplicate event names within the same group on the same date
            if not errors:
                try:
                    with db.get_cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT 1
                            FROM event_info
                            WHERE group_id = %s
                              AND event_date = %s
                              AND LOWER(TRIM(event_title)) = LOWER(TRIM(%s))
                            LIMIT 1
                            """,
                            (group_id, event_date, event_title),
                        )
                        if cursor.fetchone():
                            errors.append(
                                "An event with the same title already exists for this group on the selected date."
                            )
                except Exception:
                    # On any DB check error, do not block creation here; continue to general error flow
                    pass

            if errors:
                for err in errors:
                    flash(err, "error")
                return redirect(url_for("create_event"))

            user_id = get_current_user_id()
            with db.get_cursor() as cursor:
                # Check if user is admin
                if is_admin:
                    # Admins can create events for any approved group
                    cursor.execute(
                        """
                        SELECT 1 FROM group_info 
                        WHERE group_id = %s AND status = 'approved'
                        """,
                        (group_id,),
                    )
                    can_create_for_group = cursor.fetchone() is not None
                else:
                    # Group managers can only create for their groups
                    cursor.execute(
                        """
                        SELECT 1 
                        FROM group_members gm
                        JOIN group_info g ON gm.group_id = g.group_id
                        WHERE gm.user_id = %s AND gm.group_id = %s 
                          AND gm.group_role = 'manager' 
                          AND gm.status = 'active'
                          AND g.status = 'approved'
                        """,
                        (user_id, group_id),
                    )
                    can_create_for_group = cursor.fetchone() is not None

                if not can_create_for_group:
                    flash(
                        "You do not have permission to create events for this group.",
                        "error",
                    )
                    return redirect(url_for("create_event"))

                # --- Insert into event_info ---
                cursor.execute(
                    """
                    INSERT INTO event_info (
                        group_id, event_title, description, event_type,
                        event_date, event_time, location, max_participants,
                        status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'scheduled')
                    """,
                    (
                        group_id, event_title, description, event_type,
                        event_date, event_time, location, max_participants,
                    ),
                )

                # Get the newly created event_id
                try:
                    event_id = cursor.lastrowid
                except Exception:
                    cursor.execute("SELECT LAST_INSERT_ID() AS eid")
                    event_id = (cursor.fetchone() or {}).get("eid")



                try:
                    cursor.connection.commit()
                except Exception:
                    pass

                flash("Event created successfully!", "success")
                return redirect(url_for("manage_events"))


        except Exception as e:
            print(f"Error creating event: {e}")
            flash("An error occurred while creating the event.", "error")
            return redirect(url_for("create_event"))

    @app.route("/events/manage")
    @app.route("/events/manage")
    @require_login
    def manage_events():
        """Event management dashboard (Admin or Group Managers)"""
        # Allow access to platform administrators (super_admin/support_technician) or group administrators
        is_admin = session.get("platform_role") in ("super_admin", "support_technician")
        if (not is_admin) and (session.get("group_role") != "manager"):
            flash("Access denied. Only admins or group managers can access this page.", "error")
            return render_template("access_denied.html"), 403

        # --- Tool: Unify event_time into datetime.time ---
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
                    parts = [int(p) for p in v.split(":")]
                    while len(parts) < 3:
                        parts.append(0)
                    return time(parts[0], parts[1], parts[2])
                except Exception:
                    return None
            return None

        try:
            user_id = get_current_user_id()
            page = request.args.get("page", 1, type=int)
            per_page = 10
            sort_by = request.args.get("sort", "date_desc")
            
            # Get search parameters
            event_search = request.args.get("event_search", "").strip()
            group_filter = request.args.get("group_filter", "").strip()
            group_search = request.args.get("group_search", "").strip()  # For admin text search
            location_search = request.args.get("location_search", "").strip()
            location_type = request.args.get("location_type", "all").strip()
            group_id_param = request.args.get("group_id", "").strip()  # From summary card

            if sort_by == "date_desc":
                order_clause = "e.event_date DESC, e.event_time DESC"
            elif sort_by == "title_asc":
                order_clause = "e.event_title ASC"
            elif sort_by == "title_desc":
                order_clause = "e.event_title DESC"
            elif sort_by == "registered":
                order_clause = "registered_count DESC"
            else:
                order_clause = "e.event_date ASC, e.event_time ASC"

            with db.get_cursor() as cursor:
                # Administrators can view all; group administrators can only view the groups they manage.
                if is_admin:
                    base_query = """
                        FROM event_info e
                        JOIN group_info g ON e.group_id = g.group_id
                        LEFT JOIN event_members em ON e.event_id = em.event_id
                        AND em.participation_status IN ('registered', 'attended')
                        AND (em.event_role != 'volunteer' OR em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled')
                    """
                    where_conditions = ["1=1"]
                    params = []
                else:
                    base_query = """
                        FROM event_info e
                        JOIN group_info g ON e.group_id = g.group_id
                        JOIN group_members gm ON g.group_id = gm.group_id
                        LEFT JOIN event_members em ON e.event_id = em.event_id
                        AND em.participation_status IN ('registered', 'attended')
                        AND (em.event_role != 'volunteer' OR em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled')
                    """
                    where_conditions = [
                        "gm.user_id = %s",
                        "gm.group_role = 'manager'",
                        "gm.status = 'active'"
                    ]
                    params = [user_id]

                # Add search filters
                if event_search:
                    where_conditions.append("LOWER(e.event_title) LIKE LOWER(%s)")
                    params.append(f"%{event_search}%")

                # Group search/filter logic
                if group_id_param:
                    # Filter by specific group (from summary card)
                    where_conditions.append("g.group_id = %s")
                    params.append(group_id_param)
                elif is_admin and group_search:
                    # Admin: text search for group names
                    where_conditions.append("LOWER(g.name) LIKE LOWER(%s)")
                    params.append(f"%{group_search}%")
                elif not is_admin and group_filter:
                    # Group manager: dropdown filter for specific group
                    where_conditions.append("g.group_id = %s")
                    params.append(group_filter)

                # Location search (only for admin)
                if is_admin and location_search:
                    if location_type == "events":
                        where_conditions.append("LOWER(e.location) LIKE LOWER(%s)")
                        params.append(f"%{location_search}%")
                    elif location_type == "groups":
                        where_conditions.append("LOWER(g.group_location) LIKE LOWER(%s)")
                        params.append(f"%{location_search}%")
                    elif location_type == "all":
                        where_conditions.append("(LOWER(e.location) LIKE LOWER(%s) OR LOWER(g.group_location) LIKE LOWER(%s))")
                        params.extend([f"%{location_search}%", f"%{location_search}%"])

                where_clause = "WHERE " + " AND ".join(where_conditions)

                # Get available groups for filter dropdown (only for group managers)
                available_groups = []
                if not is_admin:
                    cursor.execute("""
                        SELECT DISTINCT g.group_id, g.name
                        FROM group_info g
                        JOIN group_members gm ON g.group_id = gm.group_id
                        WHERE gm.user_id = %s AND gm.group_role = 'manager' AND gm.status = 'active'
                        ORDER BY g.name
                    """, (user_id,))
                    available_groups = cursor.fetchall()

                count_sql = f"""
                    SELECT COUNT(DISTINCT e.event_id) AS total
                    {base_query}
                    {where_clause}
                """
                cursor.execute(count_sql, params)
                total_events = cursor.fetchone()["total"]

                total_pages = (total_events + per_page - 1) // per_page
                page = max(1, min(page, total_pages if total_pages > 0 else 1))
                offset = (page - 1) * per_page

                events_sql = f"""
                    SELECT 
                        e.event_id, e.group_id, e.event_title, e.event_type, e.event_date,
                        e.event_time, e.location, e.max_participants, e.status,
                        g.name AS group_name,
                        COUNT(DISTINCT em.membership_id) AS registered_count,
                        COUNT(DISTINCT CASE WHEN em.event_role = 'participant' THEN em.membership_id END) AS participant_count,
                        COUNT(DISTINCT CASE WHEN em.event_role = 'volunteer' AND (em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled') THEN em.membership_id END) AS volunteer_count
                    {base_query}
                    {where_clause}
                    GROUP BY e.event_id, e.group_id, e.event_title, e.event_type, e.event_date,
                            e.event_time, e.location, e.max_participants, e.status,
                            g.name
                    ORDER BY {order_clause}
                    LIMIT %s OFFSET %s
                """
                cursor.execute(events_sql, params + [per_page, offset])
                events = cursor.fetchall()

            # —— Derived fields (quota, past/future, display status)——
            today = date.today()
            now_t = datetime.now().time()

            for ev in events:
                ev["spots_available"] = ev["max_participants"] - ev["registered_count"]
                ev["is_full"] = ev["spots_available"] <= 0

                # Calculate whether it is in the past/future (including the time of day)
                et = _coerce_time(ev.get("event_time"))
                is_past = False
                is_future = False
                if ev.get("event_date"):
                    if ev["event_date"] < today:
                        is_past = True
                    elif ev["event_date"] > today:
                        is_future = True
                    else:  # 今天
                        if et is not None:
                            if et <= now_t:
                                is_past = True   # Today and the time has come/past: Considered as "Started/Completed"
                            else:
                                is_future = True  # Today but not yet: Still seen as the future
                        else:
                            # No specific time: today is considered the future until it passes.
                            is_future = True

                ev["is_upcoming"] = not is_past
                ev["is_past"] = is_past

                # Unified display status: all scheduled in the future; started but not canceled → ongoing; past and not canceled → completed
                raw_status = (ev.get("status") or "").lower()
                if raw_status in ("cancelled", "draft"):
                    display_status = raw_status
                else:
                    if is_future:
                        display_status = "scheduled"
                    elif not is_future and not is_past:
                        # Normally I won't come here, but I'll keep it as a backup.
                        display_status = "scheduled"
                    else:
                        # Today and time has come or in the past
                        if ev["event_date"] == today and et and et <= now_t:
                            display_status = "ongoing"
                        elif is_past:
                            display_status = "completed"
                        else:
                            display_status = "scheduled"

                ev["display_status"] = display_status

            # Add pending volunteer count for each event
            if events:
                try:
                    with db.get_cursor() as volunteer_cursor:
                        for ev in events:
                            try:
                                volunteer_cursor.execute("""
                                    SELECT COUNT(*) as pending_count
                                    FROM event_members em
                                    WHERE em.event_id = %s 
                                      AND em.event_role = 'volunteer' 
                                      AND em.volunteer_status = 'assigned'
                                """, (ev['event_id'],))
                                result = volunteer_cursor.fetchone()
                                ev["pending_volunteer_count"] = result['pending_count'] or 0
                            except Exception as e:
                                print(f"Error getting pending volunteer count for event {ev['event_id']}: {e}")
                                ev["pending_volunteer_count"] = 0
                except Exception as e:
                    print(f"Error creating volunteer cursor: {e}")
                    # Set default values for all events
                    for ev in events:
                        ev["pending_volunteer_count"] = 0

            # Pagination
            from eventbridge_plus.util import create_pagination_info, create_pagination_links
            base_url = url_for('manage_events')
            pagination = create_pagination_info(
                page=page,
                per_page=per_page,
                total=total_events,
                base_url=base_url,
                sort=sort_by,
                event_search=event_search or None,
                group_filter=group_filter or None,
                group_search=group_search or None,
                location_search=location_search or None,
                location_type=location_type or None
            )
            pagination_links = create_pagination_links(pagination)

            return render_template(
                "group_manager/manage_events.html",
                events=events,
                pagination=pagination,
                pagination_links=pagination_links,
                sort_by=sort_by,
                event_search=event_search,
                group_filter=group_filter,
                group_search=group_search,
                location_search=location_search,
                location_type=location_type,
                available_groups=available_groups,
                is_admin=is_admin,
            )

        except Exception as e:
            print(f"Error loading events: {e}")
            import traceback; traceback.print_exc()
            flash("Error loading events.", "error")
            return render_template(
                "group_manager/manage_events.html",
                events=[],
                pagination={"page": 1, "pages": 0, "total": 0},
                sort_by="date_asc",
                event_search="",
                group_filter="",
                group_search="",
                location_search="",
                location_type="all",
                available_groups=[],
                is_admin=is_admin,
            )


    @app.route(
        "/events/<int:event_id>/edit", methods=["GET", "POST"], endpoint="edit_event"
    )
    @require_login
    def edit_event(event_id):
        """Edit event (Admins or Group Managers)"""
        try:
            user_id = get_current_user_id()
            # Use role helpers to avoid session mismatches across branches
            is_admin = is_super_admin() or is_support_technician()

            # Check if user can manage this specific event (admins bypass)
            if (not is_admin) and (not can_user_manage_event(user_id, event_id)):
                flash(
                    "Access denied. Only admins or group managers can edit events.", "error"
                )
                return render_template("access_denied.html"), 403

            with db.get_cursor() as cursor:
                if is_admin:
                    cursor.execute(
                        """
                        SELECT e.*, g.name AS group_name
                        FROM event_info e
                        JOIN group_info g ON e.group_id = g.group_id
                        WHERE e.event_id = %s
                        """,
                        (event_id,),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT e.*, g.name AS group_name
                        FROM event_info e
                        JOIN group_info g ON e.group_id = g.group_id
                        JOIN group_members gm ON g.group_id = gm.group_id
                        WHERE e.event_id = %s 
                          AND gm.user_id = %s 
                          AND gm.group_role = 'manager' 
                          AND gm.status = 'active'
                        """,
                        (event_id, user_id),
                    )
                event = cursor.fetchone()

                if not event:
                    flash("Event not found or access denied.", "error")
                    return redirect(url_for("manage_events"))

                # Convert timedelta to HH:MM format for HTML time input
                if isinstance(event["event_time"], timedelta):
                    total_seconds = int(event["event_time"].total_seconds())
                    hours = total_seconds // 3600
                    minutes = (total_seconds % 3600) // 60
                    event["event_time"] = f"{hours:02d}:{minutes:02d}"

                # Check if event is in the past (cannot edit past events)
                if event["event_date"] < date.today():
                    flash(
                        "Cannot edit past events. Only future events can be modified.",
                        "error",
                    )
                    return redirect(url_for("manage_events"))

                if request.method == "GET":
                    # Get user's groups for dropdown
                    cursor.execute(
                        """
                        SELECT g.group_id, g.name 
                        FROM group_info g
                        JOIN group_members gm ON g.group_id = gm.group_id
                        WHERE gm.user_id = %s 
                          AND gm.group_role = 'manager'
                          AND gm.status = 'active' 
                          AND g.status = 'approved'
                        ORDER BY g.name
                        """,
                        (user_id,),
                    )
                    groups = cursor.fetchall()

                    return render_template(
                        "group_manager/create_event.html",
                        event=event,
                        groups=groups,
                        event_types=AVAILABLE_EVENT_TYPES,
                        locations=AVAILABLE_LOCATIONS,
                        mode="edit",
                    )


                event_title = request.form.get("event_title", "").strip()
                description = request.form.get("description", "").strip()
                event_type = request.form.get("event_type", "").strip()
                event_date = request.form.get("event_date", "").strip()
                event_time = request.form.get("event_time", "").strip()
                location = request.form.get("location", "").strip()
                max_participants = request.form.get("max_participants", "").strip()
                status = request.form.get("status", "").strip()

                errors = []
                if not event_title or len(event_title) < 3:
                    errors.append(
                        "Event title must be at least 3 characters long."
                    )
                if event_type not in AVAILABLE_EVENT_TYPES:
                    errors.append("Please select a valid event type.")
                if location not in AVAILABLE_LOCATIONS:
                    errors.append("Please select a valid location.")
                try:
                    max_participants = int(max_participants)
                    if max_participants < 1:
                        errors.append(
                            "Maximum participants must be at least 1."
                        )
                except ValueError:
                    errors.append(
                        "Please enter a valid number for maximum participants."
                    )
                if status not in ["draft", "scheduled", "cancelled"]:
                    errors.append("Please select a valid status.")

                if errors:
                    for err in errors:
                        flash(err, "error")
                    return redirect(url_for("edit_event", event_id=event_id))


                # Prevent global duplicate event names among active events (exclude cancelled/past and this event)
                cursor.execute(
                    """
                    SELECT 1
                    FROM event_info
                    WHERE LOWER(TRIM(event_title)) = LOWER(TRIM(%s))
                      AND (status IS NULL OR LOWER(status) <> 'cancelled')
                      AND event_date >= CURDATE()
                      AND event_id <> %s
                    LIMIT 1
                    """,
                    (event_title, event_id),
                )
                if cursor.fetchone():
                    flash(
                        "An active event with this title already exists. Please choose a different title.",
                        "error",
                    )
                    return redirect(url_for("edit_event", event_id=event_id))

                # Prevent duplicate event names within the same group on the same date (excluding this event)
                cursor.execute(
                    """
                    SELECT 1
                    FROM event_info
                    WHERE group_id = %s
                      AND event_date = %s
                      AND LOWER(TRIM(event_title)) = LOWER(TRIM(%s))
                      AND event_id <> %s
                    LIMIT 1
                    """,
                    (event["group_id"], event_date, event_title, event_id),
                )
                if cursor.fetchone():
                    flash(
                        "An event with the same title already exists for this group on the selected date.",
                        "error",
                    )
                    return redirect(url_for("edit_event", event_id=event_id))

                # --- Updated event_info (no need_volunteers column) ---
                cursor.execute(
                    """
                    UPDATE event_info 
                    SET event_title=%s, description=%s, event_type=%s,
                        event_date=%s, event_time=%s, location=%s,
                        max_participants=%s, status=%s,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE event_id=%s
                    """,
                    (
                        event_title, description, event_type,
                        event_date, event_time, location,
                        max_participants, status,
                        event_id,
                    ),
                )


             
                try:
                    cursor.connection.commit()
                except Exception:
                    pass

                flash("Event updated successfully!", "success")
                return redirect(url_for("manage_events"))


        except Exception as e:
            print(f"Error editing event: {e}")
            flash("An error occurred while updating the event.", "error")
            return redirect(url_for("manage_events"))

    @app.route("/events/<int:event_id>/delete", methods=["POST", "GET"], endpoint="delete_event")
    @require_login
    def delete_event(event_id):
        """Delete an event.
        - super_admin/support_technician: can delete any event
        - group manager: can delete events for groups they manage
        """
        try:
            user_id = get_current_user_id()
            is_admin = is_super_admin() or is_support_technician()

            if (not is_admin) and (not can_user_manage_event(user_id, event_id)):
                flash("Access denied. Only admins or the group's manager can delete this event.", "error")
                return render_template("access_denied.html"), 403

            with db.get_cursor() as cursor:
                # Best-effort cleanup of related rows first (if present)
                try:
                    cursor.execute("DELETE FROM event_members WHERE event_id = %s", (event_id,))
                except Exception:
                    pass

                try:
                    cursor.execute("DELETE FROM event_info WHERE event_id = %s", (event_id,))
                except Exception as de:
                    flash("Failed to delete event.", "error")
                    return redirect(url_for("manage_events"))

                try:
                    cursor.connection.commit()
                except Exception:
                    pass

            flash("Event deleted successfully.", "success")
            return redirect(url_for("manage_events"))

        except Exception as e:
            print(f"Error deleting event: {e}")
            flash("An error occurred while deleting the event.", "error")
            return redirect(url_for("manage_events"))

    # (compat route may already exist in this branch; avoid redefining)

    # =============================================================================
    # EVENT REGISTRATION ROUTES (Participants)
    # =============================================================================
    @app.route("/events/<int:event_id>/register", endpoint="register_for_event")
    @require_login
    def register_for_event(event_id):
        """Register as participant for event (auto-join group if public)"""
        try:
            user_id = get_current_user_id()

            with db.get_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT 
                        e.event_id, e.group_id, e.event_title, e.max_participants,
                        e.status, e.event_date,
                        g.name AS group_name, g.is_public, g.status AS group_status,
                        g.max_members,
                        COUNT(em.membership_id) AS registered_count
                    FROM event_info e
                    JOIN group_info g ON e.group_id = g.group_id
                    LEFT JOIN event_members em ON e.event_id = em.event_id
                      AND em.participation_status IN ('registered', 'attended')
                      AND (em.event_role != 'volunteer' OR em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled')
                    WHERE e.event_id = %s AND e.status = 'scheduled'
                      AND g.status = 'approved'
                    GROUP BY e.event_id, e.group_id, e.event_title, e.max_participants,
                             e.status, e.event_date, g.name, g.is_public, g.status, g.max_members
                    """,
                    (event_id,),
                )
                event = cursor.fetchone()

                if not event:
                    flash(
                        "Event not found or not available for registration.",
                        "error",
                    )
                    return redirect(url_for("search_events"))

                if event["registered_count"] >= event["max_participants"]:
                    flash("Sorry, this event is full.", "error")
                    return redirect(url_for("event_detail", event_id=event_id))

                cursor.execute(
                    """
                    SELECT 1 
                    FROM event_members 
                    WHERE user_id = %s AND event_id = %s
                    """,
                    (user_id, event_id),
                )
                if cursor.fetchone():
                    flash(
                        "You are already registered for this event.", "info"
                    )
                    return redirect(url_for("event_detail", event_id=event_id))

                # Check if user has reached the maximum event registration limit (7 events)
                cursor.execute(
                    """
                    SELECT COUNT(*) AS registered_events
                    FROM event_members em
                    JOIN event_info e ON em.event_id = e.event_id
                    WHERE em.user_id = %s 
                      AND em.participation_status = 'registered'
                      AND e.status = 'scheduled'
                      AND e.event_date >= CURDATE()
                    """,
                    (user_id,),
                )
                event_count = cursor.fetchone()["registered_events"]

                if event_count >= 7:
                    flash(
                        "Registration limit reached! You can only register for up to 7 upcoming events at a time. Please cancel some existing registrations to register for new events.",
                        "error",
                    )
                    return redirect(url_for("event_detail", event_id=event_id))

                # Check if user already has an event on the same date (one event per day limit)
                cursor.execute('''
                    SELECT 1 
                    FROM event_members em
                    JOIN event_info e ON em.event_id = e.event_id
                    WHERE em.user_id = %s 
                      AND e.event_date = %s
                      AND em.participation_status IN ('registered', 'attended')
                ''', (user_id, event['event_date']))
                
                if cursor.fetchone():
                    flash(
                        f'You can only register for one event per day. You already have an event on {nz_date(event["event_date"])}.',
                        'error'
                    )
                    return redirect(url_for('event_detail', event_id=event_id))

                # Check if user is already a group member
                cursor.execute(
                    """
                    SELECT status
                    FROM group_members 
                    WHERE user_id = %s AND group_id = %s
                    """,
                    (user_id, event["group_id"]),
                )
                group_membership = cursor.fetchone()

                if not group_membership and event["is_public"]:
                    # Check if user has reached the maximum limit of 10 groups
                    cursor.execute("""
                        SELECT COUNT(*) AS group_count
                        FROM group_members
                        WHERE user_id = %s AND status = 'active'
                    """, (user_id,))
                    user_group_count = cursor.fetchone()['group_count']
                    
                    if user_group_count >= 10:
                        flash(
                            "Cannot register for this event: You have reached the maximum limit of 10 groups. Please leave a group before registering for events from new groups.",
                            "error",
                        )
                        return redirect(url_for("event_detail", event_id=event_id))
                    
                    cursor.execute(
                        """
                        SELECT COUNT(*) AS current_members
                        FROM group_members
                        WHERE group_id = %s AND status = 'active'
                        """,
                        (event["group_id"],),
                    )
                    count_result = cursor.fetchone()
                    current_members = (
                        count_result["current_members"] if count_result else 0
                    )

                    if current_members >= event["max_members"]:
                        flash(
                            "Cannot join: The group has reached maximum capacity.",
                            "error",
                        )
                        return redirect(url_for("event_detail", event_id=event_id))

                    cursor.execute(
                        """
                        INSERT INTO group_members (user_id, group_id, group_role, status)
                        VALUES (%s, %s, 'member', 'active')
                        """,
                        (user_id, event["group_id"]),
                    )

                    noti.create_noti(
                        user_id=user_id,
                        title="Automatically Joined Group",
                        message=(
                            f'You have been automatically added to "{event["group_name"]}" '
                            f'when registering for the event "{event["event_title"]}".'
                        ),
                        category="group",
                        related_id=event["group_id"],
                    )

                can_register = True  # Default to registration allowed

                if not group_membership:
                    if event["is_public"]:
                        # Public group: auto-join then register (existing logic)
                        pass  # Already handled above
                    else:
                        # Private group: redirect to group join request
                        # Store the event ID in session for auto-registration after group approval
                        session["pending_event_registration"] = {
                            "event_id": event_id,
                            "group_id": event["group_id"],
                            "event_title": event["event_title"],
                        }
                        flash(
                            "You need to join this group first to register for events.",
                            "info",
                        )
                        return redirect(
                            url_for("group_detail", group_id=event["group_id"])
                        )
                else:
                    # Already a group member: registration allowed
                    if group_membership["status"] != "active":
                        flash(
                            "Your group membership is not active. Please contact the group manager.",
                            "error",
                        )
                        return redirect(url_for("event_detail", event_id=event_id))

                # Check if user is a group volunteer
                cursor.execute(
                    """
                    SELECT group_role
                    FROM group_members 
                    WHERE user_id = %s AND group_id = %s AND status = 'active'
                    """,
                    (user_id, event["group_id"]),
                )
                user_group_role = cursor.fetchone()
                
                # Determine event role based on group role
                if user_group_role and user_group_role["group_role"] == "volunteer":
                    event_role = "volunteer"
                    volunteer_status = "confirmed"
                else:
                    event_role = "participant"
                    volunteer_status = None

                # Insert event registration with appropriate role
                if volunteer_status:
                    cursor.execute(
                        """
                        INSERT INTO event_members (event_id, user_id, event_role, participation_status, volunteer_status)
                        VALUES (%s, %s, %s, 'registered', %s)
                        """,
                        (event_id, user_id, event_role, volunteer_status),
                    )
                else:
                    cursor.execute(
                    """
                    INSERT INTO event_members (event_id, user_id, event_role, participation_status)
                        VALUES (%s, %s, %s, 'registered')
                        """,
                        (event_id, user_id, event_role),
                    )

                # Send appropriate notification based on role
                if event_role == "volunteer":
                    noti.create_noti(
                        user_id=user_id,
                        title="Volunteer Registration Confirmed",
                        message=(
                            f'You have been automatically registered as a volunteer for "{event["event_title"]}" '
                            f'on {nz_date(event["event_date"])}. Thank you for volunteering!'
                        ),
                        category="event",
                        related_id=event_id,
                    )
                    flash(
                        f'Successfully registered as volunteer for "{event["event_title"]}"!',
                        "success",
                    )
                else:
                    noti.create_noti(
                    user_id=user_id,
                    title="Event Registration Confirmed",
                    message=(
                        f'You have successfully registered for "{event["event_title"]}" '
                        f'on {nz_date(event["event_date"])}. Don\'t forget to attend!'
                    ),
                    category="event",
                    related_id=event_id,
                )
                flash(
                    f'Successfully registered for "{event["event_title"]}"!',
                    "success",
                )
                return redirect(url_for("event_detail", event_id=event_id))

        except Exception as e:
            print(f"Error registering for event: {e}")
            flash("An error occurred during registration.", "error")
            return redirect(url_for("search_events"))

    @app.route(
        "/events/<int:event_id>/unregister", endpoint="unregister_from_event"
    )
    @require_platform_role("participant")
    def unregister_from_event(event_id):
        """Unregister from event"""
        try:
            user_id = get_current_user_id()

            with db.get_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT e.event_title, em.participation_status
                    FROM event_members em
                    JOIN event_info e ON em.event_id = e.event_id
                    WHERE em.user_id = %s AND em.event_id = %s
                    """,
                    (user_id, event_id),
                )
                registration = cursor.fetchone()

                if not registration:
                    flash("You are not registered for this event.", "error")
                    return redirect(url_for("event_detail", event_id=event_id))

                if registration["participation_status"] == "attended":
                    flash(
                        "Cannot unregister from an event you have already attended.",
                        "error",
                    )
                    return redirect(url_for("event_detail", event_id=event_id))

                cursor.execute(
                    """
                    DELETE FROM event_members 
                    WHERE user_id = %s AND event_id = %s
                    """,
                    (user_id, event_id),
                )

                noti.create_noti(
                    user_id=user_id,
                    title="Event Registration Cancelled",
                    message=(
                        f'You have cancelled your registration for "{registration["event_title"]}". '
                        f"You can register again if you change your mind."
                    ),
                    category="event",
                    related_id=event_id,
                )

                flash(
                    f'Successfully unregistered from "{registration["event_title"]}".',
                    "success",
                )
                return redirect(url_for("event_detail", event_id=event_id))

        except Exception as e:
            print(f"Error unregistering from event: {e}")
            flash("An error occurred during unregistration.", "error")
            return redirect(url_for("search_events"))

    # =============================================================================
    # VOLUNTEER ROUTES (Participants)
    # =============================================================================
    @app.route("/events/<int:event_id>/volunteer", endpoint="volunteer_for_event")
    @require_login
    def volunteer_for_event(event_id):
        """Apply to become event volunteer (requires manager approval)"""
        try:
            user_id = get_current_user_id()

            with db.get_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT 
                        e.event_id, e.group_id, e.event_title, e.event_date, e.event_time,
                        g.name AS group_name, g.is_public, g.status AS group_status
                    FROM event_info e
                    JOIN group_info g ON e.group_id = g.group_id
                    WHERE e.event_id = %s AND e.status = 'scheduled'
                      AND g.status = 'approved'
                    """,
                    (event_id,),
                )
                event = cursor.fetchone()

                if not event:
                    flash(
                        "Event not found or unavailable for volunteer application.",
                        "error",
                    )
                    return redirect(url_for("search_events"))

                # Check if user is a group volunteer (for auto-approval)
                cursor.execute(
                    """
                    SELECT group_role
                    FROM group_members 
                    WHERE user_id = %s AND group_id = %s AND status = 'active'
                    """,
                    (user_id, event["group_id"]),
                )
                group_role_result = cursor.fetchone()
                is_group_volunteer = group_role_result and group_role_result["group_role"] == "volunteer"

                cursor.execute(
                    """
                    SELECT event_role, volunteer_status
                    FROM event_members 
                    WHERE user_id = %s AND event_id = %s
                    """,
                    (user_id, event_id),
                )
                existing = cursor.fetchone()

                if existing:
                    if existing["event_role"] == "volunteer":
                        status = existing.get("volunteer_status", "pending")
                        if status == "cancelled":
                            # Allow reapplication for cancelled/rejected volunteers
                            cursor.execute(
                                """
                                UPDATE event_members
                                SET volunteer_status = 'assigned'
                                WHERE user_id = %s AND event_id = %s AND event_role = 'volunteer'
                                """,
                                (user_id, event_id),
                            )
                            flash("Volunteer application resubmitted! Please wait for manager approval.", "success")
                        else:
                            flash(
                                f"You have already applied as volunteer (status: {status}).",
                                "info",
                            )
                        return redirect(url_for("event_detail", event_id=event_id))
                    else:
                        flash(
                            "You are already registered as participant for this event.",
                            "info",
                        )
                        return redirect(url_for("event_detail", event_id=event_id))

                # Check if user has reached the maximum event registration limit (7 events)
                cursor.execute(
                    """
                    SELECT COUNT(*) AS registered_events
                    FROM event_members em
                    JOIN event_info e ON em.event_id = e.event_id
                    WHERE em.user_id = %s 
                      AND em.participation_status = 'registered'
                      AND e.status = 'scheduled'
                      AND e.event_date >= CURDATE()
                    """,
                    (user_id,),
                )
                event_count = cursor.fetchone()["registered_events"]

                if event_count >= 7:
                    flash(
                        "Registration limit reached! You can only register for up to 7 upcoming events at a time (including volunteer applications). Please cancel some existing registrations to apply for new events.",
                        "error",
                    )
                    return redirect(url_for("event_detail", event_id=event_id))

                # Check if user already has an event on the same date (one event per day limit)
                cursor.execute('''
                    SELECT 1 
                    FROM event_members em
                    JOIN event_info e ON em.event_id = e.event_id
                    WHERE em.user_id = %s 
                      AND e.event_date = %s
                      AND em.participation_status IN ('registered', 'attended')
                ''', (user_id, event['event_date']))
                
                if cursor.fetchone():
                    flash(
                        f'You can only participate in one event per day. You already have an event on {nz_date(event["event_date"])}.',
                        'error'
                    )
                    return redirect(url_for('event_detail', event_id=event_id))

                conflict_check = check_time_conflicts(
                    user_id=user_id,
                    event_date=event["event_date"],
                    event_time=event["event_time"],
                    exclude_event_id=None,
                )

                if conflict_check["has_conflict"]:
                    conflicting = conflict_check["conflicting_events"][0]
                    flash(
                        f'Time conflict: You are already registered for "{conflicting["event_title"]}" '
                        f'at {conflicting["event_date"]} {conflicting["event_time"]}.',
                        "warning",
                    )
                    return redirect(url_for("event_detail", event_id=event_id))

                # Check if user is a member of the group and their role
                cursor.execute(
                    """
                    SELECT group_role 
                    FROM group_members 
                    WHERE user_id = %s AND group_id = %s AND status = 'active'
                    """,
                    (user_id, event["group_id"]),
                )
                group_member = cursor.fetchone()

                if not group_member:
                    flash(
                        "You must be a group member to apply as volunteer.", "error"
                    )
                    return redirect(url_for("group_detail", group_id=event["group_id"]))
                # Determine if this group member has volunteer role (auto-approved)
                is_group_volunteer = bool(group_member and group_member.get('group_role') == 'volunteer')

                # Prepare status, flash message and notification based on role
                if is_group_volunteer:
                    volunteer_status = 'confirmed'
                    flash_message = f'Successfully joined as volunteer for "{event["event_title"]}"!'
                    notification_title = "Volunteer Registration Confirmed"
                    notification_message = (
                        f'You have been automatically registered as a volunteer for "{event["event_title"]}" '
                        f'on {nz_date(event["event_date"])}. Thank you for volunteering!'
                    )
                else:
                    volunteer_status = 'assigned'
                    flash_message = f'Volunteer application submitted for "{event["event_title"]}". Please wait for manager approval.'
                    notification_title = "Volunteer Application Submitted"
                    notification_message = (
                        f'Your volunteer application for "{event["event_title"]}" has been submitted. '
                        f"Please wait for group manager approval."
                    )
                cursor.execute(
                    """
                    INSERT INTO event_members (
                        event_id, user_id, event_role, 
                        participation_status, volunteer_status
                    )
                    VALUES (%s, %s, 'volunteer', 'registered', %s)
                    """,
                    (event_id, user_id, volunteer_status),
                )

                # Notify applicant
                try:
                    noti.create_noti(
                    user_id=user_id,
                        title=notification_title,
                        message=notification_message,
                    category="volunteer",
                    related_id=event_id,
                )
                except Exception:
                    pass

                flash(flash_message, "success")

                # Only notify managers if it's not a group volunteer (requires approval)
                if not is_group_volunteer:
                    cursor.execute(
                    """
                    SELECT DISTINCT gm.user_id
                    FROM group_members gm
                    WHERE gm.group_id = %s 
                      AND gm.group_role = 'manager' 
                      AND gm.status = 'active'
                    """,
                    (event["group_id"],),
                )
                managers = cursor.fetchall()

                for manager in managers:
                    noti.create_noti(
                        user_id=manager["user_id"],
                        title="New Volunteer Application",
                        message=(
                            f'New volunteer application for "{event["event_title"]}" '
                            f"awaiting your review."
                        ),
                        category="volunteer",
                        related_id=event_id,
                    )
                # flash already handled above
                return redirect(url_for("event_detail", event_id=event_id))

        except Exception as e:
            print(f"Error in volunteer application: {e}")
            flash("An error occurred while submitting volunteer application.", "error")
            return redirect(url_for("search_events"))

    # =============================================================================
    # UTILITY FUNCTIONS
    # =============================================================================
    def get_user_events(user_id, include_past=False):
        """Get events for a specific user (both participation and volunteering)"""
        try:
            with db.get_cursor() as cursor:
                date_filter = "AND e.event_date >= CURDATE()" if not include_past else ""
                cursor.execute(
                    f"""
                    SELECT 
                        e.event_id, e.event_title, e.event_type, e.event_date,
                        e.event_time, e.location, e.status AS event_status,
                        g.name AS group_name,
                        em.event_role, em.participation_status, em.volunteer_status,
                        em.volunteer_hours, em.responsibility
                    FROM event_members em
                    JOIN event_info e ON em.event_id = e.event_id
                    JOIN group_info g ON e.group_id = g.group_id
                    WHERE em.user_id = %s {date_filter}
                    ORDER BY e.event_date DESC
                    """,
                    (user_id,),
                )
                return cursor.fetchall()
        except Exception as e:
            print(f"Error getting user events: {e}")
            return []

    def get_group_events(group_id, limit=None):
        """Get events for a specific group"""
        try:
            with db.get_cursor() as cursor:
                limit_clause = f"LIMIT {limit}" if limit else ""
                cursor.execute(
                    f"""
                    SELECT 
                        e.event_id, e.event_title, e.event_type, e.event_date,
                        e.event_time, e.location, e.max_participants, e.status,
                        COUNT(em.membership_id) AS registered_count
                    FROM event_info e
                    LEFT JOIN event_members em ON e.event_id = em.event_id
                      AND em.participation_status IN ('registered', 'attended')
                      AND (em.event_role != 'volunteer' OR em.volunteer_status IS NULL OR em.volunteer_status != 'cancelled')
                    WHERE e.group_id = %s
                    GROUP BY e.event_id, e.group_id, e.event_title, e.event_type, e.event_date,
                             e.event_time, e.location, e.max_participants, e.status
                    ORDER BY e.event_date DESC
                    {limit_clause}
                    """,
                    (group_id,),
                )
                events = cursor.fetchall()

                for ev in events:
                    ev["spots_available"] = (
                        ev["max_participants"] - ev["registered_count"]
                    )
                    ev["is_full"] = ev["spots_available"] <= 0
                    ev["is_upcoming"] = ev["event_date"] >= date.today()

                return events
        except Exception as e:
            print(f"Error getting group events: {e}")
            return []

    @app.route("/events/<int:event_id>/participants/remove", methods=['POST'], endpoint='remove_event_participant')
    @require_login
    def remove_event_participant(event_id):
        """Remove a participant from an event (Manager/Admin only)"""
        # Check if user can manage this event
        user_id = get_current_user_id()
        is_admin = session.get("platform_role") in ("super_admin", "support_technician")
        is_group_mgr = session.get('group_role') == 'manager'
        
        if not (is_admin or is_group_mgr):
            flash('Access denied. Only admins or group managers can remove participants.', 'error')
            return render_template('access_denied.html'), 403
            
        if (not (is_super_admin() or is_support_technician())) and (not can_user_manage_event(user_id, event_id)):
            flash('You do not have permission to manage this event.', 'error')
            return redirect(url_for('manage_events'))
        
        try:
            membership_id = request.form.get('membership_id')
            if not membership_id:
                flash('Invalid request.', 'error')
                return redirect(url_for('event_detail', event_id=event_id))
            
            with db.get_cursor() as cursor:
                # Get participant info for confirmation
                cursor.execute("""
                    SELECT em.user_id, em.event_role, u.username, u.first_name, u.last_name, e.event_title
                    FROM event_members em
                    JOIN users u ON em.user_id = u.user_id
                    JOIN event_info e ON em.event_id = e.event_id
                    WHERE em.membership_id = %s AND em.event_id = %s
                """, (membership_id, event_id))
                
                participant = cursor.fetchone()
                if not participant:
                    flash('Participant not found.', 'error')
                    return redirect(url_for('event_detail', event_id=event_id))
                
                # Remove the participant
                cursor.execute("""
                    DELETE FROM event_members 
                    WHERE membership_id = %s AND event_id = %s
                """, (membership_id, event_id))
                
                # Send notification to the removed participant
                noti.create_noti(
                    user_id=participant['user_id'],
                    title="Removed from Event",
                    message=f'You have been removed from "{participant["event_title"]}" by the event manager.',
                    category="event",
                    related_id=event_id,
                )
                
                flash(f'Successfully removed {participant["first_name"]} {participant["last_name"]} from the event.', 'success')
                return redirect(url_for('event_detail', event_id=event_id))
                
        except Exception as e:
            print(f"Error removing participant: {e}")
            flash('An error occurred while removing the participant.', 'error')
            return redirect(url_for('event_detail', event_id=event_id))

    @app.route("/debug/session")
    @require_login
    def debug_session():
        """Debug route to check session values"""
        return f"""
        <h2>Session Debug Info</h2>
        <ul>
            <li><strong>user_id:</strong> {session.get('user_id')}</li>
            <li><strong>username:</strong> {session.get('username')}</li>
            <li><strong>platform_role:</strong> {session.get('platform_role')}</li>
            <li><strong>group_role:</strong> {session.get('group_role')}</li>
            <li><strong>group_id:</strong> {session.get('group_id')}</li>
        </ul>
        <a href="/">Back to Home</a>
        """

    @app.route('/add-event-member', methods=['POST'])
    @require_login
    def add_event_member():
        """Add a new member to an event"""
        user_id = get_current_user_id()
        event_id = request.form.get('event_id', type=int)
        member_user_id = request.form.get('user_id', type=int)
        member_role = request.form.get('member_role', '').strip()
        
        if not event_id or not member_user_id or not member_role:
            flash('Missing required information.', 'error')
            return redirect(url_for('event_detail', event_id=event_id))
        
        # Check permissions - only group managers and admins can add members
        if not (is_super_admin() or is_support_technician() or is_group_manager()):
            flash('Access denied.', 'error')
            return redirect(url_for('event_detail', event_id=event_id))
        
        try:
            with db.get_cursor() as cur:
                # Check if event exists and get info
                cur.execute("""
                    SELECT e.event_title, e.max_participants, e.status, g.name as group_name, g.group_id
                    FROM event_info e
                    JOIN group_info g ON e.group_id = g.group_id
                    WHERE e.event_id = %s
                """, (event_id,))
                event_info = cur.fetchone()
                
                if not event_info:
                    flash('Event not found.', 'error')
                    return redirect(url_for('event_detail', event_id=event_id))
                
                # Check if user is group manager for this event's group
                if not is_super_admin() and not is_support_technician():
                    cur.execute("""
                        SELECT gm.group_role
                        FROM group_members gm
                        WHERE gm.user_id = %s AND gm.group_id = %s AND gm.status = 'active'
                    """, (user_id, event_info['group_id']))
                    group_membership = cur.fetchone()
                    
                    if not group_membership or group_membership['group_role'] != 'manager':
                        flash('You can only add members to events in groups you manage.', 'error')
                        return redirect(url_for('event_detail', event_id=event_id))
                
                # Check if user exists and is active
                cur.execute("""
                    SELECT username, first_name, last_name, status
                    FROM users
                    WHERE user_id = %s
                """, (member_user_id,))
                user_info = cur.fetchone()
                
                if not user_info:
                    flash('User not found.', 'error')
                    return redirect(url_for('event_detail', event_id=event_id))
                
                if user_info['status'] != 'active':
                    flash('Cannot add inactive users to events.', 'error')
                    return redirect(url_for('event_detail', event_id=event_id))
                
                # Check user's event registration limit (7 events max)
                # Only count 'registered' status (not 'attended' past events)
                cur.execute("""
                    SELECT COUNT(*) as count
                    FROM event_members
                    WHERE user_id = %s AND participation_status = 'registered'
                """, (member_user_id,))
                user_event_count = cur.fetchone()['count']
                
                if user_event_count >= 7:
                    flash(f'{user_info["first_name"]} {user_info["last_name"]} can only register for up to 7 events.', 'error')
                    return redirect(url_for('event_detail', event_id=event_id))
                
                # Check user's group membership limit (10 groups max)
                cur.execute("""
                    SELECT COUNT(*) as count
                    FROM group_members
                    WHERE user_id = %s AND status = 'active'
                """, (member_user_id,))
                user_group_count = cur.fetchone()['count']
                
                if user_group_count >= 10:
                    flash(f'{user_info["first_name"]} {user_info["last_name"]} can only join up to 10 groups.', 'error')
                    return redirect(url_for('event_detail', event_id=event_id))
                
                # Check if user is already registered for this event
                cur.execute("""
                    SELECT participation_status
                    FROM event_members
                    WHERE user_id = %s AND event_id = %s
                """, (member_user_id, event_id))
                existing_registration = cur.fetchone()
                
                if existing_registration:
                    if existing_registration['participation_status'] in ('registered', 'attended'):
                        flash(f'{user_info["first_name"]} {user_info["last_name"]} is already registered for this event.', 'warning')
                    else:
                        # Reactivate the registration
                        cur.execute("""
                            UPDATE event_members 
                            SET participation_status = 'registered', registration_date = NOW()
                            WHERE user_id = %s AND event_id = %s
                        """, (member_user_id, event_id))
                        flash(f'Successfully re-added {user_info["first_name"]} {user_info["last_name"]} to the event.', 'success')
                    return redirect(url_for('event_detail', event_id=event_id))
                
                # Check capacity
                cur.execute("""
                    SELECT COUNT(*) as count
                    FROM event_members
                    WHERE event_id = %s AND participation_status IN ('registered', 'attended')
                """, (event_id,))
                current_count = cur.fetchone()['count']
                
                if current_count >= event_info['max_participants']:
                    flash('Event is at maximum capacity.', 'error')
                    return redirect(url_for('event_detail', event_id=event_id))
                
                # Check if user is already a member of the event's group
                cur.execute("""
                    SELECT status
                    FROM group_members
                    WHERE user_id = %s AND group_id = %s
                """, (member_user_id, event_info['group_id']))
                group_membership = cur.fetchone()
                
                # If not a group member, add them to the group first
                if not group_membership:
                    # Check group capacity before adding
                    cur.execute("""
                        SELECT COUNT(*) as count
                        FROM group_members
                        WHERE group_id = %s AND status = 'active'
                    """, (event_info['group_id'],))
                    group_current_count = cur.fetchone()['count']
                    
                    cur.execute("""
                        SELECT max_members
                        FROM group_info
                        WHERE group_id = %s
                    """, (event_info['group_id'],))
                    group_max_members = cur.fetchone()['max_members']
                    
                    if group_max_members and group_current_count >= group_max_members:
                        flash('Cannot add member: Group is at maximum capacity.', 'error')
                        return redirect(url_for('event_detail', event_id=event_id))
                    
                    # Add user to the group
                    cur.execute("""
                        INSERT INTO group_members (user_id, group_id, group_role, status, join_date)
                        VALUES (%s, %s, 'member', 'active', NOW())
                    """, (member_user_id, event_info['group_id']))
                    
                    # Create notification for group membership
                    noti.create_noti(
                        user_id=member_user_id,
                        title="Added to Group",
                        message=f'You have been automatically added to the group "{event_info["group_name"]}" as you were added to their event.',
                        category="group",
                        related_id=event_info['group_id'],
                    )
                
                # Add the member to the event
                cur.execute("""
                    INSERT INTO event_members (user_id, event_id, event_role, participation_status, registration_date)
                    VALUES (%s, %s, %s, 'registered', NOW())
                """, (member_user_id, event_id, member_role))
                
                # Create notification for the added user
                noti.create_noti(
                    user_id=member_user_id,
                    title="Added to Event",
                    message=f'You have been added to "{event_info["event_title"]}" by the event manager.',
                    category="event",
                    related_id=event_id,
                )
                
                flash(f'Successfully added {user_info["first_name"]} {user_info["last_name"]} to the event.', 'success')
                
        except Exception as e:
            print(f"Error adding event member: {e}")
            flash('An error occurred while adding the member.', 'error')
        
        return redirect(url_for('event_detail', event_id=event_id))