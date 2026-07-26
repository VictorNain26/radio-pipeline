"""
Centralized HTTP client with retry logic, SSL/TLS support, and robust error handling.

Best practices 2026:
- Exponential backoff with jitter
- Circuit breaker pattern
- Structured logging
- Type safety with dataclasses and type annotations
- File integrity verification (SHA-256/MD5)
"""

import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class HTTPError(Exception):
    """Base HTTP error with context."""

    def __init__(self, message: str, status_code: int | None = None, response: requests.Response | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class ClientError(HTTPError):
    """4xx client errors (non-retryable)."""
    pass


class ServerError(HTTPError):
    """5xx server errors (retryable)."""
    pass


class HTTPConnectionError(HTTPError):
    """Network/connection errors."""
    pass


class CircuitState(Enum):
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreaker:
    """
    Circuit breaker to prevent cascading failures.

    Opens after `failure_threshold` consecutive failures.
    Attempts recovery after `recovery_timeout` seconds.
    """
    failure_threshold: int = 5
    recovery_timeout: float = 60.0

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                logger.info("Circuit breaker: HALF_OPEN (attempting recovery)")
        return self._state

    def record_success(self) -> None:
        self._failure_count = 0
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            logger.info("Circuit breaker: CLOSED (recovered)")

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning(f"Circuit breaker: OPEN (after {self._failure_count} failures)")

    def can_execute(self) -> bool:
        return self.state != CircuitState.OPEN


@dataclass
class RetryConfig:
    """Retry configuration with exponential backoff."""
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: float = 0.1  # Random factor to prevent thundering herd

    # HTTP status codes to retry
    retry_on_status: tuple[int, ...] = (429, 500, 502, 503, 504)

    def get_delay(self, attempt: int) -> float:
        """Calculate delay with exponential backoff and jitter."""
        delay = min(
            self.base_delay * (self.exponential_base ** attempt),
            self.max_delay
        )
        jitter_range = delay * self.jitter
        return delay + random.uniform(-jitter_range, jitter_range)


class RobustHTTPClient:
    """
    Production-grade HTTP client.

    Features:
    - Automatic retry with exponential backoff
    - Circuit breaker pattern
    - SSL/TLS verification (enforced)
    - Request/response logging
    - Timeout enforcement
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        retry_config: RetryConfig | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        self.base_url = self._normalize_url(base_url)
        self.api_key = api_key
        self.timeout = timeout
        self.retry_config = retry_config or RetryConfig()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()

        self._session = self._create_session()

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URL (remove trailing slash). Validation done by settings.py."""
        return url.rstrip("/")

    def _create_session(self) -> requests.Session:
        """Create session with connection pooling."""
        session = requests.Session()

        # Configure retry adapter for connection-level retries
        retry_strategy = Retry(
            total=self.retry_config.max_retries,
            backoff_factor=self.retry_config.base_delay,
            status_forcelist=list(self.retry_config.retry_on_status),
            allowed_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=10,
        )

        session.mount("http://", adapter)
        session.mount("https://", adapter)

        # Default headers
        session.headers.update({
            "User-Agent": "RadioPipeline/2.0 (AubeSonore)",
            "Accept": "application/json",
        })

        if self.api_key:
            session.headers["X-API-Key"] = self.api_key

        return session

    def _make_request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Execute HTTP request with circuit breaker and retry logic."""
        if not self.circuit_breaker.can_execute():
            raise HTTPConnectionError(
                f"Circuit breaker is OPEN - service unavailable",
                status_code=None,
            )

        url = f"{self.base_url}{endpoint}"
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", True)  # Always verify SSL

        last_exception: Exception | None = None

        for attempt in range(self.retry_config.max_retries + 1):
            try:
                logger.debug(f"Request: {method} {url} (attempt {attempt + 1})")

                response = self._session.request(method, url, **kwargs)

                # Log response
                logger.debug(f"Response: {response.status_code}")

                # Check for errors
                if response.status_code >= 400:
                    # Either raises (terminal) or returns True after backoff (retry)
                    if self._handle_error_response(response, attempt):
                        continue

                # Success
                self.circuit_breaker.record_success()
                return response

            except requests.exceptions.SSLError as e:
                logger.error(f"SSL/TLS error: {e}")
                raise HTTPConnectionError(f"SSL verification failed: {e}") from e

            except requests.exceptions.ConnectionError as e:
                last_exception = e
                self.circuit_breaker.record_failure()

                if attempt < self.retry_config.max_retries:
                    delay = self.retry_config.get_delay(attempt)
                    logger.warning(f"Connection error, retrying in {delay:.1f}s: {e}")
                    time.sleep(delay)
                else:
                    raise HTTPConnectionError(f"Connection failed after {attempt + 1} attempts: {e}") from e

            except requests.exceptions.Timeout as e:
                last_exception = e
                self.circuit_breaker.record_failure()

                if attempt < self.retry_config.max_retries:
                    delay = self.retry_config.get_delay(attempt)
                    logger.warning(f"Timeout, retrying in {delay:.1f}s")
                    time.sleep(delay)
                else:
                    raise HTTPConnectionError(f"Request timed out after {attempt + 1} attempts") from e

        raise HTTPConnectionError(f"Request failed: {last_exception}")

    def _handle_error_response(self, response: requests.Response, attempt: int) -> bool:
        """
        Handle an HTTP error response.

        Returns True when the caller should retry (backoff already applied);
        raises ClientError/ServerError for terminal failures. Never falls
        through silently — an error response must never reach the success path.
        """
        status = response.status_code

        # Client errors (4xx) - don't retry, except 429
        if 400 <= status < 500:
            self.circuit_breaker.record_success()  # Service is responding

            if status == 401:
                raise ClientError("Authentication failed - check API key", status, response)
            if status == 403:
                raise ClientError("Access forbidden - check permissions", status, response)
            if status == 404:
                raise ClientError("Resource not found", status, response)
            if status == 429:
                if attempt < self.retry_config.max_retries:
                    retry_after = response.headers.get("Retry-After", "60")
                    delay = min(float(retry_after), 300.0) if retry_after.isdigit() else 60.0
                    logger.warning(f"Rate limited, retrying in {delay}s")
                    time.sleep(delay)
                    return True
                raise ClientError(f"Rate limited after {attempt + 1} attempts", status, response)

            raise ClientError(f"Client error: {status}", status, response)

        # Server errors (5xx) - retry with backoff
        self.circuit_breaker.record_failure()

        if attempt < self.retry_config.max_retries:
            delay = self.retry_config.get_delay(attempt)
            logger.warning(f"Server error {status}, retrying in {delay:.1f}s")
            time.sleep(delay)
            return True

        raise ServerError(f"Server error after {attempt + 1} attempts: {status}", status, response)

    def get(self, endpoint: str, **kwargs: Any) -> requests.Response:
        """HTTP GET request."""
        return self._make_request("GET", endpoint, **kwargs)

    def post(self, endpoint: str, **kwargs: Any) -> requests.Response:
        """HTTP POST request."""
        return self._make_request("POST", endpoint, **kwargs)

    def put(self, endpoint: str, **kwargs: Any) -> requests.Response:
        """HTTP PUT request."""
        return self._make_request("PUT", endpoint, **kwargs)

    def delete(self, endpoint: str, **kwargs: Any) -> requests.Response:
        """HTTP DELETE request."""
        return self._make_request("DELETE", endpoint, **kwargs)

    def health_check(self, endpoint: str = "/api/status") -> bool:
        """
        Check if the service is healthy.

        Returns True if service responds with 2xx.
        """
        try:
            response = self.get(endpoint)
            return 200 <= response.status_code < 300
        except (HTTPError, HTTPConnectionError):
            return False

    def close(self) -> None:
        """Close the session."""
        self._session.close()

    def __enter__(self) -> "RobustHTTPClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def compute_file_hashes(filepath: Path) -> tuple[str, str]:
    """
    Compute MD5 and SHA-256 hashes of a file.

    Args:
        filepath: Path to the file.

    Returns:
        Tuple of (md5_hex, sha256_hex).
    """
    md5_hash = hashlib.md5(usedforsecurity=False)
    sha256_hash = hashlib.sha256()

    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5_hash.update(chunk)
            sha256_hash.update(chunk)

    return md5_hash.hexdigest(), sha256_hash.hexdigest()


class AzuraCastClient(RobustHTTPClient):
    """
    Specialized client for AzuraCast API.

    Provides typed methods for common operations.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        station_id: int = 1,
        **kwargs: Any,
    ):
        super().__init__(base_url, api_key, **kwargs)
        self.station_id = station_id

    def get_station_files(self) -> list[dict[str, Any]]:
        """Get all files in station library."""
        response = self.get(f"/api/station/{self.station_id}/files")
        return response.json()

    def get_playlists(self) -> list[dict[str, Any]]:
        """Get all playlists for station."""
        response = self.get(f"/api/station/{self.station_id}/playlists")
        return response.json()

    def get_playlists_map(self) -> dict[str, int]:
        """
        Get playlist name to ID mapping.

        Raises:
            HTTPConnectionError: If AzuraCast is unreachable.
        """
        try:
            return {p["name"]: p["id"] for p in self.get_playlists()}
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.error(f"Failed to fetch playlists: {e}")
            raise

    def assign_playlists(self, file_id: int, playlist_ids: list[int]) -> bool:
        """
        Assign file to exactly these playlists (REPLACE semantics).

        Returns:
            True if successful.
        """
        try:
            response = self.put(
                f"/api/station/{self.station_id}/file/{file_id}",
                json={"playlists": playlist_ids},
            )
            return response.status_code == 200
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.warning(f"Failed to assign playlists: {e}")
            return False

    @staticmethod
    def _parse_played_at(value: Any) -> float:
        """Parse AzuraCast played_at which may be int (unix) or ISO-8601 string."""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            # Try ISO-8601 parsing
            from datetime import datetime, timezone
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(value, fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except ValueError:
                    continue
            # Last resort: try float conversion
            try:
                return float(value)
            except ValueError:
                pass
        return 0.0

    def get_history_since(self, since_timestamp: float) -> list[dict[str, Any]]:
        """
        Get station history entries since a given timestamp.

        Args:
            since_timestamp: Unix timestamp. Pass 0 for all available history.

        Returns:
            List of history entries with played_at > since_timestamp.
        """
        params: dict[str, Any] = {"limit": 500}

        if since_timestamp > 0:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(since_timestamp, tz=timezone.utc)
            params["start"] = dt.strftime("%Y-%m-%dT%H:%M:%S%z")

        try:
            response = self.get(
                f"/api/station/{self.station_id}/history",
                params=params,
            )
            entries = response.json()
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.warning(f"Failed to fetch history: {e}")
            return []

        if since_timestamp <= 0:
            return entries

        # Client-side filter in case API doesn't support 'start' param
        filtered = []
        for entry in entries:
            played_at = self._parse_played_at(entry.get("played_at", 0))
            if played_at > since_timestamp:
                filtered.append(entry)
        return filtered

    def update_file_art(self, file_id: int | str, image_path: Path) -> bool:
        """Upload artwork for an existing station file."""
        try:
            with open(image_path, "rb") as f:
                response = self.post(
                    f"/api/station/{self.station_id}/files/{file_id}/art",
                    files={"art": ("cover.jpg", f, "image/jpeg")},
                )
            return response.status_code in (200, 201)
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.error(f"Art update failed for file {file_id}: {e}")
            return False

    def download_file_to(self, file_id: int | str, dest: Path, timeout: float = 120.0) -> bool:
        """Download a station media file to a local path."""
        try:
            response = self.get(
                f"/api/station/{self.station_id}/file/{file_id}/download",
                timeout=timeout,
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(response.content)
            return True
        except (ClientError, ServerError, HTTPConnectionError, OSError) as e:
            logger.error(f"Download failed for file {file_id}: {e}")
            return False

    def delete_file(self, file_id: int | str) -> bool:
        """
        Delete a file from station library.

        Args:
            file_id: File ID to delete.

        Returns:
            True if successful.
        """
        try:
            response = self.delete(f"/api/station/{self.station_id}/file/{file_id}")
            return response.status_code in (200, 204)
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.error(f"Delete failed for file {file_id}: {e}")
            return False

    def health_check(self, endpoint: str = "/api/status") -> bool:
        """Check AzuraCast availability."""
        return super().health_check(endpoint)
