"""测试辅助：构造参与方与一个可推进到各阶段的协同服务。"""

from coordination.events import Ledger
from coordination.models import Actor, Role
from coordination.service import CoordinationService, StubSmsChannel

PARTIES = {
    "coordinator": "party-coord",
    "donor_center": "party-dc",
    "courier": "party-courier",
    "recipient_hospital": "party-rh",
    "donor": "party-donor",
}

DONOR_IDENTITY = {
    "full_name": "张伟",
    "national_id_masked": "110101********0011",
    "contact_phone": "13812345678",
    "medical_record_no": "MR-D-0001",
    "address": "北京市海淀区",
}
RECIPIENT_IDENTITY = {
    "full_name": "李明",
    "national_id_masked": "310101********0022",
    "contact_phone": "13987654321",
    "medical_record_no": "MR-R-0009",
    "address": "上海市黄浦区",
}


def actor(role):
    return Actor(Role(role), PARTIES[role])


def make_service(channel=None, path="", tzs=None):
    ledger = Ledger(path)
    svc = CoordinationService(ledger, channel or StubSmsChannel())
    return svc


def open_ready_case(svc, cid="CASE-DEMO", tzs=None):
    """建档 -> 筛查 -> 同意，返回 case_id（处于 consented）。"""
    cid = svc.open_case(
        actor(Role.COORDINATOR),
        donor_identity=dict(DONOR_IDENTITY),
        recipient_identity=dict(RECIPIENT_IDENTITY),
        parties=dict(PARTIES),
        timezones=tzs,
        case_id=cid)
    svc.start_screening(actor(Role.DONOR_CENTER), cid,
                        arrangements="高分辨+体检")
    svc.pass_screening(actor(Role.DONOR_CENTER), cid)
    svc.grant_consent(
        actor(Role.COORDINATOR), cid,
        document_version="CONSENT-v2025.1",
        document_hash="a" * 64,
        signed_local_iso="2026-09-10T10:00",
        witness_party=PARTIES["donor_center"])
    return cid


def schedule_and_confirm(svc, cid, *, start="2026-09-25T08:00",
                         end="2026-09-25T14:00", tz="Asia/Urumqi",
                         reason="初次排期"):
    """排期并由四方确认。"""
    svc.schedule_collection(actor(Role.COORDINATOR), cid,
                            start_local_iso=start, end_local_iso=end,
                            tz=tz, reason=reason)
    for role in ("donor", "donor_center", "courier", "recipient_hospital"):
        svc.confirm_schedule(actor(Role(role)), cid, party_id=PARTIES[role])
