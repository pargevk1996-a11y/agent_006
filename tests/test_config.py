"""Configuration edge cases — the .env a user actually writes.

`.env.example` ships optional keys empty (`VIBE_API_TOKEN=`, `POLICY_FILE=`), so
"present but empty" must mean "not configured". Getting this wrong once made the
app crash on startup with `IsADirectoryError: '.'` because an empty `POLICY_FILE`
became `Path(".")`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config import AppMode, Settings
from app.domain.policy import Policy


def settings_from(**env) -> Settings:
    return Settings(_env_file=None, **env)


class TestBlankValuesMeanUnset:
    def test_empty_policy_file_is_none(self):
        assert settings_from(policy_file="").policy_file is None

    def test_whitespace_policy_file_is_none(self):
        assert settings_from(policy_file="   ").policy_file is None

    def test_empty_token_is_none(self):
        assert settings_from(vibe_api_token="").vibe_api_token is None
        assert settings_from(vibe_api_token="").token_value is None

    def test_empty_webhook_secret_is_none(self):
        assert settings_from(vibe_webhook_secret="").webhook_secret_value is None

    def test_empty_callback_base_url_disables_callbacks(self):
        assert settings_from(callback_base_url="").callback_url is None

    def test_empty_token_still_blocks_network_modes(self):
        """An empty token must not pass for a mode that needs a real one."""
        with pytest.raises(PydanticValidationError, match="VIBE_API_TOKEN"):
            settings_from(app_mode=AppMode.ESTIMATE, vibe_api_token="")
        with pytest.raises(PydanticValidationError, match="VIBE_API_TOKEN"):
            settings_from(app_mode=AppMode.LIVE, vibe_api_token="")

    def test_real_values_survive(self, tmp_path: Path):
        policy_file = tmp_path / "policy.json"
        policy_file.write_text("{}", encoding="utf-8")
        settings = settings_from(
            app_mode=AppMode.LIVE,
            vibe_api_token="oc_token_value",
            vibe_webhook_secret="whsec_value",
            callback_base_url="https://agent.example.com/",
            policy_file=policy_file,
        )
        assert settings.token_value == "oc_token_value"
        assert settings.webhook_secret_value == "whsec_value"
        assert settings.callback_url == "https://agent.example.com/api/v1/webhooks/vibe"
        assert settings.policy_file == policy_file

    def test_mock_mode_needs_no_token(self):
        assert settings_from(app_mode=AppMode.MOCK).token_value is None


class TestPolicyLoading:
    def test_none_loads_builtin_table(self):
        policy = Policy.load(None)
        assert policy.source == "builtin"
        assert policy.format_priority

    def test_directory_gives_a_readable_error(self, tmp_path: Path):
        with pytest.raises(ValueError, match="POLICY_FILE"):
            Policy.load(tmp_path)

    def test_missing_file_gives_a_readable_error(self, tmp_path: Path):
        with pytest.raises(ValueError, match="не найден"):
            Policy.load(tmp_path / "nope.json")

    def test_override_file_is_applied_and_merged(self, tmp_path: Path):
        path = tmp_path / "policy.json"
        path.write_text(
            json.dumps({"tiers": {"image": [["z-image"]]}}, ensure_ascii=False),
            encoding="utf-8",
        )
        policy = Policy.load(path)
        assert policy.tier_of("image", "z-image") == 0
        assert policy.source == str(path)
        # Keys absent from the override keep their built-in values.
        assert policy.format_priority == Policy.load(None).format_priority

    def test_shipped_example_policy_is_valid(self):
        example = Path(__file__).resolve().parent.parent / "policy.example.json"
        policy = Policy.load(example)
        assert policy.tier_of("image", "z-image") == 0


class TestSecretsAreNotStringified:
    def test_repr_of_settings_hides_secrets(self):
        settings = settings_from(
            app_mode=AppMode.LIVE,
            vibe_api_token="oc_super_secret_token",
            vibe_webhook_secret="whsec_super_secret",
        )
        dumped = repr(settings) + str(settings.model_dump())
        assert "oc_super_secret_token" not in dumped
        assert "whsec_super_secret" not in dumped


class TestCapabilitiesSnapshot:
    """The bundled snapshot drives mock mode and every test — it must stay sane."""

    def test_snapshot_parses_and_covers_all_advertised_types(self):
        from app.clients.mock import load_capabilities_fixture
        from app.domain.capabilities import Capabilities

        payload = load_capabilities_fixture()
        caps = Capabilities.parse(payload)
        assert caps.models_by_type, "в слепке нет моделей"
        for type_ in caps.models_by_type:
            assert type_ in caps.types, f"тип {type_} не заявлен в types"
        for type_, models in caps.models_by_type.items():
            for key, spec in models.items():
                assert spec.required, f"{type_}/{key}: пустой required"
                assert "prompt" in spec.known_params or spec.required

    def test_every_model_is_priceable_or_explicitly_token_billed(self):
        from app.clients.mock import load_capabilities_fixture
        from app.domain.capabilities import Capabilities, price_bounds

        caps = Capabilities.parse(load_capabilities_fixture())
        for models in caps.models_by_type.values():
            for spec in models.values():
                bounds = price_bounds(spec)
                assert bounds.known or spec.is_token_billed or spec.price is None, (
                    f"{spec.key}: непонятная схема цены"
                )

    def test_diff_reports_added_removed_and_repriced_models(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "scripts" / "refresh_capabilities.py"
        spec = importlib.util.spec_from_file_location("refresh_capabilities", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        old = {"types": ["image"], "models": {"image": {"a": {"price": 1.0}, "b": {"price": 2.0}}}}
        new = {"types": ["image", "text"],
               "models": {"image": {"a": {"price": 5.0}, "c": {"price": 3.0}}}}
        report = "\n".join(module.diff(old, new))
        assert "+ image/c" in report
        assert "- image/b" in report
        assert "~ image/a — цена 1.0₽ → 5.0₽" in report
        assert "types" in report

    def test_identical_payloads_produce_no_diff(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "scripts" / "refresh_capabilities.py"
        spec = importlib.util.spec_from_file_location("refresh_capabilities", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        payload = {"types": ["image"], "models": {"image": {"a": {"price": 1.0}}}}
        assert module.diff(payload, dict(payload)) == []
