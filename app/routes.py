"""Core application routes grouped into logical help-desk modules."""
import csv
import os
import re
import uuid
from datetime import datetime, timedelta
from functools import wraps
from io import StringIO
from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from app import db
from app.audit import ACTION_TYPES, AUDIT_ACTIONS, record
from app.forms import (ChangePasswordForm, CommentForm, DepartmentForm, EquipmentForm, ForgotPasswordForm,
                       RejectAccountForm, ResetPasswordForm, RoleForm, TicketForm, TicketUpdateForm, UserForm)
from app.models import (Attachment, AuditLog, Department, Equipment, Notification, Ticket, TicketComment,
                        TicketEvent, User)
from app.notifications import notify, notify_role

main_bp = Blueprint("main", __name__)

STATUS_ORDER = ("Open", "Triaged", "Assigned", "In Progress", "Awaiting Parts", "Resolved", "Closed")
RESOLVED_STATUSES = ("Resolved", "Closed")

TICKET_TRANSITIONS = {
    "Open": {"Triaged"},
    "Triaged": {"Open", "Assigned"},
    "Assigned": {"Triaged", "In Progress"},
    "In Progress": {"Assigned", "Awaiting Parts", "Resolved"},
    "Awaiting Parts": {"In Progress", "Resolved"},
    "Resolved": {"In Progress", "Closed"},
    "Closed": {"Open"},
}

# Hours allowed for a first response, per priority.
SLA_TARGETS = {"Critical": 4, "High": 8, "Medium": 24, "Low": 72}


def _first_response_at(ticket):
    """When the ticket first received a technician/admin action, or None."""
    for event in ticket.events:
        if event.event_type in ("status", "assign", "resolution"):
            return event.created_at
    return None


def sla_info(ticket, now=None):
    """First-response SLA status for a ticket, or None when no target applies."""
    target_hours = SLA_TARGETS.get(ticket.priority)
    if not target_hours:
        return None
    now = now or datetime.utcnow()
    deadline = ticket.created_at + timedelta(hours=target_hours)
    first = _first_response_at(ticket)
    if first is not None:
        return {"deadline": deadline, "breached": first > deadline, "responded": True,
                "response_hours": (first - ticket.created_at).total_seconds() / 3600,
                "target_hours": target_hours}
    breached = now > deadline
    return {"deadline": deadline, "breached": breached, "responded": False,
            "response_hours": None, "target_hours": target_hours,
            "remaining_hours": max(0.0, (deadline - now).total_seconds() / 3600)}


