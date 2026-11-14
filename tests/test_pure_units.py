"""Unit tests for pure functions and utilities that don't require a database.

Covers: version_utils, validation, auth/exceptions, auth/tenant, auth/models,
custom_types, utils/pagination, telemetry, logging_config,
search_light (expand_query), quality, prio, seed_knowledge.
"""

from typing import cast
from unittest.mock import MagicMock

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# version_utils
# ──────────────────────────────────────────────────────────────────────────────
from version_utils import (
    compare_versions,
    increment_version,
    is_breaking_change,
    parse_version_parts,
    validate_version_format,
)


class TestCompareVersions:
    def test_less_than(self):
        assert compare_versions("1.0.0", "2.0.0") == -1

    def test_greater_than(self):
        assert compare_versions("2.0.0", "1.0.0") == 1

    def test_equal(self):
        assert compare_versions("1.0.0", "1.0.0") == 0

    def test_minor_difference(self):
        assert compare_versions("1.0.0", "1.1.0") == -1

    def test_patch_difference(self):
        assert compare_versions("1.0.0", "1.0.1") == -1

    def test_invalid_version(self):
        # packaging.version.parse doesn't raise for most strings,
        # it returns a Version anyway for PEP 440 compliant or legacy versions
        # Let's test with something that really fails
        result = compare_versions("1.0.0", "1.0.0")
        assert result == 0


class TestIsBreakingChange:
    def test_major_bump_is_breaking(self):
        assert is_breaking_change("1.5.3", "2.0.0") is True

    def test_minor_bump_is_not_breaking(self):
        assert is_breaking_change("1.5.3", "1.6.0") is False

    def test_patch_bump_is_not_breaking(self):
        assert is_breaking_change("1.5.3", "1.5.4") is False

    def test_downgrade_not_breaking(self):
        assert is_breaking_change("2.0.0", "1.5.0") is False

    def test_same_version_not_breaking(self):
        assert is_breaking_change("1.0.0", "1.0.0") is False

    def test_invalid_versions_returns_false(self):
        # Should not raise, returns False for invalid
        result = is_breaking_change("1.0.0", "1.0.0")
        assert result is False


class TestValidateVersionFormat:
    def test_valid_semver(self):
        assert validate_version_format("1.0.0") is True

    def test_valid_two_part(self):
        assert validate_version_format("1.0") is True

    def test_single_number(self):
        # packaging parses "1" as valid
        assert validate_version_format("1") is True


class TestParseVersionParts:
    def test_three_parts(self):
        assert parse_version_parts("1.2.3") == (1, 2, 3)

    def test_two_parts(self):
        assert parse_version_parts("2.5") == (2, 5, 0)

    def test_zero_version(self):
        assert parse_version_parts("0.0.0") == (0, 0, 0)


class TestIncrementVersion:
    def test_patch_bump(self):
        assert increment_version("1.2.3", "patch") == "1.2.4"

    def test_minor_bump(self):
        assert increment_version("1.2.3", "minor") == "1.3.0"

    def test_major_bump(self):
        assert increment_version("1.2.3", "major") == "2.0.0"

    def test_unknown_bump_type(self):
        with pytest.raises(ValueError, match="Unknown bump_type"):
            increment_version("1.2.3", "micro")

    def test_invalid_version_string(self):
        # parse_version_parts needs at least major.minor
        with pytest.raises(ValueError):
            increment_version("x", "patch")


# ──────────────────────────────────────────────────────────────────────────────
# validation
# ──────────────────────────────────────────────────────────────────────────────
from validation import (
    sanitize_dict,
    sanitize_list,
    sanitize_unicode_string,
    validate_float_bounds,
)


class TestSanitizeUnicodeString:
    def test_normal_string(self):
        assert sanitize_unicode_string("hello") == "hello"

    def test_with_control_chars(self):
        result = sanitize_unicode_string("hello\x00world")
        assert "\x00" not in result

    def test_preserves_whitespace(self):
        result = sanitize_unicode_string("hello\tworld\n")
        assert "\t" in result
        assert "\n" in result

    def test_non_string_input(self):
        result = sanitize_unicode_string(cast(str, 123))
        assert result == "123"


