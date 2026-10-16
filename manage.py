"""CLI:  python manage.py init-db | create-admin EMAIL NAME [PASSWORD]"""
import getpass
import sys

import db as database
from app import create_app
from common import now_iso
from security import create_user


def main(argv):
    app = create_app()
    if len(argv) < 2:
        print(__doc__); return 1
    if argv[1] == "init-db":
        database.init_db(app.config["DATABASE"])
        print("Database ready:", app.config["DATABASE"])
    elif argv[1] == "create-admin" and len(argv) >= 4:
        pw = argv[4] if len(argv) > 4 else getpass.getpass("Password (min 8 chars): ")
        with app.app_context():
            conn = database.get_db()
            uid, _ = create_user(conn, argv[2], argv[3], "admin", pw)
            database.audit(conn, uid, "admin_created", "users", uid)
            conn.commit()
        print("Admin created:", argv[2])
    else:
        print(__doc__); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
