import re

from abist_kb import identity


def test_identity_values_are_fixed():
    assert identity.PACKAGE_NAME == "abist_kb"
    assert identity.DISTRIBUTION_NAME == "abist-kb"
    assert identity.CLI_NAME == "abist-kb"
    assert identity.ENV_PREFIX == "ABIST_KB_"
    assert identity.DISPLAY_NAME == "ABIST Knowledge Base"


def test_env_prefix_shape():
    assert identity.ENV_PREFIX.endswith("_")
    assert re.fullmatch(r"[A-Z][A-Z0-9_]*_", identity.ENV_PREFIX)


def test_env_var_builds_prefixed_name():
    assert identity.env_var("ESA_ACCESS_TOKEN") == "ABIST_KB_ESA_ACCESS_TOKEN"
    assert identity.env_var("log_level") == "ABIST_KB_LOG_LEVEL"