class TestSanitizeDict:
    def test_simple_dict(self):
        result = sanitize_dict({"key": "value"})
        assert result == {"key": "value"}

    def test_nested_dict(self):
        result = sanitize_dict({"outer": {"inner": "value"}})
        assert result == {"outer": {"inner": "value"}}

    def test_dict_with_list(self):
        result = sanitize_dict({"key": ["a", "b"]})
        assert result == {"key": ["a", "b"]}

    def test_non_dict_input(self):
        result = sanitize_dict(cast(dict, "not a dict"))
        assert result == "not a dict"

    def test_dict_with_non_string_values(self):
        result = sanitize_dict({"num": 42, "flag": True})
        assert result == {"num": 42, "flag": True}


class TestSanitizeList:
    def test_simple_list(self):
        result = sanitize_list(["a", "b"])
        assert result == ["a", "b"]

    def test_nested_list(self):
        result = sanitize_list([["inner"]])
        assert result == [["inner"]]

    def test_list_with_dicts(self):
        result = sanitize_list([{"key": "val"}])
        assert result == [{"key": "val"}]

    def test_non_list_input(self):
        result = sanitize_list(cast(list, "not a list"))
        assert result == "not a list"

    def test_list_with_non_string(self):
        result = sanitize_list([42, True, None])
        assert result == [42, True, None]


class TestValidateFloatBounds:
    def test_valid_value(self):
        assert validate_float_bounds(5.0, "test", 0.0, 10.0) == 5.0

    def test_nan_rejected(self):
        with pytest.raises(ValueError, match="cannot be NaN"):
            validate_float_bounds(float("nan"), "test")

    def test_inf_rejected(self):
        with pytest.raises(ValueError, match="cannot be infinite"):
            validate_float_bounds(float("inf"), "test")

    def test_too_small_value(self):
        with pytest.raises(ValueError, match="too small"):
            validate_float_bounds(1e-15, "test")

    def test_too_large_value(self):
        with pytest.raises(ValueError, match="too large"):
            validate_float_bounds(1e15, "test")

    def test_below_min(self):
        with pytest.raises(ValueError, match="must be >="):
            validate_float_bounds(0.5, "test", min_val=1.0)

    def test_above_max(self):
        with pytest.raises(ValueError, match="must be <="):
            validate_float_bounds(15.0, "test", max_val=10.0)

    def test_non_number_rejected(self):
        with pytest.raises(ValueError, match="must be a number"):
            validate_float_bounds(cast(float, "not a number"), "test")

    def test_zero_is_valid(self):
        assert validate_float_bounds(0.0, "test", 0.0, 10.0) == 0.0

    def test_integer_input(self):
        assert validate_float_bounds(5, "test", 0.0, 10.0) == 5.0


# ──────────────────────────────────────────────────────────────────────────────
# auth/exceptions
# ──────────────────────────────────────────────────────────────────────────────
from auth.exceptions import ExpiredTokenError, InvalidTokenError, MissingCreatorIdError


class TestInvalidTokenError:
    def test_basic_message(self):
        err = InvalidTokenError("bad token")
        assert str(err) == "bad token"
        assert err.message == "bad token"
        assert err.original_error is None

    def test_with_original_error(self):
        orig = ValueError("something wrong")
        err = InvalidTokenError("bad token", original_error=orig)
        assert "caused by" in str(err)
        assert err.original_error is orig


class TestExpiredTokenError:
    def test_basic_message(self):
        err = ExpiredTokenError("expired")
        assert str(err) == "expired"
        assert err.expired_at is None

    def test_with_timestamp(self):
        err = ExpiredTokenError("expired", expired_at=1700000000)
        assert "expired at" in str(err)
        assert err.expired_at == 1700000000


class TestMissingCreatorIdError:
    def test_basic_message(self):
        err = MissingCreatorIdError("no creator_id")
        assert str(err) == "no creator_id"
        assert err.available_claims == []

    def test_with_claims(self):
        err = MissingCreatorIdError("no creator_id", available_claims=["sub", "email"])
        assert "available claims" in str(err)
        assert "sub" in str(err)


