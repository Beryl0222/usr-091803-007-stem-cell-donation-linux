"""按职责最小化展示（字段级 RBAC 脱敏）。"""

import unittest

from coord.projection import project_case, project_product, visible_notifications
from coord.testsupport import Driver, build_app


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.d = Driver(self.app)
        self.d.to_delivered()

    def _case(self, viewer_id):
        return project_case(self.d.case, self.app.directory.people[viewer_id],
                            self.app.directory, self.app.store)

    def test_carrier_sees_no_donor_identity_or_hla(self):
        view = self._case("u_car")
        self.assertTrue(view["donor"]["masked"])
        self.assertIsNone(view["donor"]["person_id"])
        self.assertNotIn("hla_summary", view)
        for c in view["consent_versions"]:
            self.assertIsNone(c["document_ref"])

    def test_transplant_physician_sees_hla_but_not_donor_identity(self):
        view = self._case("u_doc")
        self.assertTrue(view["donor"]["masked"])
        self.assertEqual(view["hla_summary"], "HLA 10/10")
        # 医生不见同意书原件编号
        for c in view["consent_versions"]:
            self.assertIsNone(c["document_ref"])

    def test_donor_affairs_sees_donor_but_no_hla(self):
        view = self._case("u_daff")
        self.assertFalse(view["donor"]["masked"])
        self.assertEqual(view["donor"]["name"], "艾（供者）")
        self.assertNotIn("hla_summary", view)
        self.assertEqual(view["consent_versions"][0]["document_ref"], "CONS-T-001")

    def test_coordinator_and_regulator_see_fullest(self):
        for vid in ("u_coord", "u_reg"):
            view = self._case(vid)
            self.assertFalse(view["donor"]["masked"])
            self.assertEqual(view["hla_summary"], "HLA 10/10")
            self.assertEqual(view["consent_versions"][0]["document_ref"], "CONS-T-001")

    def test_product_view_keps_worker_names_but_no_donor(self):
        prod = self.d.product
        car_view = project_product(prod, self.app.directory.people["u_car"],
                                   self.app.directory)
        self.assertEqual(car_view["donor_case_ref"], prod.donor_case_ref)
        self.assertIsNone(car_view["case_id"])  # 承运视角不暴露病例归属
        names = {(h["from_person_name"], h["to_person_name"]) for h in car_view["handovers"]}
        self.assertIn(("王护士", "赵押运"), names)

    def test_notification_visibility_is_scoped(self):
        mine = visible_notifications(self.d.case_id,
                                     self.app.directory.people["u_car"], self.app.store)
        self.assertTrue(mine)
        self.assertTrue(all(n.recipient_person_id == "u_car" for n in mine))
        # 协调员可见病例下全部通知
        alln = visible_notifications(self.d.case_id,
                                     self.app.directory.people["u_coord"], self.app.store)
        self.assertGreater(len(alln), len(mine))


if __name__ == "__main__":
    unittest.main()
