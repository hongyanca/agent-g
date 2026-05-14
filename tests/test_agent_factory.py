"""测试 PydanticAI Agent factory 的输出模式。"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import agents.factory as agent_factory_module
except ModuleNotFoundError as exc:
    pytest.skip(f"skip agent factory tests: missing dependency ({exc})", allow_module_level=True)


@pytest.fixture(autouse=True)
def reset_agent_caches():
    agent_factory_module._conversation_agents.clear()
    agent_factory_module._choices_agent = None
    agent_factory_module._state_updater_agent = None
    agent_factory_module._consolidation_agents.clear()
    yield
    agent_factory_module._conversation_agents.clear()
    agent_factory_module._choices_agent = None
    agent_factory_module._state_updater_agent = None
    agent_factory_module._consolidation_agents.clear()


def _fake_config(temperature=None):
    return {
        "api_url": "https://example.com/v1",
        "api_key": "test-key",
        "model_id": "deepseek-chat",
        "temperature": 0.2 if temperature is None else temperature,
        "provider": "openai",
    }


def test_conversation_agents_use_prompted_output(monkeypatch):
    monkeypatch.setattr(agent_factory_module, "read_agent_file", lambda *_args: "# soul")
    monkeypatch.setattr(agent_factory_module, "build_system_prompt", lambda *_args: "system prompt")
    monkeypatch.setattr(agent_factory_module, "get_llm_config", _fake_config)

    narrator = agent_factory_module.get_conversation_agent("narrator")
    character = agent_factory_module.get_conversation_agent("mitsuki")

    assert narrator._output_schema.mode == "prompted"
    assert character._output_schema.mode == "prompted"


def test_prompted_output_cleanup_strips_leading_channel_prefix():
    raw = '<|channel>thought\n<channel|>{"ok": true}'

    assert agent_factory_module.strip_leading_channel_prefix(raw) == '{"ok": true}'


@pytest.mark.asyncio
async def test_prompted_output_processor_accepts_leading_channel_prefix(monkeypatch):
    monkeypatch.setattr(agent_factory_module, "read_agent_file", lambda *_args: "# soul")
    monkeypatch.setattr(agent_factory_module, "build_system_prompt", lambda *_args: "system prompt")
    monkeypatch.setattr(agent_factory_module, "get_llm_config", _fake_config)

    narrator = agent_factory_module.get_conversation_agent("narrator")
    raw = """<|channel>thought
<channel|>{
  "targets": ["mitsuki"],
  "date": "4月3日 星期一",
  "time": "08:27",
  "location": "图书馆旧阅览室",
  "present_characters": {
    "北原悠": "站在门口",
    "一之濑美月": "离北原悠很近"
  },
  "scene_description": "旧教室里很安静。",
  "new_characters": []
}
"""

    output = await narrator._output_schema.text_processor.process(
        raw,
        run_context=SimpleNamespace(validation_context=None),
    )

    assert output.targets == ["mitsuki"]
    assert output.location == "图书馆旧阅览室"


def test_auxiliary_structured_agents_use_prompted_output(monkeypatch):
    monkeypatch.setattr(agent_factory_module, "get_llm_config", _fake_config)

    choices = agent_factory_module.get_choices_agent()
    state_updater = agent_factory_module.get_state_updater_agent()
    episode_memory_generator = agent_factory_module.get_episode_memory_generator_agent()
    understanding_patch = agent_factory_module.get_understanding_patch_agent()

    assert choices._output_schema.mode == "prompted"
    assert state_updater._output_schema.mode == "prompted"
    assert episode_memory_generator._output_schema.mode == "prompted"
    assert understanding_patch._output_schema.mode == "prompted"


def test_state_updater_uses_deterministic_generation_settings(monkeypatch):
    monkeypatch.setattr(agent_factory_module, "get_llm_config", _fake_config)

    state_updater = agent_factory_module.get_state_updater_agent()

    assert (
        state_updater.model_settings["temperature"]
        == agent_factory_module.STATE_UPDATER_TEMPERATURE
    )
    assert state_updater._max_result_retries == agent_factory_module.STATE_UPDATER_OUTPUT_RETRIES


def test_consolidation_agents_use_configured_max_tokens(monkeypatch):
    monkeypatch.setattr(agent_factory_module, "get_llm_config", _fake_config)
    monkeypatch.setattr(agent_factory_module, "CONSOLIDATION_MAX_TOKENS", 1234)

    episode_memory_generator = agent_factory_module.get_episode_memory_generator_agent()
    understanding_patch = agent_factory_module.get_understanding_patch_agent()

    assert episode_memory_generator.model_settings["max_tokens"] == 1234
    assert understanding_patch.model_settings["max_tokens"] == 1234


def test_make_sdk_model_uses_configured_openai_compatible_url():
    model = agent_factory_module._make_sdk_model(
        {
            "api_url": "https://example.com/v1",
            "api_key": "test-key",
            "model_id": "moonshotai/kimi-k2.5",
            "temperature": 0.2,
            "provider": "openai",
        }
    )

    assert model._provider.name == "openai"
    assert str(model._provider.client.base_url) == "https://example.com/v1/"
    assert model._provider.client.api_key == "test-key"
