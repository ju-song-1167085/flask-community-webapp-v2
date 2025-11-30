"""
Validation module for user input validation.

Provides individual field validation functions for Flask forms.
Each function returns None for valid input, or error message string for invalid input.
"""

import re
from eventbridge_plus import db
from eventbridge_plus.util import (
    AVAILABLE_LOCATIONS, 
    MAX_USERNAME_LENGTH,
    MAX_EMAIL_LENGTH, 
    MAX_NAME_LENGTH,
    MIN_PASSWORD_LENGTH,
    MIN_AGE_DEFAULT,
    MAX_AGE_DEFAULT,
    ADULT_AGE
)
from flask_bcrypt import Bcrypt
from datetime import datetime, date


# Initialize bcrypt for password hashing
flask_bcrypt = Bcrypt()


# =============================================================================
# Basic validation functions (for individual fields)
# =============================================================================

def check_username(username, check_db=True):
    """
    Validate username format and uniqueness
    
    Args:
        username (str): Username to validate
        check_db (bool): Whether to check database for duplicates
    
    Returns:
        str or None: Error message if invalid, None if valid
    """
    # Check if username is provided
    if not username or not username.strip():
        return 'Username is required.'
    
    username = username.strip()
    
    # Check length using constant
    if len(username) > MAX_USERNAME_LENGTH:
        return f'Username cannot exceed {MAX_USERNAME_LENGTH} characters.'
    
    # Check format (only letters, numbers, underscores, dots)
    if not re.match(r'^[A-Za-z0-9_.]+$', username):
        return 'Username can only contain letters, numbers, underscores, and dots.'
    
    # Check database for duplicates
    if check_db:
        try:
            with db.get_cursor() as cursor:
                cursor.execute('SELECT user_id FROM users WHERE username = %s;', (username,))
                if cursor.fetchone() is not None:
                    return 'An account already exists with this username.'
        except Exception:
            return 'Unable to verify username availability. Please try again.'
    
    return None  # No errors


def check_email(email, check_db=True):
    """
    Validate email format and uniqueness
    
    Args:
        email (str): Email to validate
        check_db (bool): Whether to check database for duplicates
    
    Returns:
        str or None: Error message if invalid, None if valid
    """
    # Check if email is provided
    if not email or not email.strip():
        return 'Email address is required.'
    
    email = email.strip().lower()
    
    # Check length using constant
    if len(email) > MAX_EMAIL_LENGTH:
        return f'Email address cannot exceed {MAX_EMAIL_LENGTH} characters.'
    
    # Check for exactly one @ symbol
    if email.count('@') != 1:
        return 'Email address must contain exactly one @ symbol.'
    
    # Check if starts or ends with @
    if email.startswith('@') or email.endswith('@'):
        return 'Email address cannot start or end with @.'
    
    # Check for consecutive dots
    if '..' in email:
        return 'Email address cannot contain consecutive dots.'
    
    # Check if ends with dot
    if email.endswith('.'):
        return 'Email address cannot end with a dot.'
    
    # Validate email format using regex
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(email_pattern, email):
        return 'Please enter a valid email address (e.g., user@example.com).'
    
    # Check database for duplicates
    if check_db:
        try:
            with db.get_cursor() as cursor:
                cursor.execute('SELECT user_id FROM users WHERE email = %s;', (email,))
                if cursor.fetchone() is not None:
                    return 'An account already exists with this email address.'
        except Exception:
            return 'Unable to verify email availability. Please try again.'
    
    return None  # No errors


def check_password(password):
    """
    Validate password strength
    
    Password requirements:
    - At least MIN_PASSWORD_LENGTH characters long (from constants)
    - Contains at least one letter
    - Contains at least one number
    - Contains at least one special character
    
    Args:
        password (str): Password to validate
    
    Returns:
        str or None: Error message if invalid, None if valid
    """
    # Check if password is provided
    if not password:
        return 'Password is required.'
    
    # Check minimum length using constant
    if len(password) < MIN_PASSWORD_LENGTH:
        return f'Password must be at least {MIN_PASSWORD_LENGTH} characters long.'
    
    # Check for at least one letter
    if not re.search(r'[A-Za-z]', password):
        return 'Password must contain at least one letter.'
    
    # Check for at least one number
    if not re.search(r'[0-9]', password):
        return 'Password must contain at least one number.'
    
    # Check for at least one special character
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        return 'Password must contain at least one special character.'
    
    return None  # No errors


