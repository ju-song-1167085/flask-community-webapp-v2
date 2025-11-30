"""Implements simple MySQL database connectivity for a Flask web app.

This approach is based on the "Define and Access the Database" Flask
tutorial [1]. It gives you an easy way to request a database connection or
cursor while processing a Flask request, and gives you access to that
connection from anywhere in your app (including other functions or modules)
until the request is complete.

This version of db.py uses the unofficial mysqlclient[2] package, instead of
the official MySQL-Connector-Python, as the latter currently appears to be
incompatible with Microsoft Windows 11. As of 24 February 2025, this version
has been verified as working on Windows 10, Windows 11, and PythonAnywhere.

Usage:
------
When initialising your Flask application, call `init_db` passing in your
`Flask` application object and database connection details:
```
>>> app = Flask(__name__)
>>> db.init_db(app, 'username', 'password', 'host', 'database', port)
```

Then, while handling a Flask request you can get a database connection
specific to that request by calling:
```
>>> db = db.get_db()
>>> # Your database code here...
```

If you need a new cursor, you can call:
```
>>> cursor = db.get_cursor()
>>> # Your database code here...
>>> cursor.close()
```

Alternatively, consider using a `with` block to ensure that the cursor is
automatically closed at the end of your query:
```
>>> with get_cursor() as cursor:
>>>     # Your query here...
```

Note that you don't have to close the database connection returned by
`get_db()` as it will be closed automatically at the end of the Flask request.
However, you should ensure that you close all cursors: this includes any
created by the `get_cursor()` function, and any you create manually using the
database connection.

References:
-----------
    [1] https://flask.palletsprojects.com/en/stable/tutorial/database/
    [2] https://pypi.org/project/mysqlclient/
"""
from flask import Flask, g
import MySQLdb

# Database connection parameters (set when calling `init_db`).
connection_params = {}

def init_db(app: Flask, user: str, password: str, host: str, database: str,
            port: int = 3306, autocommit: bool = True):
    """Sets up MySQL connectivity for the specified Flask app.

    This must be called once while initialising your Flask web app, before any
    other `db` module functions are called.

    Args:
        app: The `Flask` application to set up database connectivity for.
        user: Username used to connect to the MySQL server.
        password: Password used to connect to the MySQL server.
        host: Host name or IP address of the MySQL server.
        database: Name of the database to connect to on the MySQL server.
        port: Port used to connect to the MySQL server (default `3306`).
        autocommit: Whether or not to enable auto-commit (default `True`) .
    """
    # Save connection details.
    connection_params['user'] = user
    connection_params['password'] = password
    connection_params['host'] = host
    connection_params['database'] = database
    connection_params['port'] = port
    connection_params['autocommit'] = autocommit

    # Register `close_db()` to run every time the application context is torn
    # down at the end of a Flask request, ensuring that any database connection
    # using during that request gets closed.
    app.teardown_appcontext(close_db)

def get_db():
    """Gets a MySQL database connection to use while serving the current Flask
    request.

    The first time you call this during a request, a new connection will be
    created. After that, any additional calls to `get_db()` during the same
    request are guaranteed to return the same connection.
    
    If you only need a MySQL cursor, and not a reference to the database, you
    can just call the `get_cursor()` function. There's no need to call
    `get_db()` first.

    You don't need to manually close the connection returned by `get_db()` - it
    will be closed automatically at the end of the Flask request. However, you
    should be sure to close any cursors that you create, including any created
    by the `get_cursor()` function.

    Returns:
        A `Connection` instance.
    """
    if 'db' not in g:
        g.db = MySQLdb.connect(**connection_params)
        # Set timezone to New Zealand for consistent datetime handling
        # Only set timezone if not already in NZ timezone (for PythonAnywhere)
        cursor = g.db.cursor()
        cursor.execute("SELECT @@session.time_zone")
        current_tz = cursor.fetchone()[0]
        if current_tz == 'UTC' or current_tz == 'SYSTEM':
            cursor.execute("SET time_zone = '+13:00'")  # New Zealand timezone (DST)
        cursor.close()

    return g.db

def get_cursor():
    """Gets a new MySQL dictionary cursor to use while serving the current
    Flask request.
    
    All cursors created by this function during a single Flask request will
    belong to the same connection. You can get a reference to that connection
    at any time during the request by calling `get_db()`.
    
    Ensure that you close all cursors before the end of the Flask request.
    
    Returns:
        A new `MySQLdb.cursors.DictCursor` instance.
    """
    return get_db().cursor(cursorclass=MySQLdb.cursors.DictCursor)

def close_db(exception = None):
    """Closes the MySQL database connection associated with the current Flask
    request (if any).
    
    There should be no need to call this manually: this function is called
    automatically when the application context is torn down at the end of each
    Flask request.

    Args:
        exception: The exception that terminated the Flask request, or `None`
            if the request terminated successfully.
    """
    # Get the database connection from the current application context (the one
    # that's being torn down), or `None` if there is no connection.
    db = g.pop('db', None)
    
    if db is not None:
        db.close()