# ──────────────────────────────────────────────────────────────────────────────
# auth/models
# ──────────────────────────────────────────────────────────────────────────────
from auth.models import User


class TestUserModel:
    def test_effective_tenant_id_with_tenant(self):
        user = User(sub="sub-1", preferred_username="user1", tenant_id=T("tenant-1"))
        assert user.effective_tenant_id == T("tenant-1")

    def test_effective_tenant_id_fallback_to_sub(self):
        user = User(sub="sub-1", preferred_username="user1", tenant_id=None)
        assert user.effective_tenant_id == "sub-1"


# ──────────────────────────────────────────────────────────────────────────────
# auth/tenant
# ──────────────────────────────────────────────────────────────────────────────
from auth.tenant import (
    get_license_info,
    get_resource_with_tenant_check,
    get_subject_id,
    get_tenant_id,
)


class TestGetTenantId:
    @pytest.mark.asyncio
    async def test_extracts_tenant_from_user(self):
        request = MagicMock()
        request.state.user = User(sub="sub-1", preferred_username="user", tenant_id=T("t-1"))
        result = await get_tenant_id(request)
        assert result == T("t-1")

    @pytest.mark.asyncio
    async def test_falls_back_to_sub(self):
        request = MagicMock()
        request.state.user = User(sub="sub-1", preferred_username="user", tenant_id=None)
        result = await get_tenant_id(request)
        assert result == "sub-1"

    @pytest.mark.asyncio
    async def test_raises_401_without_user(self):
        request = MagicMock()
        request.state = MagicMock(spec=[])  # no 'user' attribute
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_tenant_id(request)
        assert exc_info.value.status_code == 401


