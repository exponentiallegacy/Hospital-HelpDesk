"""Database models for users, departments, equipment and support tickets."""
from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash
from app import db, login_manager


@login_manager.user_loader
def load_user(user_id):
    user = db.session.get(User, int(user_id))
    return user if user and user.is_active_account else None


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Department(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    location = db.Column(db.String(150))
    contact_extension = db.Column(db.String(20))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    users = db.relationship("User", back_populates="department")
    equipment = db.relationship("Equipment", back_populates="department", cascade="all, delete-orphan")
    tickets = db.relationship("Ticket", back_populates="department")

    def __repr__(self):
        return f"<Department {self.name}>"


class User(UserMixin, TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="Staff")
    is_active_account = db.Column(db.Boolean, default=True, nullable=False)
    approved_at = db.Column(db.DateTime)
    rejection_reason = db.Column(db.Text)
    reset_token = db.Column(db.String(120))
    reset_token_expires_at = db.Column(db.DateTime)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"))
    department = db.relationship("Department", back_populates="users")
    submitted_tickets = db.relationship("Ticket", foreign_keys="Ticket.reporter_id", back_populates="reporter")
    assigned_tickets = db.relationship("Ticket", foreign_keys="Ticket.technician_id", back_populates="technician")
    comments = db.relationship("TicketComment", back_populates="author", cascade="all, delete-orphan")
    notifications = db.relationship("Notification", back_populates="user", cascade="all, delete-orphan",
                                    order_by="Notification.created_at.desc()")
    audit_entries = db.relationship("AuditLog", back_populates="actor")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return self.is_active_account

    @property
    def unread_notifications_count(self):
        return Notification.query.filter_by(user_id=self.id, is_read=False).count()

    def has_role(self, *roles):
        return self.role in roles

    @property
    def account_status(self):
        """Active, or Pending (never approved) vs Inactive (deactivated)."""
        if self.is_active_account:
            return "Active"
        if self.approved_at:
            return "Inactive"
        if self.rejection_reason:
            return "Rejected"
        return "Pending"


class Equipment(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    asset_tag = db.Column(db.String(60), unique=True, nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(100))
    manufacturer = db.Column(db.String(100))
    model_number = db.Column(db.String(100))
    serial_number = db.Column(db.String(100))
    location = db.Column(db.String(150))
    operational_status = db.Column(db.String(30), default="Operational", nullable=False)
    purchase_date = db.Column(db.Date)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    department = db.relationship("Department", back_populates="equipment")
    tickets = db.relationship("Ticket", back_populates="equipment")


class Ticket(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default="Open", nullable=False, index=True)
    priority = db.Column(db.String(20), default="Medium", nullable=False, index=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    technician_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    equipment_id = db.Column(db.Integer, db.ForeignKey("equipment.id"))
    resolved_at = db.Column(db.DateTime)
    resolution = db.Column(db.Text)
    reporter = db.relationship("User", foreign_keys=[reporter_id], back_populates="submitted_tickets")
    technician = db.relationship("User", foreign_keys=[technician_id], back_populates="assigned_tickets")
    department = db.relationship("Department", back_populates="tickets")
    equipment = db.relationship("Equipment", back_populates="tickets")
    comments = db.relationship("TicketComment", back_populates="ticket", cascade="all, delete-orphan", order_by="TicketComment.created_at")
    events = db.relationship("TicketEvent", back_populates="ticket", cascade="all, delete-orphan", order_by="TicketEvent.created_at")
    attachments = db.relationship("Attachment", back_populates="ticket", cascade="all, delete-orphan", order_by="Attachment.created_at")


class TicketComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    body = db.Column(db.Text, nullable=False)
    is_internal = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ticket_id = db.Column(db.Integer, db.ForeignKey("ticket.id"), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    ticket = db.relationship("Ticket", back_populates="comments")
    author = db.relationship("User", back_populates="comments")


class TicketEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("ticket.id"), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    event_type = db.Column(db.String(20), nullable=False)
    from_value = db.Column(db.String(200))
    to_value = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ticket = db.relationship("Ticket", back_populates="events")
    actor = db.relationship("User", backref="ticket_events")


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    message = db.Column(db.Text, nullable=False)
    url = db.Column(db.String(200))
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    user = db.relationship("User", back_populates="notifications")


class Attachment(db.Model):
    """An image uploaded against a support ticket."""

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("ticket.id"), nullable=False, index=True)
    uploader_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    stored_name = db.Column(db.String(160), nullable=False, unique=True)
    original_name = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(100), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ticket = db.relationship("Ticket", back_populates="attachments")
    uploader = db.relationship("User", backref="attachments")


class AuditLog(db.Model):
    """Immutable trail of administrative and system actions."""

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    actor_name = db.Column(db.String(120), nullable=False)
    action = db.Column(db.String(60), nullable=False, index=True)
    entity_type = db.Column(db.String(30))
    entity_id = db.Column(db.Integer)
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    actor = db.relationship("User", back_populates="audit_entries")
