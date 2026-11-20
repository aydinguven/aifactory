"""DLP check for text pasted from a user's PC into a browser workspace.

The browser workspace reaches external sites (Claude, ChatGPT, ...), so text
going *into* it can leave the organization. Its automatic clipboard sync is
one-way out (``AIFACTORY_BROWSER_CLIPBOARD=out``); the only way to paste in
is the side panel's "Tarayıcıya gönder", which calls
``POST /api/workspaces/{id}/clipboard``. That route asks :func:`check_paste`
before the text reaches the remote browser and records every attempt in the
audit log (length and SHA-256, never the text itself).

Connect the organization's DLP here: return ``PasteDecision(False, reason)``
to refuse a paste; ``reason`` is shown to the user. Raising an exception
refuses the paste too (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.user import User
from app.models.workspace import Workspace


@dataclass(frozen=True)
class PasteDecision:
    allowed: bool
    reason: str = ""


async def check_paste(*, user: User, workspace: Workspace, text: str) -> PasteDecision:
    """Whether ``user`` may paste ``text`` into ``workspace``.

    No DLP is connected yet, so every paste is allowed (and audited by the
    caller). Replace this body with the call to the DLP service.
    """
    return PasteDecision(True)
