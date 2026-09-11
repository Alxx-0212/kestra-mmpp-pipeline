import os

# --- Core Configuration ---
SECRET_KEY = os.environ.get("SUPERSET_SECRET_KEY", "change-me-use-a-long-random-secret")

# --- Security ---
SUPERSET_DEV_AUTO_LOGIN = os.environ.get("SUPERSET_DEV_AUTO_LOGIN", "false").lower() == "true"
MCP_AUTH_ENABLED = os.environ.get("SUPERSET_MCP_AUTH_ENABLED", "false").lower() == "true"
MCP_DEV_USERNAME = os.environ.get("SUPERSET_MCP_DEV_USERNAME", "admin")

# --- Auto-login Middleware (for local dev) ---
if SUPERSET_DEV_AUTO_LOGIN:
    from flask_appbuilder.security.manager import AUTH_REMOTE_USER
    AUTH_TYPE = AUTH_REMOTE_USER
    AUTH_USER_REGISTRATION = True
    AUTH_USER_REGISTRATION_ROLE = "Admin"
    AUTH_ROLES_SYNC_AT_LOGIN = True

    class DevAutoLoginMiddleware:
        def __init__(self, app):
            self.app = app

        def __call__(self, environ, start_response):
            environ["REMOTE_USER"] = MCP_DEV_USERNAME
            return self.app(environ, start_response)

    ADDITIONAL_MIDDLEWARE = [DevAutoLoginMiddleware]

# --- Session Configuration ---
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = os.environ.get("SUPERSET_MCP_COOKIE_SECURE", "false").lower() == "true"
SESSION_COOKIE_NAME = "superset_session"
PERMANENT_SESSION_LIFETIME = int(os.environ.get("SUPERSET_MCP_SESSION_SECONDS", "86400"))

# --- MCP Session Configuration ---
MCP_SESSION_CONFIG = {
    "SESSION_COOKIE_HTTPONLY": True,
    "SESSION_COOKIE_SECURE": os.environ.get("SUPERSET_MCP_COOKIE_SECURE", "false").lower() == "true",
    "SESSION_COOKIE_SAMESITE": os.environ.get("SUPERSET_MCP_COOKIE_SAMESITE", "Lax"),
    "SESSION_COOKIE_NAME": "superset_session",
    "PERMANENT_SESSION_LIFETIME": int(os.environ.get("SUPERSET_MCP_SESSION_SECONDS", "86400")),
}

# --- CSRF Configuration ---
MCP_CSRF_CONFIG = {
    "WTF_CSRF_ENABLED": os.environ.get("SUPERSET_MCP_CSRF_ENABLED", "true").lower() == "true",
    "WTF_CSRF_TIME_LIMIT": None,
}

# --- Superset Metadata Database Configuration ---
# The SQLALCHEMY_DATABASE_URI is set externally via Docker Compose environment
# to use PostgreSQL with credentials from SUPERSET_DB_NAME/USER/PASSWORD
SQLALCHEMY_DATABASE_URI = os.environ.get(
    "SQLALCHEMY_DATABASE_URI",
    "sqlite:////app/superset_home/superset.db"
)

# Pool settings for PostgreSQL connection
# These are set externally via environment variables
SQLALCHEMY_ENGINE_OPTIONS = {
    "pool_size": int(os.environ.get("SQLALCHEMY_POOL_SIZE", "10")),
    "max_overflow": int(os.environ.get("SQLALCHEMY_MAX_OVERFLOW", "10")),
    "pool_timeout": int(os.environ.get("SQLALCHEMY_POOL_TIMEOUT", "30")),
    "pool_pre_ping": True,
}

# --- Webdriver and Charts ---
WEBDRIVER_BASEURL = os.environ.get("SUPERSET_WEBDRIVER_BASEURL", "http://superset:8088/")

# --- MCP Service Configuration ---
# The MCP server runs as a separate container that connects to Superset's metadata DB
MCP_SERVICE_HOST = os.environ.get("SUPERSET_MCP_HOST", "0.0.0.0")
MCP_SERVICE_PORT = os.environ.get("SUPERSET_MCP_PORT", "5008")
