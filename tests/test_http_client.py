"""
Tests for RobustHTTPClient retry semantics.

Regression targets:
- A retryable 5xx must never be returned to the caller as a success
  (historical bug: the retry path fell through to `return response`).
- 429 must be retried after honouring Retry-After, not slept-then-failed.
"""

from unittest.mock import Mock, patch

import pytest

from http_client import (
    CircuitBreaker,
    ClientError,
    RetryConfig,
    RobustHTTPClient,
    ServerError,
)


def make_response(status: int, headers: dict | None = None) -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.headers = headers or {}
    return resp


def make_client(max_retries: int = 2) -> RobustHTTPClient:
    return RobustHTTPClient(
        base_url="http://localhost",
        retry_config=RetryConfig(max_retries=max_retries, base_delay=0.001, jitter=0.0),
        circuit_breaker=CircuitBreaker(failure_threshold=100),
    )


@patch("http_client.time.sleep")
def test_5xx_then_success_returns_the_success(mock_sleep):
    client = make_client()
    responses = [make_response(500), make_response(200)]
    with patch.object(client._session, "request", side_effect=responses) as req:
        result = client.get("/api/status")
    assert result.status_code == 200
    assert req.call_count == 2


@patch("http_client.time.sleep")
def test_5xx_exhausted_raises_server_error(mock_sleep):
    client = make_client(max_retries=2)
    responses = [make_response(503)] * 3
    with patch.object(client._session, "request", side_effect=responses) as req:
        with pytest.raises(ServerError):
            client.get("/api/status")
    assert req.call_count == 3


@patch("http_client.time.sleep")
def test_5xx_never_returned_as_success(mock_sleep):
    """The caller must never receive a >=500 response object."""
    client = make_client(max_retries=1)
    responses = [make_response(500), make_response(502)]
    with patch.object(client._session, "request", side_effect=responses):
        with pytest.raises(ServerError):
            client.get("/api/nowplaying")


@patch("http_client.time.sleep")
def test_4xx_raises_client_error_without_retry(mock_sleep):
    client = make_client()
    with patch.object(client._session, "request", return_value=make_response(404)) as req:
        with pytest.raises(ClientError):
            client.get("/api/missing")
    assert req.call_count == 1


@patch("http_client.time.sleep")
def test_429_is_retried_after_wait(mock_sleep):
    client = make_client()
    responses = [make_response(429, {"Retry-After": "7"}), make_response(200)]
    with patch.object(client._session, "request", side_effect=responses) as req:
        result = client.get("/api/files")
    assert result.status_code == 200
    assert req.call_count == 2
    assert any(call.args and call.args[0] == 7.0 for call in mock_sleep.call_args_list)


@patch("http_client.time.sleep")
def test_429_exhausted_raises(mock_sleep):
    client = make_client(max_retries=1)
    responses = [make_response(429, {"Retry-After": "1"})] * 2
    with patch.object(client._session, "request", side_effect=responses):
        with pytest.raises(ClientError):
            client.get("/api/files")


@patch("http_client.time.sleep")
def test_circuit_breaker_opens_on_repeated_5xx(mock_sleep):
    client = RobustHTTPClient(
        base_url="http://localhost",
        retry_config=RetryConfig(max_retries=2, base_delay=0.001, jitter=0.0),
        circuit_breaker=CircuitBreaker(failure_threshold=3),
    )
    with patch.object(client._session, "request", return_value=make_response(500)):
        with pytest.raises(ServerError):
            client.get("/api/status")
    assert not client.circuit_breaker.can_execute()
