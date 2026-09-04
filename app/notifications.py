"""In-app notification helpers."""
from app import db
from app.models import Notification, User


def notify(user, title, message, url=None):
    """Create a notification for a single active user."""
    if user and user.is_active_account:
        db.session.add(Notification(user_id=user.id, title=title, message=message, url=url))


def notify_role(role, title, message, url=None, exclude=None):
    """Create a notification for every active user of a given role."""
    recipients = User.query.filter_by(role=role, is_active_account=True).all()
    for user in recipients:
        if user.id != exclude:
            notify(user, title, message, url)
