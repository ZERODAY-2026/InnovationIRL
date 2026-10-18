"""The second dataset works through configuration alone: profiles/owid.yaml, no code changes."""
import json
from decimal import Decimal

import pytest

from datamind.gate import check_sql
from datamind.pipeline import DataMind
from datamind.profile import load_profile
from tests.test_pipeline import FakeGemma, FakeRunner, plan


@pytest.fixture()
def owid(tmp_path):
    (tmp_path / "owid.json").write_text(json.dumps({
        "coverage": {"CO2.YEAR": ["2000", "2023"]},
        "values": {"CO2.COUNTRY": ["Afghanistan", "China", "India", "United States", "World", "Asia"] +
                   [f"Country {i}" for i in range(40)]}}), encoding="utf-8")
    return load_profile("owid", cache_dir=tmp_path)


def test_owid_profile_and_examples(owid):
    assert list(owid.tables) == ["CO2"] and owid.schema == "OWID" and owid.time_anchor == "CO2.YEAR"
    for example in owid.examples:
        if example["plan"]["status"] == "ANSWER":
            assert check_sql(example["plan"]["sql"], owid).ok
    assert not check_sql("SELECT N_NAME FROM NATION", owid).ok  # TPC-H tables are not visible from this profile


def test_owid_value_hints_confirm_country_names(owid):
    hints = owid.value_hints("What were the CO2 emissions of the United States and Indai in 2010?")
    assert "CO2.COUNTRY = 'United States'" in hints
    assert any("'India'" in h for h in hints)


def test_owid_years_outside_coverage_get_a_note(owid):
    sql = "SELECT COUNTRY, CO2 FROM CO2 WHERE YEAR = 2030 AND ISO_CODE IS NOT NULL ORDER BY CO2 DESC NULLS LAST LIMIT 5"
    runner = FakeRunner(rows=[])
    gemma = FakeGemma([plan(sql=sql)])
    dm = DataMind(settings=object(), profile=owid, gemma=gemma, runner=runner)
    env = dm.ask("Top emitters in 2030?")
    assert "the data only covers CO2.YEAR from 2000 to 2023, so 2030 is outside it" in env.answer.text


def test_owid_nulls_render_as_missing(owid):
    runner = FakeRunner(rows=[["India", None], ["China", Decimal("11472.369")]])
    gemma = FakeGemma([plan(sql="SELECT COUNTRY, GDP FROM CO2 WHERE YEAR = 2022 AND ISO_CODE IS NOT NULL")],
                      [{"answer": "{{r0.NATION}} has {{r0.REVENUE}}."}])
    env = DataMind(settings=object(), profile=owid, gemma=gemma, runner=runner).ask("GDP of India and China in 2022?")
    assert env.answer.text == "India has no value."
