"""
Application constants and configuration values
"""

# --- Jinja filters for NZ-style date/time ---
from datetime import datetime, date, time, timedelta, timezone
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    from zoneinfo import ZoneInfoNotFoundError  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    ZoneInfo = None  # Fallback if unavailable; we will guard usage
    class ZoneInfoNotFoundError(Exception):
        pass
import os
import uuid
from flask import current_app
from werkzeug.utils import secure_filename

def _to_nz_datetime(dt: datetime) -> datetime:
    """Return a datetime converted to Pacific/Auckland (with DST),
    assuming naive datetimes are UTC from the database."""
    if not isinstance(dt, datetime):
        return dt
    
    # If zoneinfo is not available, return the datetime as-is
    if ZoneInfo is None:
        return dt
    
    try:
        nz_zone = ZoneInfo("Pacific/Auckland")
    except Exception:
        # Zone database not available (e.g., PythonAnywhere without tzdata)
        # Return the datetime as-is, assuming it's already in local time
        return dt

    if dt.tzinfo is None:
        # Assume naive datetimes from DB are already in NZ local time
        aware_local = dt.replace(tzinfo=nz_zone)
        return aware_local
    else:
        # Convert any aware datetime to NZ time
        return dt.astimezone(nz_zone)


def nz_date(value):
    """Convert date/datetime to NZ date format (DD/MM/YYYY).
    Datetimes are converted to Pacific/Auckland first to reflect local date."""
    if not value:
        return ''
    try:
        if isinstance(value, datetime):
            nz_dt = _to_nz_datetime(value)
            return nz_dt.strftime('%d/%m/%Y')
        if isinstance(value, date):
            return value.strftime('%d/%m/%Y')
        if isinstance(value, str):
            try:
                d = date.fromisoformat(value.strip())
                return d.strftime('%d/%m/%Y')
            except Exception:
                return value
        return str(value)
    except Exception:
        return str(value) if value is not None else ''