def check_password_match(password, password_confirm):
    """
    Validate that password confirmation matches the original password
    
    Args:
        password (str): Original password
        password_confirm (str): Password confirmation
    
    Returns:
        str or None: Error message if invalid, None if valid
    """
    # Check if password confirmation is provided
    if not password_confirm:
        return 'Please confirm your password.'
    
    # Check if passwords match
    if password != password_confirm:
        return 'Passwords do not match.'
    
    return None  # No errors


def check_name(name, field_name):
    """
    Validate first name or last name
    
    Args:
        name (str): Name to validate
        field_name (str): Field name for error messages ('First name' or 'Last name')
    
    Returns:
        str or None: Error message if invalid, None if valid
    """
    # Check if name is provided
    if not name or not name.strip():
        return f'{field_name} is required.'
    
    name = name.strip()
    
    # Check minimum length (at least 2 characters)
    if len(name) < 2:
        return f'{field_name} must be at least 2 characters long.'
    
    # Check maximum length using constant
    if len(name) > MAX_NAME_LENGTH:
        return f'{field_name} cannot exceed {MAX_NAME_LENGTH} characters.'
    
    return None  # No errors


def check_location(location):
    """
    Validate that the selected location is valid
    
    Args:
        location (str): Selected location
    
    Returns:
        str or None: Error message if invalid, None if valid
    
    Note: Now uses AVAILABLE_LOCATIONS from constants instead of parameter
    """
    # Check if location is provided
    if not location or not location.strip():
        return 'Please select your location.'
    
    location = location.strip()
    
    # Check if location is in the available list from constants
    if location not in AVAILABLE_LOCATIONS:
        return 'Please select a valid location from the list.'
    
    return None  # No errors


def check_current_password(current_password, user_password_hash):
    """
    Validate that the current password is correct
    
    Args:
        current_password (str): Current password entered by user
        user_password_hash (str): Hashed password from database
    
    Returns:
        str or None: Error message if invalid, None if valid
    """
    # Check if current password is provided
    if not current_password:
        return 'Current password is required.'
    
    # Check if current password is correct
    if not flask_bcrypt.check_password_hash(user_password_hash, current_password):
        return 'Current password is incorrect.'
    
    return None  # No errors


def check_new_password_different(new_password, user_password_hash):
    """
    Validate that the new password is different from the current password
    
    Args:
        new_password (str): New password
        user_password_hash (str): Current hashed password from database
    
    Returns:
        str or None: Error message if invalid, None if valid
    """
    # Check if new password is the same as current password
    if flask_bcrypt.check_password_hash(user_password_hash, new_password):
        return 'New password must be different from current password.'
    
    return None  # No errors


def calculate_age(birth_date_str):
    """
    Calculate age from birth date string
    
    Args:
        birth_date_str (str): Birth date in YYYY/MM/DD format
    
    Returns:
        int or None: Age in years, None if invalid date
    """
    try:
        birth_date = datetime.strptime(birth_date_str, '%Y-%m-%d').date()
        today = date.today()
        age = today.year - birth_date.year
        if today.month < birth_date.month or (today.month == birth_date.month and today.day < birth_date.day):
            age -= 1
        return age
    except ValueError:
        return None


def check_birth_date(birth_date_str, min_age=None, max_age=None):
    """
    Validate birth date with age restrictions
    
    Args:
        birth_date_str (str): Birth date in YYYY/MM/DD format
        min_age (int, optional): Minimum age required (defaults to MIN_AGE_DEFAULT from constants)
        max_age (int, optional): Maximum age allowed (defaults to MAX_AGE_DEFAULT from constants)
    
    Returns:
        str or None: Error message if invalid, None if valid
    """
    # Use constants as defaults
    if min_age is None:
        min_age = MIN_AGE_DEFAULT
    if max_age is None:
        max_age = MAX_AGE_DEFAULT
    
    # Check if birth date is provided
    if not birth_date_str or not birth_date_str.strip():
        return 'Birth date is required.'
    
    birth_date_str = birth_date_str.strip()
    
    # Check date format (YYYY/MM/DD)
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', birth_date_str):
        return 'Birth date must be in YYYY/MM/DD format.'
    
    try:
        # Parse the date
        birth_date = datetime.strptime(birth_date_str, '%Y-%m-%d').date()
    except ValueError:
        return 'Please enter a valid date (e.g., February cannot have 30 days).'
    
    # Get today's date
    today = date.today()
    
    # Check if birth date is in the future
    if birth_date > today:
        return 'Birth date cannot be in the future.'
    
    # Calculate age
    age = calculate_age(birth_date_str)
    if age is None:
        return 'Unable to calculate age from birth date.'
    
    # Check minimum age
    if age < min_age:
        return f'You must be at least {min_age} years old to register.'
    
    # Check maximum age
    if age > max_age:
        return f'Please enter a valid birth date (maximum age: {max_age}).'
    
    return None  # No errors


