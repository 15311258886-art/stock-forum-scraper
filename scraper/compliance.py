"""合规层：硬性隔离用户行为 / 画像字段，仅保留公开内容。

这是程序的“安全网”——即便解析器误抓取了被禁字段，落库前也会被剔除。
与需求一致：不采集用户活跃度、在线状态、互动量、粉丝数、年龄、地域等画像信息。
"""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)

# 明确禁止采集的用户行为 / 画像字段（白名单外的“人”属性一律禁止）
BANNED_FIELDS = {
    # 在线 / 活跃
    "online_status", "is_online", "last_active", "last_active_at",
    "active_days", "online_duration", "login_count", "login_at",
    # 互动 / 社交
    "follower_count", "fans_count", "following_count", "friend_count",
    "post_count", "like_count", "comment_count", "view_count",
    "repost_count", "collect_count", "interaction_count",
    # 等级 / 信用
    "reputation", "level", "vip_level", "credit", "score", "medal",
    # 画像（即便公开也不采集，避免用于用户画像）
    "age", "gender", "birthday", "location", "city", "province", "country",
    "income", "occupation", "education", "device", "os", "browser",
    "registration_date", "register_date", "register_time",
    # 直接标识
    "phone", "email", "real_name", "id_number", "id_card", "ip", "ip_address",
}


def assert_no_banned_fields(record: dict) -> dict:
    """若记录含被禁字段则剔除并告警，返回清洗后的记录。"""
    dropped = [k for k in record if k in BANNED_FIELDS]
    if dropped:
        logger.warning("合规：已剔除被禁字段 %s", dropped)
        record = {k: v for k, v in record.items() if k not in BANNED_FIELDS}
    return record


def filter_public_only(records: Iterable[dict]) -> list[dict]:
    """批量清洗：仅保留公开内容字段。"""
    return [assert_no_banned_fields(r) for r in records]


def contains_pii_risk(text: str) -> bool:
    """粗略检测文本里是否混入疑似私人联系方式（用于日志脱敏提醒）。"""
    import re
    if re.search(r"(1[3-9]\d{9})", text):          # 手机号
        return True
    if re.search(r"[\w.]+@[\w.]+\.\w+", text):     # 邮箱
        return True
    return False
