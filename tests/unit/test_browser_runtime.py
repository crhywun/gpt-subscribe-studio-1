"""Verify the installed native library supports every browser profile the app uses."""
import pytest
from curl_cffi import Curl

from gss_core.chatgpt import _IMPERSONATE
from gss_core.har_device import _CHROME_BUILDS


@pytest.mark.parametrize("target", sorted({_IMPERSONATE, *[target for _, target in _CHROME_BUILDS]}))
def test_configured_browser_profile_is_supported(target):
    curl = Curl()
    try:
        assert curl.impersonate(target) == 0, f"Installed curl_cffi does not support {target}"
    finally:
        curl.close()