def roles_required(*roles):
    """Restrict a view to named roles while retaining Flask-Login protection."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not current_user.has_role(*roles):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def department_choices(form, include=None):
    """Populate the department selector. Disabled departments are hidden unless explicitly included."""
    if include is None:
        include = []
    elif isinstance(include, int):
        include = [include]
    departments = [d for d in Department.query.order_by(Department.name) if d.is_active or d.id in include]
    form.department_id.choices = [(d.id, d.name) for d in departments]


def _is_last_active_admin(user):
    """True when the user is the only remaining active administrator."""
    if user.role != "Administrator":
        return False
    return User.query.filter(User.role == "Administrator", User.is_active_account.is_(True)).count() <= 1


def next_asset_id():
    """Return the next sequential asset identifier, e.g. EQ-000125."""
    sequence = 0
    for (tag,) in db.session.query(Equipment.asset_tag).all():
        match = re.fullmatch(r"EQ-(\d{6})", tag or "")
        if match:
            sequence = max(sequence, int(match.group(1)))
    return f"EQ-{sequence + 1:06d}"


@main_bp.route("/")
@login_required
def dashboard():
    if current_user.role == "Administrator":
        return _admin_dashboard()
    tickets = Ticket.query
    if current_user.role == "Staff":
        tickets = tickets.filter_by(reporter_id=current_user.id)
    elif current_user.role == "Technician":
        tickets = tickets.filter((Ticket.technician_id == current_user.id) | (Ticket.technician_id.is_(None)))
    scope = tickets.order_by(Ticket.updated_at.desc())
    totals = {status: scope.filter_by(status=status).count() for status in STATUS_ORDER}
    return render_template("dashboard.html", totals=totals, recent_tickets=scope.limit(8).all(),
                           equipment_count=Equipment.query.count(), sla=_sla_summary())


def technician_workload():
    """Per-technician open/assigned/resolved counts for active technicians."""
    total_counts = dict(db.session.query(Ticket.technician_id, func.count(Ticket.id))
                        .filter(Ticket.technician_id.is_not(None)).group_by(Ticket.technician_id).all())
    done_counts = dict(db.session.query(Ticket.technician_id, func.count(Ticket.id))
                       .filter(Ticket.technician_id.is_not(None), Ticket.status.in_(RESOLVED_STATUSES))
                       .group_by(Ticket.technician_id).all())
    workload = []
    for tech in User.query.filter_by(role="Technician", is_active_account=True).order_by(User.full_name).all():
        total = total_counts.get(tech.id, 0)
        resolved = done_counts.get(tech.id, 0)
        open_count = total - resolved
        workload.append({
            "id": tech.id,
            "name": tech.full_name,
            "department": tech.department.name if tech.department else "—",
            "open": open_count,
            "total": total,
            "resolved": resolved,
            "level": "heavy" if open_count >= 10 else ("medium" if open_count >= 5 else "light"),
        })
    return workload


def _sla_summary():
    """Aggregate first-response SLA compliance across all tickets."""
    assessed = breached = 0
    for ticket in Ticket.query.all():
        info = sla_info(ticket)
        if info is None:
            continue
        assessed += 1
        if info["breached"]:
            breached += 1
    return {"assessed": assessed, "breached": breached, "on_time": assessed - breached}


def _admin_dashboard():
    """Administrator overview: workload, approvals, assets and activity."""
    status_counts = dict(db.session.query(Ticket.status, func.count(Ticket.id)).group_by(Ticket.status).all())
    active_statuses = ("Open", "Triaged", "Assigned", "In Progress", "Awaiting Parts")
    status_rows = [(s, status_counts.get(s, 0)) for s in STATUS_ORDER]
    status_max = max((count for _, count in status_rows), default=1) or 1

    workload = technician_workload()

    # Tickets created per day over the last 14 days.
    trend_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=13)
    day_counts = dict(db.session.query(func.date(Ticket.created_at), func.count(Ticket.id))
                      .filter(Ticket.created_at >= trend_start).group_by(func.date(Ticket.created_at)).all())
    trend = [{"label": day.strftime("%d %b"), "count": day_counts.get(day.strftime("%Y-%m-%d"), 0)}
             for day in (trend_start + timedelta(days=offset) for offset in range(14))]
    trend_max = max((row["count"] for row in trend), default=1) or 1

    # Tickets by department.
    dept_counts = dict(db.session.query(Ticket.department_id, func.count(Ticket.id)).group_by(Ticket.department_id).all())
    dept_open = dict(db.session.query(Ticket.department_id, func.count(Ticket.id))
                     .filter(Ticket.status.notin_(RESOLVED_STATUSES)).group_by(Ticket.department_id).all())
    department_rows = [(d.name, dept_counts.get(d.id, 0), dept_open.get(d.id, 0))
                       for d in Department.query.order_by(Department.name)]
    dept_max = max((row[1] for row in department_rows), default=1) or 1

    activity = [
        {"when": entry.created_at,
         "type": ACTION_TYPES.get(entry.action, "ticket"),
         "actor": entry.actor_name,
         "text": entry.details or entry.action,
         "extra": ""}
        for entry in AuditLog.query.order_by(AuditLog.created_at.desc()).limit(20)
    ]

    stats = {
        "total_users": User.query.count(),
        "active_users": User.query.filter_by(is_active_account=True).count(),
        "inactive_users": User.query.filter(User.is_active_account.is_(False), User.approved_at.is_not(None)).count(),
        "pending_approvals": User.query.filter(User.is_active_account.is_(False), User.approved_at.is_(None)).count(),
        "technicians_available": len(workload),
        "open_tickets": status_counts.get("Open", 0),
        "critical_tickets": Ticket.query.filter(Ticket.priority == "Critical", Ticket.status.notin_(RESOLVED_STATUSES)).count(),
        "unassigned_tickets": Ticket.query.filter(Ticket.technician_id.is_(None), Ticket.status.notin_(RESOLVED_STATUSES)).count(),
        "in_progress_tickets": sum(status_counts.get(s, 0) for s in active_statuses[1:]),
        "resolved_tickets": status_counts.get("Resolved", 0) + status_counts.get("Closed", 0),
        "equipment_count": Equipment.query.count(),
        "faulty_equipment": Equipment.query.filter(Equipment.operational_status != "Operational").count(),
    }
    return render_template("admin_dashboard.html", stats=stats, status_rows=status_rows,
                           status_max=status_max, workload=workload, activity=activity[:10],
                           trend=trend, trend_max=trend_max, department_rows=department_rows,
                           dept_max=dept_max, sla=_sla_summary())


@main_bp.route("/tickets")
@login_required
def tickets():
    query = Ticket.query
    if current_user.role == "Staff":
        query = query.filter_by(reporter_id=current_user.id)
    elif current_user.role == "Technician":
        query = query.filter((Ticket.technician_id == current_user.id) | (Ticket.technician_id.is_(None)))
    q = request.args.get("q", "").strip()
    status = request.args.get("status")
    priority = request.args.get("priority")
    department_id = request.args.get("department", type=int)
    technician_id = request.args.get("technician", type=int)
    start = request.args.get("start")
    end = request.args.get("end")
    if q:
        like = f"%{q}%"
        equipment_ids = [e_id for (e_id,) in db.session.query(Equipment.id)
                         .filter(db.or_(Equipment.name.ilike(like), Equipment.asset_tag.ilike(like))).all()]
        reporter_ids = [u_id for (u_id,) in db.session.query(User.id)
                        .filter(User.full_name.ilike(like)).all()]
        query = query.filter(db.or_(
            Ticket.title.ilike(like),
            Ticket.description.ilike(like),
            Ticket.equipment_id.in_(equipment_ids),
            Ticket.reporter_id.in_(reporter_ids)))
    if status:
        query = query.filter_by(status=status)
    if priority:
        query = query.filter_by(priority=priority)
    if department_id:
        query = query.filter_by(department_id=department_id)
    if technician_id:
        query = query.filter_by(technician_id=technician_id)
    if start:
        try:
            query = query.filter(Ticket.created_at >= datetime.strptime(start, "%Y-%m-%d"))
        except ValueError:
            start = None
    if end:
        try:
            query = query.filter(Ticket.created_at < datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1))
        except ValueError:
            end = None
    department = db.session.get(Department, department_id) if department_id else None
    technicians = User.query.filter_by(role="Technician", is_active_account=True).order_by(User.full_name).all()
    tickets = query.order_by(Ticket.updated_at.desc()).all()
    sla_rows = {ticket.id: sla_info(ticket) for ticket in tickets}
    return render_template("tickets/list.html", tickets=tickets, sla_rows=sla_rows,
                           selected_status=status, department=department, q=q, priority=priority,
                           technician_id=technician_id, start=start, end=end,
                           departments=Department.query.order_by(Department.name).all(),
                           technicians=technicians)


@main_bp.route("/tickets/new", methods=["GET", "POST"])
@login_required
def create_ticket():
    form = TicketForm()
    department_choices(form, include=current_user.department_id if current_user.role == "Staff" else None)
    equipment_query = Equipment.query
    if current_user.role == "Staff" and current_user.department_id:
        equipment_query = equipment_query.filter_by(department_id=current_user.department_id)
    form.equipment_id.choices = [(0, "-- Not linked to an asset --")] + \
        [(e.id, f"{e.asset_tag} — {e.name}") for e in equipment_query.order_by(Equipment.name)]
    if current_user.role == "Staff" and current_user.department_id:
        form.department_id.data = current_user.department_id
    equipment_arg = request.args.get("equipment", type=int)
    if equipment_arg:
        form.equipment_id.data = equipment_arg
    if form.validate_on_submit():
        # Staff are always restricted to reporting faults for their own department.
        department_id = current_user.department_id if current_user.role == "Staff" else form.department_id.data
        selected_equipment = db.session.get(Equipment, form.equipment_id.data) if form.equipment_id.data else None
        if selected_equipment and selected_equipment.department_id != department_id:
            flash("The selected equipment does not belong to the selected department.", "danger")
            return render_template("tickets/form.html", form=form, title="Report equipment fault")
        ticket = Ticket(title=form.title.data, description=form.description.data, priority=form.priority.data,
                        department_id=department_id, equipment_id=form.equipment_id.data or None,
                        reporter_id=current_user.id)
        db.session.add(ticket)
        db.session.flush()
        db.session.add(TicketEvent(ticket=ticket, actor=current_user, event_type="created", to_value=ticket.status))
        record("ticket.created", "ticket", ticket.id,
               f"Created Ticket #{ticket.id}: \"{ticket.title}\" ({ticket.priority} priority).")
        if form.photo.data and form.photo.data.filename:
            _save_attachment(form.photo.data, ticket.id, current_user)
        notify_role("Technician", f"New ticket #{ticket.id}",
                    f"{current_user.full_name} reported \"{ticket.title}\" — {ticket.priority} priority.",
                    url_for("main.ticket_detail", ticket_id=ticket.id),
                    exclude=current_user.id if current_user.role == "Technician" else None)
        db.session.commit()
        flash(f"Ticket #{ticket.id} has been submitted.", "success")
        return redirect(url_for("main.ticket_detail", ticket_id=ticket.id))
    return render_template("tickets/form.html", form=form, title="Report equipment fault")


def _save_attachment(file_storage, ticket_id, uploader):
    """Persist an uploaded photo and register it against the ticket."""
    extension = file_storage.filename.rsplit(".", 1)[-1].lower()
    stored_name = f"{uuid.uuid4().hex}.{extension}"
    folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    file_storage.save(os.path.join(folder, stored_name))
    attachment = Attachment(ticket_id=ticket_id, uploader_id=uploader.id, stored_name=stored_name,
                            original_name=file_storage.filename[:255],
                            mime_type=file_storage.mimetype or "application/octet-stream",
                            size_bytes=os.path.getsize(os.path.join(folder, stored_name)))
    db.session.add(attachment)
    return attachment


@main_bp.route("/tickets/<int:ticket_id>/attachments/<int:attachment_id>")
@login_required
def view_attachment(ticket_id, attachment_id):
    """Stream an attachment to anyone who can view the ticket."""
    ticket = db.get_or_404(Ticket, ticket_id)
    if not can_access_ticket(ticket):
        abort(403)
    attachment = Attachment.query.filter_by(id=attachment_id, ticket_id=ticket_id).first_or_404()
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], attachment.stored_name)


def can_access_ticket(ticket):
    return current_user.role == "Administrator" or ticket.reporter_id == current_user.id or ticket.technician_id == current_user.id or (current_user.role == "Technician" and ticket.technician_id is None)


def _record_event(ticket, event_type, from_value, to_value, actor):
    db.session.add(TicketEvent(ticket=ticket, actor=actor, event_type=event_type,
                               from_value=str(from_value) if from_value else None,
                               to_value=str(to_value) if to_value else None))


def _apply_update(ticket, form):
    """Apply an administrator/technician update, enforcing the workflow transitions."""
    status_changed = bool(form.status.data) and form.status.data != ticket.status
    if status_changed and form.status.data not in TICKET_TRANSITIONS.get(ticket.status, set()):
        flash(f"Cannot change status from {ticket.status} to {form.status.data}.", "danger")
        return False

    new_tech = form.technician_id.data or None
    if new_tech != ticket.technician_id:
        old_name = ticket.technician.full_name if ticket.technician else "Unassigned"
        new_name = db.session.get(User, new_tech).full_name if new_tech else "Unassigned"
        _record_event(ticket, "assign", old_name, new_name, current_user)
        ticket.technician_id = new_tech
        record("ticket.assigned", "ticket", ticket.id,
               f"Assigned Ticket #{ticket.id} to {new_name}.")
        detail_url = url_for("main.ticket_detail", ticket_id=ticket.id)
        if new_tech:
            notify(db.session.get(User, new_tech), f"Ticket #{ticket.id} assigned to you",
                   f"{current_user.full_name} assigned \"{ticket.title}\" to you.",
                   detail_url)
        if ticket.reporter_id != current_user.id:
            notify(ticket.reporter, f"Ticket #{ticket.id} assigned",
                   f"\"{ticket.title}\" has been assigned to {new_name}.",
                   detail_url)

    new_status = form.status.data or ticket.status
    if not status_changed:
        if new_tech and ticket.status in ("Open", "Triaged"):
            new_status = "Assigned"
        elif not new_tech and ticket.status == "Assigned":
            new_status = "Triaged"
    if new_status != ticket.status:
        _record_event(ticket, "status", ticket.status, new_status, current_user)
        record("ticket.status_changed", "ticket", ticket.id,
               f"Changed Ticket #{ticket.id} status {ticket.status} → {new_status}.")
        ticket.status = new_status
        if new_status in RESOLVED_STATUSES and not ticket.resolved_at:
            ticket.resolved_at = datetime.utcnow()
        if new_status == "Resolved" and ticket.reporter_id != current_user.id:
            notify(ticket.reporter, f"Ticket #{ticket.id} resolved",
                   f"\"{ticket.title}\" has been marked as resolved.",
                   url_for("main.ticket_detail", ticket_id=ticket.id))

    new_priority = form.priority.data or ticket.priority
    if new_priority and new_priority != ticket.priority:
        _record_event(ticket, "priority", ticket.priority, new_priority, current_user)
        record("ticket.priority_changed", "ticket", ticket.id,
               f"Changed Ticket #{ticket.id} priority {ticket.priority} → {new_priority}.")
        ticket.priority = new_priority

    if form.resolution.data:
        _record_event(ticket, "resolution", ticket.resolution or None, form.resolution.data, current_user)
        record("ticket.resolution", "ticket", ticket.id,
               f"Recorded resolution for Ticket #{ticket.id}.")
        ticket.resolution = form.resolution.data

    if form.comment.data:
        db.session.add(TicketComment(body=form.comment.data, ticket=ticket, author=current_user, is_internal=False))
        record("ticket.commented", "ticket", ticket.id,
               f"Commented on Ticket #{ticket.id}: \"{form.comment.data[:100]}\"")
    if form.internal_note.data:
        db.session.add(TicketComment(body=form.internal_note.data, ticket=ticket, author=current_user, is_internal=True))
        record("ticket.note", "ticket", ticket.id,
               f"Added internal note to Ticket #{ticket.id}: \"{form.internal_note.data[:100]}\"")
    db.session.commit()
    return True


@main_bp.route("/tickets/<int:ticket_id>", methods=["GET", "POST"])
@login_required
def ticket_detail(ticket_id):
    ticket = db.get_or_404(Ticket, ticket_id)
    if not can_access_ticket(ticket):
        abort(403)

    update_form = TicketUpdateForm(obj=ticket)
    comment_form = CommentForm()
    if current_user.has_role("Administrator", "Technician"):
        tech_choices = [(0, "Unassigned")]
        for tech in sorted(technician_workload(), key=lambda t: (t["open"], t["name"])):
            tech_choices.append((tech["id"], f"{tech['name']} ({tech['open']} open)"))
        update_form.technician_id.choices = tech_choices
        allowed = TICKET_TRANSITIONS.get(ticket.status, set())
        update_form.status.choices = [(s, s) for s in STATUS_ORDER if s in allowed or s == ticket.status]
        if request.method == "GET":
            update_form.technician_id.data = ticket.technician_id or 0

    if request.method == "POST":
        if current_user.has_role("Administrator", "Technician"):
            if update_form.validate_on_submit():
                if _apply_update(ticket, update_form):
                    flash("Ticket updated.", "success")
                return redirect(url_for("main.ticket_detail", ticket_id=ticket.id))
        elif comment_form.validate_on_submit():
            if comment_form.confirm.data and ticket.status == "Resolved":
                _record_event(ticket, "status", ticket.status, "Closed", current_user)
                record("ticket.closed", "ticket", ticket.id,
                       f"Confirmed resolution and closed Ticket #{ticket.id}.")
                ticket.status = "Closed"
                db.session.commit()
                flash("Resolution confirmed. Ticket closed.", "success")
                return redirect(url_for("main.ticket_detail", ticket_id=ticket.id))
            if comment_form.body.data:
                db.session.add(TicketComment(body=comment_form.body.data, ticket=ticket, author=current_user, is_internal=False))
                db.session.commit()
                flash("Comment added.", "success")
                return redirect(url_for("main.ticket_detail", ticket_id=ticket.id))
            flash("Nothing to update.", "info")
            return redirect(url_for("main.ticket_detail", ticket_id=ticket.id))

    comments = TicketComment.query.filter_by(ticket_id=ticket.id).order_by(TicketComment.created_at.asc()).all()
    if current_user.role == "Staff":
        comments = [c for c in comments if not c.is_internal]
    events = TicketEvent.query.filter_by(ticket_id=ticket.id).order_by(TicketEvent.created_at.desc()).all()
    return render_template("tickets/detail.html", ticket=ticket, update_form=update_form, comment_form=comment_form,
                           comments=comments, events=events, status_order=STATUS_ORDER)


@main_bp.route("/equipment")
@login_required
def equipment():
    query = Equipment.query
    q = request.args.get("q", "").strip()
    status = request.args.get("status")
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Equipment.name.ilike(like), Equipment.asset_tag.ilike(like),
                                    Equipment.serial_number.ilike(like)))
    if status:
        query = query.filter_by(operational_status=status)
    items = query.order_by(Equipment.asset_tag).all()
    status_counts = dict(db.session.query(Equipment.operational_status, func.count(Equipment.id)).group_by(Equipment.operational_status).all())
    return render_template("equipment/list.html", equipment=items, q=q, selected_status=status,
                           status_counts=status_counts, total=Equipment.query.count())


@main_bp.route("/equipment/<int:equipment_id>")
@login_required
def equipment_detail(equipment_id):
    item = db.get_or_404(Equipment, equipment_id)
    history = Ticket.query.filter_by(equipment_id=item.id).order_by(Ticket.created_at.desc()).all()
    return render_template("equipment/detail.html", item=item, history=history)


@main_bp.route("/equipment/new", methods=["GET", "POST"])
@roles_required("Administrator", "Technician")
def create_equipment():
    form = EquipmentForm()
    department_choices(form)
    if form.validate_on_submit():
        if Equipment.query.filter_by(asset_tag=form.asset_tag.data).first():
            flash("That asset tag is already in use.", "danger")
        else:
            item = Equipment()
            form.populate_obj(item)
            if not item.asset_tag:
                item.asset_tag = next_asset_id()
            db.session.add(item)
            db.session.flush()
            record("equipment.created", "equipment", item.id,
                   f"Registered equipment {item.asset_tag} — {item.name}.")
            db.session.commit()
            flash("Equipment registered.", "success")
            return redirect(url_for("main.equipment"))
    return render_template("equipment/form.html", form=form, title="Add equipment")


@main_bp.route("/equipment/<int:equipment_id>/edit", methods=["GET", "POST"])
@roles_required("Administrator", "Technician")
def edit_equipment(equipment_id):
    item = db.get_or_404(Equipment, equipment_id)
    form = EquipmentForm(obj=item)
    department_choices(form, include=item.department_id)
    if form.validate_on_submit():
        duplicate = Equipment.query.filter(Equipment.asset_tag == form.asset_tag.data, Equipment.id != item.id).first()
        if duplicate:
            flash("That asset tag is already in use.", "danger")
        else:
            fields = ("asset_tag", "name", "category", "manufacturer", "model_number",
                      "serial_number", "location", "operational_status", "purchase_date", "department_id")
            old_values = {field: getattr(item, field) for field in fields}
            form.populate_obj(item)
            changes = [f"{field} {old_values[field]} → {getattr(item, field)}"
                       for field in fields if old_values[field] != getattr(item, field)]
            record("equipment.updated", "equipment", item.id,
                   f"Updated equipment {item.asset_tag}." + (f" ({'; '.join(changes)})" if changes else ""))
            db.session.commit()
            flash("Equipment updated.", "success")
            return redirect(url_for("main.equipment"))
    return render_template("equipment/form.html", form=form, title="Edit equipment")


@main_bp.route("/departments", methods=["GET", "POST"])
@roles_required("Administrator")
def departments():
    form = DepartmentForm()
    if form.validate_on_submit():
        if Department.query.filter(func.lower(Department.name) == form.name.data.lower()).first():
            flash("That department already exists.", "danger")
        else:
            department = Department()
            form.populate_obj(department)
            db.session.add(department)
            db.session.flush()
            record("department.created", "department", department.id,
                   f"Created department {department.name}.")
            db.session.commit()
            flash("Department added.", "success")
            return redirect(url_for("main.departments"))
    return render_template("departments/list.html", departments=Department.query.order_by(Department.name).all(), form=form)


@main_bp.route("/departments/<int:department_id>/edit", methods=["GET", "POST"])
@roles_required("Administrator")
def edit_department(department_id):
    department = db.get_or_404(Department, department_id)
    form = DepartmentForm(obj=department)
    if form.validate_on_submit():
        duplicate = Department.query.filter(func.lower(Department.name) == form.name.data.lower(),
                                            Department.id != department.id).first()
        if duplicate:
            flash("That department already exists.", "danger")
        else:
            old_name, old_location, old_extension = department.name, department.location, department.contact_extension
            form.populate_obj(department)
            changes = []
            for label, old, new in (("name", old_name, department.name),
                                    ("location", old_location, department.location),
                                    ("extension", old_extension, department.contact_extension)):
                if old != new:
                    changes.append(f"{label} {old} → {new}")
            record("department.updated", "department", department.id,
                   f"Updated department {department.name}." + (f" ({'; '.join(changes)})" if changes else ""))
            db.session.commit()
            flash("Department updated.", "success")
            return redirect(url_for("main.departments"))
    return render_template("departments/form.html", form=form, department=department)


@main_bp.route("/departments/<int:department_id>/toggle", methods=["POST"])
@roles_required("Administrator")
def toggle_department(department_id):
    department = db.get_or_404(Department, department_id)
    if department.is_active:
        active_count = Department.query.filter_by(is_active=True).count()
        if active_count <= 1:
            flash("You cannot disable the last active department — registrations would be blocked.", "danger")
        else:
            department.is_active = False
            record("department.disabled", "department", department.id, f"Disabled department {department.name}.")
            db.session.commit()
            flash(f"{department.name} has been disabled. Existing users, tickets and equipment are preserved.",
                  "success")
    else:
        department.is_active = True
        record("department.enabled", "department", department.id, f"Re-enabled department {department.name}.")
        db.session.commit()
        flash(f"{department.name} has been re-enabled.", "success")
    return redirect(request.referrer or url_for("main.departments"))


@main_bp.route("/users")
@roles_required("Administrator")
def users():
    query = User.query
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("filter", "all")
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.full_name.ilike(like), User.email.ilike(like)))
    if status_filter == "active":
        query = query.filter_by(is_active_account=True)
    elif status_filter == "pending":
        query = query.filter(User.is_active_account.is_(False), User.approved_at.is_(None))
    elif status_filter == "inactive":
        query = query.filter(User.is_active_account.is_(False), User.approved_at.is_not(None))
    department_id = request.args.get("department", type=int)
    if department_id:
        query = query.filter_by(department_id=department_id)
    counts = {
        "all": User.query.count(),
        "active": User.query.filter_by(is_active_account=True).count(),
        "pending": User.query.filter(User.is_active_account.is_(False), User.approved_at.is_(None)).count(),
        "inactive": User.query.filter(User.is_active_account.is_(False), User.approved_at.is_not(None)).count(),
    }
    department = db.session.get(Department, department_id) if department_id else None
    return render_template("users/list.html", users=query.order_by(User.full_name).all(),
                           q=q, status_filter=status_filter, counts=counts, department=department)


@main_bp.route("/users/<int:user_id>")
@roles_required("Administrator")
def user_detail(user_id):
    user = db.get_or_404(User, user_id)
    submitted = Ticket.query.filter_by(reporter_id=user.id).order_by(Ticket.created_at.desc()).limit(10).all()
    assigned = Ticket.query.filter_by(technician_id=user.id).order_by(Ticket.created_at.desc()).limit(10).all()
    activity = AuditLog.query.filter(db.or_(
        AuditLog.actor_id == user.id,
        db.and_(AuditLog.entity_type == "user", AuditLog.entity_id == user.id),
    )).order_by(AuditLog.created_at.desc()).limit(20).all()
    return render_template("users/detail.html", user=user, submitted=submitted,
                           assigned=assigned, activity=activity)


@main_bp.route("/users/<int:user_id>/activate", methods=["POST"])
@roles_required("Administrator")
def activate_user(user_id):
    user = db.get_or_404(User, user_id)
    if user.is_active_account:
        flash(f"{user.full_name} is already active.", "info")
    else:
        user.is_active_account = True
        user.approved_at = user.approved_at or datetime.utcnow()
        user.rejection_reason = None
        record("user.activated", "user", user.id, f"Approved and activated {user.full_name}.")
        notify(user, "Account approved", "Your account has been approved and you can now sign in.",
               url_for("auth.login"))
        db.session.commit()
        flash(f"{user.full_name} has been activated.", "success")
    return redirect(request.referrer or url_for("main.users"))


@main_bp.route("/users/<int:user_id>/deactivate", methods=["POST"])
@roles_required("Administrator")
def deactivate_user(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("You cannot deactivate your own account.", "danger")
    elif _is_last_active_admin(user):
        flash("You cannot deactivate the last active administrator.", "danger")
    elif not user.is_active_account:
        flash(f"{user.full_name} is already inactive.", "info")
    else:
        user.is_active_account = False
        record("user.deactivated", "user", user.id, f"Deactivated {user.full_name}.")
        db.session.commit()
        flash(f"{user.full_name} has been deactivated.", "success")
    return redirect(request.referrer or url_for("main.users"))


@main_bp.route("/users/<int:user_id>/reject", methods=["GET", "POST"])
@roles_required("Administrator")
def reject_user(user_id):
    user = db.get_or_404(User, user_id)
    form = RejectAccountForm()
    if form.validate_on_submit():
        if user.is_active_account or user.approved_at:
            flash("Only pending applications can be rejected.", "warning")
        else:
            user.rejection_reason = form.reason.data.strip()
            record("user.rejected", "user", user.id,
                   f"Rejected {user.full_name}'s application: {user.rejection_reason}")
            db.session.commit()
            flash(f"{user.full_name}'s application was rejected.", "success")
            return redirect(url_for("main.user_detail", user_id=user.id))
    return render_template("users/reject.html", form=form, user=user)


@main_bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("Your current password is incorrect.", "danger")
        else:
            current_user.set_password(form.new_password.data)
            record("user.password_changed", "user", current_user.id,
                   f"{current_user.full_name} changed their own password.")
            db.session.commit()
            flash("Password changed.", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("account/password.html", form=form)


@main_bp.route("/users/<int:user_id>/reset-password", methods=["GET", "POST"])
@roles_required("Administrator")
def reset_password(user_id):
    user = db.get_or_404(User, user_id)
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        record("user.password_reset", "user", user.id, f"Password reset for {user.full_name}.")
        notify(user, "Password changed", "An administrator reset your password.", url_for("auth.login"))
        db.session.commit()
        flash(f"Password updated for {user.full_name}.", "success")
        return redirect(url_for("main.user_detail", user_id=user.id))
    return render_template("users/reset_password.html", form=form, user=user)


@main_bp.route("/users/<int:user_id>/role", methods=["GET", "POST"])
@roles_required("Administrator")
def change_role(user_id):
    user = db.get_or_404(User, user_id)
    form = RoleForm(obj=user)
    if form.validate_on_submit():
        if user.id == current_user.id and form.role.data != user.role:
            flash("You cannot change your own role.", "danger")
        elif _is_last_active_admin(user) and form.role.data != "Administrator":
            flash("You cannot demote the last active administrator.", "danger")
        elif form.role.data == user.role:
            flash(f"{user.full_name} already has the role {user.role}.", "info")
        else:
            old_role = user.role
            user.role = form.role.data
            record("user.role_changed", "user", user.id,
                   f"Changed {user.full_name}'s role {old_role} → {user.role}.")
            notify(user, "Role changed", f"Your role has been changed to {user.role}.",
                   url_for("main.dashboard"))
            db.session.commit()
            flash(f"{user.full_name} is now {user.role}.", "success")
        return redirect(url_for("main.user_detail", user_id=user.id))
    return render_template("users/role.html", form=form, user=user)


@main_bp.route("/technicians")
@roles_required("Administrator")
def technicians():
    workload = technician_workload()
    total_open = sum(t["open"] for t in workload)
    unassigned = Ticket.query.filter(Ticket.technician_id.is_(None), Ticket.status.notin_(RESOLVED_STATUSES)).count()
    max_open = max((t["open"] for t in workload), default=1) or 1
    return render_template("technicians/list.html", workload=workload, total_open=total_open,
                           unassigned=unassigned, max_open=max_open)


@main_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@roles_required("Administrator")
def edit_user(user_id):
    user = db.get_or_404(User, user_id)
    form = UserForm(obj=user)
    department_choices(form, include=user.department_id)
    if form.validate_on_submit():
        duplicate = User.query.filter(User.email == form.email.data.lower(), User.id != user.id).first()
        if duplicate:
            flash("That email address is already in use.", "danger")
        elif user.id == current_user.id and (not form.is_active_account.data or form.role.data != user.role):
            flash("You cannot deactivate or change the role of your own account.", "danger")
        elif _is_last_active_admin(user) and (not form.is_active_account.data or form.role.data != "Administrator"):
            flash("You cannot deactivate or demote the last active administrator.", "danger")
        else:
            old_role, old_active = user.role, user.is_active_account
            old_name, old_email, old_department_id = user.full_name, user.email, user.department_id
            form.populate_obj(user)
            user.email = user.email.lower()

            if old_role != user.role:
                record("user.role_changed", "user", user.id,
                       f"Changed {user.full_name}'s role {old_role} → {user.role}.")
            if not old_active and user.is_active_account:
                user.approved_at = user.approved_at or datetime.utcnow()
                record("user.activated", "user", user.id, f"Approved and activated {user.full_name}.")
            if old_active and not user.is_active_account:
                record("user.deactivated", "user", user.id, f"Deactivated {user.full_name}.")
            if old_name != user.full_name:
                record("user.renamed", "user", user.id, f"Changed name {old_name} → {user.full_name}.")
            if old_email != user.email:
                record("user.email_changed", "user", user.id, f"Changed email {old_email} → {user.email}.")
            if old_department_id != user.department_id:
                old_dept = db.session.get(Department, old_department_id).name if old_department_id else "None"
                new_dept = db.session.get(Department, user.department_id).name if user.department_id else "None"
                record("user.department_changed", "user", user.id,
                       f"Changed {user.full_name}'s department {old_dept} → {new_dept}.")
            db.session.commit()
            flash("User updated.", "success")
            return redirect(url_for("main.users"))
    return render_template("users/form.html", form=form, user=user)


def _format_duration(seconds):
    """Human-friendly duration like '5h 32m'."""
    if seconds is None:
        return "N/A"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _report_data():
    """Aggregate ticket summary for the reports page and exports."""
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    resolutions = [(t.resolved_at - t.created_at).total_seconds()
                   for t in Ticket.query.filter(Ticket.resolved_at.is_not(None)).all()]
    avg_seconds = sum(resolutions) / len(resolutions) if resolutions else None
    total = Ticket.query.count()
    resolved_count = Ticket.query.filter(Ticket.status.in_(RESOLVED_STATUSES)).count()

    by_status = dict(db.session.query(Ticket.status, func.count(Ticket.id)).group_by(Ticket.status).all())
    status_rows = [(s, by_status.get(s, 0)) for s in STATUS_ORDER]
    by_priority = dict(db.session.query(Ticket.priority, func.count(Ticket.id)).group_by(Ticket.priority).all())

    dept_counts = dict(db.session.query(Ticket.department_id, func.count(Ticket.id)).group_by(Ticket.department_id).all())
    dept_open = dict(db.session.query(Ticket.department_id, func.count(Ticket.id))
                     .filter(Ticket.status.notin_(RESOLVED_STATUSES)).group_by(Ticket.department_id).all())
    department_rows = [(d.name, dept_counts.get(d.id, 0), dept_open.get(d.id, 0))
                       for d in Department.query.order_by(Department.name)]

    stats = {
        "month": Ticket.query.filter(Ticket.created_at >= month_start).count(),
        "avg_resolution": _format_duration(avg_seconds),
        "critical": Ticket.query.filter(Ticket.priority == "Critical",
                                        Ticket.status.notin_(RESOLVED_STATUSES)).count(),
        "resolved": resolved_count,
        "unresolved": total - resolved_count,
        "total": total,
    }

    # First-response SLA compliance across all tickets.
    sla_stats = {"assessed": 0, "on_time": 0, "breached": 0, "avg_response_seconds": None}
    response_times = []
    for ticket in Ticket.query.all():
        info = sla_info(ticket)
        if info is None:
            continue
        sla_stats["assessed"] += 1
        if info["breached"]:
            sla_stats["breached"] += 1
        else:
            sla_stats["on_time"] += 1
        if info["responded"]:
            response_times.append(info["response_hours"] * 3600)
    if response_times:
        sla_stats["avg_response_seconds"] = sum(response_times) / len(response_times)
    stats["sla"] = sla_stats
    return stats, status_rows, by_priority, department_rows


@main_bp.route("/reports")
@roles_required("Administrator", "Technician")
def reports():
    stats, status_rows, by_priority, department_rows = _report_data()
    return render_template("reports.html", stats=stats, status_rows=status_rows,
                           by_priority=by_priority, department_rows=department_rows,
                           workload=technician_workload())


@main_bp.route("/reports/departments.csv")
@roles_required("Administrator")
def department_report_export():
    _, _, _, department_rows = _report_data()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Department", "Tickets", "Open"])
    writer.writerows(department_rows)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=departments-report.csv"})


@main_bp.route("/reports/technicians.csv")
@roles_required("Administrator")
def technician_report_export():
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Technician", "Department", "Assigned", "Resolved", "Open"])
    for entry in technician_workload():
        writer.writerow([entry["name"], entry["department"], entry["total"], entry["resolved"], entry["open"]])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=technicians-report.csv"})


@main_bp.route("/reports/export.pdf")
@roles_required("Administrator")
def report_pdf():
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    stats, _, _, department_rows = _report_data()
    workload = technician_workload()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm)
    styles = getSampleStyleSheet()
    elements = [Paragraph("Hospital Help Desk - Support Report", styles["Title"]),
                Paragraph("Generated " + datetime.utcnow().strftime("%d %b %Y at %H:%M UTC"), styles["Normal"]),
                Spacer(1, 6 * mm), Paragraph("Ticket summary", styles["Heading2"])]

    summary_table = Table([["Tickets this month", stats["month"]],
                           ["Average resolution time", stats["avg_resolution"]],
                           ["Critical tickets open", stats["critical"]],
                           ["Resolved", stats["resolved"]],
                           ["Unresolved", stats["unresolved"]],
                           ["Total tickets", stats["total"]]],
                          colWidths=[60 * mm, 40 * mm])
    summary_table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                       ("FONTSIZE", (0, 0), (-1, -1), 10)]))
    elements.append(summary_table)

    elements.extend([Spacer(1, 6 * mm), Paragraph("Tickets by department", styles["Heading2"])])
    dept_table = Table([["Department", "Tickets", "Open"]] +
                       [[name, count, open_count] for name, count, open_count in department_rows],
                       colWidths=[60 * mm, 20 * mm, 20 * mm])
    dept_table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                    ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.85, 0.9, 0.95)),
                                    ("FONTSIZE", (0, 0), (-1, -1), 10)]))
    elements.append(dept_table)

    elements.extend([Spacer(1, 6 * mm), Paragraph("Technician performance", styles["Heading2"])])
    tech_table = Table([["Technician", "Department", "Assigned", "Resolved", "Open"]] +
                       [[w["name"], w["department"], w["total"], w["resolved"], w["open"]] for w in workload],
                       colWidths=[40 * mm, 30 * mm, 16 * mm, 16 * mm, 16 * mm])
    tech_table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                    ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.85, 0.9, 0.95)),
                                    ("FONTSIZE", (0, 0), (-1, -1), 9)]))
    elements.append(tech_table)

    doc.build(elements)
    pdf = buffer.getvalue()
    buffer.close()
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=support-report.pdf"})


@main_bp.route("/reports/tickets.csv")
@roles_required("Administrator")
def ticket_export():
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Title", "Status", "Priority", "Department", "Equipment", "Reporter", "Technician", "Created"])
    for ticket in Ticket.query.order_by(Ticket.created_at.desc()):
        writer.writerow([ticket.id, ticket.title, ticket.status, ticket.priority, ticket.department.name,
                         ticket.equipment.asset_tag if ticket.equipment else "", ticket.reporter.full_name,
                         ticket.technician.full_name if ticket.technician else "", ticket.created_at.isoformat()])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=tickets.csv"})


def _audit_query():
    """Build the audit-log query from the request's filter arguments."""
    query = AuditLog.query
    actor_id = request.args.get("actor", type=int)
    action = request.args.get("action")
    start = request.args.get("start")
    end = request.args.get("end")
    if actor_id:
        query = query.filter_by(actor_id=actor_id)
    if action:
        query = query.filter_by(action=action)
    if start:
        try:
            query = query.filter(AuditLog.created_at >= datetime.strptime(start, "%Y-%m-%d"))
        except ValueError:
            pass
    if end:
        try:
            query = query.filter(AuditLog.created_at < datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1))
        except ValueError:
            pass
    return query


