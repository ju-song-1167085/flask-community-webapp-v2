from datetime import date, timedelta
from flask import request, render_template, redirect, url_for, session, flash
from eventbridge_plus import db, app
from flask import session


@app.get("/search/explore", endpoint="explore")
def explore():
    """Unified Explore page: shows Events or Groups based on the 'tab' parameter."""
    
    # Get search parameters from URL
    tab = request.args.get("tab", "events").strip()
    q = request.args.get("q", "").strip()
    gtype = request.args.get("type", "").strip()
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    location = request.args.get("location", "").strip()
    event_type = request.args.get("event_type", "").strip()
    privacy_type = request.args.get("privacy_type", "").strip()
    sort = request.args.get("sort", "").strip()

    # Initialize results
    results = {"events": [], "groups": []}
    event_locations = []
    group_locations = []
    user_id = session.get("user_id")
    user_role = session.get("platform_role")
    group_role = session.get("group_role")
    today_str = date.today().isoformat()

    # Pagination params (used for Events tab pagination)
    from eventbridge_plus.util import get_pagination_params, create_pagination_info, create_pagination_links
    page, per_page = get_pagination_params(request, default_per_page=9)
    

    try:
        with db.get_cursor() as cursor:
            
            # Get event locations (for Events tab)
            cursor.execute("""
                SELECT DISTINCT TRIM(e.location) AS location
                FROM event_info e
                JOIN group_info g2 ON g2.group_id = e.group_id
                WHERE g2.status = 'approved'
                  AND e.status = 'scheduled'
                  AND TIMESTAMP(e.event_date, COALESCE(e.event_time,'23:59:59')) >= NOW()
                  AND e.location IS NOT NULL AND e.location <> ''
                ORDER BY location ASC
                LIMIT 200
            """)
            event_locations = [r["location"] for r in cursor.fetchall()]

            # Get group locations (for Groups tab)
            cursor.execute("""
                SELECT DISTINCT TRIM(g.group_location) AS location
                FROM group_info g
                WHERE g.status = 'approved'
                  AND g.group_location IS NOT NULL
                  AND g.group_location <> ''
                ORDER BY location ASC
                LIMIT 200
            """)
            group_locations = [r["location"] for r in cursor.fetchall()]

            # Search for EVENTS
            if tab == "events":
                where_conditions = [
                    "g.status = 'approved'",
                    "e.status = 'scheduled'",
                    "TIMESTAMP(e.event_date, COALESCE(e.event_time, '23:59:59')) >= NOW()"
                ]
                params = []

                # Privacy filtering based on user role
                if user_role == 'super_admin':
                    # Super Admin can see all groups and filter by privacy_type
                    if privacy_type == 'public':
                        where_conditions.append("g.is_public = 1")
                    elif privacy_type == 'private':
                        where_conditions.append("g.is_public = 0")
                    # If privacy_type is empty or 'all', don't add filter (show all)
                elif group_role == 'manager':
                    # Group managers can see all groups and filter by privacy_type
                    if privacy_type == 'public':
                        where_conditions.append("g.is_public = 1")
                    elif privacy_type == 'private':
                        where_conditions.append("g.is_public = 0")
                    # If privacy_type is empty or 'all', don't add filter (show all)
                elif user_id:
                    # Logged in users: show public groups + private groups they're members of
                    where_conditions.append("""
                        (g.is_public = 1 OR EXISTS (
                            SELECT 1 FROM group_members gm 
                            WHERE gm.group_id = g.group_id 
                            AND gm.user_id = %s 
                            AND gm.status = 'active'
                        ))
                    """)
                    params.append(user_id)
                else:
                    # Visitors: only show public groups
                    where_conditions.append("g.is_public = 1")

                # Search filter: ONLY search event_title
                if q:
                    where_conditions.append("LOWER(e.event_title) LIKE LOWER(%s)")
                    params.append(f"%{q}%")

                # Normalize/guard start date: do not allow past dates; clamp to today
                if date_from and date_from < today_str:
                    date_from = today_str

                # Filter by date or date range
                if date_from and date_to:
                    # Ensure end >= start
                    if date_to < date_from:
                        # Swap to be safe; also surface feedback
                        date_from, date_to = date_to, date_from
                        flash("End date was before start date. Dates were adjusted.", "warning")
                    where_conditions.append("DATE(e.event_date) BETWEEN %s AND %s")
                    params.extend([date_from, date_to])
                elif date_from:
                    where_conditions.append("DATE(e.event_date) >= %s")
                    params.append(date_from)
                elif date_to:
                    where_conditions.append("DATE(e.event_date) <= %s")
                    params.append(date_to)

                # Filter by location
                if location:
                    where_conditions.append("LOWER(TRIM(e.location)) LIKE LOWER(%s)")
                    location_pattern = f"%{location}%"
                    params.append(location_pattern)

                # Filter by event type
                if event_type:
                    where_conditions.append("e.event_type = %s")
                    params.append(event_type)

                # Build query
                sql = f"""
                    SELECT
                        e.event_id, e.event_title, e.event_date, e.event_time, e.location,
                        e.event_type, e.max_participants,
                        g.group_id, g.name AS group_name, g.is_public,
                        COUNT(DISTINCT CASE
                            WHEN em2.participation_status IN ('registered','attended') THEN em2.user_id
                        END) AS registered_count
                    FROM event_info e
                    JOIN group_info g ON g.group_id = e.group_id
                    LEFT JOIN event_members em2 ON em2.event_id = e.event_id
                    WHERE {" AND ".join(where_conditions)}
                    GROUP BY e.event_id, e.event_title, e.event_date, e.event_time, e.location,
                            e.event_type, e.max_participants, g.group_id, g.name, g.is_public
                """

                cursor.execute(sql, tuple(params))
                events_list = cursor.fetchall()

                # Sort results
                if q and not sort:
                    # Sort by relevance: how well event_title matches search term
                    search_lower = q.lower()
                    def calc_relevance(event):
                        title = event.get('event_title', '').lower()
                        
                        if title == search_lower:
                            return 1
                        elif title.startswith(search_lower):
                            return 2
                        elif f' {search_lower}' in title:
                            return 3
                        elif search_lower in title:
                            return 4
                        else:
                            return 999
                    
                    events_list = sorted(events_list, key=lambda e: (calc_relevance(e), e.get('event_date', '')))
                elif sort == "date_desc":
                    events_list = sorted(events_list, key=lambda e: e.get('event_date', ''), reverse=True)
                elif sort == "popularity":
                    events_list = sorted(events_list, key=lambda e: e.get('registered_count', 0), reverse=True)
                elif sort == "name":
                    events_list = sorted(events_list, key=lambda e: e.get('event_title', '').lower())
                else:
                    # Default: sort by date ascending
                    events_list = sorted(events_list, key=lambda e: e.get('event_date', ''))

                # Calculate spots available for each event
                for event in events_list:
                    registered = event.get("registered_count") or 0
                    max_spots = event.get("max_participants")
                    
                    if max_spots is None:
                        event["spots_available"] = None
                        event["is_unlimited"] = True
                        event["is_full"] = False
                    else:
                        event["spots_available"] = max_spots - registered
                        event["is_unlimited"] = False
                        event["is_full"] = event["spots_available"] <= 0

                # Apply pagination for Events tab
                total_events = len(events_list)
                start_idx = (page - 1) * per_page
                end_idx = start_idx + per_page
                paged_events = events_list[start_idx:end_idx]

                # Build pagination info/links using current filters
                base_url = url_for('explore')
                events_pagination = create_pagination_info(
                    page=page,
                    per_page=per_page,
                    total=total_events,
                    base_url=base_url,
                    tab=tab,
                    q=q or None,
                    type=gtype or None,
                    **({"from": date_from} if date_from else {}),
                    **({"to": date_to} if date_to else {}),
                    location=location or None,
                    event_type=event_type or None,
                    privacy_type=privacy_type or None,
                    sort=sort or None
                )
                events_pagination_links = create_pagination_links(events_pagination)

                results["events"] = paged_events
                results["events_pagination"] = events_pagination
                results["events_pagination_links"] = events_pagination_links

            # Search for GROUPS
            if tab == "groups":
                where_conditions = ["g.status = 'approved'"]
                params = []

                # Privacy filtering based on user role
                if user_role == 'super_admin':
                    # Super Admin can see all groups and filter by privacy_type
                    if privacy_type == 'public':
                        where_conditions.append("g.is_public = 1")
                    elif privacy_type == 'private':
                        where_conditions.append("g.is_public = 0")
                    # If privacy_type is empty or 'all', don't add filter (show all)
                elif group_role == 'manager':
                    # Group managers can see all groups and filter by privacy_type
                    if privacy_type == 'public':
                        where_conditions.append("g.is_public = 1")
                    elif privacy_type == 'private':
                        where_conditions.append("g.is_public = 0")
                    # If privacy_type is empty or 'all', don't add filter (show all)
                elif user_id:
                    # Logged in users: show all groups (public + private) by default
                    # But allow privacy_type filtering
                    if privacy_type == 'public':
                        where_conditions.append("g.is_public = 1")
                    elif privacy_type == 'private':
                        where_conditions.append("g.is_public = 0")
                    # If privacy_type is empty or 'all', don't add filter (show all)
                else:
                    # Visitors: only show public groups
                    where_conditions.append("g.is_public = 1")

                # Search filter: ONLY search group name
                if q:
                    where_conditions.append("LOWER(g.name) LIKE LOWER(%s)")
                    params.append(f"%{q}%")

                # Filter by group type
                if gtype:
                    where_conditions.append("g.group_type = %s")
                    params.append(gtype)

                # Filter by location
                if location:
                    where_conditions.append("LOWER(TRIM(g.group_location)) LIKE LOWER(%s)")
                    params.append(f"%{location}%")

                # Build query
                sql = f"""
                    SELECT
                        g.group_id, g.name, g.description, g.group_type, g.is_public,
                        g.group_location,
                        COUNT(DISTINCT CASE WHEN gm2.status='active' THEN gm2.user_id END) AS member_count,
                        COUNT(DISTINCT CASE 
                            WHEN TIMESTAMP(e.event_date, COALESCE(e.event_time,'23:59:59'))
                                BETWEEN DATE_SUB(NOW(), INTERVAL 30 DAY) AND NOW()
                            THEN e.event_id END
                        ) AS events_30d,
                        COUNT(DISTINCT CASE 
                            WHEN em.event_id IS NOT NULL 
                            AND em.participation_status IN ('registered','attended')
                            AND TIMESTAMP(e.event_date, COALESCE(e.event_time,'23:59:59'))
                                BETWEEN DATE_SUB(NOW(), INTERVAL 30 DAY) AND NOW()
                            THEN em.user_id END
                        ) AS participants_30d,
                        (
                            COUNT(DISTINCT CASE WHEN gm2.status='active' THEN gm2.user_id END)
                            + COUNT(DISTINCT CASE 
                                WHEN TIMESTAMP(e.event_date, COALESCE(e.event_time,'23:59:59'))
                                    BETWEEN DATE_SUB(NOW(), INTERVAL 30 DAY) AND NOW()
                                THEN e.event_id END)
                            + COUNT(DISTINCT CASE 
                                WHEN em.event_id IS NOT NULL 
                                AND em.participation_status IN ('registered','attended')
                                AND TIMESTAMP(e.event_date, COALESCE(e.event_time,'23:59:59'))
                                    BETWEEN DATE_SUB(NOW(), INTERVAL 30 DAY) AND NOW()
                                THEN em.user_id END)
                        ) AS popularity
                    FROM group_info g
                    LEFT JOIN group_members gm2 ON gm2.group_id = g.group_id
                    LEFT JOIN event_info e ON e.group_id = g.group_id
                    LEFT JOIN event_members em ON em.event_id = e.event_id
                    WHERE {" AND ".join(where_conditions)}
                    GROUP BY g.group_id, g.name, g.description, g.group_type, g.group_location, g.is_public
                    LIMIT 60
                """
                
                cursor.execute(sql, tuple(params))
                groups_list = cursor.fetchall()

                # Sort results
                if q and not sort:
                    # Sort by relevance: how well group name matches search term
                    search_lower = q.lower()
                    def calc_group_relevance(group):
                        name = group.get('name', '').lower()
                        
                        if name == search_lower:
                            return 1
                        elif name.startswith(search_lower):
                            return 2
                        elif f' {search_lower}' in name:
                            return 3
                        elif search_lower in name:
                            return 4
                        else:
                            return 999
                    
                    groups_list = sorted(groups_list, key=lambda g: (calc_group_relevance(g), g.get('name', '')))
                elif sort == "popularity":
                    groups_list = sorted(groups_list, key=lambda g: g.get('popularity', 0), reverse=True)
                elif sort == "members":
                    groups_list = sorted(groups_list, key=lambda g: g.get('member_count', 0), reverse=True)
                else:
                    # Default: sort by name
                    groups_list = sorted(groups_list, key=lambda g: g.get('name', '').lower())

                results["groups"] = groups_list

                # If Groups tab only, paginate groups independently
                if tab == "groups":
                    total_groups = len(groups_list)
                    start_idx = (page - 1) * per_page
                    end_idx = start_idx + per_page
                    paged_groups = groups_list[start_idx:end_idx]

                    base_url = url_for('explore')
                    groups_pagination = create_pagination_info(
                        page=page,
                        per_page=per_page,
                        total=total_groups,
                        base_url=base_url,
                        tab=tab,
                        q=q or None,
                        type=gtype or None,
                        location=location or None,
                        privacy_type=privacy_type or None,
                        sort=sort or None
                    )
                    groups_pagination_links = create_pagination_links(groups_pagination)

                    results["groups"] = paged_groups
                    results["groups_pagination"] = groups_pagination
                    results["groups_pagination_links"] = groups_pagination_links


    except Exception as e:
        print("[/explore] error:", e)
        import traceback
        traceback.print_exc()

    # Set page title based on active tab
    if tab == "events":
        tab_title = "Browse Events"
    elif tab == "groups":
        tab_title = "Groups"
    else:
        tab_title = "Browse Events"  # Default fallback

    return render_template(
                            "search/search.html",
                            tab=tab,
                            tab_title=tab_title,
                            q=q,
                            gtype=gtype,
                            date_from=date_from,
                            date_to=date_to,
                            location=location,
                            event_type=event_type,
                            privacy_type=privacy_type,
                            sort=sort,
                            results=results,
                            event_locations=event_locations,
                            group_locations=group_locations,
                            user_role=user_role,
                            group_role=group_role,
                            today=today_str,
                            active_page="search_explore"
                        )


