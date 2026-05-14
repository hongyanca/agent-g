"""统一创建并缓存所有 Agent。"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from agents.schema import (
    CharacterOutput,
    ChoicesOutput,
    EpisodeClosureOutput,
    EpisodeMemoryBlock,
    NarratorOutput,
    NewCharacterProfile,
    StateUpdaterOutput,
    UnderstandingPatchOutput,
)
from engine.prompt_builder import build_system_prompt
from llm.config import get_llm_config
from prompts.consolidation_prompts import (
    EPISODE_CLOSURE_DETECTOR,
    EPISODE_MEMORY_GENERATOR,
    UNDERSTANDING_PATCH,
)
from prompts.runtime_prompts import CHOICES, NARRATOR_OBSERVATION, STATE_UPDATER
from prompts.worldgen_prompts import CHARACTER_FACTORY
from shared.config import (
    CONSOLIDATION_MAX_TOKENS,
    CONSOLIDATION_TEMPERATURE,
    STATE_UPDATER_OUTPUT_RETRIES,
    STATE_UPDATER_TEMPERATURE,
    get_agent_names,
)
from storage.agent_files import read_agent_file

ConversationAgent = Agent[None, CharacterOutput | NarratorOutput]
StructuredAgent = Agent[None, object]

_conversation_agents: dict[str, ConversationAgent] = {}
_observation_narrator_agent: ConversationAgent | None = None
_choices_agent: Agent[None, ChoicesOutput] | None = None
_state_updater_agent: Agent[None, StateUpdaterOutput] | None = None
_character_factory_agent: Agent[None, NewCharacterProfile] | None = None
_consolidation_agents: dict[str, StructuredAgent] = {}

_CHANNEL_PREFIX_RE = re.compile(r"^\s*<\|channel\>.*?<channel\|>\s*", re.DOTALL)


_GOOGLE_SAFETY_OFF = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
]

_MakeModel = Callable[[dict], Model]
_MakeSettings = Callable[[dict], ModelSettings]

_default_make_settings: _MakeSettings = lambda c: ModelSettings(temperature=c["temperature"])

# 新增 provider：加一行 tuple 即可
_PROVIDER_REGISTRY: dict[str, tuple[_MakeModel, _MakeSettings]] = {
    "openai": (
        lambda c: OpenAIChatModel(
            c["model_id"],
            provider=OpenAIProvider(base_url=c["api_url"] or None, api_key=c["api_key"]),
        ),
        _default_make_settings,
    ),
    "google": (
        lambda c: GoogleModel(
            c["model_id"],
            provider=GoogleProvider(api_key=c["api_key"]),
        ),
        lambda c: GoogleModelSettings(
            temperature=c["temperature"],
            google_safety_settings=_GOOGLE_SAFETY_OFF,
        ),
    ),
    "anthropic": (
        lambda c: AnthropicModel(
            c["model_id"],
            provider=AnthropicProvider(api_key=c["api_key"]),
        ),
        _default_make_settings,
    ),
    "deepseek": (
        lambda c: OpenAIChatModel(
            c["model_id"],
            provider=DeepSeekProvider(api_key=c["api_key"]),
        ),
        _default_make_settings,
    ),
}


def _make_sdk_model(config: dict) -> Model:
    entry = _PROVIDER_REGISTRY.get(config["provider"])
    if entry is None:
        raise ValueError(
            f"Unsupported LLM_PROVIDER: {config['provider']!r}. "
            f"Supported: {list(_PROVIDER_REGISTRY)}"
        )
    make_model, _ = entry
    return make_model(config)


def _build_model_settings(
    config: dict,
    *,
    max_tokens: int | None = None,
) -> ModelSettings:
    _, make_settings = _PROVIDER_REGISTRY[config["provider"]]
    settings = make_settings(config)
    if max_tokens is not None:
        settings["max_tokens"] = max_tokens
    return settings


def strip_leading_channel_prefix(data: str) -> str:
    """Remove local LLM channel preambles that appear before prompted JSON output."""
    return _CHANNEL_PREFIX_RE.sub("", data, count=1)


class _ChannelPrefixStrippingProcessor:
    """Adapter that cleans prompted text before pydantic-ai validates the JSON schema."""

    def __init__(self, processor: Any) -> None:
        self._processor = processor
        self.object_def = processor.object_def

    async def process(
        self,
        data: str | dict[str, Any] | None,
        *,
        run_context: Any,
        allow_partial: bool = False,
        wrap_validation_errors: bool = True,
    ) -> Any:
        if isinstance(data, str):
            data = strip_leading_channel_prefix(data)
        return await self._processor.process(
            data,
            run_context=run_context,
            allow_partial=allow_partial,
            wrap_validation_errors=wrap_validation_errors,
        )


def _install_prompted_output_cleanup(agent: StructuredAgent) -> None:
    """Patch PromptedOutput processors so provider-specific channel prefixes do not break JSON parsing."""
    output_schema = agent._output_schema
    processor = getattr(output_schema, "processor", None)
    if processor is None or isinstance(processor, _ChannelPrefixStrippingProcessor):
        return

    cleaned = _ChannelPrefixStrippingProcessor(processor)
    output_schema.processor = cleaned
    output_schema.text_processor = cleaned


def _build_agent(
    *,
    name: str,
    instructions: str,
    config: dict,
    output_type: type,
    max_tokens: int | None = None,
    output_retries: int | None = None,
) -> StructuredAgent:
    agent = Agent(
        _make_sdk_model(config),
        name=name,
        instructions=instructions,
        model_settings=_build_model_settings(config, max_tokens=max_tokens),
        output_type=PromptedOutput(output_type),
        output_retries=output_retries,
    )
    _install_prompted_output_cleanup(agent)
    return agent


def initialize_conversation_agents() -> None:
    for name in get_agent_names(include_narrator=True):
        reload_conversation_agent(name)


def reload_conversation_agent(name: str) -> None:
    global _choices_agent, _observation_narrator_agent

    soul = read_agent_file(name, "soul.md")
    config = get_llm_config()
    output_type = NarratorOutput if name == "narrator" else CharacterOutput
    _conversation_agents[name] = _build_agent(
        name=name,
        instructions=build_system_prompt(name, soul),
        config=config,
        output_type=output_type,
    )
    if name == "narrator":
        _choices_agent = None
        _observation_narrator_agent = None


def get_conversation_agent(name: str) -> ConversationAgent:
    if name not in _conversation_agents:
        reload_conversation_agent(name)
    return _conversation_agents[name]


def get_observation_narrator_agent() -> ConversationAgent:
    global _observation_narrator_agent
    if _observation_narrator_agent is None:
        soul = read_agent_file("narrator", "soul.md")
        config = get_llm_config()
        _observation_narrator_agent = _build_agent(
            name="narrator_observation",
            instructions=NARRATOR_OBSERVATION.format(soul=soul),
            config=config,
            output_type=NarratorOutput,
        )
    return _observation_narrator_agent


def get_choices_agent() -> Agent[None, ChoicesOutput]:
    global _choices_agent

    if _choices_agent is None:
        config = get_llm_config()
        _choices_agent = _build_agent(
            name="choices",
            instructions=CHOICES,
            config=config,
            output_type=ChoicesOutput,
        )
    return _choices_agent


def get_state_updater_agent() -> Agent[None, StateUpdaterOutput]:
    global _state_updater_agent

    if _state_updater_agent is None:
        config = get_llm_config(temperature=STATE_UPDATER_TEMPERATURE)
        _state_updater_agent = _build_agent(
            name="state_updater",
            instructions=STATE_UPDATER,
            config=config,
            output_type=StateUpdaterOutput,
            output_retries=STATE_UPDATER_OUTPUT_RETRIES,
        )
    return _state_updater_agent


def get_character_factory_agent() -> Agent[None, NewCharacterProfile]:
    global _character_factory_agent

    if _character_factory_agent is None:
        config = get_llm_config()
        _character_factory_agent = _build_agent(
            name="character_factory",
            instructions=CHARACTER_FACTORY,
            config=config,
            output_type=NewCharacterProfile,
        )
    return _character_factory_agent


def _ensure_consolidation_agents() -> None:
    if _consolidation_agents:
        return

    config = get_llm_config(temperature=CONSOLIDATION_TEMPERATURE)
    _consolidation_agents["episode_memory_generator"] = _build_agent(
        name="episode_memory_generator",
        instructions=EPISODE_MEMORY_GENERATOR,
        config=config,
        output_type=EpisodeMemoryBlock,
        max_tokens=CONSOLIDATION_MAX_TOKENS,
    )
    _consolidation_agents["episode_closure_detector"] = _build_agent(
        name="episode_closure_detector",
        instructions=EPISODE_CLOSURE_DETECTOR,
        config=config,
        output_type=EpisodeClosureOutput,
        max_tokens=CONSOLIDATION_MAX_TOKENS,
    )
    _consolidation_agents["understanding_patch"] = _build_agent(
        name="understanding_patch",
        instructions=UNDERSTANDING_PATCH,
        config=config,
        output_type=UnderstandingPatchOutput,
        max_tokens=CONSOLIDATION_MAX_TOKENS,
    )


def _get_consolidation_agent(key: str) -> StructuredAgent:
    _ensure_consolidation_agents()
    return _consolidation_agents[key]


def get_episode_memory_generator_agent() -> Agent[None, EpisodeMemoryBlock]:
    return _get_consolidation_agent("episode_memory_generator")


def get_episode_closure_detector_agent() -> Agent[None, EpisodeClosureOutput]:
    return _get_consolidation_agent("episode_closure_detector")


def get_understanding_patch_agent() -> Agent[None, UnderstandingPatchOutput]:
    return _get_consolidation_agent("understanding_patch")
