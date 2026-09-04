"""Audit trail helpers. Every significant action is recorded for compliance review."""
from app import db
from app.models import AuditLog

# Canonical action codes and their human-readable labels.
AUDIT_ACTIONS = {
    "auth.login": "Sign in",
    "auth.login_failed": "Failed sign-in",
    "auth.logout": "Sign out",
    "auth.register": "Account registered",
    "user.activated": "Account approved / activated",
    "user.deactivated": "Account deactivated",
    "user.role_changed": "Role changed",
    "user.renamed": "Name changed",
    "user.email_changed": "Email changed",
    "user.department_changed": "Department changed",
    "user.password_reset": "Password reset",
    "user.password_changed": "Password changed",
    "auth.password_reset_requested": "Password reset requested",
    "ticket.created": "Ticket created",
    "ticket.assigned": "Ticket assigned",
    "ticket.status_changed": "Ticket status changed",
    "ticket.priority_changed": "Ticket priority changed",
    "ticket.resolution": "Resolution recorded",
    "ticket.commented": "Comment added",
    "ticket.note": "Internal note added",
    "ticket.closed": "Ticket closed",
    "equipment.created": "Equipment created",
    "equipment.updated": "Equipment updated",
    "department.created": "Department created",
    "department.updated": "Department updated",
    "department.disabled": "Department disabled",
    "department.enabled": "Department enabled",
}

# Icon bucket for the dashboard activity feed.
ACTION_TYPES = {
    "auth.login": "login",
    "auth.login_failed": "login",
    "auth.logout": "logout",
    "auth.register": "user",
    "user.activated": "user",
    "user.deactivated": "user",
    "user.role_changed": "user",
    "user.renamed": "user",
    "user.email_changed": "user",
    "user.department_changed": "user",
    "user.password_reset": "user",
    "user.password_changed": "user",
    "auth.password_reset_requested": "user",
    "ticket.created": "ticket",
    "ticket.assigned": "assign",
    "ticket.status_changed": "status",
    "ticket.priority_changed": "priority",
    "ticket.resolution": "resolved",
    "ticket.commented": "comment",
    "ticket.note": "note",
    "ticket.closed": "status",
    "equipment.created": "equipment",
    "equipment.updated": "equipment",
    "department.created": "department",
    "department.updated": "department",
    "department.disabled": "department",
    "department.enabled": "department",
}


def _current_actor():
    """Best-effort lookup of the signed-in actor; safe outside request context."""
    from flask_login import current_user
    try:
        return current_user if current_user.is_authenticated else None
    except RuntimeError:
        return None


def record(action, entity_type=None, entity_id=None, details=None, actor=None, actor_name=None):
    """Append an entry to the audit log.

    Falls back to the signed-in user when no actor is supplied. All entries are
    committed together with the caller's transaction; nothing is written here alone.
    """
    actor = actor if actor is not None else _current_actor()
    if actor_name is None:
        actor_name = actor.full_name if actor else "System"
    db.session.add(AuditLog(
        actor_id=actor.id if actor else None,
        actor_name=actor_name,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    ))