@app.get("/search/events", endpoint="search_events")
def search_events_shortcut():
    """Direct shortcut to the Explore page with events tab."""
    return redirect(url_for("explore", tab="events", **request.args))


@app.get("/search/groups", endpoint="search_groups")
def search_groups_shortcut():
    """Direct shortcut to the Explore page with groups tab."""
    return redirect(url_for("explore", tab="groups", **request.args))


# =============================================================================
# GROUP MANAGER EVENT TRACKING ROUTES (US-GM-08)
# =============================================================================

@app.route('/test-template-filters')
def test_template_filters():
    """Test route to verify template filters work on PythonAnywhere"""
    from datetime import datetime, date, time
    from .util import nz_date, nz_time12_upper, nz_month_year
    
    test_data = {
        'datetime_now': datetime.now(),
        'date_today': date.today(),
        'time_now': time(14, 30),
        'string_date': '2024-01-15',
        'string_time': '14:30:00'
    }
    
    try:
        results = {}
        for key, value in test_data.items():
            results[key] = {
                'original': str(value),
                'nz_date': nz_date(value),
                'nz_time12_upper': nz_time12_upper(value),
                'nz_month_year': nz_month_year(value)
            }
        return f"<pre>Template filter test results:\n{results}</pre>"
    except Exception as e:
        return f"<pre>Error testing template filters: {str(e)}</pre>"

