# -*- coding: utf-8 -*-
"""onebot.py — OneBot v11 报文解析与构造（纯函数，无依赖）

NapCat 的 messagePostFormat 是 "array"：消息本体是分段数组，例如
    [{"type":"text","data":{"text":"你好"}},
     {"type":"face","data":{"id":178}},
     {"type":"at","data":{"qq":"123"}}]
本模块负责：事件解析 → 转成给 LLM 看的文本 / 构造回复分段 → 发送动作 JSON。
"""

import json
from pathlib import Path


def parse_event(raw: str):
    """把收到的 JSON 报文解析成事件字典；非 JSON 时返回 None。"""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def segments_to_text(segments, self_id=None) -> str:
    """分段数组 → 给 LLM 看的纯文本。图片/语音等内容用占位符表示。"""
    parts = []
    for seg in segments or []:
        t = seg.get("type", "")
        data = seg.get("data") or {}
        if t == "text":
            parts.append(data.get("text", ""))
        elif t == "at":
            qq = str(data.get("qq", ""))
            parts.append("@我" if (self_id and qq == str(self_id)) else f"@{qq}")
        elif t == "image":
            parts.append("[图片]")
        elif t == "face":
            parts.append(f"[表情{data.get('id', '')}]")
        elif t == "record":
            parts.append("[语音]")
        elif t == "video":
            parts.append("[视频]")
        elif t == "reply":
            parts.append("[引用回复]")
        else:
            parts.append(f"[{t}]")
    return "".join(parts)


def forward_nodes_to_text(nodes, max_chars: int = 800) -> str:
    """合并转发消息的节点列表 → 纯文本（"昵称：内容"逐行，可嵌套）。

    兼容两种节点形态：
      - OneBot get_forward_msg 返回的节点：{"sender": {...}, "content"/"message": ...}
      - NapCat 数组格式 inline content 里的节点：{"type": "node", "data": {...}}
    超过 max_chars 截断（保护提示词上下文）。
    """
    lines = []
    total = 0

    def walk(node_list, indent=""):
        nonlocal total
        for node in node_list or []:
            if not isinstance(node, dict):
                continue
            data = node.get("data") if node.get("type") == "node" else node
            if not isinstance(data, dict):
                data = {"message": node.get("message")}
            sender = (
                (data.get("sender") or {}).get("nickname")
                or data.get("name")
                or str(data.get("uin", "") or "")
            )
            inner = data.get("content")
            if inner is None:
                inner = data.get("message")
            if isinstance(inner, list):
                fwd_segs = [s for s in inner
                            if isinstance(s, dict) and s.get("type") == "forward"]
                if fwd_segs:
                    # 嵌套合并转发：递归展开
                    walk((fwd_segs[0].get("data") or {}).get("content"), indent + "  ")
                    continue
                text = segments_to_text(inner)
            elif isinstance(inner, str):
                text = inner
            else:
                continue
            line = f"{indent}{sender}：{text}".strip()
            if not line:
                continue
            lines.append(line)
            total += len(line)

    walk(nodes)
    out = "\n".join(lines)
    if total > max_chars:
        out = out[:max_chars] + "……"
    return out


def build_image_segment(path) -> dict:
    """本地图片 → OneBot image 分段（NapCat 支持 file:// 绝对路径）。"""
    p = Path(path).resolve().as_posix()
    return {"type": "image", "data": {"file": f"file:///{p}"}}


def build_reply_segments(text: str, image_path=None) -> list:
    """回复文本（可带图）→ 分段数组。"""
    segs = [{"type": "text", "data": {"text": text}}]
    if image_path:
        segs.append(build_image_segment(image_path))
    return segs


def build_send_action(event: dict, segments: list, mention: bool = False) -> dict:
    """构造 send_msg 动作。

    群聊默认【不 @】任何人（回复不上身，像真人接话）；
    mention=True 时才 @ 消息发送者（预留给「主动发起话题」功能）。
    """
    if event.get("message_type") == "group":
        if mention:
            at = {"type": "at", "data": {"qq": str(event.get("user_id", ""))}}
            blank = {"type": "text", "data": {"text": " "}}
            segments = [at, blank] + segments
        return {
            "action": "send_msg",
            "params": {
                "message_type": "group",
                "group_id": event["group_id"],
                "message": segments,
            },
        }
    return {
        "action": "send_msg",
        "params": {
            "message_type": "private",
            "user_id": event["user_id"],
            "message": segments,
        },
    }


# ---- 请求事件（好友申请 / 入群申请 / 入群邀请）----

_REQUEST_KIND = {"add": "group_add", "invite": "group_invite"}


def parse_request_event(event: dict):
    """request 事件 → 归一化信息字典；不支持的请求类型返回 None。

    OneBot v11 请求事件：
        request_type=friend                好友申请（user_id、comment、flag）
        request_type=group, sub_type=add   入群申请（user_id、group_id、comment、flag）
        request_type=group, sub_type=invite 入群邀请（user_id 为邀请人、group_id、flag）
    """
    if event.get("post_type") != "request":
        return None
    rtype = event.get("request_type")
    if rtype == "friend":
        kind = "friend"
        sub_type = ""
    elif rtype == "group":
        sub_type = event.get("sub_type", "")
        kind = _REQUEST_KIND.get(sub_type)
        if kind is None:
            return None
    else:
        return None
    return {
        "kind": kind,
        "sub_type": sub_type,
        "user_id": event.get("user_id"),
        "group_id": event.get("group_id"),
        "comment": (event.get("comment") or "").strip(),
        "flag": event.get("flag", ""),
    }


def build_set_request_action(info: dict, approve: bool, reason: str = "") -> dict:
    """同意/拒绝请求 → set_friend_add_request / set_group_add_request 动作。

    拒绝理由（reason）只会放进群请求的 reason 字段（OneBot 会转达给申请人）；
    好友请求没有该字段，拒绝时静默丢弃。
    """
    if info["kind"] == "friend":
        return {
            "action": "set_friend_add_request",
            "params": {"flag": info["flag"], "approve": bool(approve)},
        }
    params = {
        "flag": info["flag"],
        "sub_type": info["sub_type"],
        "approve": bool(approve),
    }
    if not approve and reason:
        params["reason"] = reason
    return {"action": "set_group_add_request", "params": params}


def build_send_qzone_action(content: str, images=None, ugc_right: int = 1) -> dict:
    """构造 send_qzone_msg 动作（NapCat 扩展接口：发表 QQ 空间说说）。

    ugc_right（查看权限）：1 所有人可见 / 4 好友可见 / 16 部分好友可见 /
    64 仅自己可见 / 128 部分好友不可见。images 支持 file://、http(s)://、
    base64://（NapCat 自动上传）。
    """
    params = {"content": content, "ugc_right": int(ugc_right)}
    if images:
        params["images"] = list(images)
    return {"action": "send_qzone_msg", "params": params}
