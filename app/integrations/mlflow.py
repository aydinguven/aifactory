import asyncio
import time
from dataclasses import dataclass

import httpx

from app.models.mlflow_settings import MlflowSettings
from app.models.mlflow_server_settings import MlflowServerSettings
from app.schemas.mlflow import MlflowSettingsUpdate
from app.security.secrets import SecretDecryptionError, decrypt_secret


class MlflowConfigurationError(ValueError):
    pass


class MlflowConnectionError(RuntimeError):
    pass


class MlflowPayloadTooLargeError(RuntimeError):
    pass


def _response_error_detail(response: httpx.Response) -> str:
    """Return a short upstream error without exposing request credentials."""
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(
                payload.get("message")
                or payload.get("detail")
                or payload.get("error_code")
                or ""
            )
    except ValueError:
        detail = response.text.strip()
    if not detail:
        return ""
    compact = " ".join(detail.split())
    return f": {compact[:500]}"


@dataclass(frozen=True)
class MlflowConfig:
    enabled: bool
    base_url: str
    auth_type: str
    username: str
    secret: str
    validate_tls: bool
    ca_cert_file: str
    timeout_seconds: int


def config_from_record(
    record: MlflowSettings,
    server: MlflowServerSettings,
) -> MlflowConfig:
    try:
        secret = decrypt_secret(record.encrypted_secret)
    except SecretDecryptionError as exc:
        raise MlflowConfigurationError(
            "Kayıtlı MLflow parolası/token'ı çözülemedi; yeniden kaydedin."
        ) from exc
    return MlflowConfig(
        enabled=record.enabled and server.enabled,
        base_url=server.base_url,
        auth_type=record.auth_type,
        username=record.username,
        secret=secret,
        validate_tls=server.validate_tls,
        ca_cert_file=server.ca_cert_file,
        timeout_seconds=server.timeout_seconds,
    )


def config_from_update(
    update: MlflowSettingsUpdate,
    server: MlflowServerSettings,
    record: MlflowSettings | None = None,
) -> MlflowConfig:
    secret = update.secret or ""
    if not secret and record:
        try:
            secret = decrypt_secret(record.encrypted_secret)
        except SecretDecryptionError as exc:
            raise MlflowConfigurationError(
                "Kayıtlı MLflow parolası/token'ı çözülemedi; yeniden kaydedin."
            ) from exc
    return MlflowConfig(
        enabled=update.enabled and server.enabled,
        base_url=server.base_url,
        auth_type=update.auth_type,
        username=update.username,
        secret=secret,
        validate_tls=server.validate_tls,
        ca_cert_file=server.ca_cert_file,
        timeout_seconds=server.timeout_seconds,
    )


def validate_config(config: MlflowConfig, require_enabled: bool = False) -> None:
    if require_enabled and not config.enabled:
        raise MlflowConfigurationError("MLflow entegrasyonu etkin değil.")
    if not config.base_url.startswith(("http://", "https://")):
        raise MlflowConfigurationError("MLflow adresi http:// veya https:// ile başlamalıdır.")
    parsed = httpx.URL(config.base_url)
    if not parsed.host or parsed.username or parsed.password:
        raise MlflowConfigurationError("Geçerli ve kullanıcı bilgisi içermeyen bir MLflow adresi girin.")
    if config.auth_type == "basic" and (not config.username or not config.secret):
        raise MlflowConfigurationError("Basic authentication için kullanıcı adı ve parola gereklidir.")
    if config.auth_type == "bearer" and not config.secret:
        raise MlflowConfigurationError("Bearer authentication için token gereklidir.")
    if config.ca_cert_file and not config.validate_tls:
        raise MlflowConfigurationError("Özel CA kullanılırken TLS doğrulaması açık olmalıdır.")