@app.route('/groups/<int:group_id>/event-registrations')
def group_event_registrations(group_id):
    """Event Registration Status Page"""
    from .auth import require_login, get_current_user_id, is_group_manager, is_super_admin, is_support_technician
    
    user_id = get_current_user_id()
    if not user_id:
        flash('Please log in to view this page.', 'warning')
        return redirect(url_for('login'))
    
    # Authorization check (group manager or admin)
    is_admin = is_super_admin() or is_support_technician()
    is_group_mgr = is_group_manager() and _is_group_manager_of_group(user_id, group_id)
    
    if not is_admin and not is_group_mgr:
        flash('You are not authorized to view this page.', 'error')
        return redirect(url_for('explore'))
    
    try:
        with db.get_cursor() as cursor:
            # Get group information
            cursor.execute("""
                SELECT group_id, name, description
                FROM group_info
                WHERE group_id = %s AND status = 'approved'
            """, (group_id,))
            group = cursor.fetchone()
            
            if not group:
                flash('Group not found.', 'error')
                return redirect(url_for('explore'))
            
            # Get event registration status by event - Include users with valid race results
            cursor.execute("""
                SELECT 
                    e.event_id,
                    e.event_title,
                    e.event_date,
                    e.event_time,
                    e.location,
                    e.max_participants,
                    COUNT(em.membership_id) as registered_count,
                    SUM(CASE 
                        WHEN em.participation_status = 'attended' 
                             OR (rr.start_time IS NOT NULL 
                                 AND rr.finish_time IS NOT NULL 
                                 AND rr.finish_time > rr.start_time)
                        THEN 1 
                        ELSE 0 
                    END) as attended_count
                FROM event_info e
                LEFT JOIN event_members em ON e.event_id = em.event_id 
                    AND em.participation_status IN ('registered', 'attended')
                LEFT JOIN race_results rr ON em.membership_id = rr.membership_id
                WHERE e.group_id = %s AND e.status = 'scheduled'
                GROUP BY e.event_id, e.event_title, e.event_date, e.event_time, e.location, e.max_participants
                ORDER BY e.event_date DESC, e.event_time DESC
            """, (group_id,))
            events = cursor.fetchall()
            
            # Get registrant list for each event - Show attended status for users with valid race results
            for event in events:
                cursor.execute("""
                    SELECT 
                        u.username,
                        u.first_name,
                        u.last_name,
                        em.event_role,
                        CASE 
                            WHEN em.participation_status = 'attended' 
                                 OR (rr.start_time IS NOT NULL 
                                     AND rr.finish_time IS NOT NULL 
                                     AND rr.finish_time > rr.start_time)
                            THEN 'attended'
                            ELSE em.participation_status
                        END as participation_status,
                        CASE 
                            WHEN em.event_role = 'volunteer' AND em.volunteer_status = 'assigned'
                            THEN 'pending'
                            WHEN em.event_role = 'volunteer' AND em.volunteer_status = 'confirmed'
                            THEN 'confirmed'
                            ELSE em.volunteer_status
                        END as volunteer_status,
                        em.registration_date
                    FROM event_members em
                    JOIN users u ON em.user_id = u.user_id
                    LEFT JOIN race_results rr ON em.membership_id = rr.membership_id
                    WHERE em.event_id = %s AND em.participation_status IN ('registered', 'attended')
                    ORDER BY em.registration_date DESC
                """, (event['event_id'],))
                event['registrations'] = cursor.fetchall()
    
    except Exception as e:
        print(f"Error loading event registrations: {e}")
        flash('An error occurred while loading event registrations.', 'error')
        return redirect(url_for('explore'))
    
    return render_template('group_manager/manage_events.html',
                         group=group,
                         events=events,
                         group_id=group_id)


