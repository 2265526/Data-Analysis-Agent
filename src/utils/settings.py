"""统一配置管理(pydantic-settings)。

集中托管 API Key、数据库连接串、超时/重试等参数;
启动时自动校验必填项, 避免因漏配环境变量导致运行时崩溃。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录(本文件位于 src/utils/ 下)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """应用统一配置, 字段名与环境变量一一对应(见 .env)。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- LLM 服务 ----
    deepseek_api_key: str = Field(default="", description="DeepSeek API Key")
    deepseek_base_url: str = "https://api.deepseek.com"
    dashscope_api_key: str = Field(default="", description="阿里云百炼 API Key")
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # 百炼 OpenAI 兼容端点

    # 模型分级路由(开发流程 2.2 表1): 模型名统一在 .env 配置(MODEL_*), 改后重新 seed 同步到 model_routes 表
    model_supervisor: str = "qwen-flash"        # 路由分类(表1: 辅助模型, 走百炼)
    model_planner: str = "deepseek-v4-flash"    # 任务拆解(核心推理)
    model_coder: str = "deepseek-v4-flash"      # 代码生成/修复(核心推理)
    model_reporter: str = "deepseek-v4-flash"   # 报告汇总(核心推理)
    model_aux: str = "qwen-flash"               # 辅助任务: 错误分类/大结果摘要压缩(表1)
    model_switch_failures: int = 2              # 主备切换阈值: 连续失败次数(开发流程 2.2)

    # ---- 基础设施 ----
    database_url: str = "postgresql://postgres:236591@localhost:5432/postgres"
    redis_url: str = "redis://127.0.0.1:6379/0"
    chroma_host: str = "localhost"
    chroma_port: int = 8000
    chroma_collection: str = "schema_history"
    celery_enabled: bool = False   # 显式启用 Celery 队列才走 worker; 默认 FastAPI 后台任务直接执行

    # ---- 沙箱 ----
    sandbox_image: str = "data-sandbox:v1"
    sandbox_mem_limit: str = "512m"
    sandbox_cpu_quota: int = 50_000          # 50ms / 100ms 周期
    sandbox_timeout: int = 30                # 执行超时(秒)

    # ---- 可靠性 ----
    llm_timeout: int = 30                    # LLM 调用超时(秒)
    llm_max_retries: int = 3                 # LLM 重试次数
    structured_output_max_retries: int = 3   # 结构化输出格式错误上限
    circuit_failure_threshold: int = 5       # 熔断器失败阈值

    # ---- 人机协同 ----
    approval_timeout_hours: int = 24         # 审批超时自动处理(小时)
    approval_timeout_action: str = "reject"  # 超时默认策略: reject=拒绝 | continue=继续
    approval_threshold_rows: int = 10_000    # 超过该行数触发人工审批
    exec_result_full_limit_rows: int = 500   # 结果行数超过该值时: exec_result 截断进 state/prompt, 全量落盘 exec_full 供 reporter 精确统计

    # ---- 成本核算(优化方案-成本核算与预算控制) ----
    max_estimate_cost: float = 5.0           # Planner 后预估总成本上限(元), 超限转人工审批
    default_price_per_1k_prompt: float = 0.001     # 兜底单价(元/1k输入token, 无 model_routes 配置时)
    default_price_per_1k_completion: float = 0.002  # 兜底单价(元/1k输出token)

    # ---- 限流与缓存(阶段3 OR-11/OR-06) ----
    rate_limit_per_min: int = 20             # 全局限流: LLM 调用 20 次/分钟
    rate_limit_burst: int = 50               # 令牌桶容量
    rate_limit_wait_seconds: int = 60        # 限流时最大等待时间(秒), 超时拒绝并计数
    cache_ttl_seconds: int = 86400           # 结果缓存 TTL(1天)

    # ---- 报告 ----
    reports_retention_days: int = 7          # 报告保留天数(定时清理)
    report_timezone: str = "Asia/Shanghai"

    # ---- 上下文窗口管理(多轮对话) ----
    context_window_enabled: bool = True      # 总开关: 关闭后各节点退化为单轮(不注入历史)
    context_sliding_turns: int = 3           # L2 滑动层: 最多保留最近 N 轮(用户+助手各 N 条)
    context_summary_trigger_tokens: int = 4000  # L3 更早历史超过该 token 数时触发 qwen-flash 摘要
    context_result_max_chars: int = 500      # 历史任务报告/结论进上下文的截断长度(结果集防塞入)
    context_token_per_char: float = 0.6      # 近似 token 估算系数(中英混合约 0.6 token/字符)
    # 各节点"对话上下文"注入预算(只约束历史上下文块, 不含 schema/指标目录/系统提示)
    context_budget_planner_tokens: int = 6000
    context_budget_coder_tokens: int = 8000
    context_budget_supervisor_tokens: int = 1000
    context_budget_reporter_tokens: int = 2000

    # ---- 通知与认证(预留接口, 见 data/开发流程.docx 5.5/6.2/6.3 备注) ----
    notify_channel: str = "console"          # console|wecom|dingtalk|pagerduty(后三者为预留)
    wecom_webhook_url: str = ""              # 预留: 企业微信群机器人 webhook
    dingtalk_webhook_url: str = ""           # 预留: 钉钉机器人 webhook
    pagerduty_routing_key: str = ""          # 预留: PagerDuty routing key
    auth_mode: str = "dev"                   # dev|oauth2(oauth2 为本地 JWT 认证, 见 6.3 备注)
    jwt_secret: str = ""                     # JWT 签名密钥(oauth2 模式必填, >=16 字符)
    jwt_expire_minutes: int = 60             # JWT 有效期(分钟)

    # ---- 其他 ----
    app_port: int = 8001
    cors_origins: list[str] = ["*"]
    log_level: str = "INFO"
    checkpointer_backend: str = "postgres"   # postgres | sqlite | memory

    # ---- 派生路径 ----
    @property
    def project_root(self) -> Path:
        """项目根目录。"""
        return PROJECT_ROOT

    @property
    def static_dir(self) -> Path:
        """静态资源根目录(含 reports)。"""
        return PROJECT_ROOT / "static"

    @property
    def reports_dir(self) -> Path:
        """报告产物目录 static/reports。"""
        return self.static_dir / "reports"

    def validate_required(self) -> None:
        """启动时校验必填配置, 缺失直接抛错。"""
        if not self.deepseek_api_key and not self.dashscope_api_key:
            raise RuntimeError(
                "缺少 LLM API Key: 请在 .env 中配置 DEEPSEEK_API_KEY 或 DASHSCOPE_API_KEY"
            )


@lru_cache
def get_settings() -> Settings:
    """获取配置单例(带缓存, 全进程复用)。"""
    return Settings()
