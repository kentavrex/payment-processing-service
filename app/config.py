from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    rabbitmq_url: str
    api_key: str

    outbox_poll_interval: float = 1.0
    outbox_batch_size: int = 100

    gateway_min_delay: float = 2.0
    gateway_max_delay: float = 5.0
    gateway_success_rate: float = 0.9

    max_attempts: int = 3
    retry_base_delay: float = 2.0
    webhook_timeout: float = 5.0
    webhook_allow_private_networks: bool = False
    consumer_prefetch: int = 10


settings = Settings()  # type: ignore[call-arg]
