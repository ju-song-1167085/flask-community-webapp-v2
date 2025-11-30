from eventbridge_plus import app, db
from flask import render_template, redirect, url_for, flash
from eventbridge_plus.auth import require_login, get_current_user_id

@app.route('/participant/volunteer-records', endpoint='volunteer_records')
@require_login
def volunteer_records():
    """Display participant's volunteer activity history"""
    user_id = get_current_user_id()
    
    try:
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
            
            # Get all past volunteer activities for this user
            cursor.execute('''
                SELECT 
                    e.event_id,
                    e.event_title,
                    e.event_date,
                    e.event_time,
                    e.location,
                    e.event_type,
                    g.group_id,
                    g.name AS group_name,
                    em.membership_id,
                    em.volunteer_status,
                    em.registration_date,
                    em.participation_status,
                    em.volunteer_hours,
                    em.responsibility
                FROM event_members em
                JOIN event_info e ON em.event_id = e.event_id
                JOIN group_info g ON e.group_id = g.group_id
                WHERE em.user_id = %s 
                  AND em.event_role = 'volunteer'
                  AND CONCAT(e.event_date, ' ', e.event_time) < NOW()
                ORDER BY e.event_date DESC
            ''', (user_id,))
            volunteer_activities = cursor.fetchall()
            
            # Calculate statistics
            total_activities = len(volunteer_activities)
            completed_activities = len([v for v in volunteer_activities 
                                       if v['participation_status'] == 'attended'])
            cancelled_activities = len([v for v in volunteer_activities 
                                       if v['participation_status'] == 'cancelled'])
            
            # Calculate total volunteer hours from actual database values
            # Only count hours for activities that were actually attended
            total_hours = sum([v['volunteer_hours'] or 0 for v in volunteer_activities 
                              if v['participation_status'] == 'attended'])
            
            stats = {
                'total_activities': total_activities,
                'completed_activities': completed_activities,
                'cancelled_activities': cancelled_activities,
                'total_hours': total_hours
            }
            
        return render_template('volunteer_records.html',
                             volunteer_activities=volunteer_activities,
                             stats=stats)
                             
    except Exception as e:
        print(f"Error loading volunteer records: {e}")
        import traceback
        traceback.print_exc()
        flash('Error loading volunteer records.', 'error')
        return redirect(url_for('participant_dashboard'))