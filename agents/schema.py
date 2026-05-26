"""所有 Agent 的结构化输出类型（对话 + 记忆整理）。"""

from typing import Annotated

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
)

from shared.config import MAX_CHOICE_CHARS, MAX_EPISODE_KEYWORDS

ChoiceText = Annotated[str, Field(max_length=MAX_CHOICE_CHARS)]


# ---------------------------------------------------------------------------
# 对话类
# ---------------------------------------------------------------------------


class CharacterOutput(BaseModel):
    content: str
    memory: str
    status: dict[str, str] = Field(default_factory=dict)
    triggered: list[str] = Field(default_factory=list)
    add_event: list[str] = Field(default_factory=list)


class NarratorStatus(BaseModel):
    """narrator status 的强类型字段，防止 LLM 将多个字段混写成单个 key 的值导致 JSON 非法。"""

    场景: str = ""
    角色位置: str = ""
    当前时间: str = ""
    叙事焦点: str = ""
    最近世界事件: str = ""


class NewCharacterRequest(BaseModel):
    """上游请求动态生成新角色时的最小锚点。"""

    name_hint: str = ""
    background_hint: str
    initial_location: str = ""


class NarratorOutput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    targets: list[str]
    date: str
    time: str
    location: str
    present_characters: dict[str, str]
    scene_description: str
    new_characters: list[NewCharacterRequest] = Field(default_factory=list)

    @field_validator("targets", mode="before")
    @classmethod
    def trim_targets(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [target.strip() if isinstance(target, str) else target for target in value]

    @field_validator("present_characters", mode="before")
    @classmethod
    def clean_present_characters(cls, value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        cleaned = {
            str(name).strip(): str(description).strip()
            for name, description in value.items()
            if str(name).strip() and str(description).strip()
        }
        if not cleaned:
            raise ValueError("present_characters cannot be empty")
        return cleaned

    @field_validator("date", "time", "location", "scene_description")
    @classmethod
    def ensure_scene_field_not_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("field cannot be empty")
        return value

class NewCharacterProfile(BaseModel):
    """character_factory agent 的结构化输出：一次返回完整骨架种子。

    character_id 由 character_factory 生成，是最终目录名 / agent 标识。
    display_name 是最终展示名，会写入 soul.md / status.md。
    soul 分成六段（identity / goal / past / habits / reactions / voice），和模板对齐。
    """

    character_id: str = Field(validation_alias=AliasChoices("character_id", "agent_id"))
    display_name: str
    identity: str
    goal: str
    past: list[str] = Field(default_factory=list)
    habits: list[str] = Field(default_factory=list)
    reactions: list[str] = Field(default_factory=list)
    voice: list[str] = Field(default_factory=list)
    initial_status: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "character_id",
        "display_name",
        "identity",
        "goal",
        mode="before",
    )
    @classmethod
    def trim_creation_fields(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("past", "habits", "reactions", "voice", mode="before")
    @classmethod
    def trim_list_items(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [item.strip() if isinstance(item, str) else item for item in value]

    @field_validator("character_id", "display_name", "identity", "goal")
    @classmethod
    def ensure_creation_fields_not_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("field cannot be empty")
        return value

    @field_validator("character_id")
    @classmethod
    def ensure_character_id_is_lower_ascii_letters(cls, value: str) -> str:
        if not value.isascii() or not value.isalpha() or value != value.lower():
            raise ValueError("character_id must contain only lowercase ASCII letters")
        return value

    @field_validator("identity")
    @classmethod
    def normalize_identity_to_single_line(cls, value: str) -> str:
        return " ".join(value.split())


class StateUpdaterOutput(BaseModel):
    status: NarratorStatus = Field(default_factory=NarratorStatus)
    triggered: list[str] = Field(default_factory=list)
    add_event: list[str] = Field(default_factory=list)
    world_schedule_update: str = ""
    triggered_world_events: list[str] = Field(default_factory=list)


class ChoicesOutput(BaseModel):
    choices: list[ChoiceText]

    @field_validator("choices", mode="before")
    @classmethod
    def trim_choice_lengths(cls, choices: object) -> object:
        if not isinstance(choices, list):
            return choices
        return [
            choice.strip()[:MAX_CHOICE_CHARS] if isinstance(choice, str) else choice
            for choice in choices
        ]


def _strip_dict_keys(value: object) -> object:
    """Strip whitespace from dict keys; shared by patch output types."""
    if not isinstance(value, dict):
        return {}
    return {
        str(k).strip(): item
        for k, item in value.items()
        if str(k).strip()
    }


# ---------------------------------------------------------------------------
# 记忆整理类
# ---------------------------------------------------------------------------


class EpisodeMemoryBlock(BaseModel):
    """EpisodeMemoryGenerator 输出的单条长期记忆事件。

    memory_owner 与 raw_dialogue 由整理流程注入，不交给 LLM 判断。
    raw_dialogue 只作为可回溯的源对话 metadata，不参与向量索引。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    date: str
    time: str
    location: str
    participants: str
    keywords: list[str] = Field(default_factory=list)
    importance: int = 3
    content: str
    title: str = ""
    raw_dialogue: str = ""

    @field_validator("keywords", mode="before")
    @classmethod
    def clean_keywords(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [s for item in value if (s := str(item).strip())][:MAX_EPISODE_KEYWORDS]

    @field_validator("importance", mode="before")
    @classmethod
    def clamp_importance(cls, value: object) -> int:
        try:
            return max(1, min(5, int(value)))
        except (TypeError, ValueError):
            return 3


class EpisodeClosureBoundary(BaseModel):
    """单个主题边界：旧主题最后停留的 turn 号 + 主题/原因元信息。"""

    end_turn: int
    old_theme: str = ""
    new_theme: str = ""
    reason: str = ""


class EpisodeClosureOutput(RootModel[dict[str, list[EpisodeClosureBoundary]]]):
    """key 是 history 里的角色 agent_name，value 是该角色在 history 中检测到的所有主题边界。

    空数组表示该角色无边界。RootModel 让 dict 直接做根 schema。访问内部 dict 用 `.root`。
    消费方通常取每个数组里 end_turn 最大的边界作为本轮可归并的闭合点。
    """


class UnderstandingEntry(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    subject: str = ""
    keywords: list[str] = Field(default_factory=list)
    content: str = ""


class UnderstandingPatchOutput(BaseModel):
    add: list[UnderstandingEntry] = Field(default_factory=list)
    update: dict[str, UnderstandingEntry] = Field(default_factory=dict)

    @field_validator("update", mode="before")
    @classmethod
    def clean_update_ids(cls, value: object) -> object:
        return _strip_dict_keys(value)


