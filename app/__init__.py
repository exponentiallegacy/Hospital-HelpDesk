"""Application factory and extension setup."""
from datetime import timedelta
from flask import Flask
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
import click
from datetime import datetime
from config import Config

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"
login_manager.session_protection = "basic"
login_manager.remember_cookie_duration = timedelta(days=30)
csrf = CSRFProtect()


def _ensure_schema():
    """Idempotently add columns introduced after the initial `create_all` (SQLite)."""
    engine = db.engine
    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(user)")}
    if "approved_at" not in columns:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE user ADD COLUMN approved_at DATETIME")
    if "rejection_reason" not in columns:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE user ADD COLUMN rejection_reason TEXT")
    for column in ("reset_token", "reset_token_expires_at"):
        if column not in columns:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE user ADD COLUMN {column} DATETIME" if "expires" in column
                                     else f"ALTER TABLE user ADD COLUMN {column} VARCHAR(120)")
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE user SET approved_at = created_at "
                             "WHERE approved_at IS NULL AND is_active_account = 1")
    with engine.connect() as conn:
        dept_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(department)")}
    if "is_active" not in dept_columns:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE department ADD COLUMN is_active BOOLEAN DEFAULT 1")
    with engine.connect() as conn:
        equip_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(equipment)")}
    if "purchase_date" not in equip_columns:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE equipment ADD COLUMN purchase_date DATE")
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE equipment SET operational_status = 'Faulty' "
                             "WHERE operational_status = 'Out of Service'")


def create_app(config_class=Config):
    """Create and configure a Hospital Help Desk application instance."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    from app.auth import auth_bp
    from app.routes import main_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    @app.context_processor
    def inject_template_helpers():
        from app.routes import sla_info
        return {"sla_info": sla_info}

    with app.app_context():
        from app import models  # Registers metadata with SQLAlchemy.
        db.create_all()  # Convenient for SQLite; use `flask db upgrade` in production.
        _ensure_schema()

    @app.cli.command("seed-admin")
    @click.option("--name", default="System Administrator", show_default=True)
    @click.option("--email", default="admin@hospital.local", show_default=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def seed_admin(name, email, password):
        """Create the initial Administration department and administrator account."""
        from app.models import Department, User
        if User.query.filter_by(email=email.lower()).first():
            raise click.ClickException("An account already uses this email address.")
        department = Department.query.filter_by(name="Administration").first()
        if not department:
            department = Department(name="Administration", location="Main Hospital")
            db.session.add(department)
            db.session.flush()
        admin = User(full_name=name, email=email.lower(), role="Administrator", department=department,
                     is_active_account=True, approved_at=datetime.utcnow())
        admin.set_password(password)
        db.session.add(admin)
        db.session.flush()
        from app.audit import record
        record("user.activated", "user", admin.id, f"Created initial administrator {name} ({email.lower()}).")
        db.session.commit()
        click.echo(f"Created administrator account for {email}.")

    @app.cli.command("reset-password")
    @click.option("--email", required=True, help="Email address of the account to reset.")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def reset_password(email, password):
        """Reset a user's password for local administration and testing."""
        from app.models import User
        user = User.query.filter_by(email=email.lower()).first()
        if not user:
            raise click.ClickException("No account was found with that email address.")
        user.set_password(password)
        from app.audit import record
        record("user.password_reset", "user", user.id, f"Password reset for {user.full_name} via CLI.")
        db.session.commit()
        click.echo(f"Password updated for {email}.")

    return app
