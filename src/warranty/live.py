"""Explicit GMS execution scopes for trusted command-line entry points.

Entering a scope does not authorize any source, question, or document. Adapters
still check their processing guards and persistent budgets before every request.
Nothing in this module constructs a client, reserves credit, or sends data.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hmac
import re

from .errors import ServiceError


LIVE_PURPOSES = ("evaluation", "verification", "final_verification", "demo", "embedding_preparation")
_GATEWAY = "https://gms.ssafy.io/gmsapi/api.openai.com/v1"
_APPROVAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")


def _blocked():
    return ServiceError("MODEL_REMOTE_BLOCKED", "Explicit LIVE model configuration is required.", 503)


def _approval_required():
    return ServiceError("PROCESSING_APPROVAL_REQUIRED", "Confirmed LIVE processing budgets are required.", 503)


def _valid_approval(value):
    return isinstance(value, str) and _APPROVAL.fullmatch(value) is not None


def _configuration(app):
    values = app.runtime["models"]
    if (not isinstance(values, dict) or app.settings.remote_models is not True
            or values.get("external_processing_approved") is not True):
        raise _blocked()
    approval = values.get("usage_approval_id")
    if not _valid_approval(approval):
        raise _approval_required()
    providers = [values.get("chat_provider"), values.get("embedding_provider")]
    # This boundary is specifically for GMS. A mixed remote provider could send
    # through a different adapter while the caller believes it is testing GMS.
    if any(provider not in ("gms", "mock") for provider in providers) or "gms" not in providers:
        raise _blocked()
    return values, approval


def _budget_matches(budget, approval):
    return budget is not None and budget.approval_matches(approval) is True


def _check_budgets(app, approval):
    usage = app.models.usage_budget
    credit = app.models.credit_budget
    if (not _budget_matches(usage, approval) or not _budget_matches(credit, approval)
            or credit.confirmed is not True or usage.ready is not True or credit.ready is not True):
        raise _approval_required()


def _selection(app, values, *, active=False):
    selected = []
    slots = (("chat", app.rag.model), ("embedding", app.rag.vectors.embeddings))
    for slot, used_model in slots:
        model = getattr(app.models, slot)
        if model is not used_model:
            raise _blocked()
        if slot == "chat" and hasattr(app.rag, "runner") and app.rag.runner.model is not model:
            raise _blocked()
        if values[slot + "_provider"] != "gms":
            if getattr(model, "is_mock", None) is not True:
                raise _blocked()
            continue
        adapter = getattr(model, "adapter", model)
        info = model.metadata()
        if (getattr(model, "is_remote", None) is not True or getattr(model, "is_mock", None) is not False
                or getattr(adapter, "is_remote", None) is not True or getattr(adapter, "is_mock", None) is not False
                or not callable(getattr(adapter, "live_execution", None)) or not isinstance(info, dict)
                or info.get("provider") != "gms" or info.get("is_remote") is not True
                or info.get("is_mock") is not False or info.get("live_execution_required") is not True
                or info.get("configuration_ready") is not True or info.get("gateway_base_url") != _GATEWAY
                or (active and info.get("can_generate") is not True)):
            raise _blocked()
        model_id = info.get("model_id")
        if (not isinstance(model_id, str) or _MODEL.fullmatch(model_id) is None
                or model_id != getattr(model, "model_id", None) or model_id != getattr(adapter, "model_id", None)):
            raise _blocked()
        for field, actual in ((slot + "_model", model_id), (slot + "_model_version", info.get("model_version"))):
            if values.get(field) is not None and values[field] != actual:
                raise _blocked()
        if slot == "chat":
            if info.get("route") not in ("responses", "chat/completions"):
                raise _blocked()
            if values.get("gms_chat_route") is not None and values["gms_chat_route"] != info["route"]:
                raise _blocked()
        else:
            dimension = info.get("dimension")
            if (type(dimension) is not int or not 1 <= dimension <= 8192
                    or dimension != getattr(adapter, "dimension", None)
                    or (values.get("embedding_dimension") is not None and dimension != values["embedding_dimension"])):
                raise _blocked()
        configured_base = values.get("gms_base_url")
        if configured_base is not None and configured_base not in (_GATEWAY, _GATEWAY + "/"):
            raise _blocked()
        selected.append(adapter)
    if not selected or len({id(adapter) for adapter in selected}) != len(selected):
        raise _blocked()
    return selected


@contextmanager
def live_execution(app, purpose, approval_id):
    """Open selected GMS scopes only after explicit server configuration checks.

    Call only from a trusted, explicit LIVE entry point. Ordinary requests and
    background workers must not call this helper. The approval ID is checked but
    never returned, logged, or forwarded into a model prompt.
    """
    if (not isinstance(purpose, str) or purpose not in LIVE_PURPOSES or not _valid_approval(approval_id)):
        raise ServiceError("INVALID_INPUT", "A supported LIVE purpose and approval identifier are required.", 400)
    with ExitStack() as stack:
        try:
            values, expected_approval = _configuration(app)
            if not hmac.compare_digest(expected_approval, approval_id):
                raise _approval_required()
            _check_budgets(app, approval_id)
            adapters = _selection(app, values)
            for adapter in adapters:
                stack.enter_context(adapter.live_execution(purpose, approval_id))
            _check_budgets(app, approval_id)
            if _selection(app, values, active=True) != adapters:
                raise _blocked()
        except ServiceError as exc:
            # Only server-authored, non-sensitive errors leave this boundary.
            if exc.code == "PROCESSING_APPROVAL_REQUIRED":
                raise _approval_required() from None
            if exc.code == "USAGE_LIMIT_REACHED":
                raise ServiceError("USAGE_LIMIT_REACHED", "The approved LIVE usage limit has been reached.", 429) from None
            raise _blocked() from None
        except Exception:
            raise _blocked() from None
        yield app


def live_status(app):
    """Return a read-only, configuration-level DTO; no source approval is implied."""
    result = {"enabled": False, "ready": False, "purposes": list(LIVE_PURPOSES), "budget_confirmed": False}
    try:
        values, approval = _configuration(app)
        result["enabled"] = True
        credit = app.models.credit_budget
        result["budget_confirmed"] = bool(_budget_matches(credit, approval) and credit.confirmed is True)
        _check_budgets(app, approval)
        _selection(app, values)
        result["ready"] = True
    except Exception:
        pass
    return result