def check_members(member_text):
    """
    Validate comma-separated list of usernames for group initial members
    
    This function checks if all provided usernames exist in the database.
    Used for group creation when specifying initial members.
    
    Args:
        member_text (str): Comma-separated string of usernames (e.g., "john, jane, mike")
    
    Returns:
        str or None: Error message if any username doesn't exist, None if all valid
    """
    # Check if member_text is provided (it's optional)
    if not member_text or not member_text.strip():
        return None  # Empty is allowed
    
    # Parse usernames from comma-separated string
    member_text = member_text.strip()
    usernames = [username.strip() for username in member_text.split(',')]
    
    # Remove empty usernames (in case of double commas or trailing commas)
    usernames = [username for username in usernames if username]
    
    # Check if any usernames are provided after cleaning
    if not usernames:
        return None  # No usernames after cleaning is OK
    
    # Validate each username format first
    for username in usernames:
        username_error = check_username(username, check_db=False)
        if username_error:
            return f"Invalid username format '{username}': {username_error}"
    
    # Check database to find which usernames exist
    try:
        existing_users = []
        with db.get_cursor() as cursor:
            for username in usernames:
                cursor.execute('SELECT username FROM users WHERE username = %s;', (username,))
                if cursor.fetchone() is not None:
                    existing_users.append(username)
        
        # Find usernames that don't exist in database
        invalid_usernames = [username for username in usernames if username not in existing_users]
        
        # Return error message if any invalid usernames found
        if invalid_usernames:
            if len(invalid_usernames) == 1:
                return f"Username '{invalid_usernames[0]}' doesn't exist."
            else:
                return f"These usernames don't exist: {', '.join(invalid_usernames)}"
        
        return None  # All usernames are valid
        
    except Exception:
        return 'Unable to verify usernames. Please try again.'


def check_duplicates(member_text):
    """
    Check for duplicate usernames in the comma-separated list
    
    Args:
        member_text (str): Comma-separated string of usernames
    
    Returns:
        str or None: Error message if duplicates found, None if no duplicates
    """
    if not member_text or not member_text.strip():
        return None
    
    # Parse usernames
    usernames = [username.strip() for username in member_text.split(',')]
    usernames = [username for username in usernames if username]  # Remove empty ones
    
    # Check for duplicates (case-insensitive)
    username_lower = [username.lower() for username in usernames]
    unique_usernames = set(username_lower)
    
    if len(username_lower) != len(unique_usernames):
        return 'Duplicate usernames are not allowed.'
    
    return None  # No duplicates


def is_adult(birth_date_str):
    """
    Quick function to check if someone is 18 or older
    
    Args:
        birth_date_str (str): Birth date in YYYY/MM/DD format
    
    Returns:
        bool: True if 18 or older, False otherwise
    
    Note: Uses ADULT_AGE from constants
    """
    age = calculate_age(birth_date_str)
    return age is not None and age >= ADULT_AGE


def is_child(birth_date_str):
    """
    Quick function to check if someone is under 18 (child/teen)
    
    Args:
        birth_date_str (str): Birth date in YYYY/MM/DD format
    
    Returns:
        bool: True if under 18, False otherwise
    
    Note: Uses ADULT_AGE from constants
    """
    age = calculate_age(birth_date_str)
    return age is not None and age < ADULT_AGE


def get_age_category(birth_date_str):
    """
    Get age category for fun run events
    
    Args:
        birth_date_str (str): Birth date in YYYY/MM/DD format
    
    Returns:
        str: Age category ('child', 'teen', 'adult', 'senior') or 'invalid'
    
    Note: Uses ADULT_AGE from constants
    """
    age = calculate_age(birth_date_str)
    
    if age is None:
        return 'invalid'
    elif age < 13:
        return 'child'
    elif age < ADULT_AGE:
        return 'teen'
    elif age < 65:
        return 'adult'
    else:
        return 'senior'
