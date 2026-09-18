"""敏感身份按职责最小化展示。

非血缘捐献要求"供-患双盲"：受者医院与承运方不得获得捐献者身份，
捐献者也不得获得受者身份。协调员与供者侧医院在医疗必需范围内可见。
本模块集中维护字段级可见性矩阵，任何对外投影都必须经过 redact。
"""

from .models import Role


# 捐献者身份字段 -> 可见角色
DONOR_FIELD_VISIBILITY = {
    "full_name":         {Role.COORDINATOR, Role.DONOR_CENTER, Role.DONOR},
    "national_id_masked":{Role.COORDINATOR, Role.DONOR_CENTER},
    "contact_phone":     {Role.COORDINATOR, Role.DONOR_CENTER, Role.DONOR},
    "medical_record_no": {Role.COORDINATOR, Role.DONOR_CENTER},
    "address":           {Role.COORDINATOR},
    "extras":            {Role.COORDINATOR},
}

# 受者身份字段 -> 可见角色
RECIPIENT_FIELD_VISIBILITY = {
    "full_name":         {Role.COORDINATOR, Role.RECIPIENT_HOSPITAL},
    "national_id_masked":{Role.COORDINATOR, Role.RECIPIENT_HOSPITAL},
    "contact_phone":     {Role.COORDINATOR, Role.RECIPIENT_HOSPITAL},
    "medical_record_no": {Role.COORDINATOR, Role.RECIPIENT_HOSPITAL},
    "address":           {Role.COORDINATOR},
    "extras":            {Role.COORDINATOR},
}


def _scalar(value, field_name, viewer, matrix):
    if viewer in matrix.get(field_name, set()):
        return value
    # 无可见性：一律不返回。掩码不用于"越权后部分展示"——
    # 供-患双盲方连号段也不应获得。
    return None


def project_identity(identity, viewer: Role, matrix) -> dict:
    """按 viewer 角色裁剪身份对象。identity 为 models.Identity 或 dict。"""
    data = identity if isinstance(identity, dict) else {
        "full_name": identity.full_name,
        "national_id_masked": identity.national_id_masked,
        "contact_phone": identity.contact_phone,
        "medical_record_no": identity.medical_record_no,
        "address": identity.address,
        "extras": identity.extras,
    }
    out = {}
    for name, value in data.items():
        visible = _scalar(value, name, viewer, matrix)
        if visible is not None:
            out[name] = visible
    return out


def donor_pseudonym(case_id: str) -> str:
    """对供-患双盲方展示的供者代号（不含任何身份信息）。"""
    return f"DONOR-{case_id[:8].upper()}"


def recipient_pseudonym(case_id: str) -> str:
    return f"RECIP-{case_id[:8].upper()}"
