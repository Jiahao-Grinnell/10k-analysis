"""python -m monitor  — serves the monitor on http://127.0.0.1:5000 (local only)."""
from monitor.app import create_app

create_app().run(host="127.0.0.1", port=5000, debug=False)
