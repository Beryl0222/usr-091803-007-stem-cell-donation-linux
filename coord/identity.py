"""机构、人员、角色目录。

非血缘造血干细胞捐献遵循“双盲”原则：供、患双方互不知晓身份。
角色决定了其可见数据的最小集合（见 projection.py）。
"""

from dataclasses import dataclass, field

from .errors import AuthError, ValidationError

# ---- 机构类型 ----
REGISTRY = "registry"                 # 骨髓库管理中心/分库
COLLECTION_HOSPITAL = "collection_hospital"  # 采集医院
TRANSPLANT_HOSPITAL = "transplant_hospital"  # 受者医院
CARRIER = "carrier"                   # 冷链承运方

# ---- 角色 ----
COORDINATOR = "coordinator"           # 分库协调员（全流程协同，可见双方必要信息）
DONOR_AFFAIRS = "donor_affairs"       # 捐献者联络员（供者侧，可见供者 PII，不见受者身份）
COLLECTION_STAFF = "collection_staff"  # 采集医院工作人员
CARRIER_HANDLER = "carrier_handler"   # 冷链交接/押运员（只认物，不认人）
RECEIVING_STAFF = "receiving_staff"   # 受者医院接收人员
TRANSPLANT_PHYSICIAN = "transplant_physician"  # 受者主治医生
REGULATOR = "regulator"               # 监管/质控复核（可看全量时间线，访问被记录）


@dataclass
class Org:
    id: str
    name: str
    kind: str
    tz: str                       # 机构所在地时区，窗口在本地解释
    contact: str = ""

    def ref(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "tz": self.tz}


@dataclass
class Channel:
    kind: str                     # sms / email / callback（中心回调）
    address: str

    def to_dict(self):
        return {"kind": self.kind, "address": self.address}


@dataclass
class Person:
    id: str
    name: str
    title: str
    roles: tuple
    org_id: str
    channels: list = field(default_factory=list)
    token: str = ""

    def has(self, role: str) -> bool:
        return role in self.roles

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "roles": list(self.roles),
            "org_id": self.org_id,
            "channels": [c.to_dict() for c in self.channels],
        }


class Directory:
    def __init__(self):
        self.orgs = {}
        self.people = {}
        self._tokens = {}

    def add_org(self, org: Org) -> Org:
        if org.id in self.orgs:
            raise ValidationError(f"机构已存在: {org.id}")
        # 提前校验时区
        from .clock import get_zone
        get_zone(org.tz)
        self.orgs[org.id] = org
        return org

    def add_person(self, person: Person) -> Person:
        if person.id in self.people:
            raise ValidationError(f"人员已存在: {person.id}")
        if person.org_id not in self.orgs:
            raise ValidationError(f"未知机构: {person.org_id}")
        person.token = person.token or f"tk_{person.id}"
        self.people[person.id] = person
        if person.token:
            self._tokens[person.token] = person.id
        return person

    def get_org(self, org_id: str) -> Org:
        return self.orgs.get(org_id)

    def require_org(self, org_id: str) -> Org:
        org = self.get_org(org_id)
        if not org:
            raise ValidationError(f"未知机构: {org_id}")
        return org

    def org_tz(self, org_id: str) -> str:
        org = self.require_org(org_id)
        return org.tz

    def staff_of(self, org_id: str, role: str | None = None) -> list:
        out = [p for p in self.people.values() if p.org_id == org_id]
        if role:
            out = [p for p in out if p.has(role)]
        return out

    def authenticate(self, token: str) -> Person:
        if not token:
            raise AuthError("缺少访问令牌（X-Staff-Token）")
        pid = self._tokens.get(token)
        if not pid:
            raise AuthError("令牌无效")
        return self.people[pid]
