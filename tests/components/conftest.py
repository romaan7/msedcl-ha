"""HA-dependent test setup — this whole directory skips when Home Assistant
(via pytest-homeassistant-custom-component) isn't installed."""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")


@pytest.fixture(autouse=True)
async def auto_setup(recorder_mock, enable_custom_integrations):
    """Recorder + custom-integration enablement for every test in this dir.

    Argument order is load-bearing: `recorder_mock` MUST resolve before
    anything that instantiates `hass` (its `recorder_db_url` dependency
    asserts hass isn't set up yet, because the DB URL has to be configured
    before hass boots). `enable_custom_integrations` depends on `hass`, so it
    comes second. The recorder itself is required because the manifest
    declares it as a dependency (statistics insertion).
    """
    return
