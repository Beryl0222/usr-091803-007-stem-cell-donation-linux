"""角色工作台视图：隐私最小化的病例视图与个人通知收件箱。

任何返回给具体参与方的数据都经字段级裁剪；供-患双盲方只能看到代号。
待办 = needs_action 为真且尚未送达关闭的通知（送达仅表示收到，动作是否完成
以领域状态推进为准——这里提供"需我处理"的当前快照）。
"""

from .events import Ledger
from .models import Role, NotificationStatus
from .projection import replay
from .privacy import (
    project_identity, DONOR_FIELD_VISIBILITY, RECIPIENT_FIELD_VISIBILITY,
    donor_pseudonym, recipient_pseudonym,
)
from .notifications import build_notification_projection


def case_for_party(ledger: Ledger, case_id: str, viewer: Role) -> dict:
    snap = replay(ledger.events_for(case_id))

    if viewer == Role.DONOR:
        donor = project_identity(snap.donor_identity, viewer, DONOR_FIELD_VISIBILITY)
        recipient = {"pseudonym": recipient_pseudonym(case_id)}
    elif viewer == Role.DONOR_CENTER:
        donor = project_identity(snap.donor_identity, viewer, DONOR_FIELD_VISIBILITY)
        recipient = {"pseudonym": recipient_pseudonym(case_id)}
    elif viewer == Role.RECIPIENT_HOSPITAL:
        donor = {"pseudonym": donor_pseudonym(case_id)}
        recipient = project_identity(snap.recipient_identity, viewer,
                                     RECIPIENT_FIELD_VISIBILITY)
    elif viewer == Role.COURIER:
        donor = {"pseudonym": donor_pseudonym(case_id)}
        recipient = {"pseudonym": recipient_pseudonym(case_id)}
    elif viewer == Role.COORDINATOR:
        donor = project_identity(snap.donor_identity, viewer, DONOR_FIELD_VISIBILITY)
        recipient = project_identity(snap.recipient_identity, viewer,
                                     RECIPIENT_FIELD_VISIBILITY)
    else:  # REGULATOR / 其它：不给明文，去标识
        donor = {"pseudonym": donor_pseudonym(case_id)}
        recipient = {"pseudonym": recipient_pseudonym(case_id)}

    sch = snap.current_schedule
    view = {
        "case_id": case_id,
        "status": snap.status.value,
        "consent_state": snap.consent_state.value,
        "shipment_status": snap.shipment_status.value,
        "product_type": snap.product_type,
        "identities": {"donor": donor, "recipient": recipient},
        "schedule": None,
        "product": snap.product,
        "handovers": [vars(h) for h in snap.handovers],
    }
    if sch:
        tz = snap.party_tz(_viewer_party(snap, viewer))
        view["schedule"] = {
            "schedule_version": sch.schedule_version,
            "window": sch.window.view(tz),
            "reason": sch.reason,
            "confirmed_by": sorted(sch.confirmations.keys()),
            "all_confirmed": snap.all_confirmed(),
        }
    return view


def _viewer_party(snap, viewer: Role) -> str:
    return {
        Role.DONOR: snap.donor_party_id,
        Role.DONOR_CENTER: snap.donor_center_party_id,
        Role.COURIER: snap.courier_party_id,
        Role.RECIPIENT_HOSPITAL: snap.recipient_party_id,
        Role.COORDINATOR: snap.coordinator_party_id,
    }.get(viewer, "")


def party_inbox(ledger: Ledger, party_id: str) -> dict:
    """某参与方的通知收件箱 + 当前待办统计。"""
    proj = build_notification_projection(ledger.all_events())
    notes = proj.for_party(party_id)
    delivered = {n.notification_id for n in notes}
    open_actions = [
        {
            "notification_id": n.notification_id,
            "case_id": n.case_id,
            "kind": n.kind,
            "urgency": n.urgency,
            "status": n.status,
            "attempts": len(n.attempts),
            "delivered_at": n.delivered_at,
        }
        for n in notes if n.needs_action
    ]
    return {
        "party_id": party_id,
        "total": len(notes),
        "delivered": sum(1 for n in notes
                         if n.status == NotificationStatus.DELIVERED.value),
        "failed": sum(1 for n in notes
                      if n.status == NotificationStatus.FAILED.value),
        "actions": open_actions,
    }
