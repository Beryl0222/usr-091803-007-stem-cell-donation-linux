"""测试/演示支撑：固定时钟的装配与“把病例推进到某阶段”的便捷驱动。"""

from datetime import datetime, timezone

from . import states as S
from .app import App, seed_demo
from .clock import Clock

START = datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc)


def build_app():
    app = App(Clock(START))
    seed_demo(app)
    return app


class Driver:
    """以协调员身份顺序投递事件，返回每次 ingest 的结果。"""

    def __init__(self, app, actor="u_coord"):
        self.app = app
        self.wf = app.workflow
        self.actor = actor
        self.search_id = None
        self.case_id = None
        self.product_code = "HSC-TEST-001"
        self.excursion_id = None

    def go(self, etype, payload=None, *, ext=None, source="api", actor=None, case_id=None, idem=None):
        n = len(self.app.store.events) + 1
        ext = ext or f"EXT-{n:04d}"
        cid = case_id or self.case_id
        return self.wf.ingest(
            etype, payload or {}, external_id=ext, source=source,
            actor_id=actor or self.actor, case_id=cid, idem=idem,
        )

    def open_search(self, hla="HLA 10/10", org="HOSP-SH", actor="u_doc"):
        sr = self.wf.open_search(actor_id=actor, transplant_org_id=org,
                                 hla_summary=hla, urgency="routine")
        self.search_id = sr.id
        return sr

    def to_matched(self, donor="donor-77", **extra):
        if self.case_id:
            return None
        if not self.search_id:
            self.open_search()
        payload = {"search_id": self.search_id, "donor_person_id": donor,
                   "donor_org_id": "REG-XJ", "collection_org_id": "HOSP-BJ",
                   "carrier_org_id": "COLD-CHAIN", **extra}
        r = self.go(S.EV_MATCH_SUCCESS, payload, actor="u_coord")
        self.case_id = r["case_id"]
        return r

    def to_screened(self, pass_=True):
        if self.case_id and self.case.ever_reached(S.SCREENING):
            return None
        self.to_matched()
        return self.go(S.EV_SCREENING_RESULT,
                       {"pass": pass_, "summary": "体检合格" if pass_ else "不合格",
                        "exam_ref": "EX-1"}, actor="u_coord")

    def to_consented(self, doc="CONS-T-001"):
        if self.case_id and self.case.ever_reached(S.CONSENTED):
            return None
        self.to_screened()
        return self.go(S.EV_CONSENT_GRANTED,
                       {"document_ref": doc, "signed_at": "2026-09-15T10:00:00+08:00"},
                       actor="u_daff")

    def to_scheduled(self, start="2026-09-25T09:00", end="2026-09-25T14:00"):
        if self.case_id and self.case.ever_reached(S.SCHEDULED):
            return None
        self.to_consented()
        self.go(S.EV_SLOT_PROPOSED,
                {"start_local": start, "end_local": end}, actor="u_coll")
        return self.go(S.EV_SLOT_CONFIRMED, {}, actor="u_coord")

    def to_collected(self):
        if self.product_code in [p.code for p in self.app.store.products.values()]:
            return None
        self.to_scheduled()
        self.go(S.EV_COLLECTION_STARTED, {}, actor="u_coll")
        return self.go(S.EV_COLLECTION_COMPLETED,
                       {"product_code": self.product_code, "collected_by": "u_coll"},
                       actor="u_coll")

    def to_in_transit(self, temp=5.0):
        self.to_collected()
        prod = self.product
        if any(h.to_org_id == "COLD-CHAIN" for h in prod.handovers):
            return None
        return self.go(S.EV_HANDOVER, {
            "product_code": self.product_code, "from_person_id": "u_coll",
            "to_org_id": "COLD-CHAIN", "to_person_id": "u_car",
            "temp_c": temp, "at_utc": "2026-09-25T03:00:00Z"}, actor="u_car")

    def report_excursion(self, temp=11.0, minutes=30):
        r = self.go(S.EV_TEMP_ALERT, {
            "product_code": self.product_code, "temp_c": temp,
            "duration_minutes": minutes, "detected_at_utc": "2026-09-25T04:00:00Z"},
            actor="u_car")
        prod = next(p for p in self.app.store.products.values()
                    if p.code == self.product_code)
        self.excursion_id = prod.excursions[-1].id
        return r

    def resolve_excursion(self, disposition=S.DISP_CONTINUE, **extra):
        return self.go(S.EV_TEMP_RESOLVED,
                       {"excursion_id": self.excursion_id, "disposition": disposition,
                        **extra}, actor="u_doc")

    def to_delivered(self, temp=6.0):
        self.to_in_transit()
        prod = self.product
        if prod.status in (S.PROD_DELIVERED, S.PROD_INFUSED):
            return None
        if not any(h.to_org_id == "HOSP-SH" for h in prod.handovers):
            self.go(S.EV_HANDOVER, {
                "product_code": self.product_code, "from_person_id": "u_car",
                "to_org_id": "HOSP-SH", "to_person_id": "u_recv",
                "temp_c": temp, "at_utc": "2026-09-25T08:00:00Z"}, actor="u_recv")
        return self.go(S.EV_DELIVERY, {"product_code": self.product_code}, actor="u_recv")

    def to_infused(self):
        self.to_delivered()
        if self.product.status == S.PROD_INFUSED:
            return None
        return self.go(S.EV_INFUSION, {
            "product_code": self.product_code, "physician_id": "u_doc",
            "infused_at": "2026-09-25T10:00:00Z"}, actor="u_doc")

    # ---- 读取 ----
    @property
    def case(self):
        return self.app.store.cases[self.case_id]

    @property
    def product(self):
        return next(p for p in self.app.store.products.values()
                    if p.code == self.product_code)

    def notifications_for(self, role, *, suppressed=None):
        out = [n for n in self.app.store.notifications.values()
               if n.case_id == self.case_id and n.audience_role == role]
        if suppressed is not None:
            out = [n for n in out if n.suppressed == suppressed]
        return out
