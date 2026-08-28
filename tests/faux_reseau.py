"""Un faux `aiohttp.ClientSession`, juste assez pour ce que le code appelle."""

from __future__ import annotations

from typing import Any


class Reponse:
    def __init__(
        self,
        status: int = 200,
        charge: Any = None,
        corps: str = "",
        headers: dict | None = None,
    ) -> None:
        self.status = status
        self._charge = charge
        self._corps = corps
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self._charge

    async def text(self):
        return self._corps

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class Session:
    """Rend les réponses dans l'ordre où elles ont été posées."""

    def __init__(self, *reponses: Reponse) -> None:
        self._reponses = list(reponses)
        self.appels: list[dict] = []

    def _servir(self, methode: str, url: str, **reste):
        self.appels.append({"methode": methode, "url": url, **reste})
        if not self._reponses:
            raise AssertionError(f"appel non prévu : {methode} {url}")
        return self._reponses.pop(0)

    def get(self, url, params=None, headers=None):
        return self._servir("GET", url, params=params, entetes=headers or {})

    def post(self, url, data=None, headers=None):
        return self._servir("POST", url, corps=data, entetes=headers or {})

    def put(self, url, data=None, headers=None):
        return self._servir("PUT", url, corps=data, entetes=headers or {})

    def head(self, url, headers=None):
        return self._servir("HEAD", url, entetes=headers or {})


def listing(*publications: dict) -> dict:
    """La forme d'un listing Reddit, réduite à ce que le collecteur lit."""
    return {"data": {"children": [{"data": p} for p in publications]}}


def publication(**remplacements) -> dict:
    defauts = {
        "name": "t3_abc",
        "title": "un titre",
        "url": "https://exemple.net/a",
        "permalink": "/r/programming/comments/abc/un_titre/",
        "author": "quelqu-un",
        "score": 1234,
        "num_comments": 89,
        "created_utc": 1787000000.0,
    }
    return {**defauts, **remplacements}
