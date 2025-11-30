"""
Notification System - Business Logic

Handles all notification-related operations:
- Creating notifications
- Checking notification settings
- Managing read/unread status
- Deleting notifications
- Sending email notifications
"""

from eventbridge_plus import db, app
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os


def is_noti_enabled(user_id):
    """
    Check if user has notifications enabled
    
    Args:
        user_id: User ID to check
        
    Returns:
        bool: True if enabled, False if disabled
    """
    with db.get_cursor() as cursor:
        cursor.execute("""
            SELECT notifications_enabled 
            FROM users 
            WHERE user_id = %s
        """, (user_id,))
        
        result = cursor.fetchone()
        
        if result:
            return result['notifications_enabled']
        return False


def create_noti(user_id, title, message, category, related_id=None, *, force: bool = False):
    """
    Create a new notification
    
    Important: Does NOT send notification if user has it disabled!
    
    Args:
        user_id: User ID to receive notification
        title: Notification title (max 200 chars)
        message: Notification message
        category: 'event', 'group', 'volunteer', or 'system'
        related_id: (Optional) Related entity ID
        
    Returns:
        int: Created notification ID, or None if disabled
    """
    # Check if notifications are enabled for this user (unless forced)
    if (not force) and (not is_noti_enabled(user_id)):
        return None
    
    # Normalize category to match DB enum/constraint
    allowed_categories = { 'event', 'group', 'volunteer', 'system' }
    # Map legacy/custom categories (store as 'system' to satisfy DB constraints)
    if category == 'help_request':
        safe_category = 'system'
    else:
        safe_category = category if (category in allowed_categories) else 'system'

    with db.get_cursor() as cursor:
        try:
            cursor.execute("""
                INSERT INTO notifications (user_id, title, message, category, related_id)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, title, message, safe_category, related_id))
            
            noti_id = cursor.lastrowid
            try:
                cursor.connection.commit()
            except Exception:
                pass
            return noti_id
            
        except Exception:
            return None


def toggle_noti_setting(user_id, enabled):
    """
    Toggle notification settings (enable/disable)
    
    Args:
        user_id: User ID
        enabled: True to enable, False to disable
        
    Returns:
        bool: True if successful, False otherwise
    """
    with db.get_cursor() as cursor:
        try:
            cursor.execute("""
                UPDATE users 
                SET notifications_enabled = %s 
                WHERE user_id = %s
            """, (enabled, user_id))
            
            return True
            
        except Exception:
            return False

def get_user_notis(user_id, category='all', limit=50):
    """
    Get user's notifications
    
    Args:
        user_id: User ID
        category: 'all' or specific category ('event', 'group', 'volunteer', 'system')
        limit: Maximum number of notifications to fetch (default: 50)
        
    Returns:
        list: List of notification dictionaries
    """
    with db.get_cursor() as cursor:
        if category == 'all':
            cursor.execute("""
                SELECT * FROM notifications 
                WHERE user_id = %s 
                ORDER BY created_at DESC 
                LIMIT %s
            """, (user_id, limit))
        elif category == 'other':
            # Backward compatibility: include legacy helpdesk/system-style messages
            cursor.execute("""
                SELECT * FROM notifications 
                WHERE user_id = %s 
                  AND (
                        category = 'other'
                     OR (category = 'system' AND (
                            LOWER(title)   LIKE '%help request%'
                         OR LOWER(message) LIKE '%help request%'
                         OR LOWER(title)   LIKE '%helpdesk%'
                         OR LOWER(message) LIKE '%helpdesk%'
                     ))
                  )
                ORDER BY created_at DESC 
                LIMIT %s
            """, (user_id, limit))
        else:
            cursor.execute("""
                SELECT * FROM notifications 
                WHERE user_id = %s AND category = %s 
                ORDER BY created_at DESC 
                LIMIT %s
            """, (user_id, category, limit))
        
        notis = cursor.fetchall()
        return notis


def get_unread_count(user_id):
    """
    Get count of unread notifications
    
    Args:
        user_id: User ID
        
    Returns:
        int: Number of unread notifications
    """
    with db.get_cursor() as cursor:
        cursor.execute("""
            SELECT COUNT(*) as count 
            FROM notifications 
            WHERE user_id = %s AND is_read = FALSE
        """, (user_id,))
        
        result = cursor.fetchone()
        return result['count'] if result else 0


def mark_as_read(noti_id, user_id):
    """
    Mark a notification as read
    
    Args:
        noti_id: Notification ID
        user_id: User ID (for security check)
        
    Returns:
        bool: True if successful, False otherwise
    """
    with db.get_cursor() as cursor:
        try:
            cursor.execute("""
                UPDATE notifications 
                SET is_read = TRUE 
                WHERE notification_id = %s AND user_id = %s
            """, (noti_id, user_id))
            
            return True
            
        except Exception:
            return False

def mark_all_read(user_id):
    """
    Mark all notifications as read for a user
    
    Args:
        user_id: User ID
        
    Returns:
        int: Number of notifications marked as read
    """
    with db.get_cursor() as cursor:
        try:
            cursor.execute("""
                UPDATE notifications 
                SET is_read = TRUE 
                WHERE user_id = %s AND is_read = FALSE
            """, (user_id,))
            
            affected_rows = cursor.rowcount
            return affected_rows
            
        except Exception:
            return 0


def delete_noti(noti_id, user_id):
    """
    Delete a notification
    
    Args:
        noti_id: Notification ID
        user_id: User ID (for security check)
        
    Returns:
        bool: True if successful, False otherwise
    """
    with db.get_cursor() as cursor:
        try:
            cursor.execute("""
                DELETE FROM notifications 
                WHERE notification_id = %s AND user_id = %s
            """, (noti_id, user_id))
            
            return True
            
        except Exception:
            return False


def delete_all_notis(user_id):
    """
    Delete all notifications for a user
    
    Args:
        user_id: User ID
        
    Returns:
        int: Number of notifications deleted
    """
    with db.get_cursor() as cursor:
        try:
            cursor.execute("""
                DELETE FROM notifications 
                WHERE user_id = %s
            """, (user_id,))
            
            affected_rows = cursor.rowcount
            return affected_rows
            
        except Exception:
            return 0


# ===== Email notification functions =====

def send_email(to_email, subject, body, is_html=False):
    """
    Send email notification
    
    Args:
        to_email (str): Recipient email address
        subject (str): Email subject
        body (str): Email body content
        is_html (bool): Whether body is HTML format
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Email configuration (you can move these to config)
        smtp_server = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
        smtp_port = int(os.getenv('SMTP_PORT', '587'))
        sender_email = os.getenv('SENDER_EMAIL', '')
        sender_password = os.getenv('SENDER_PASSWORD', '')
        
        # Skip sending if no email configuration
        if not sender_email or not sender_password:
            return True
            
        # Create message
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = sender_email
        message["To"] = to_email
        
        # Create the plain-text and HTML version of your message
        if is_html:
            text_part = MIMEText(body.replace('<br>', '\n').replace('<p>', '').replace('</p>', '\n'), "plain")
            html_part = MIMEText(body, "html")
            message.attach(text_part)
            message.attach(html_part)
        else:
            text_part = MIMEText(body, "plain")
            message.attach(text_part)
        
        # Create secure connection and send email
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls(context=context)
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, to_email, message.as_string())
            
        return True
        
    except Exception:
        return False


