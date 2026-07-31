"""HA-dependent test setup — this whole directory skips when Home Assistant
(via pytest-homeassistant-custom-component) isn't installed."""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/msedcl in the test hass."""
    return


@pytest.fixture(autouse=True)
async def auto_recorder(recorder_mock):
    """Set up the (in-memory) recorder before each test.

    The manifest declares recorder as a dependency (statistics insertion), so
    loading the integration in a test hass requires a live recorder instance.
    """
    return
