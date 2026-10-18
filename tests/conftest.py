import pytest

from datamind.profile import load_profile


@pytest.fixture(scope="session")
def tpch(tmp_path_factory):
    # Empty cache dir: tests never depend on a locally generated Snowflake cache.
    return load_profile("tpch", cache_dir=tmp_path_factory.mktemp("no_cache"))
