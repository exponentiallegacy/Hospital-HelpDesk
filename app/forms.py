"""WTForms definitions. Dynamic choices are assigned by the route handlers."""
import re

from email_validator import EmailNotValidError, validate_email
from flask_wtf import FlaskForm
from wtforms import (BooleanField, DateField, FileField, PasswordField, SelectField, StringField, SubmitField,
                     TextAreaField)
from wtforms.validators import DataRequired, EqualTo, Length, Optional, ValidationError

INTERNAL_EMAIL_RE = re.compile(r"(?i)^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+local$")
PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}


def validate_photo(form, field):
    """Reject non-image uploads attached to a fault report."""
    file = field.data
    if not file or not file.filename:
        return
    extension = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if extension not in PHOTO_EXTENSIONS:
        raise ValidationError("Only JPG, PNG, GIF or WEBP images may be attached.")


def internal_email(form, field):
    """Accept internal accounts (e.g. name@hospital.local) and normal addresses."""
    address = field.data or ""
    if INTERNAL_EMAIL_RE.match(address):
        return
    try:
        validate_email(address, check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValidationError(str(exc)) from exc


def strong_password(form, field):
    """Enforce a minimum password strength for new and reset passwords."""
    value = field.data or ""
    checks = {
        "at least 8 characters": len(value) >= 8,
        "an uppercase letter": bool(re.search(r"[A-Z]", value)),
        "a lowercase letter": bool(re.search(r"[a-z]", value)),
        "a number": bool(re.search(r"\d", value)),
    }
    missing = [label for label, ok in checks.items() if not ok]
    if missing:
        raise ValidationError("Password must include " + ", ".join(missing) + ".")

ROLES = [("Administrator", "Administrator"), ("Technician", "Technician"), ("Staff", "Staff")]
STATUSES = [(s, s) for s in ("Open", "Triaged", "Assigned", "In Progress", "Awaiting Parts", "Resolved", "Closed")]
PRIORITIES = [(p, p) for p in ("Low", "Medium", "High", "Critical")]
EQUIPMENT_STATUSES = [("Operational", "Operational"), ("Faulty", "Faulty"),
                      ("Under Maintenance", "Under Maintenance"), ("Retired", "Retired"), ("Lost", "Lost")]


class LoginForm(FlaskForm):
    # Authentication accepts internal account identifiers, including .local addresses.
    # The account lookup and password verification provide the security boundary here.
    email = StringField("Email address", validators=[DataRequired(), Length(max=120)])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember me")
    submit = SubmitField("Sign in")


class RegistrationForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(max=120)])
    email = StringField("Email address", validators=[DataRequired(), internal_email, Length(max=120)])
    department_id = SelectField("Department", coerce=int, validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired(), strong_password])
    confirm_password = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password")])
    submit = SubmitField("Create account")


class DepartmentForm(FlaskForm):
    name = StringField("Department name", validators=[DataRequired(), Length(max=100)])
    location = StringField("Location", validators=[Optional(), Length(max=150)])
    contact_extension = StringField("Contact extension", validators=[Optional(), Length(max=20)])
    submit = SubmitField("Save department")


class EquipmentForm(FlaskForm):
    asset_tag = StringField("Asset ID", validators=[Optional(), Length(max=60)])
    name = StringField("Equipment name", validators=[DataRequired(), Length(max=150)])
    category = StringField("Category", validators=[Optional(), Length(max=100)])
    manufacturer = StringField("Manufacturer", validators=[Optional(), Length(max=100)])
    model_number = StringField("Model number", validators=[Optional(), Length(max=100)])
    serial_number = StringField("Serial number", validators=[Optional(), Length(max=100)])
    location = StringField("Location", validators=[Optional(), Length(max=150)])
    operational_status = SelectField("Operational status", choices=EQUIPMENT_STATUSES, default="Operational")
    purchase_date = DateField("Purchase date", validators=[Optional()], format="%Y-%m-%d")
    department_id = SelectField("Department", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Save equipment")


class TicketForm(FlaskForm):
    title = StringField("Fault summary", validators=[DataRequired(), Length(max=200)])
    description = TextAreaField("Detailed description", validators=[DataRequired(), Length(min=10)])
    priority = SelectField("Priority", choices=PRIORITIES, default="Medium")
    department_id = SelectField("Department", coerce=int, validators=[DataRequired()])
    equipment_id = SelectField("Affected equipment", coerce=int, validators=[Optional()])
    photo = FileField("Photo", validators=[Optional(), validate_photo])
    submit = SubmitField("Submit ticket")


class TicketUpdateForm(FlaskForm):
    status = SelectField("Status", choices=STATUSES)
    priority = SelectField("Priority", choices=PRIORITIES)
    technician_id = SelectField("Assign technician", coerce=int, validators=[Optional()])
    resolution = TextAreaField("Resolution", validators=[Optional(), Length(max=2000)])
    internal_note = TextAreaField("Internal note", validators=[Optional(), Length(max=2000)])
    comment = TextAreaField("Add a public comment", validators=[Optional(), Length(max=2000)])
    submit = SubmitField("Save changes")


class CommentForm(FlaskForm):
    body = TextAreaField("Add a comment", validators=[Optional(), Length(max=2000)])
    submit = SubmitField("Post comment")
    confirm = SubmitField("Confirm resolution and close")


class UserForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(max=120)])
    email = StringField("Email address", validators=[DataRequired(), internal_email, Length(max=120)])
    role = SelectField("Role", choices=ROLES)
    department_id = SelectField("Department", coerce=int, validators=[DataRequired()])
    is_active_account = BooleanField("Account is active", default=True)
    submit = SubmitField("Save user")


class ResetPasswordForm(FlaskForm):
    password = PasswordField("New password", validators=[DataRequired(), strong_password])
    confirm_password = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("password")])
    submit = SubmitField("Reset password")


class ForgotPasswordForm(FlaskForm):
    email = StringField("Email address", validators=[DataRequired(), Length(max=120)])
    submit = SubmitField("Request reset link")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired()])
    new_password = PasswordField("New password", validators=[DataRequired(), strong_password])
    confirm_password = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("new_password")])
    submit = SubmitField("Change password")


class RoleForm(FlaskForm):
    role = SelectField("Role", choices=ROLES)
    submit = SubmitField("Save role")


class RejectAccountForm(FlaskForm):
    reason = TextAreaField("Reason for rejection", validators=[DataRequired(), Length(min=5, max=1000)])
    submit = SubmitField("Reject application")
