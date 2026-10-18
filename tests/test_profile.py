import json

import pytest

from datamind.gemma import estimate_tokens
from datamind.profile import ProfileError, load_profile


def test_structure(tpch):
    assert len(tpch.tables) == 8
    assert len(tpch.joins) == 10
    composite = [g for g in tpch.joins if len(g) == 2]
    assert len(composite) == 1
    assert tpch.time_anchor == "ORDERS.O_ORDERDATE"


def test_key_domains(tpch):
    assert tpch.same_key_domain("CUSTOMER", "C_NATIONKEY", "SUPPLIER", "S_NATIONKEY")
    assert tpch.same_key_domain("LINEITEM", "L_PARTKEY", "PARTSUPP", "PS_PARTKEY")
    assert not tpch.same_key_domain("NATION", "N_NATIONKEY", "SUPPLIER", "S_SUPPKEY")
    assert not tpch.same_key_domain("ORDERS", "O_ORDERKEY", "LINEITEM", "L_PARTKEY")


def test_relevant_tables_connects_join_path(tpch):
    tables = tpch.relevant_tables("Which 5 countries had the highest revenue in 1995?")
    assert {"LINEITEM", "NATION", "ORDERS", "CUSTOMER"} <= set(tables)
    assert "PARTSUPP" not in tables


def test_relevant_tables_value_match(tpch):
    assert "CUSTOMER" in tpch.relevant_tables("How many customers are in the AUTOMOBILE segment?")


def test_schema_render_fits_small_context(tpch):
    full = tpch.render_schema()
    assert "C_PHONE" not in full  # hidden columns stay out of prompts
    assert "{F|O|P}" in full
    assert estimate_tokens(full) < 2400, estimate_tokens(full)
    reduced = tpch.render_schema(tpch.relevant_tables("revenue by country in 1995"))
    assert estimate_tokens(reduced) < estimate_tokens(full)


def test_cache_merges_coverage(tmp_path):
    (tmp_path / "tpch.json").write_text(
        json.dumps({"coverage": {"ORDERS.O_ORDERDATE": ["1992-01-01", "1998-08-02"]},
                    "values": {"PART.P_CONTAINER": ["SM CASE", "LG BOX"]}}),
        encoding="utf-8",
    )
    profile = load_profile("tpch", cache_dir=tmp_path)
    text = profile.render_schema()
    assert "[1992-01-01..1998-08-02]" in text
    assert profile.tables["PART"].columns["P_CONTAINER"].values == ["SM CASE", "LG BOX"]


def test_bad_join_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "name: bad\ntables:\n  A:\n    columns:\n      X: {type: number}\njoins:\n  - A.X = B.Y\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileError, match="unknown column B.Y"):
        load_profile("bad", profiles_dir=tmp_path, cache_dir=tmp_path)
