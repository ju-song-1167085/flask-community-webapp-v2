"""
Unified assignment system for helpdesk requests
Combines simple and advanced assignment algorithms
"""

from eventbridge_plus import db
from datetime import datetime, timedelta
import math

# Priority weights for workload calculation
PRIORITY_WEIGHTS = {
    'urgent': 4.0,
    'medium': 2.0, 
    'low': 1.0
}

# Status weights for workload calculation
STATUS_WEIGHTS = {
    'assigned': 1.0,
    'blocked': 0.5  # Blocked requests are waiting for user response
}

def get_available_technicians(priority='medium'):
    """Get list of available support technicians based on priority"""
    try:
        with db.get_cursor() as cursor:
            if priority == 'high':
                # For high priority requests, only super admins
                cursor.execute("""
                    SELECT user_id, username, first_name, last_name, platform_role
                    FROM users
                    WHERE platform_role = 'super_admin'
                    AND status = 'active'
                    ORDER BY user_id
                """)
            else:
                # For urgent, medium, low requests, all support staff
                cursor.execute("""
                    SELECT user_id, username, first_name, last_name, platform_role
                    FROM users
                    WHERE platform_role IN ('super_admin', 'support_technician')
                    AND status = 'active'
                    ORDER BY user_id
                """)
            
            return cursor.fetchall()
            
    except Exception as e:
        return []

