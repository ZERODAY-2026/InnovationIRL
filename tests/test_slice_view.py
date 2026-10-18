from streamlit.testing.v1 import AppTest


def test_slice_view_renders_without_errors():
    app = AppTest.from_file("../app/slice_view.py", default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "DataMind: ask the data"
