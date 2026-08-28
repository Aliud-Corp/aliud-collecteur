"""Le harnais : Home Assistant charge les intégrations personnalisées."""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def _integrations_personnalisees(enable_custom_integrations):
    yield
