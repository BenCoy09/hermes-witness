"""Hermes Witness: evidence-gated learning for Hermes Agent."""
import logging

from . import cli
from .engine import get_witness

log = logging.getLogger(__name__)


def _key(task_id=None, session_id=None, **_):
    return str(session_id or task_id or "default")


def _post_tool_call(tool_name=None, args=None, result=None, task_id=None, session_id=None, **kw):
    try:
        get_witness().on_tool(_key(task_id, session_id), tool_name or "", args or {}, result if result is not None else "")
    except Exception:
        log.exception("witness post_tool_call failed")


def _pre_llm_call(task_id=None, session_id=None, **kw):
    try:
        w, k = get_witness(), _key(task_id, session_id)
        msg = kw.get("user_message") or kw.get("prompt") or ""
        if isinstance(msg, str):
            w.on_user_message(k, msg)
        notice = w.notice_for(k)
        return {"context": notice} if notice else None
    except Exception:
        log.exception("witness pre_llm_call failed")
        return None


def _on_session_end(task_id=None, session_id=None, **kw):
    try:
        get_witness().on_end(_key(task_id, session_id))
    except Exception:
        log.exception("witness on_session_end failed")


def register(ctx):
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_command("witness", cli.slash, "Witness: graded skills (read-only)")
    ctx.register_cli_command("witness", "Evidence-gated learning for skills", cli.setup, cli.handle)
