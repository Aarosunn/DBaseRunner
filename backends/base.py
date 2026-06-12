"""Abstract base for all LittleX benchmark backends."""

from abc import ABC, abstractmethod


class BackendBase(ABC):
    """Each backend implements auth, load_own_tweets, health, and seed.

    All methods that hit the server must return raw response data so the
    harness can validate correctness across backends.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    @abstractmethod
    def auth(self, username: str, password: str) -> None:
        """Authenticate and store credentials for subsequent calls."""

    @abstractmethod
    def load_own_tweets(self, user_id: str, limit: int, selectivity: int) -> dict:
        """POST /walker/load_own_tweets — returns full response dict.

        Args:
            user_id: target user's jac_id
            limit: max followees to traverse (fanout parameter)
            selectivity: percentage of tweets to return (0-100)
        """

    @abstractmethod
    def health(self) -> bool:
        """GET /health — returns True if backend is up."""

    @abstractmethod
    def seed(self, dataset_path: str) -> None:
        """Load dataset into backend from JSON file."""