def send_welcome_email(user_email, user_name):
    """
    Send welcome email to new user
    
    Args:
        user_email (str): User's email address
        user_name (str): User's full name
        
    Returns:
        bool: True if successful, False otherwise
    """
    subject = "Welcome to ActiveLoop!"
    
    body = f"""
    <html>
    <body>
        <h2>Welcome to ActiveLoop, {user_name}!</h2>
        <p>Thank you for joining our community. We're excited to have you on board!</p>
        
        <h3>What's next?</h3>
        <ul>
            <li>Complete your profile to help others get to know you</li>
            <li>Browse and join groups that interest you</li>
            <li>Create your own group and start organizing activities</li>
            <li>Participate in events and meet new people</li>
        </ul>
        
        <p>If you have any questions, feel free to contact our support team.</p>
        
        <p>Best regards,<br>
        The ActiveLoop Team</p>
    </body>
    </html>
    """
    
    return send_email(user_email, subject, body, is_html=True)


def send_goodbye_email(user_email, user_name):
    """
    Send goodbye email to user who is leaving
    
    Args:
        user_email (str): User's email address
        user_name (str): User's full name
        
    Returns:
        bool: True if successful, False otherwise
    """
    subject = "We're sorry to see you go"
    
    body = f"""
    <html>
    <body>
        <h2>Goodbye, {user_name}</h2>
        <p>We're sorry to see you leave ActiveLoop. Your account has been successfully deleted.</p>
        
        <p>If you change your mind, you're always welcome to create a new account and rejoin our community.</p>
        
        <p>Thank you for being part of our community, and we wish you all the best!</p>
        
        <p>Best regards,<br>
        The ActiveLoop Team</p>
    </body>
    </html>
    """
    
    return send_email(user_email, subject, body, is_html=True)