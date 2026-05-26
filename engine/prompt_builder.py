"""对话运行时的 prompt 构建（角色 / narrator user message、history 窗口）。"""

from __future__ import annotations

import re

from prompts.runtime_prompts import CHARACTER, NARRATOR
from shared.config import (
    HISTORY_HIGH,
    HISTORY_LOW,
    get_agent_names,
)
from shared.narrator_output import raw_message_text
from shared.text_utils import extract_identity, get_display_name, role_to_speaker
from storage.agent_files import (
    extract_player_name,
    get_allowed_fields,
    read_agent_file,
    read_player_name,
    read_sidecar_json,
    write_sidecar_json,
)
from log_config.routing import routing_logger


# ---------------------------------------------------------------------------
# 对话历史窗口（原 engine/history.py）
# ---------------------------------------------------------------------------


def _msg_turn(msg: dict) -> int:
    return int(msg.get("turn") or 0)


def _apply_high_low_watermark(
    visible: list[dict],
    anchor_turn: int,
    high: int,
    low: int,
) -> tuple[int, list[dict], bool]:
    """按 turn 号锚定的高低水位缓冲，以 distinct turn 数为截断单位。

    返回 (新锚点 turn, 保留消息, 是否触发截断)。
    """
    if not visible:
        return anchor_turn, [], False

    kept = [m for m in visible if _msg_turn(m) >= anchor_turn]
    if not kept:
        kept = visible
        anchor_turn = _msg_turn(kept[0])

    was_truncated = False
    distinct_turns = sorted({_msg_turn(m) for m in kept})
    if len(distinct_turns) > high:
        kept_turns = set(distinct_turns[-low:])
        kept = [m for m in kept if _msg_turn(m) in kept_turns]
        anchor_turn = min(kept_turns)
        was_truncated = True

    return anchor_turn, kept, was_truncated


def _get_windowed_visible_messages(
    agent_name: str, raw_messages: list[dict]
) -> tuple[list[dict], bool]:
    """按 agent 维度应用高低水位窗口，并持久化窗口起点。返回 (消息列表, 是否触发截断)。"""
    if not raw_messages:
        return [], False

    if agent_name != "narrator":
        visible = [m for m in raw_messages if agent_name in m.get("visible_to", [])]
    else:
        visible = list(raw_messages)

    if not visible:
        return [], False

    sidecar = read_sidecar_json(agent_name, ".history_window_state.json")
    anchor_turn = max(0, int(sidecar.get("start_turn", 0)))
    new_anchor, kept, was_truncated = _apply_high_low_watermark(
        visible, anchor_turn, HISTORY_HIGH, HISTORY_LOW
    )
    write_sidecar_json(
        agent_name,
        ".history_window_state.json",
        {"start_turn": new_anchor},
    )
    return kept, was_truncated


def build_history_transcript(
    agent_name: str,
    raw_messages: list[dict],
    *,
    inject_turn_markers: bool = False,
) -> tuple[str, bool]:
    """将 JSONL 原始消息转为历史文本。

    高低水位窗口与可见性过滤仍照常应用。
    inject_turn_markers=True 时在每条前加 `[turn=N]` 前缀，供 detector / 整理输入使用。
    返回 (文本, 是否触发截断)。
    """
    visible, was_truncated = _get_windowed_visible_messages(agent_name, raw_messages)

    if not visible:
        return "", False

    lines: list[str] = []
    for idx, msg in enumerate(visible):
        role = msg.get("role", "unknown")
        content = re.sub(r"\n+", "\n", raw_message_text(msg))
        if not content:
            continue
        prefix = ""
        if inject_turn_markers:
            turn = msg.get("turn")
            if isinstance(turn, int) and turn > 0:
                prefix = f"[turn={turn}] "
        lines.append(f"{prefix}{role_to_speaker(role)}: {content}")

    return "\n\n".join(lines), was_truncated


# ---------------------------------------------------------------------------
# 对话 prompt 构建
# ---------------------------------------------------------------------------


def build_system_prompt(agent_name: str, soul_content: str) -> str:
    """构建 system prompt（仅包含稳定的身份与规则部分）。"""
    if agent_name == "narrator":
        return NARRATOR.format(soul=soul_content)

    excluded_status_fields = {"打算"}
    status_fields = "、".join(
        field
        for field in get_allowed_fields(agent_name, "status")
        if field not in excluded_status_fields
    )
    display_name = get_display_name(agent_name, soul_content)

    return CHARACTER.format(
        agent_name=agent_name,
        display_name=display_name,
        soul=soul_content,
        status_fields=status_fields,
    )


def build_characters_block(tag: str = "characters") -> str:
    """列出所有主要角色的 id / 显示名 / identity。

    narrator user message 用 tag='fields'，state_updater 用默认 tag='characters'。
    """
    rows: list[str] = []
    for name in get_agent_names(include_narrator=False):
        soul = read_agent_file(name, "soul.md")
        display = get_display_name(name, soul)
        identity = extract_identity(soul)
        suffix = f"｜{identity}" if identity else ""
        rows.append(f"- {name}: {display}{suffix}")
    if not rows:
        return ""
    return f"<{tag}>\n" + "\n".join(rows) + f"\n</{tag}>"


def build_player_block(latest_user_input: str) -> str:
    """Expose the player's display name to narrator prompts when known."""
    player_name = read_player_name() or extract_player_name(latest_user_input)
    if not player_name:
        return ""
    return f"<player>\ndisplay_name: {player_name}\n</player>"


def build_user_message(
    agent_name: str,
    latest_user_input: str,
    memory_prefix: str,
    raw_messages: list[dict] | None = None,
    understandings_prefix: str = "",
    observation_mode: bool = False,
) -> tuple[str, bool]:
    """构建单条大 user message，按稳定度排序上下文。返回 (消息文本, 是否触发历史截断)。"""
    parts: list[str] = []

    is_narrator = agent_name == "narrator"
    history, was_truncated = build_history_transcript(agent_name, raw_messages or [])
    status_content = read_agent_file(agent_name, "status.md")

    parts.append(build_player_block(latest_user_input) if is_narrator else "")
    parts.append(build_characters_block(tag="fields") if is_narrator else "")
    parts.append(f"最近对话历史:\n\n{history}" if history else "")
    parts.append(
        f"<status>\n{status_content.strip()}\n</status>" if status_content.strip() else ""
    )
    parts.append(memory_prefix if memory_prefix else "")
    parts.append(understandings_prefix if understandings_prefix else "")
    if not observation_mode:
        parts.append(f"玩家新消息：{latest_user_input}")
    elif is_narrator:
        parts.append(f"玩家想旁观：{latest_user_input}")

    return "\n\n---\n\n".join(part for part in parts if part), was_truncated
