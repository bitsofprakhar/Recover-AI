from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "RecoverAI"
    database_url: str = "postgresql+psycopg2://recoverai:recoverai@localhost:5432/recoverai"

    max_recovery_attempts: int = 2
    case_window_hours: int = 24
    repeated_uncertain_failure_threshold: int = 3

    razorpay_webhook_secret: str = ""

    agent_llm_base_url: str = ""
    agent_llm_api_key: str = ""
    agent_llm_model: str = "agentrouter/glm-5.3"
    agent_max_tool_calls: int = 6

    score_high_threshold: int = 80
    score_stop_threshold: int = 35

    gate_reselection_budget: int = 1

    default_notification_channel: str = "EMAIL"

    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = 10
    verification_delay_seconds: int = 30
    sweep_interval_seconds: int = 60

    seed: int = 42

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
