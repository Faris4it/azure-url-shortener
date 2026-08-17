"""
Unit tests for function_app.py.

These call the HTTP-triggered functions directly (bypassing the Functions
host) and mock out `_get_table_client`, so they run in milliseconds and
need no Azurite instance, real storage account, or `func start`.
"""

import json
from typing import Optional
from unittest.mock import MagicMock, patch

import azure.functions as func
import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

import function_app


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    ["https://example.com", "http://example.com/path?query=1"],
)
def test_is_valid_url_accepts_http_and_https(url):
    assert function_app._is_valid_url(url) is True


@pytest.mark.parametrize(
    "url",
    ["", "not-a-url", "ftp://example.com", "example.com", "https://"],
)
def test_is_valid_url_rejects_invalid(url):
    assert function_app._is_valid_url(url) is False


def test_generate_short_code_uses_expected_length_and_alphabet():
    code = function_app._generate_short_code()
    assert len(code) == function_app.SHORT_CODE_LENGTH
    assert all(c in function_app.SHORT_CODE_ALPHABET for c in code)


# ---------------------------------------------------------------------------
# POST /api/shorten
# ---------------------------------------------------------------------------

def _make_shorten_request(body: Optional[dict], raw_body: Optional[bytes] = None) -> func.HttpRequest:
    if raw_body is None:
        raw_body = json.dumps(body).encode("utf-8") if body is not None else b""
    return func.HttpRequest(method="POST", url="http://localhost:7071/api/shorten", body=raw_body)


def test_shorten_rejects_invalid_json():
    req = _make_shorten_request(None, raw_body=b"not-json")
    resp = function_app.shorten(req)
    assert resp.status_code == 400


def test_shorten_rejects_missing_url_field():
    req = _make_shorten_request({})
    resp = function_app.shorten(req)
    assert resp.status_code == 400
    assert "url" in json.loads(resp.get_body())["error"]


def test_shorten_rejects_invalid_url():
    req = _make_shorten_request({"url": "not-a-url"})
    resp = function_app.shorten(req)
    assert resp.status_code == 400


@patch("function_app._get_table_client")
def test_shorten_success_stores_entity_and_returns_short_url(mock_get_table_client):
    mock_table_client = MagicMock()
    mock_get_table_client.return_value = mock_table_client

    req = _make_shorten_request({"url": "https://example.com"})
    resp = function_app.shorten(req)

    assert resp.status_code == 201
    payload = json.loads(resp.get_body())
    assert "short_code" in payload
    assert payload["short_url"] == f"http://localhost:7071/api/{payload['short_code']}"

    mock_table_client.create_entity.assert_called_once()
    stored_entity = mock_table_client.create_entity.call_args.kwargs["entity"]
    assert stored_entity["PartitionKey"] == function_app.PARTITION_KEY
    assert stored_entity["RowKey"] == payload["short_code"]
    assert stored_entity["OriginalUrl"] == "https://example.com"


@patch("function_app._get_table_client")
def test_shorten_retries_on_short_code_collision(mock_get_table_client):
    mock_table_client = MagicMock()
    mock_table_client.create_entity.side_effect = [ResourceExistsError(), None]
    mock_get_table_client.return_value = mock_table_client

    req = _make_shorten_request({"url": "https://example.com"})
    resp = function_app.shorten(req)

    assert resp.status_code == 201
    assert mock_table_client.create_entity.call_count == 2


@patch("function_app._get_table_client")
def test_shorten_returns_500_when_all_attempts_collide(mock_get_table_client):
    mock_table_client = MagicMock()
    mock_table_client.create_entity.side_effect = ResourceExistsError()
    mock_get_table_client.return_value = mock_table_client

    req = _make_shorten_request({"url": "https://example.com"})
    resp = function_app.shorten(req)

    assert resp.status_code == 500
    assert mock_table_client.create_entity.call_count == function_app.MAX_GENERATION_ATTEMPTS


# ---------------------------------------------------------------------------
# GET /api/{short_code}
# ---------------------------------------------------------------------------

def _make_redirect_request(short_code: str) -> func.HttpRequest:
    return func.HttpRequest(
        method="GET",
        url=f"http://localhost:7071/api/{short_code}",
        body=None,
        route_params={"short_code": short_code},
    )


@patch("function_app._get_table_client")
def test_redirect_returns_302_when_code_found(mock_get_table_client):
    mock_table_client = MagicMock()
    mock_table_client.get_entity.return_value = {"OriginalUrl": "https://example.com"}
    mock_get_table_client.return_value = mock_table_client

    req = _make_redirect_request("abc1234")
    resp = function_app.redirect_to_original(req)

    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://example.com"
    mock_table_client.get_entity.assert_called_once_with(
        partition_key=function_app.PARTITION_KEY, row_key="abc1234"
    )


@patch("function_app._get_table_client")
def test_redirect_returns_404_when_code_not_found(mock_get_table_client):
    mock_table_client = MagicMock()
    mock_table_client.get_entity.side_effect = ResourceNotFoundError()
    mock_get_table_client.return_value = mock_table_client

    req = _make_redirect_request("doesnotexist")
    resp = function_app.redirect_to_original(req)

    assert resp.status_code == 404
