# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""``FgaConnection`` and how one gets built (spec §5).

``fga.py`` no longer reads ``OWNERSHIP_FGA_*`` globals at import time; it
threads an :class:`FgaConnection` through a single ``_request()`` instead,
so token rotation, a ``superset ownership fga reconnect``, and a 401 retry
can all swap the connection without a process restart.

This module owns the connection type, the static (config/env/keys)
provider, and resolution of ``OWNERSHIP_FGA_CONFIG_PROVIDER``. Building a
connection is pure -- no I/O beyond reading environment variables and, for
``StaticProvider``, whatever config layer its caller resolved the four keys
from -- so every failure here is a configuration mistake
(:class:`CredentialError`), never a store problem.

Ownership note (PCS-10243 §3-§4): the shared plug-in loader and its
``Registry`` (``superset_ownership.plugins``, ``settings.get()``, the
``FgaConnection`` accessors, and ``plugins.PluginError``) own the ONE
precedence rule -- config layer, then environment, then default -- and call
:class:`StaticProvider` with values already resolved that way
(``plugins._build_connection``). Nothing in this module reads
``OWNERSHIP_FGA_*`` directly from ``os.environ`` for that reason: a second,
environment-only read path here would silently disagree with ``plugins.py``
about what a deployment configured the moment the two differed.
"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Same defaults `fga.py` has always used (see its history for why the model
# is unpinned by default and why that is a deliberate, documented choice).
DEFAULT_API_URL = "http://openfga:8080"
DEFAULT_STORE_ID = "01M1TT1CJ9PWQVF6KWBEJNSKB8"
DEFAULT_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class FgaConnection:
    """How to reach one OpenFGA store (spec §5.1 / contract §4.1)."""

    api_url: str
    store_id: str
    model_id: str | None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_S

    def url(self, path: str) -> str:
        return f"{self.api_url}/stores/{self.store_id}{path}"

    def model(self) -> dict[str, str]:
        """The ``authorization_model_id`` field, present only when pinned."""
        return {"authorization_model_id": self.model_id} if self.model_id else {}


class CredentialError(RuntimeError):
    """A connection could not be built: a malformed
    ``OWNERSHIP_FGA_CREDENTIALS`` shape, a missing ``token_env``, an
    unresolvable ``OWNERSHIP_FGA_CONFIG_PROVIDER``, or a provider that
    returned something other than an :class:`FgaConnection`.

    Raised at connection-build time, which is call time, not import time
    (closing PCS-10243's #88). Stands in for
    ``superset_ownership.plugins.PluginError`` (spec §4.1) until that
    module is on this branch: callers that want to treat "connection could
    not be built" uniformly across both names should catch this type and,
    when importable, ``plugins.PluginError`` as well.
    """


def _headers_from_credentials(
    credentials: Mapping[str, Any],
    *,
    warn_inline_token: Callable[[], None] = lambda: None,
) -> dict[str, str]:
    """``OWNERSHIP_FGA_CREDENTIALS`` shapes, v1 (spec §5.2).

    ``warn_inline_token`` (N5) is called, never logged here directly: this
    function runs on every connection build -- every 401 refresh, every
    ``reconnect`` -- not just at boot, and §5.2 calls the inline-secret
    warning "a boot WARNING". :class:`StaticProvider` passes a callback that
    fires at most once per provider instance; the default no-op keeps a
    caller that does not care (a one-off ``--credentials-env`` override,
    the tests below) silent.
    """
    cred_type = credentials.get("type", "none")
    if cred_type == "none":
        return {}
    if cred_type == "api_token":
        token_env = credentials.get("token_env")
        token = credentials.get("token")
        if token_env:
            value = os.environ.get(token_env)
            if not value:
                raise CredentialError(
                    f"OWNERSHIP_FGA_CREDENTIALS: token_env={token_env!r} is not "
                    "set or is blank"
                )
            return {"Authorization": f"Bearer {value}"}
        if token:
            warn_inline_token()
            return {"Authorization": f"Bearer {token}"}
        raise CredentialError(
            "OWNERSHIP_FGA_CREDENTIALS: type=api_token needs token_env or token"
        )
    if cred_type == "oidc_client_credentials":
        raise CredentialError(
            "OWNERSHIP_FGA_CREDENTIALS: type=oidc_client_credentials is not "
            "supported in v1; use OWNERSHIP_FGA_CONFIG_PROVIDER"
        )
    raise CredentialError(
        f"OWNERSHIP_FGA_CREDENTIALS: unknown type {cred_type!r}; accepted: "
        "none, api_token"
    )


class StaticProvider:
    """Builds an :class:`FgaConnection` from the four keys plus credentials.

    A callable, not a one-shot value: credentials -- specifically
    ``token_env`` -- are re-read from the environment on every call, so a
    rotated token is picked up by the next ``reconnect`` or 401 refresh
    without a process restart.
    """

    def __init__(
        self,
        *,
        api_url: str,
        store_id: str,
        model_id: str | None,
        credentials: Mapping[str, Any],
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_url = api_url
        self._store_id = store_id
        self._model_id = model_id
        self._credentials = credentials
        self._timeout_s = timeout_s
        self._warned_inline_token = False  # N5: at most one WARNING per instance

    def _warn_inline_token_once(self) -> None:
        if self._warned_inline_token:
            return
        self._warned_inline_token = True
        logger.warning(
            "OWNERSHIP_FGA_CREDENTIALS: secret in config; prefer token_env over "
            "an inline token"
        )

    def __call__(self) -> FgaConnection:
        headers = _headers_from_credentials(
            self._credentials, warn_inline_token=self._warn_inline_token_once
        )
        return FgaConnection(
            api_url=self._api_url,
            store_id=self._store_id,
            model_id=self._model_id,
            headers=headers,
            timeout_s=self._timeout_s,
        )


def resolve_provider(value: Any) -> Callable[[], FgaConnection] | None:
    """``OWNERSHIP_FGA_CONFIG_PROVIDER``: ``None``, a callable, or a dotted
    reference (``"pkg.mod:fn"`` or ``"pkg.mod.fn"``) to one (contract §4.1).
    """
    if value is None:
        return None
    if callable(value):
        return value
    if isinstance(value, str):
        return _import_callable(value)
    raise CredentialError(
        f"OWNERSHIP_FGA_CONFIG_PROVIDER={value!r}: must be a callable or "
        "'pkg.mod:fn' / 'pkg.mod.fn'"
    )


def _import_callable(dotted: str) -> Callable[[], FgaConnection]:
    module_name, sep, attr = dotted.partition(":")
    if not sep:
        module_name, _, attr = dotted.rpartition(".")
    if not module_name or not attr:
        raise CredentialError(
            f"OWNERSHIP_FGA_CONFIG_PROVIDER={dotted!r}: expected "
            "'pkg.mod:fn' or 'pkg.mod.fn'"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise CredentialError(
            f"OWNERSHIP_FGA_CONFIG_PROVIDER={dotted!r}: {exc}"
        ) from exc
    try:
        fn = getattr(module, attr)
    except AttributeError as exc:
        raise CredentialError(
            f"OWNERSHIP_FGA_CONFIG_PROVIDER={dotted!r}: {exc}"
        ) from exc
    if not callable(fn):
        raise CredentialError(
            f"OWNERSHIP_FGA_CONFIG_PROVIDER={dotted!r}: {attr!r} is not callable"
        )
    return fn