@app.route('/groups/<int:group_id>/attendance-list')
def group_attendance_list(group_id):
    """Attendance List Page"""
    from .auth import require_login, get_current_user_id, is_group_manager, is_super_admin, is_support_technician
    
    user_id = get_current_user_id()
    if not user_id:
        flash('Please log in to view this page.', 'warning')
        return redirect(url_for('login'))
    
    # Authorization check (group manager or admin)
    is_admin = is_super_admin() or is_support_technician()
    is_group_mgr = is_group_manager() and _is_group_manager_of_group(user_id, group_id)
    
    if not is_admin and not is_group_mgr:
        flash('You are not authorized to view this page.', 'error')
        return redirect(url_for('explore'))
    
    try:
        with db.get_cursor() as cursor:
            # Get group information
            cursor.execute("""
                SELECT group_id, name, description
                FROM group_info
                WHERE group_id = %s AND status = 'approved'
            """, (group_id,))
            group = cursor.fetchone()
            
            if not group:
                flash('Group not found.', 'error')
                return redirect(url_for('explore'))
            
            # Get attendance list (by attended events) - Include users with valid race results
            cursor.execute("""
                SELECT 
                    e.event_id,
                    e.event_title,
                    e.event_date,
                    e.event_time,
                    e.location,
                    COUNT(DISTINCT em.membership_id) as attended_count
                FROM event_info e
                LEFT JOIN event_members em ON e.event_id = em.event_id 
                LEFT JOIN race_results rr ON em.membership_id = rr.membership_id
                WHERE e.group_id = %s
                  AND (em.participation_status = 'attended' 
                       OR (rr.start_time IS NOT NULL 
                           AND rr.finish_time IS NOT NULL 
                           AND rr.finish_time > rr.start_time))
                GROUP BY e.event_id, e.event_title, e.event_date, e.event_time, e.location
                ORDER BY e.event_date DESC, e.event_time DESC
            """, (group_id,))
            events = cursor.fetchall()
            
            # Get attendee list for each event - Include users with valid race results
            for event in events:
                cursor.execute("""
                    SELECT DISTINCT
                        u.username,
                        u.first_name,
                        u.last_name,
                        em.event_role,
                        em.registration_date
                    FROM event_members em
                    JOIN users u ON em.user_id = u.user_id
                    LEFT JOIN race_results rr ON em.membership_id = rr.membership_id
                    WHERE em.event_id = %s 
                      AND (em.participation_status = 'attended' 
                           OR (rr.start_time IS NOT NULL 
                               AND rr.finish_time IS NOT NULL 
                               AND rr.finish_time > rr.start_time))
                    ORDER BY em.registration_date DESC
                """, (event['event_id'],))
                event['attendees'] = cursor.fetchall()
    
    except Exception as e:
        print(f"Error loading attendance list: {e}")
        flash('An error occurred while loading attendance list.', 'error')
        return redirect(url_for('explore'))
    
    return render_template('group_manager/attendance_list.html',
                         group=group,
                         events=events)