class MlflowClient:
    def __init__(self, config: MlflowConfig):
        validate_config(config)
        self.config = config

    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/json"}
        auth = None
        if self.config.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self.config.secret}"
        elif self.config.auth_type == "basic":
            auth = (self.config.username, self.config.secret)
        verify: bool | str = self.config.validate_tls
        if self.config.ca_cert_file:
            verify = self.config.ca_cert_file
        return httpx.AsyncClient(
            base_url=self.config.base_url,
            headers=headers,
            auth=auth,
            verify=verify,
            timeout=self.config.timeout_seconds,
            follow_redirects=False,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict:
        try:
            async with self._client() as client:
                # A relative request target preserves an optional reverse-proxy
                # prefix in the administrator-managed base URL.
                response = await client.request(
                    method, path.lstrip("/"), params=params, json=json
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            detail = _response_error_detail(exc.response)
            raise MlflowConnectionError(
                f"MLflow API isteği başarısız ({exc.response.status_code}){detail}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise MlflowConnectionError(f"MLflow API isteği başarısız: {exc}") from exc

    async def _get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, payload: dict) -> dict:
        return await self._request("POST", path, json=payload)

    async def _optional_text(self, path: str) -> str:
        """Probe optional server metadata without failing core connectivity."""
        try:
            async with self._client() as client:
                response = await client.get(path.lstrip("/"))
                response.raise_for_status()
                return response.text.strip()[:100]
        except (httpx.HTTPError, ValueError):
            return ""

    async def _request_bytes(
        self,
        path: str,
        *,
        params: dict | None = None,
        max_bytes: int,
    ) -> tuple[bytes, str]:
        try:
            async with self._client() as client:
                async with client.stream(
                    "GET", path.lstrip("/"), params=params
                ) as response:
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise MlflowPayloadTooLargeError(
                            f"Artifact önizleme sınırı {max_bytes} bayttır."
                        )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise MlflowPayloadTooLargeError(
                                f"Artifact önizleme sınırı {max_bytes} bayttır."
                            )
                        chunks.append(chunk)
                    return (
                        b"".join(chunks),
                        response.headers.get("content-type", ""),
                    )
        except MlflowPayloadTooLargeError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise MlflowConnectionError(f"MLflow artifact isteği başarısız: {exc}") from exc

    async def search_registered_models(
        self,
        search: str = "",
        page_token: str = "",
        max_results: int = 100,
    ) -> dict:
        params: dict[str, str | int | list[str]] = {
            "max_results": max(1, min(max_results, 200)),
            "order_by": ["last_updated_timestamp DESC"],
        }
        if search:
            safe_search = search.replace("'", "")
            params["filter"] = f"name LIKE '{safe_search}'"
        if page_token:
            params["page_token"] = page_token
        return await self._get("/api/2.0/mlflow/registered-models/search", params)

    async def get_registered_model(self, name: str) -> dict:
        return await self._get(
            "/api/2.0/mlflow/registered-models/get",
            {"name": name},
        )

    async def get_model_version(self, name: str, version: str) -> dict:
        """Fetch one immutable model version without relying on search ordering."""
        if not name.strip() or not version.isdigit() or int(version) < 1:
            raise MlflowConfigurationError(
                "Model adı ve pozitif sayısal model versiyonu gereklidir."
            )
        return await self._get(
            "/api/2.0/mlflow/model-versions/get",
            {"name": name, "version": str(int(version))},
        )

    async def set_model_version_tag(
        self, name: str, version: str, key: str, value: str
    ) -> None:
        """The one write AI Factory makes: record where a version was published."""
        if not name.strip() or not version.isdigit() or int(version) < 1:
            raise MlflowConfigurationError(
                "Model adı ve pozitif sayısal model versiyonu gereklidir."
            )
        await self._post(
            "/api/2.0/mlflow/model-versions/set-tag",
            {"name": name, "version": str(int(version)), "key": key, "value": value},
        )

    async def search_model_versions(
        self,
        name: str = "",
        run_id: str = "",
        max_results: int = 200,
    ) -> dict:
        if "'" in name or "'" in run_id:
            raise MlflowConfigurationError(
                "Model adı veya run ID içindeki tek tırnak API filtresiyle kullanılamıyor."
            )
        # Do not send order_by here. MLflow releases disagree on whether this
        # field is named version or version_number and some managed servers
        # reject either spelling. Sort the returned page locally.
        params: dict[str, str | int | list[str]] = {
            "max_results": max(1, min(max_results, 1000)),
        }
        filters = []
        if name:
            filters.append(f"name = '{name}'")
        if run_id:
            filters.append(f"run_id = '{run_id}'")
        if filters:
            params["filter"] = " AND ".join(filters)
        payload = await self._get(
            "/api/2.0/mlflow/model-versions/search",
            params,
        )
        versions = payload.get("model_versions") or []
        if isinstance(versions, list):
            versions.sort(
                key=lambda item: int(item.get("version") or 0)
                if str(item.get("version") or "").isdigit()
                else -1,
                reverse=True,
            )
        return payload

    async def search_experiments(
        self,
        page_token: str = "",
        max_results: int = 100,
    ) -> dict:
        payload: dict[str, object] = {
            "max_results": max(1, min(max_results, 1000)),
            "view_type": "ACTIVE_ONLY",
            "order_by": ["last_update_time DESC"],
        }
        if page_token:
            payload["page_token"] = page_token
        return await self._post("/api/2.0/mlflow/experiments/search", payload)

    async def get_experiment(self, experiment_id: str) -> dict:
        return await self._get(
            "/api/2.0/mlflow/experiments/get",
            {"experiment_id": experiment_id},
        )

    async def search_runs(
        self,
        experiment_ids: list[str],
        filter_string: str = "",
        page_token: str = "",
        max_results: int = 100,
    ) -> dict:
        payload: dict[str, object] = {
            "experiment_ids": experiment_ids,
            "filter": filter_string,
            "run_view_type": "ACTIVE_ONLY",
            "max_results": max(1, min(max_results, 1000)),
            "order_by": ["attributes.start_time DESC"],
        }
        if page_token:
            payload["page_token"] = page_token
        return await self._post("/api/2.0/mlflow/runs/search", payload)

    async def get_run(self, run_id: str) -> dict:
        return await self._get(
            "/api/2.0/mlflow/runs/get",
            {"run_id": run_id},
        )

    async def get_metric_history(
        self,
        run_id: str,
        metric_key: str,
        max_results: int = 2000,
    ) -> dict:
        return await self._get(
            "/api/2.0/mlflow/metrics/get-history",
            {
                "run_id": run_id,
                "metric_key": metric_key,
                "max_results": max(1, min(max_results, 5000)),
            },
        )

    async def list_artifacts(
        self,
        run_id: str,
        path: str = "",
        page_token: str = "",
    ) -> dict:
        params = {"run_id": run_id}
        if path:
            params["path"] = path
        if page_token:
            params["page_token"] = page_token
        return await self._get("/api/2.0/mlflow/artifacts/list", params)

    async def download_artifact(
        self,
        run_id: str,
        path: str,
        *,
        max_bytes: int,
    ) -> tuple[bytes, str]:
        return await self._request_bytes(
            "/get-artifact",
            params={"run_id": run_id, "path": path},
            max_bytes=max_bytes,
        )

    async def test(self) -> tuple[int, int, int, str]:
        started = time.monotonic()
        experiments_result, models_result, version = await asyncio.gather(
            self.search_experiments(max_results=1),
            self.search_registered_models(max_results=1),
            self._optional_text("/version"),
            return_exceptions=True,
        )
        if isinstance(experiments_result, Exception):
            raise experiments_result
        elapsed_ms = round((time.monotonic() - started) * 1000)
        experiment_count = len(experiments_result.get("experiments") or [])
        model_count = (
            len(models_result.get("registered_models") or [])
            if isinstance(models_result, dict)
            else -1
        )
        server_version = version if isinstance(version, str) else ""
        return experiment_count, model_count, elapsed_ms, server_version


def normalize_model(model: dict) -> dict:
    versions = model.get("latest_versions") or []
    versions = sorted(
        versions,
        key=lambda item: int(item.get("version") or 0),
        reverse=True,
    )
    latest = versions[0] if versions else None
    aliases = model.get("aliases") or []
    if isinstance(aliases, dict):
        aliases = list(aliases.keys())
    return {
        **model,
        "tags_map": {item.get("key", ""): item.get("value", "") for item in model.get("tags") or []},
        "aliases_list": aliases,
        "latest_version": latest,
        "version_count": len(versions),
    }


def _key_value_map(items: list[dict] | None) -> dict[str, object]:
    return {
        str(item.get("key", "")): item.get("value", "")
        for item in items or []
        if item.get("key") is not None
    }


def normalize_experiment(experiment: dict) -> dict:
    return {
        **experiment,
        "tags_map": _key_value_map(experiment.get("tags")),
    }


def normalize_run(run: dict) -> dict:
    info = run.get("info") or {}
    data = run.get("data") or {}
    tags_map = _key_value_map(data.get("tags"))
    return {
        **run,
        "info": info,
        "run_id": info.get("run_id") or info.get("run_uuid") or "",
        "experiment_id": info.get("experiment_id") or "",
        "run_name": tags_map.get("mlflow.runName") or info.get("run_name") or info.get("run_id") or "",
        "status": info.get("status") or "",
        "start_time": info.get("start_time"),
        "end_time": info.get("end_time"),
        "artifact_uri": info.get("artifact_uri") or "",
        "params_map": _key_value_map(data.get("params")),
        "metrics_map": _key_value_map(data.get("metrics")),
        "tags_map": tags_map,
    }
