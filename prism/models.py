"""Data model.

Design rules (the things Trakkr got wrong):
- Every answer is stored in full, word for word, with model, settings, tokens,
  cost and a hash. Nothing is summarised away.
- Several samples per prompt per engine per run; stats carry confidence ranges.
- Each answer records whether it came from model memory or live search.
- Spend is recorded per call so budgets can be enforced while a run is going.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- portfolio
class Brand(Base):
    __tablename__ = "brand"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    domain: Mapped[str] = mapped_column(String(200), default="")
    aliases: Mapped[list] = mapped_column(JSON, default=list)  # other names AI might use
    category: Mapped[str] = mapped_column(String(200), default="")
    is_portco: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    competitors: Mapped[list["Competitor"]] = relationship(back_populates="brand", cascade="all, delete-orphan")
    markets: Mapped[list["Market"]] = relationship(back_populates="brand", cascade="all, delete-orphan")
    prompts: Mapped[list["Prompt"]] = relationship(back_populates="brand", cascade="all, delete-orphan")


class Competitor(Base):
    __tablename__ = "competitor"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(200), default="")
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    brand: Mapped[Brand] = relationship(back_populates="competitors")


class Market(Base):
    __tablename__ = "market"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"), index=True)
    country: Mapped[str] = mapped_column(String(2), default="GB")  # ISO code
    language: Mapped[str] = mapped_column(String(10), default="en")
    city: Mapped[str] = mapped_column(String(100), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    brand: Mapped[Brand] = relationship(back_populates="markets")

    @property
    def label(self) -> str:
        return f"{self.country}/{self.language}" + (f" {self.city}" if self.city else "")


class Prompt(Base):
    __tablename__ = "prompt"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    intent: Mapped[str] = mapped_column(String(50), default="discovery")  # discovery|comparison|brand|product
    source: Mapped[str] = mapped_column(String(30), default="manual")  # manual|gsc|site_search|generated
    demand: Mapped[int] = mapped_column(Integer, default=0)  # e.g. GSC impressions behind it
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    brand: Mapped[Brand] = relationship(back_populates="prompts")


class Fact(Base):
    """The brand's source of truth, checked against what AI says."""
    __tablename__ = "fact"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"), index=True)
    attribute: Mapped[str] = mapped_column(String(200))  # e.g. "Headquarters", "Pricing model"
    value: Mapped[str] = mapped_column(Text)
    wrong_patterns: Mapped[list] = mapped_column(JSON, default=list)  # regexes that signal a known false claim
    source: Mapped[str] = mapped_column(String(300), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------------------------------------------------------- command centre
class Engine(Base):
    """One measured surface, e.g. 'ChatGPT (live search)'."""
    __tablename__ = "engine"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(40))  # openai|anthropic|gemini|perplexity|xai|deepseek|openai_compat|dataforseo|mock
    adapter: Mapped[str] = mapped_column(String(60))  # adapter name in providers.registry
    mode: Mapped[str] = mapped_column(String(20), default="search")  # search|memory|app
    model: Mapped[str] = mapped_column(String(120), default="")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)  # base_url, max_uses, preset, ...
    price_in_per_m: Mapped[float] = mapped_column(Float, default=0.0)  # USD per 1M input tokens
    price_out_per_m: Mapped[float] = mapped_column(Float, default=0.0)
    price_per_search: Mapped[float] = mapped_column(Float, default=0.0)  # USD per search call
    price_per_call: Mapped[float] = mapped_column(Float, default=0.0)  # flat fee per request (data providers)
    est_in_tokens: Mapped[int] = mapped_column(Integer, default=3000)  # for pre-run estimates
    est_out_tokens: Mapped[int] = mapped_column(Integer, default=700)
    est_searches: Mapped[float] = mapped_column(Float, default=1.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    concurrency: Mapped[int] = mapped_column(Integer, default=3)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_msg: Mapped[str] = mapped_column(Text, default="")

    def est_cost(self) -> float:
        return (self.est_in_tokens * self.price_in_per_m / 1e6
                + self.est_out_tokens * self.price_out_per_m / 1e6
                + self.est_searches * self.price_per_search
                + self.price_per_call)


class ProviderKey(Base):
    """Where a provider's credential lives. The secret itself is in Key Vault."""
    __tablename__ = "provider_key"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), unique=True)
    secret_name: Mapped[str] = mapped_column(String(120))
    last4: Mapped[str] = mapped_column(String(8), default="")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(200), default="")


class Budget(Base):
    """Monthly caps. scope='global' or scope='provider' with provider set."""
    __tablename__ = "budget"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(20), default="global")
    provider: Mapped[str] = mapped_column(String(40), default="")
    monthly_cap_usd: Mapped[float] = mapped_column(Float, default=300.0)
    alert_pcts: Mapped[list] = mapped_column(JSON, default=lambda: [50, 80, 100])
    alerts_sent: Mapped[dict] = mapped_column(JSON, default=dict)  # {"2026-11": [50, 80]}
    __table_args__ = (UniqueConstraint("scope", "provider", name="uq_budget_scope"),)


class Schedule(Base):
    __tablename__ = "schedule"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"), index=True)
    cadence: Mapped[str] = mapped_column(String(20), default="weekly")  # daily|weekly|fortnightly|monthly
    weekday: Mapped[int] = mapped_column(Integer, default=0)  # 0=Mon
    hour_utc: Mapped[int] = mapped_column(Integer, default=3)
    samples: Mapped[int] = mapped_column(Integer, default=3)
    engine_keys: Mapped[list] = mapped_column(JSON, default=list)  # empty = all enabled
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AppSetting(Base):
    __tablename__ = "app_setting"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class ApiToken(Base):
    """Service tokens for the REST API / MCP (not tied to one person's login)."""
    __tablename__ = "api_token"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    scope: Mapped[str] = mapped_column(String(20), default="read")  # read|write
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    user: Mapped[str] = mapped_column(String(200), default="system")
    action: Mapped[str] = mapped_column(String(120))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------- measurement
