"""应用装配：时钟、审计链、存储、目录、工作流，以及演示用种子数据。"""

from .audit import AuditLog
from .clock import Clock
from .identity import (
    CARRIER, COLLECTION_HOSPITAL, REGISTRY, TRANSPLANT_HOSPITAL,
    CARRIER_HANDLER, COLLECTION_STAFF, COORDINATOR, DONOR_AFFAIRS,
    RECEIVING_STAFF, REGULATOR, TRANSPLANT_PHYSICIAN,
    Channel, Directory, Org, Person,
)
from .store import Store
from .workflow import Workflow


class App:
    def __init__(self, clock: Clock | None = None):
        self.clock = clock or Clock()
        self.audit = AuditLog()
        self.store = Store()
        self.directory = Directory()
        self.workflow = Workflow(self.store, self.directory, self.clock, self.audit)


def seed_demo(app: App) -> dict:
    """一套横跨乌鲁木齐（分库）、北京（采集）、上海（受者）的演示目录。

    三地时区不同：窗口一律按各机构所在地时间解释。
    """
    d = app.directory
    org_reg = d.add_org(Org("REG-XJ", "中华骨髓库新疆分库", REGISTRY, "Asia/Urumqi", "0991-0000000"))
    org_coll = d.add_org(Org("HOSP-BJ", "北京采集医院", COLLECTION_HOSPITAL, "Asia/Shanghai", "010-0000000"))
    org_tx = d.add_org(Org("HOSP-SH", "上海受者医院", TRANSPLANT_HOSPITAL, "Asia/Shanghai", "021-0000000"))
    org_car = d.add_org(Org("COLD-CHAIN", "神州冷链承运", CARRIER, "Asia/Shanghai", "400-0000000"))

    people = {}
    def add(pid, name, title, roles, org, token, channels):
        p = Person(pid, name, title, tuple(roles), org,
                   [Channel(k, v) for k, v in channels], token)
        d.add_person(p)
        people[pid] = p
        return p

    add("u_coord", "古丽娜（协调员）", "新疆分库协调员", [COORDINATOR], "REG-XJ", "tk_coord",
        [("sms", "13900000001"), ("email", "coord@reg-xj.example")])
    add("u_daff", "刘联络", "捐献者联络员", [DONOR_AFFAIRS], "REG-XJ", "tk_daff",
        [("sms", "13900000002")])
    add("u_coll", "王护士", "采集科护士", [COLLECTION_STAFF], "HOSP-BJ", "tk_coll",
        [("sms", "13800000003")])
    add("u_car", "赵押运", "冷链押运员", [CARRIER_HANDLER], "COLD-CHAIN", "tk_car",
        [("sms", "13700000004")])
    add("u_recv", "孙接收", "输血科接收员", [RECEIVING_STAFF], "HOSP-SH", "tk_recv",
        [("sms", "13600000005")])
    add("u_doc", "李医生", "血液科主治医生", [TRANSPLANT_PHYSICIAN], "HOSP-SH", "tk_doc",
        [("sms", "13500000006"), ("email", "doc@hosp-sh.example")])
    add("u_reg", "周督查", "卫生监管质控", [REGULATOR], "REG-XJ", "tk_reg",
        [("email", "reg@example.gov")])

    # 供者本人不是系统操作账号，但以人员身份存在（联络员代表其交互）
    add("donor-77", "艾（供者）", "志愿捐献者", [], "REG-XJ", "", [])

    return {"orgs": [org_reg, org_coll, org_tx, org_car], "people": people}
