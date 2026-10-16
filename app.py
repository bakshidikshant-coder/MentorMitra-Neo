import logging
import secrets

from flask import Flask, g, jsonify, send_from_directory

import db as database
from common import ApiError
from config import Config


def create_app(overrides=None):
    app = Flask(__name__, static_folder="frontend", static_url_path="/static")
    app.config.from_object(Config)
    app.config.update(overrides or {})
    if not app.config.get("SECRET_KEY"):
        logging.warning("MENTOR_SECRET not set: using a random key (tokens reset on restart)")
        app.config["SECRET_KEY"] = secrets.token_hex(32)

    database.init_db(app.config["DATABASE"])
    app.teardown_appcontext(database.close_db)

    from routes import admin, auth_routes, mentor, student
    for m in (auth_routes, admin, student, mentor):
        app.register_blueprint(m.bp)

    @app.errorhandler(ApiError)
    def api_error(e):
        if "db" in g:
            g.db.rollback()   # discard any partial writes made before the error
        return jsonify(error=e.message, **e.extra), e.status

    @app.errorhandler(404)
    def nf(_):
        return jsonify(error="Not found"), 404

    @app.errorhandler(413)
    def too_big(_):
        return jsonify(error="File too large"), 413

    @app.errorhandler(Exception)
    def boom(e):
        if hasattr(e, "code") and isinstance(e.code, int):   # werkzeug HTTPException
            return jsonify(error=getattr(e, "description", str(e))), e.code
        app.logger.exception("Unhandled error")
        return jsonify(error="Internal server error"), 500

    @app.after_request
    def cors(resp):
        origin = app.config.get("CORS_ORIGIN")
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        return resp

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/health")
    def health():
        return jsonify(status="ok")

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000)