def nz_time12_upper(value):
    """Convert time/datetime/duration to 12-hour (H:MM AM/PM).
    Datetimes are converted to Pacific/Auckland first (DST-aware)."""
    if value is None:
        return ''
    
    try:
        def to_12h(h24, m):
            suffix = 'AM' if h24 < 12 else 'PM'
            h12 = h24 % 12 or 12
            return f"{h12}:{m:02d} {suffix}"

        if isinstance(value, datetime):
            nz_dt = _to_nz_datetime(value)
            s = nz_dt.strftime('%I:%M %p').upper()
            return s[1:] if s.startswith('0') else s

        if isinstance(value, time):
            # Time alone (without date) has no timezone context; format as-is
            s = value.strftime('%I:%M %p').upper()
            return s[1:] if s.startswith('0') else s

        if isinstance(value, timedelta):
            total = int(value.total_seconds())
            if total < 0: total = -total
            h24 = (total // 3600) % 24
            m   = (total % 3600) // 60
            return to_12h(h24, m)

        s = str(value).strip()
        parts = s.split(':')
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return to_12h(int(parts[0]) % 24, int(parts[1]) % 60)
        return s
    except Exception:
        return str(value) if value is not None else ''


def nz_time24(value):
    """Convert time/datetime to 24-hour format (HH:MM:SS).
    Datetimes are converted to Pacific/Auckland first (DST-aware)."""
    if value is None:
        return ''
    
    try:
        if isinstance(value, datetime):
            nz_dt = _to_nz_datetime(value)
            return nz_dt.strftime('%H:%M:%S')
        
        if isinstance(value, time):
            # Time alone (without date) has no timezone context; format as-is
            return value.strftime('%H:%M:%S')
        
        if isinstance(value, str):
            # Try to parse as time string
            try:
                if ':' in value:
                    parts = value.split(':')
                    if len(parts) >= 2:
                        h = int(parts[0])
                        m = int(parts[1])
                        s = int(parts[2]) if len(parts) > 2 else 0
                        return f"{h:02d}:{m:02d}:{s:02d}"
            except Exception:
                pass
            return value
        
        return str(value)
    except Exception:
        return str(value) if value is not None else ''


def nz_month_year(value):
    """Convert date/datetime to NZ month-year format (MMM 'YY).
    Datetimes are converted to Pacific/Auckland first to reflect local date."""
    if not value:
        return ''
    try:
        if isinstance(value, datetime):
            nz_dt = _to_nz_datetime(value)
            return nz_dt.strftime('%b \'%y')
        if isinstance(value, date):
            return value.strftime('%b \'%y')
        if isinstance(value, str):
            try:
                d = date.fromisoformat(value.strip())
                return d.strftime('%b \'%y')
            except Exception:
                return value
        return str(value)
    except Exception:
        return str(value) if value is not None else ''


def register_template_filters(app):
    @app.template_filter('nz_date')
    def nz_date_filter(value):
        return nz_date(value)

    @app.template_filter('nz_time12_upper')
    def nz_time12_upper_filter(value):
        return nz_time12_upper(value)

    @app.template_filter('nz_time24')
    def nz_time24_filter(value):
        return nz_time24(value)

    @app.template_filter('nz_month_year')
    def nz_month_year_filter(value):
        return nz_month_year(value)


# Example extension sets (keep or adapt to your project settings)
# PROFILE_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
# CSV_ALLOWED_EXTENSIONS = {'csv'}

def allowed_file(filename: str, allowed_exts: set) -> bool:
    """
    Return True if filename has an allowed extension.
    """
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in (allowed_exts or set())

def _uploads_root() -> str:
    """
    Return the absolute path to <static>/uploads and create it if missing.
    """
    static_root = current_app.static_folder
    root = os.path.join(static_root, "uploads")
    os.makedirs(root, exist_ok=True)
    return root

def save_uploaded_file(file_storage,
                       subdir: str,
                       allowed_exts: set,
                       filename_prefix: str | None = None) -> str:
    """
    Save a user-provided file to static/uploads/<subdir>/ safely.
    Returns a static-relative path like: 'uploads/profiles/abcd1234.png'
    """
    if not file_storage or not file_storage.filename:
        raise ValueError("No file provided.")

    if not allowed_file(file_storage.filename, allowed_exts):
        raise ValueError("Invalid file type.")

    # Prepare target folder
    uploads_root = _uploads_root()                      # <static>/uploads
    target_dir = os.path.join(uploads_root, subdir)     # <static>/uploads/<subdir>
    os.makedirs(target_dir, exist_ok=True)

    # Secure name + random suffix to avoid collisions
    original = secure_filename(file_storage.filename)
    _, ext = os.path.splitext(original)
    ext = ext.lower()
    unique = uuid.uuid4().hex
    base = f"{filename_prefix.strip()}_{unique}" if filename_prefix else unique
    final_name = f"{base}{ext}"

    abs_path = os.path.join(target_dir, final_name)
    file_storage.save(abs_path)

    # Return static-relative path for url_for('static', filename=...)
    rel_path = os.path.join("uploads", subdir, final_name).replace("\\", "/")
    return rel_path

def remove_uploaded_file(rel_path: str) -> bool:
    """
    Delete a file previously saved under static/uploads/... .
    Accepts a static-relative path beginning with 'uploads/'.
    Returns True if removed, False otherwise.
    """
    if not rel_path:
        return False

    safe_rel = rel_path.replace("\\", "/")
    if not safe_rel.startswith("uploads/"):
        return False

    abs_path = os.path.join(current_app.static_folder, safe_rel)
    if os.path.exists(abs_path):
        try:
            os.remove(abs_path)
            return True
        except Exception:
            return False
    return False



# User roles
DEFAULT_USER_ROLE = 'participant'

# Service locations
AVAILABLE_LOCATIONS = [
    'Christchurch',
    'Dunedin', 
    'Nelson',
    'Queenstown',
    'Tekapo',
    'Wanaka'
]

# Event types
AVAILABLE_EVENT_TYPES = [
    'Swimming',
    'Trail Running', 
    'Cycling',
    'Park Walk',
    'Fun Run',
    'Marathon'
]

# File upload settings
UPLOAD_FOLDER = 'static/uploads/profile_pics'
CSV_UPLOAD_FOLDER = 'static/uploads/csv_results'

# Allowed file extensions
PROFILE_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
CSV_ALLOWED_EXTENSIONS = {'csv'}

# Password requirements
MIN_PASSWORD_LENGTH = 8
MAX_USERNAME_LENGTH = 50
MAX_EMAIL_LENGTH = 100
MAX_NAME_LENGTH = 50

# Age restrictions
MIN_AGE_DEFAULT = 5
MAX_AGE_DEFAULT = 110
ADULT_AGE = 18

# User status
USER_STATUS_ACTIVE = 'active'
USER_STATUS_BANNED = 'banned'

# Pagination
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# Helpdesk settings
HELP_REQUEST_CATEGORIES = [
    'technical_issue',
    'account_problem', 
    'event_inquiry',
    'group_management',
    'rejection_inquiry',
    'general_help'
]

HELP_REQUEST_PRIORITIES = ['low', 'medium', 'high', 'urgent']

HELP_REQUEST_STATUSES = ['new', 'assigned', 'blocked', 'solved']

# Helpdesk validation limits
MAX_HELP_TITLE_LENGTH = 200
MIN_HELP_TITLE_LENGTH = 5
MAX_HELP_DESCRIPTION_LENGTH = 2000
MIN_HELP_DESCRIPTION_LENGTH = 10
MAX_HELP_REPLY_LENGTH = 1000

def create_pagination_info(page, per_page, total, base_url, **kwargs):
    """
    Create pagination information for templates.
    
    Args:
        page (int): Current page number (1-based)
        per_page (int): Items per page
        total (int): Total number of items
        base_url (str): Base URL for pagination links
        **kwargs: Additional query parameters to include in URLs
    
    Returns:
        dict: Pagination information including:
            - items: List of items for current page
            - page: Current page number
            - per_page: Items per page
            - total: Total items
            - pages: Total pages
            - has_prev: Has previous page
            - has_next: Has next page
            - prev_num: Previous page number
            - next_num: Next page number
            - page_urls: Dict of page numbers to URLs
    """
    from math import ceil
    
    # Validate inputs
    page = max(1, int(page) if page else 1)
    per_page = max(1, min(int(per_page) if per_page else DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE))
    total = max(0, int(total) if total else 0)
    
    # Calculate pagination info
    pages = ceil(total / per_page) if total > 0 else 1
    page = min(page, pages)  # Ensure page doesn't exceed total pages
    
    has_prev = page > 1
    has_next = page < pages
    prev_num = page - 1 if has_prev else None
    next_num = page + 1 if has_next else None
    
    # Create page URLs
    page_urls = {}
    for p in range(1, pages + 1):
        params = kwargs.copy()
        params['page'] = p
        params['per_page'] = per_page
        query_string = '&'.join([f'{k}={v}' for k, v in params.items() if v is not None])
        page_urls[p] = f"{base_url}?{query_string}" if query_string else base_url
    
    return {
        'page': page,
        'per_page': per_page,
        'total': total,
        'pages': pages,
        'has_prev': has_prev,
        'has_next': has_next,
        'prev_num': prev_num,
        'next_num': next_num,
        'page_urls': page_urls,
        'start_index': (page - 1) * per_page + 1,
        'end_index': min(page * per_page, total)
    }

def get_pagination_params(request, default_per_page=DEFAULT_PAGE_SIZE):
    """
    Extract pagination parameters from Flask request.
    
    Args:
        request: Flask request object
        default_per_page (int): Default items per page
    
    Returns:
        tuple: (page, per_page) as integers
    """
    page = max(1, int(request.args.get('page', 1)))
    per_page = max(1, min(int(request.args.get('per_page', default_per_page)), MAX_PAGE_SIZE))
    return page, per_page

def create_pagination_links(pagination_info, max_links=5):
    """
    Create a list of page links for pagination display.
    
    Args:
        pagination_info (dict): Pagination info from create_pagination_info()
        max_links (int): Maximum number of page links to show
    
    Returns:
        list: List of page link dictionaries with 'num', 'url', 'is_current'
    """
    current_page = pagination_info['page']
    total_pages = pagination_info['pages']
    page_urls = pagination_info['page_urls']
    
    if total_pages <= max_links:
        # Show all pages
        pages = list(range(1, total_pages + 1))
    else:
        # Show limited pages around current page
        half = max_links // 2
        start = max(1, current_page - half)
        end = min(total_pages, start + max_links - 1)
        
        # Adjust if we're near the end
        if end - start + 1 < max_links:
            start = max(1, end - max_links + 1)
        
        pages = list(range(start, end + 1))
    
    links = []
    for page_num in pages:
        links.append({
            'num': page_num,
            'url': page_urls.get(page_num, '#'),
            'is_current': page_num == current_page
        })
    
    return links