@app.route('/groups/<int:group_id>/statistics')
def group_statistics(group_id):
    """Group Statistics Page - Coming Soon"""
    from .auth import require_login, get_current_user_id, is_group_manager, is_super_admin, is_support_technician
    
    user_id = get_current_user_id()
    if not user_id:
        flash('Please log in to view this page.', 'warning')
        return redirect(url_for('login'))
    
    # Authorization check (group manager or admin)
    is_admin = is_super_admin() or is_support_technician()
    is_group_mgr = is_group_manager() and _is_group_manager_of_group(user_id, group_id)
    
    if not is_admin and not is_group_mgr:
        flash('You are not authorized to view this page.', 'error')
        return redirect(url_for('explore'))
    
    try:
        with db.get_cursor() as cursor:
            # Get only group information
            cursor.execute("""
                SELECT group_id, name, description, created_at
                FROM group_info
                WHERE group_id = %s AND status = 'approved'
            """, (group_id,))
            group = cursor.fetchone()
            
            if not group:
                flash('Group not found.', 'error')
                return redirect(url_for('explore'))
    
    except Exception as e:
        print(f"Error loading group info: {e}")
        flash('An error occurred while loading group information.', 'error')
        return redirect(url_for('explore'))
    
    return render_template('group_manager/statistics.html',
                         group=group,
                         stats=None,
                         monthly_stats=None,
                         top_participants=None)


def _is_group_manager_of_group(user_id, group_id):
    """Check if user is manager of specific group"""
    try:
        with db.get_cursor() as cursor:
            cursor.execute("""
                SELECT 1
                FROM group_members
                WHERE user_id = %s AND group_id = %s 
                  AND group_role = 'manager' AND status = 'active'
                LIMIT 1
            """, (user_id, group_id))
            return cursor.fetchone() is not None
    except Exception:
        return False