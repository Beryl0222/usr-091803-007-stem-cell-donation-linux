"""领域枚举与值对象：参与角色、病例/同意/交接状态、隐私分级。

隐私最小化原则：工作人员只在其职责所需范围内看到身份字段。
每个数据字段标注可见角色集合，查询时按当前调用方角色裁剪。
"""

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    """协同中的职责角色。同一自然人可持有多个角色（如协调员）。"""

    COORDINATOR = "coordinator"        # 中华骨髓库协调员：全流程调度、台账
    DONOR = "donor"                    # 捐献者
    DONOR_CENTER = "donor_center"      # 采集医院/分库（供者侧）
    COURIER = "courier"                # 冷链承运方
    RECIPIENT_HOSPITAL = "recipient_hospital"  # 受者医院
    REGULATOR = "regulator"            # 监管/审计：可看完整去标识时间线
    SYSTEM = "system"                  # 外部系统回调（短信网关、HIS 等）


class CaseStatus(str, Enum):
    """病例主状态机。只允许经合法事件向前/向取消/替代迁移。"""

    MATCHED = "matched"                # 非血缘配型成功，待启动
    SCREENING = "screening"            # 供者高分辨/体检筛查中
    CONSENT_PENDING = "consent_pending"  # 等待签署知情同意
    CONSENTED = "consented"            # 已取得有效同意
    SCHEDULED = "scheduled"            # 采集窗口已与各方确认
    COLLECTED = "collected"            # 采集完成，待交接
    IN_TRANSIT = "in_transit"          # 冷链运输中
    INFUSED = "infused"                # 受者已回输
    CANCELLED = "cancelled"            # 取消（撤回/病情等），终态
    SUPERSEDED = "superseded"          # 由替代供者承接，本病例归档，终态


class ConsentAction(str, Enum):
    """同意书版本的语义。撤回不是删除旧同意，而是追加一个新版本。"""

    GRANT = "grant"        # 授予知情同意
    WITHDRAW = "withdraw"  # 撤回同意：最新生效版本决定当前是否有效


class ConsentState(str, Enum):
    NONE = "none"
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"


class HandoverKind(str, Enum):
    """采集物交接环节。"""

    COLLECTION_TO_COURIER = "collection_to_courier"  # 采集医院 -> 承运方
    COURIER_TO_RECIPIENT = "courier_to_recipient"    # 承运方 -> 受者医院


class ShipmentStatus(str, Enum):
    PREPARING = "preparing"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    QUARANTINED = "quarantined"  # 途中温控异常，待处置
    REJECTED = "rejected"        # 受者医院拒收（如温度失超限且不可放行）
    ACCEPTED = "accepted"        # 受者医院核验接收


class ExcursionResult(str, Enum):
    """温控异常的处置结果（监管时间线必须明确）。"""

    OPEN = "open"                 # 待评估
    RELEASED = "released"         # 医学评估后放行
    RELEASED_WITH_NOTE = "released_with_note"  # 附条件放行并记录
    DISCARDED = "discarded"       # 报废，启动替代/重排
    DIVERTED = "diverted"         # 改送（就近中心）


# 采集物/样本类型（外周血造血干细胞为主，亦含可能的骨髓/附带标本）
class ProductType(str, Enum):
    PBSC = "pbsc"        # 外周血造血干细胞
    MARROW = "marrow"    # 骨髓


class NotificationStatus(str, Enum):
    PENDING = "pending"      # 已生成，待发送
    SENT = "sent"            # 已发送
    DELIVERED = "delivered"  # 收到送达回执
    FAILED = "failed"        # 发送失败（可重发）


@dataclass(frozen=True)
class Actor:
    """一个操作/通知主体：角色 + 所属中心 + 标识。"""

    role: Role
    party_id: str
    center_id: str = ""
    tz: str = "Asia/Shanghai"

    def __post_init__(self):
        if not isinstance(self.role, Role):
            object.__setattr__(self, "role", Role(self.role))
        if not self.party_id:
            raise ValueError("Actor 必须有 party_id")


@dataclass
class Identity:
    """敏感身份信息。展示时按字段可见性裁剪，绝不整体下发。"""

    full_name: str = ""
    national_id_masked: str = ""   # 仅存掩码，明文不入库
    contact_phone: str = ""
    medical_record_no: str = ""
    address: str = ""
    extras: dict = field(default_factory=dict)