def get_technician_current_workload(technician_id):
    """Get simple count of current assigned requests"""
    try:
        with db.get_cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*) as count
                FROM help_requests 
                WHERE assigned_to = %s 
                AND status IN ('assigned', 'blocked')
            """, (technician_id,))
            
            result = cursor.fetchone()
            return result['count'] if result else 0
            
    except Exception as e:
        return 999  # Return high number to avoid assignment if error

def calculate_technician_workload(technician_id):
    """
    Calculate current workload score for a technician using advanced algorithm
    Higher score = more workload = less available
    """
    try:
        with db.get_cursor() as cursor:
            # Get all active requests assigned to this technician
            cursor.execute("""
                SELECT priority, status, created_at, updated_at
                FROM help_requests 
                WHERE assigned_to = %s 
                AND status IN ('assigned', 'blocked')
                ORDER BY updated_at DESC
            """, (technician_id,))
            
            requests = cursor.fetchall()
            
            if not requests:
                return 0.0  # No active requests = no workload
            
            workload_score = 0.0
            
            for request in requests:
                # Base weight from priority
                priority_weight = PRIORITY_WEIGHTS.get(request['priority'], 1.0)
                
                # Status weight
                status_weight = STATUS_WEIGHTS.get(request['status'], 1.0)
                
                # Time factor - older requests get slightly higher weight
                assigned_time = request['updated_at'] or request['created_at']
                hours_since_assigned = (datetime.now() - assigned_time).total_seconds() / 3600
                time_factor = 1.0 + (hours_since_assigned * 0.1)  # 10% increase per hour
                
                # Calculate request weight
                request_weight = priority_weight * status_weight * time_factor
                workload_score += request_weight
            
            return workload_score
            
    except Exception as e:
        return float('inf')  # Return high value to avoid assignment if error

def find_least_busy_technician(priority='medium'):
    """
    Find the technician with the lowest workload score using advanced algorithm
    Returns (technician_id, workload_score) or (None, None) if no one available
    """
    available_technicians = get_available_technicians(priority)
    
    if not available_technicians:
        return None, None
    
    best_technician = None
    lowest_workload = float('inf')
    
    for technician in available_technicians:
        workload = calculate_technician_workload(technician['user_id'])
        
        if workload < lowest_workload:
            lowest_workload = workload
            best_technician = technician
    
    return best_technician, lowest_workload

def simple_auto_assign(request_id, priority='medium'):
    """Simple auto-assignment using round robin with workload consideration"""
    try:
        from .helpdesk import update_help_request_status
        
        # Get available technicians
        technicians = get_available_technicians(priority)
        if not technicians:
            return False, None, "No available technicians found"
        
        # Find technician with lowest current workload
        best_technician = None
        lowest_workload = 999
        
        for tech in technicians:
            current_workload = get_technician_current_workload(tech['user_id'])
            if current_workload < lowest_workload:
                lowest_workload = current_workload
                best_technician = tech
        
        if not best_technician:
            return False, None, "No suitable technician found"
        
        # Assign the request
        success = update_help_request_status(
            request_id=request_id,
            status='assigned',
            assigned_to=best_technician['user_id'],
            priority=priority
        )
        
        if success:
            message = f"Assigned to {best_technician['first_name']} {best_technician['last_name']} (current workload: {lowest_workload})"
            return True, best_technician['user_id'], message
        else:
            return False, None, "Failed to update request status"
            
    except Exception as e:
        return False, None, f"Auto assignment failed: {str(e)}"

def auto_assign_request(request_id, priority='medium'):
    """
    Automatically assign a help request to the least busy technician using advanced algorithm
    Returns (success, assigned_technician_id, message)
    """
    try:
        # Find the least busy technician
        technician, workload = find_least_busy_technician(priority)
        
        if not technician:
            return False, None, "No available technicians found"
        
        # Import here to avoid circular imports
        from .helpdesk import update_help_request_status
        
        # Assign the request
        success = update_help_request_status(
            request_id=request_id,
            status='assigned',
            assigned_to=technician['user_id'],
            priority=priority
        )
        
        if success:
            message = f"Auto-assigned to {technician['first_name']} {technician['last_name']} (workload: {workload:.2f})"
            return True, technician['user_id'], message
        else:
            return False, None, "Failed to update request status"
            
    except Exception as e:
        return False, None, f"Auto assignment failed: {str(e)}"

def bulk_simple_assign():
    """Bulk assign all unassigned requests using simple method"""
    try:
        # Get all unassigned requests (exclude solved status)
        with db.get_cursor() as cursor:
            cursor.execute("""
                SELECT request_id, priority, created_at, status
                FROM help_requests 
                WHERE assigned_to IS NULL 
                AND status != 'solved'
                ORDER BY 
                    CASE priority
                        WHEN 'urgent' THEN 1
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 3
                    END,
                    created_at ASC
            """)
            unassigned_requests = cursor.fetchall()
        
        
        if not unassigned_requests:
            return {
                'success': True,
                'message': 'No unassigned requests found',
                'assigned_count': 0,
                'failed_assignments': []
            }
        
        assigned_count = 0
        failed_assignments = []
        
        for request in unassigned_requests:
            success, assigned_to, message = simple_auto_assign(
                request['request_id'], 
                request['priority']
            )
            
            if success:
                assigned_count += 1
            else:
                failed_assignments.append({
                    'request_id': request['request_id'],
                    'error': message
                })
        
        return {
            'success': True,
            'message': f'Bulk assignment completed. {assigned_count} requests assigned.',
            'assigned_count': assigned_count,
            'failed_assignments': failed_assignments
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'assigned_count': 0,
            'failed_assignments': []
        }

def bulk_auto_assign_balanced(unassigned_requests):
    """
    Assign multiple requests in a balanced way across all technicians using advanced algorithm
    Returns (assigned_count, failed_assignments)
    """
    try:
        from .helpdesk import update_help_request_status
        
        # Get all available technicians
        technicians = get_available_technicians()
        if not technicians:
            return 0, [{'request_id': req['request_id'], 'error': 'No available technicians found'} for req in unassigned_requests]
        
        # Initialize workload tracking for each technician
        technician_workloads = {}
        for tech in technicians:
            technician_workloads[tech['user_id']] = {
                'technician': tech,
                'current_workload': calculate_technician_workload(tech['user_id']),
                'new_assignments': 0
            }
        
        assigned_count = 0
        failed_assignments = []
        
        # Sort requests by priority (urgent first, then by creation time)
        sorted_requests = sorted(unassigned_requests, key=lambda x: (
            {'urgent': 1, 'medium': 2, 'low': 3}.get(x['priority'], 4),
            x.get('created_at', '')
        ))
        
        # Assign each request to the technician with lowest total workload
        for request in sorted_requests:
            # Find technician with lowest total workload (current + new assignments)
            best_tech_id = min(technician_workloads.keys(), 
                             key=lambda tid: technician_workloads[tid]['current_workload'] + 
                                           technician_workloads[tid]['new_assignments'])
            
            best_tech = technician_workloads[best_tech_id]['technician']
            
            # Check if this technician can handle this priority
            if request['priority'] == 'high' and best_tech['platform_role'] != 'super_admin':
                # Skip high priority requests for non-super-admin technicians
                failed_assignments.append({
                    'request_id': request['request_id'],
                    'error': 'No super admin available for high priority request'
                })
                continue
            
            # Assign the request
            success = update_help_request_status(
                request_id=request['request_id'],
                status='assigned',
                assigned_to=best_tech_id,
                priority=request['priority']
            )
            
            if success:
                assigned_count += 1
                # Update workload tracking
                technician_workloads[best_tech_id]['new_assignments'] += 1
            else:
                failed_assignments.append({
                    'request_id': request['request_id'],
                    'error': 'Failed to update request status'
                })
        
        return assigned_count, failed_assignments
        
    except Exception as e:
        return 0, [{'request_id': req['request_id'], 'error': f'System error: {str(e)}'} for req in unassigned_requests]

def get_simple_workload_dashboard():
    """Get simple workload information for dashboard"""
    try:
        technicians = get_available_technicians()
        workload_data = []
        
        for technician in technicians:
            current_workload = get_technician_current_workload(technician['user_id'])
            
            # Get additional stats
            with db.get_cursor() as cursor:
                cursor.execute("""
                    SELECT 
                        COUNT(*) as total_assigned,
                        COUNT(CASE WHEN status = 'assigned' THEN 1 END) as active_count,
                        COUNT(CASE WHEN status = 'blocked' THEN 1 END) as blocked_count,
                        COUNT(CASE WHEN priority = 'urgent' THEN 1 END) as urgent_count
                    FROM help_requests 
                    WHERE assigned_to = %s 
                    AND status IN ('assigned', 'blocked')
                """, (technician['user_id'],))
                
                stats = cursor.fetchone()
            
            workload_data.append({
                'technician_id': technician['user_id'],
                'name': f"{technician['first_name']} {technician['last_name']}",
                'role': technician['platform_role'],
                'workload_score': current_workload,
                'total_assigned': stats['total_assigned'],
                'active_count': stats['active_count'],
                'blocked_count': stats['blocked_count'],
                'urgent_count': stats['urgent_count']
            })
        
        # Sort by workload (ascending)
        workload_data.sort(key=lambda x: x['workload_score'])
        
        return workload_data
        
    except Exception as e:
        print(f"Error in get_simple_workload_dashboard: {e}")
        return []

def get_workload_dashboard():
    """
    Get workload information for all technicians for dashboard display using advanced algorithm
    """
    try:
        technicians = get_available_technicians()
        workload_data = []
        
        for technician in technicians:
            workload = calculate_technician_workload(technician['user_id'])
            
            # Get additional stats
            with db.get_cursor() as cursor:
                cursor.execute("""
                    SELECT 
                        COUNT(*) as total_assigned,
                        COUNT(CASE WHEN status = 'assigned' THEN 1 END) as active_count,
                        COUNT(CASE WHEN status = 'blocked' THEN 1 END) as blocked_count,
                        COUNT(CASE WHEN priority = 'urgent' THEN 1 END) as urgent_count
                    FROM help_requests 
                    WHERE assigned_to = %s 
                    AND status IN ('assigned', 'blocked')
                """, (technician['user_id'],))
                
                stats = cursor.fetchone()
            
            workload_data.append({
                'technician_id': technician['user_id'],
                'name': f"{technician['first_name']} {technician['last_name']}",
                'role': technician['platform_role'],
                'workload_score': round(workload, 2),
                'total_assigned': stats['total_assigned'],
                'active_count': stats['active_count'],
                'blocked_count': stats['blocked_count'],
                'urgent_count': stats['urgent_count']
            })
        
        # Sort by workload score (ascending)
        workload_data.sort(key=lambda x: x['workload_score'])
        
        return workload_data
        
    except Exception as e:
        return []

def should_auto_assign(request_priority='medium'):
    """
    Determine if a request should be auto-assigned based on priority and system load
    """
    # For now, auto-assign all requests except high priority (super admin only)
    return request_priority != 'high'
