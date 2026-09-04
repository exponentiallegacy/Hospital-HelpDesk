"""Authentication blueprint."""
import secrets
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from app import db
from app.audit import record
from app.forms import ForgotPasswordForm, LoginForm, RegistrationForm, ResetPasswordForm
from app.models import AuditLog, Department, User
from app.notifications import notify_role

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _is_safe_redirect(target):
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in ("http", "https") and host_url.netloc == redirect_url.netloc


def _login_locked(email):
    """True when recent failed attempts for an account exceed the configured limit."""
    cutoff = datetime.utcnow() - timedelta(seconds=current_app.config["LOGIN_LOCKOUT_SECONDS"])
    limit = current_app.config["LOGIN_FAILURE_LIMIT"]
    failures = AuditLog.query.filter(
        AuditLog.action == "auth.login_failed",
        AuditLog.actor_name == email.lower(),
        AuditLog.created_at >= cutoff,
    ).count()
    return failures >= limit


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.lower()
        if _login_locked(email):
            record("auth.login_failed", details=f"Sign-in blocked for {email}: too many failed attempts.",
                   actor_name=email)
            db.session.commit()
            flash("Too many failed attempts. Please try again later.", "danger")
        else:
            user = User.query.filter_by(email=email).first()
            if user and user.check_password(form.password.data):
                if not user.is_active_account:
                    record("auth.login_failed", "user", user.id,
                           f"Sign-in blocked for inactive account {user.full_name}.",
                           actor_name=email)
                    db.session.commit()
                    if user.rejection_reason and not user.approved_at:
                        flash(f"Your account application was not approved: {user.rejection_reason}", "danger")
                    else:
                        flash("Your account is awaiting approval by an administrator.", "warning")
                else:
                    login_user(user, remember=form.remember.data)
                    record("auth.login", "user", user.id, f"Signed in as {user.full_name}.")
                    db.session.commit()
                    destination = request.args.get("next")
                    return redirect(destination if destination and _is_safe_redirect(destination) else url_for("main.dashboard"))
            else:
                record("auth.login_failed", details=f"Failed sign-in attempt for {email}.",
                       actor_name=email)
                db.session.commit()
                flash("Invalid email or password.", "danger")
    return render_template("auth/login.html", form=form)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    form = RegistrationForm()
    departments = Department.query.filter_by(is_active=True).order_by(Department.name).all()
    form.department_id.choices = [(d.id, d.name) for d in departments]
    if not departments:
        flash("An administrator must create a department before accounts can be registered.", "warning")
    if form.validate_on_submit():
        if User.query.filter_by(email=form.email.data.lower()).first():
            flash("An account with that email already exists.", "danger")
        else:
            user = User(full_name=form.full_name.data, email=form.email.data.lower(), department_id=form.department_id.data, is_active_account=False)
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.flush()
            department_name = db.session.get(Department, form.department_id.data).name if form.department_id.data else "No department"
            notify_role("Administrator", "New account awaiting approval",
                        f"{user.full_name} · {department_name}", url_for("main.users"))
            record("auth.register", "user", user.id,
                   f"Registered {user.full_name} ({user.email}) for approval. Department: {department_name}.", actor=user)
            db.session.commit()
            flash("Your account has been created and is pending approval by an administrator. You can sign in once it is activated.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/register.html", form=form)


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower()).first()
        if user and user.is_active_account:
            user.reset_token = secrets.token_urlsafe(32)
            user.reset_token_expires_at = datetime.utcnow() + timedelta(hours=1)
            record("auth.password_reset_requested", "user", user.id,
                   f"Password reset requested for {user.full_name} ({user.email}).")
            db.session.commit()
            reset_url = url_for("auth.reset_with_token", token=user.reset_token, _external=True)
            flash("A one-time reset link has been generated (valid for 1 hour): " + reset_url, "success")
            return redirect(url_for("auth.forgot_password"))
        flash("If an account exists for that email, a reset link has been generated.", "info")
    return render_template("auth/forgot_password.html", form=form)


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_with_token(token):
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    user = User.query.filter_by(reset_token=token).first()
    if not user or not user.reset_token_expires_at or user.reset_token_expires_at < datetime.utcnow():
        flash("That reset link is invalid or has expired.", "danger")
        return redirect(url_for("auth.login"))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        user.reset_token = None
        user.reset_token_expires_at = None
        record("user.password_reset", "user", user.id,
               f"Password reset via reset link for {user.full_name}.")
        db.session.commit()
        flash("Password has been reset. You can now sign in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", form=form)


@auth_bp.route("/logout")
def logout():
    user = current_user._get_current_object() if current_user.is_authenticated else None
    logout_user()
    record("auth.logout", "user", user.id if user else None,
           f"Signed out {user.full_name}." if user else "Signed out.", actor=user)
    db.session.commit()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))