class Run(Base):
    __tablename__ = "run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"), index=True)
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("schedule.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned|running|done|partial|budget_stopped|failed
    triggered_by: Mapped[str] = mapped_column(String(200), default="schedule")
    samples: Mapped[int] = mapped_column(Integer, default=3)
    engine_keys: Mapped[list] = mapped_column(JSON, default=list)
    planned_calls: Mapped[int] = mapped_column(Integer, default=0)
    done_calls: Mapped[int] = mapped_column(Integer, default=0)
    failed_calls: Mapped[int] = mapped_column(Integer, default=0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    identical_to_previous_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class Answer(Base):
    __tablename__ = "answer"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id"), index=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"), index=True)
    prompt_id: Mapped[int] = mapped_column(ForeignKey("prompt.id"), index=True)
    market_id: Mapped[int | None] = mapped_column(ForeignKey("market.id"), nullable=True)
    engine_key: Mapped[str] = mapped_column(String(60), index=True)
    mode: Mapped[str] = mapped_column(String(20))  # search|memory|app
    sample_idx: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(120), default="")
    request_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    prompt_text: Mapped[str] = mapped_column(Text)  # exactly what was sent
    response_text: Mapped[str] = mapped_column(Text, default="")  # word for word
    raw: Mapped[str] = mapped_column(Text, default="")  # provider JSON, trimmed to a cap
    search_queries: Mapped[list] = mapped_column(JSON, default=list)
    citations: Mapped[list] = mapped_column(JSON, default=list)  # [{url, title, in_text}] as returned
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    search_calls: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok|error
    error: Mapped[str] = mapped_column(Text, default="")
    text_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    analysed: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_analysed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class Mention(Base):
    __tablename__ = "mention"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("answer.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(20))  # self|competitor|other
    entity_name: Mapped[str] = mapped_column(String(200), index=True)
    competitor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(Integer)  # 1 = first tracked entity named
    char_offset: Mapped[int] = mapped_column(Integer, default=0)
    count: Mapped[int] = mapped_column(Integer, default=1)
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)  # -1..1, LLM pass only
    recommended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class Citation(Base):
    __tablename__ = "citation"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("answer.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(200), index=True)
    path: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    owner_type: Mapped[str] = mapped_column(String(20), default="third_party")  # own|competitor|third_party
    owner_name: Mapped[str] = mapped_column(String(200), default="")
    content_ref: Mapped[str] = mapped_column(String(300), default="")  # e.g. Amplience content item id


class Claim(Base):
    """A statement AI made about a brand, checked against the fact sheet."""
    __tablename__ = "claim"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("answer.id"), index=True)
    brand_id: Mapped[int] = mapped_column(Integer, index=True)
    subject: Mapped[str] = mapped_column(String(200))
    attribute: Mapped[str] = mapped_column(String(200), default="")
    statement: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(String(20), default="unchecked")  # correct|wrong|unverifiable|unchecked
    fact_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    method: Mapped[str] = mapped_column(String(20), default="pattern")  # pattern|llm
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class SpendEntry(Base):
    __tablename__ = "spend"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    month: Mapped[str] = mapped_column(String(7), index=True)  # 2026-11
    provider: Mapped[str] = mapped_column(String(40), index=True)
    engine_key: Mapped[str] = mapped_column(String(60), default="")
    brand_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purpose: Mapped[str] = mapped_column(String(30), default="collect")  # collect|extract|test
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


# ---------------------------------------------------------------- improve + prove
class Change(Base):
    """Something the team shipped (page edit, new content, PR) to measure."""
    __tablename__ = "change"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text, default="")
    shipped_on: Mapped[datetime] = mapped_column(DateTime)
    target_prompt_ids: Mapped[list] = mapped_column(JSON, default=list)
    control_prompt_ids: Mapped[list] = mapped_column(JSON, default=list)  # empty = all other prompts
    window_days: Mapped[int] = mapped_column(Integer, default=28)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SiteAudit(Base):
    __tablename__ = "site_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    results: Mapped[dict] = mapped_column(JSON, default=dict)


class CrawlerHit(Base):
    """AI crawler requests ingested from server/CDN logs."""
    __tablename__ = "crawler_hit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    bot: Mapped[str] = mapped_column(String(60), index=True)
    operator: Mapped[str] = mapped_column(String(60), default="")
    purpose: Mapped[str] = mapped_column(String(30), default="")  # training|search|user
    path: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[int] = mapped_column(Integer, default=200)


class SiteLogo(Base):
    """A tracked company's icon (portco or competitor), keyed by its bare domain. Fetched once from the company's
    own site by the worker (prism/analysis/logos.py) or uploaded in the Command Centre, then served by Prism itself
    at /prism/logo/<domain>, so browsers never call third-party logo services. Raster images only, never SVG."""
    __tablename__ = "site_logo"
    domain: Mapped[str] = mapped_column(String(200), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok | none (nothing usable found) | upload
    content_type: Mapped[str] = mapped_column(String(60), default="")
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    source: Mapped[str] = mapped_column(Text, default="")  # the URL it came from, or "upload by <email>"
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=now)