@main_bp.route("/audit")
@roles_required("Administrator")
def audit_log():
    page = request.args.get("page", 1, type=int)
    entries = _audit_query().order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template("audit/list.html", entries=entries, actors=User.query.order_by(User.full_name).all(),
                           actions=AUDIT_ACTIONS, selected_actor=request.args.get("actor", type=int),
                           selected_action=request.args.get("action"),
                           start=request.args.get("start"), end=request.args.get("end"))


@main_bp.route("/audit/export.csv")
@roles_required("Administrator")
def audit_export():
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Actor", "Action", "Entity", "Entity ID", "Details"])
    for entry in _audit_query().order_by(AuditLog.created_at.desc()).limit(10000):
        writer.writerow([entry.created_at.isoformat(), entry.actor_name,
                         AUDIT_ACTIONS.get(entry.action, entry.action),
                         entry.entity_type or "", entry.entity_id if entry.entity_id is not None else "",
                         entry.details or ""])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=audit-log.csv"})


@main_bp.route("/notifications")
@login_required
def notifications():
    return render_template("notifications/list.html", notifications=current_user.notifications)


@main_bp.route("/notifications/read-all")
@login_required
def notifications_read_all():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    return redirect(url_for("main.notifications"))


@main_bp.route("/notifications/<int:notification_id>/open")
@login_required
def notification_open(notification_id):
    notification = db.get_or_404(Notification, notification_id)
    if notification.user_id != current_user.id:
        abort(403)
    notification.is_read = True
    db.session.commit()
    return redirect(notification.url or url_for("main.notifications"))


@main_bp.app_errorhandler(403)
def forbidden(_error):
    return render_template("errors/403.html"), 403
