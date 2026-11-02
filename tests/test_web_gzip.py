"""The dashboard polls large JSON every few seconds; the paper report alone was 1.24 MiB
uncompressed and ~105 KiB gzipped against the live ledger."""
import gzip
import json
import os

import pytest
from flask import Flask, jsonify


@pytest.fixture()
def app():
    from sniper.web.server import _gzip_json

    application = Flask(__name__)
    application.after_request(_gzip_json)

    @application.get("/big")
    def big():
        return jsonify({"rows": [{"mint": f"M{i}", "pnl": 1.25} for i in range(4000)]})

    @application.get("/small")
    def small():
        return jsonify({"ok": True})

    @application.get("/page")
    def page():
        return "<html>hi</html>"

    return application


def test_large_json_is_compressed_and_still_decodes(app):
    response = app.test_client().get("/big", headers={"Accept-Encoding": "gzip"})
    assert response.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in response.headers["Vary"]
    raw = response.get_data()
    assert int(response.headers["Content-Length"]) == len(raw)
    assert len(json.loads(gzip.decompress(raw))["rows"]) == 4000
    assert len(raw) < 100_000     # ~1.5x smaller at minimum; measured ~12x


def test_client_without_gzip_support_gets_plain_json(app):
    response = app.test_client().get("/big", headers={"Accept-Encoding": "identity"})
    assert "Content-Encoding" not in response.headers
    assert len(json.loads(response.get_data())["rows"]) == 4000


def test_small_and_non_json_responses_are_left_alone(app):
    client = app.test_client()
    for path in ("/small", "/page"):
        response = client.get(path, headers={"Accept-Encoding": "gzip"})
        assert "Content-Encoding" not in response.headers, path


def test_versioned_static_text_uses_gzip_and_immutable_cache(tmp_path):
    from sniper.web.server import _gzip_json

    source = ("const live = true;\n" * 2000).encode()
    asset = tmp_path / "app.js"
    asset.write_bytes(source)
    version = str(int(os.path.getmtime(asset)))
    application = Flask(
        __name__,
        static_folder=str(tmp_path),
        static_url_path="/static",
    )
    application.after_request(_gzip_json)

    response = application.test_client().get(
        f"/static/app.js?v={version}",
        headers={"Accept-Encoding": "gzip"},
    )

    compressed = response.get_data()
    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "Accept-Encoding" in response.headers["Vary"]
    assert gzip.decompress(compressed) == source
    assert len(compressed) < len(source) / 4


def test_unversioned_static_text_does_not_get_an_immutable_cache(tmp_path):
    from sniper.web.server import _gzip_json

    asset = tmp_path / "styles.css"
    asset.write_text(".live { color: green; }\n" * 1000)
    application = Flask(
        __name__,
        static_folder=str(tmp_path),
        static_url_path="/static",
    )
    application.after_request(_gzip_json)

    response = application.test_client().get(
        "/static/styles.css",
        headers={"Accept-Encoding": "identity"},
    )

    assert response.status_code == 200
    assert "immutable" not in response.headers.get("Cache-Control", "")