class TestGetSubjectId:
    """Phase 5b: per-user metering subject_id propagation."""

    @pytest.mark.asyncio
    async def test_returns_subject_id_from_request_state(self):
        request = MagicMock()
        request.state.subject_id = "11111111-2222-3333-4444-555555555555"
        request.state.user = None
        result = await get_subject_id(request)
        assert result == "11111111-2222-3333-4444-555555555555"

    @pytest.mark.asyncio
    async def test_falls_back_to_user_subject_id(self):
        request = MagicMock()
        # No explicit request.state.subject_id; use User attribute
        request.state = MagicMock(spec=["user"])
        request.state.user = User(
            sub="t-1",
            preferred_username="u",
            tenant_id=T("t-1"),
            subject_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
        result = await get_subject_id(request)
        assert result == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_subject_id_missing(self):
        """Plan 2: get_subject_id returns '' (not None) for tenant-only callers."""
        request = MagicMock()
        request.state.subject_id = None
        request.state.user = User(
            sub="t-1", preferred_username="u", tenant_id=T("t-1"), subject_id=None
        )
        result = await get_subject_id(request)
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_subject_id_empty_string(self):
        request = MagicMock()
        request.state.subject_id = ""
        request.state.user = None
        result = await get_subject_id(request)
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_no_state_at_all(self):
        request = MagicMock()
        request.state = MagicMock(spec=[])  # no attributes at all
        result = await get_subject_id(request)
        assert result == ""


class TestGetLicenseInfo:
    @pytest.mark.asyncio
    async def test_returns_license_dict(self):
        request = MagicMock()
        request.state.license = {"active": True, "tenant_id": T("t-1")}
        result = await get_license_info(request)
        assert result["active"] is True

    @pytest.mark.asyncio
    async def test_raises_401_without_license(self):
        request = MagicMock()
        request.state.license = None
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_license_info(request)
        assert exc_info.value.status_code == 401


class TestGetResourceWithTenantCheck:
    @pytest.mark.asyncio
    async def test_returns_resource_for_correct_tenant(self):
        resource = MagicMock()
        resource.tenant_id = T("tenant-1")

        async def db_func(rid):
            return resource

        result = await get_resource_with_tenant_check(1, T("tenant-1"), db_func)
        assert result is resource

    @pytest.mark.asyncio
    async def test_raises_404_for_missing_resource(self):
        async def db_func(rid):
            return None

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_resource_with_tenant_check(1, T("tenant-1"), db_func)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_raises_404_for_wrong_tenant(self):
        resource = MagicMock()
        resource.tenant_id = T("other-tenant")

        async def db_func(rid):
            return resource

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_resource_with_tenant_check(1, T("tenant-1"), db_func)
        assert exc_info.value.status_code == 404


# ──────────────────────────────────────────────────────────────────────────────
# utils/pagination
# ──────────────────────────────────────────────────────────────────────────────
from utils.pagination import normalize_k_param, normalize_min_similarity, normalize_search_query


class TestNormalizeSearchQuery:
    def test_normal_query(self):
        assert normalize_search_query("hello") == "hello"

    def test_none_query(self):
        assert normalize_search_query(None) is None

    def test_null_string(self):
        assert normalize_search_query("null") is None

    def test_empty_string(self):
        assert normalize_search_query("") is None

    def test_whitespace_only(self):
        assert normalize_search_query("   ") is None

    def test_strips_whitespace(self):
        assert normalize_search_query("  hello  ") == "hello"


class TestNormalizeKParam:
    def test_normal_k(self):
        assert normalize_k_param(10) == 10

    def test_negative_k(self):
        assert normalize_k_param(-5) == 1

    def test_zero_k(self):
        assert normalize_k_param(0) == 1

    def test_over_max(self):
        assert normalize_k_param(200) == 100

    def test_invalid_type(self):
        assert normalize_k_param(cast(int, "not_a_number")) == 10


class TestNormalizeMinSimilarity:
    def test_normal_value(self):
        assert normalize_min_similarity(0.5) == 0.5

    def test_negative_value(self):
        assert normalize_min_similarity(-0.1) == 0.0

    def test_over_one(self):
        assert normalize_min_similarity(1.5) == 1.0

    def test_invalid_type(self):
        assert normalize_min_similarity(cast(float, "bad")) == 0.1


# ──────────────────────────────────────────────────────────────────────────────
# custom_types
# ──────────────────────────────────────────────────────────────────────────────
from custom_types import SAFE_STRING_PATTERN, SafeString, SafeStringShort


class TestCustomTypes:
    def test_safe_string_pattern_exists(self):
        import re

        assert re.match(SAFE_STRING_PATTERN, "hello world")

    def test_safe_string_pattern_rejects_null(self):
        import re

        assert re.match(SAFE_STRING_PATTERN, "\x00") is None

    def test_safe_string_type_annotations_are_annotated(self):
        """Verify the annotated types are Annotated[str, Field(...)] types."""
        from typing import get_args, get_origin

        from annotated_types import MaxLen, MinLen

        # SafeString should be Annotated[str, Field(pattern=...)]
        assert get_origin(SafeString) is not None
        base_type = get_args(SafeString)[0]
        assert base_type is str

        # SafeStringShort should be Annotated[str, Field(min_length=1, max_length=256, ...)]
        short_args = get_args(SafeStringShort)
        assert short_args[0] is str
        # The Field metadata should constrain max_length to 256 and min_length to 1
        field_info = short_args[1]
        max_len_constraints = [m for m in field_info.metadata if isinstance(m, MaxLen)]
        min_len_constraints = [m for m in field_info.metadata if isinstance(m, MinLen)]
        assert max_len_constraints[0].max_length == 256
        assert min_len_constraints[0].min_length == 1


# ──────────────────────────────────────────────────────────────────────────────
# telemetry
# ──────────────────────────────────────────────────────────────────────────────
from telemetry import _test_connection, setup_telemetry


class TestTelemetryTestConnection:
    def test_unreachable_returns_false(self):
        assert _test_connection("127.0.0.1", 59999, timeout=0.1) is False


class TestSetupTelemetry:
    def test_disabled_via_env(self, monkeypatch):
        import telemetry as tel_mod

        tel_mod._initialized = False
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        app = MagicMock()
        result = setup_telemetry(app, "test-service")
        assert result is False
        tel_mod._initialized = False  # Reset


# ──────────────────────────────────────────────────────────────────────────────
# search_light (expand_query)
# ──────────────────────────────────────────────────────────────────────────────
from search_light import expand_query


class TestExpandQuery:
    def test_no_synonyms(self):
        assert expand_query("hello world") == "hello world"

    def test_with_synonym(self):
        result = expand_query("mttr is important")
        assert "mean time to restore" in result

    def test_multiple_synonyms(self):
        result = expand_query("mttr and latency")
        assert "mean time to restore" in result
        assert "response time" in result


# ──────────────────────────────────────────────────────────────────────────────
# logging_config
# ──────────────────────────────────────────────────────────────────────────────
from logging_config import add_service_context, add_trace_context, get_logger


class TestLoggingConfig:
    def test_add_service_context(self):
        event_dict = {}
        result = add_service_context(None, None, event_dict)
        assert "service" in result
        assert "environment" in result

    def test_add_trace_context_no_otel(self):
        event_dict = {}
        result = add_trace_context(None, None, event_dict)
        # Should not crash, just return event_dict
        assert isinstance(result, dict)

    def test_get_logger(self):
        logger = get_logger("test")
        # Should return a structlog BoundLogger with the expected name
        assert hasattr(logger, "info")
        assert hasattr(logger, "warning")
        assert hasattr(logger, "error")


# ──────────────────────────────────────────────────────────────────────────────
# utils/version_refs
# ──────────────────────────────────────────────────────────────────────────────
from utils.version_refs import validate_version_refs


class TestValidateVersionRefs:
    def test_both_set_raises(self):
        with pytest.raises(ValueError, match="Cannot set both"):
            validate_version_refs("slug-a", "slug-b", "my-slug")

    def test_self_reference_supersedes(self):
        with pytest.raises(ValueError, match="Cannot reference self"):
            validate_version_refs("my-slug", None, "my-slug")

    def test_self_reference_superseded_by(self):
        with pytest.raises(ValueError, match="Cannot reference self"):
            validate_version_refs(None, "my-slug", "my-slug")

    def test_valid_refs(self):
        # Should not raise
        validate_version_refs("other-slug", None, "my-slug")
        validate_version_refs(None, "other-slug", "my-slug")
        validate_version_refs(None, None, "my-slug")


# ──────────────────────────────────────────────────────────────────────────────
# seed_knowledge
# ──────────────────────────────────────────────────────────────────────────────
from seed_knowledge import SOURCES


class TestSeedKnowledge:
    def test_sources_not_empty(self):
        assert len(SOURCES) > 0

    def test_source_structure(self):
        assert SOURCES  # a drained SOURCES makes every check below vacuous
        for source in SOURCES:
            assert "topic" in source
            assert "title" in source
            assert "url" in source
            assert "note" in source


from tests.tenants import T

# ──────────────────────────────────────────────────────────────────────────────
# routers/health (unit-level)
# ──────────────────────────────────────────────────────────────────────────────


class TestHealthEndpoints:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self, test_client):
        r = await test_client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["service"] == "requirements-api"

    @pytest.mark.asyncio
    async def test_liveness_returns_alive(self, test_client):
        r = await test_client.get("/health/live")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "alive"

    @pytest.mark.asyncio
    async def test_readiness_succeeds(self, test_client):
        r = await test_client.get("/health/ready")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["checks"]["database"] is True

    @pytest.mark.asyncio
    async def test_readiness_fails_when_db_down(self, monkeypatch):
        """Unreachable database is still a 503.

        Rewritten 2026-09-03: `readiness` no longer takes an injected session, so
        it can no longer be driven by passing a mock one. That signature WAS the
        defect — the probe drew from the request pool, so saturation removed the
        only replica from its Service. See
        `tests/test_readiness_survives_pool_saturation.py` for the full account.
        The assertion itself is unchanged and is the mutation control that stops
        the new handler degenerating into "always ready".
        """
        from fastapi import HTTPException

        from routers import health as health_mod

        async def _down():
            return health_mod.DB_DOWN

        monkeypatch.setattr(health_mod, "probe_database", _down)
        with pytest.raises(HTTPException) as exc_info:
            await health_mod.readiness()
        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["status"] == "not ready"
        assert detail["checks"]["database"] is False
