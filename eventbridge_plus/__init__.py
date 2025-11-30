from flask import Flask
from . import connect, db

app = Flask(__name__)
app.secret_key = 'Te1am5g0ld&'


db.init_db(app, connect.dbuser, connect.dbpass, connect.dbhost, connect.dbname,
           connect.dbport)

# ---------- Add a "session" adapter to activity.db ----------
class _SessionResult:
    def __init__(self, rows=None, lastrowid=None):
        self._rows = rows or []
        self._lastrowid = lastrowid
    def fetchall(self):
        return list(self._rows)
    def fetchone(self):
        return self._rows[0] if self._rows else None
    def scalar(self):
        row = self.fetchone()
        if row is None:
            return None
        # Support tuple/list or single value
        return row[0] if isinstance(row, (tuple, list)) else row
    @property
    def lastrowid(self):
        return self._lastrowid

class _DBSessionShim:
    def __init__(self, dbmod):
        self._db = dbmod
    def execute(self, sql, params=None):
        rows, lastrowid = [], None
        with self._db.get_cursor() as cur:
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)
            lastrowid = getattr(cur, "lastrowid", None)
            try:
                rows = cur.fetchall()
            except Exception:
                rows = []
        return _SessionResult(rows, lastrowid)
    def commit(self):
        try:
            self._db.commit()
        except Exception:
            pass

# Only mount the db module if it does not have a session attribute to avoid duplicate definitions.
if not hasattr(db, "session"):
    db.session = _DBSessionShim(db)

from .util import register_template_filters
register_template_filters(app)

from . import user       
from . import profile    
from . import participant
from . import super_admin
from . import support_tech
from . import group_manager
from . import events
from . import groups
from . import helpdesk
from . import analytics
from . import results
from . import search
from . import noti

from .events import register_event_routes

if not app.config.get('EVENT_ROUTES_LOADED'):
    register_event_routes(app)
    app.config['EVENT_ROUTES_LOADED'] = True
