from collections.abc import Mapping

from app.integrations.mlflow import MlflowConfig


MLFLOW_WORKSPACE_ENV_KEYS = {
    "MLFLOW_TRACKING_URI",
    "MLFLOW_TRACKING_TOKEN",
    "MLFLOW_TRACKING_USERNAME",
    "MLFLOW_TRACKING_PASSWORD",
    "MLFLOW_TRACKING_INSECURE_TLS",
}


def environment_from_config(config: MlflowConfig) -> dict[str, str]:
    """Build the standard MLflow client environment for one user."""
    environment = {"MLFLOW_TRACKING_URI": config.base_url}
    if config.auth_type == "bearer":
        environment["MLFLOW_TRACKING_TOKEN"] = config.secret
    elif config.auth_type == "basic":
        environment["MLFLOW_TRACKING_USERNAME"] = config.username
        environment["MLFLOW_TRACKING_PASSWORD"] = config.secret
    if not config.validate_tls:
        environment["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
    return environment


MLFLOW_SERVICE_ENV_KEYS = {
    "DISABLE_NGINX",
    "GUNICORN_CMD_ARGS",
}


def validate_mlflow_service_environment(values: Mapping | None) -> dict[str, str]:
    """Allow only the small serving-runtime surface controlled by AI Factory."""
    if not values:
        return {}
    if not isinstance(values, Mapping):
        raise ValueError("MLflow servis ayarları geçersiz.")
    environment = {}
    for raw_key, raw_value in values.items():
        key = str(raw_key)
        value = str(raw_value)
        if key not in MLFLOW_SERVICE_ENV_KEYS:
            raise ValueError("Desteklenmeyen MLflow servis ayarı.")
        if len(value) > 1024 or any(ord(character) < 32 for character in value):
            raise ValueError("MLflow servis ayarı geçersiz karakter içeriyor.")
        environment[key] = value
    return environment


def validate_mlflow_environment(values: Mapping | None) -> dict[str, str]:
    """Reject arbitrary environment injection at controller/worker boundaries."""
    if not values:
        return {}
    if not isinstance(values, Mapping):
        raise ValueError("MLflow workspace ayarları geçersiz.")
    environment = {}
    for raw_key, raw_value in values.items():
        key = str(raw_key)
        value = str(raw_value)
        if key not in MLFLOW_WORKSPACE_ENV_KEYS:
            raise ValueError("Desteklenmeyen MLflow workspace ayarı.")
        if len(value) > 4096 or any(ord(character) < 32 for character in value):
            raise ValueError("MLflow workspace ayarı geçersiz karakter içeriyor.")
        environment[key] = value
    return environment
