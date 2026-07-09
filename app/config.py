from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class Server:
    key: str
    name: str
    api_url: str
    api_token: str
    sub_url: str
    verify_ssl: bool = False


@dataclass(frozen=True)
class Plan:
    id: str
    title: str
    days: int
    traffic_gb: int
    devices: int
    price_rub: int
    price_stars: int


@dataclass(frozen=True)
class TrialCfg:
    enabled: bool
    days: int
    traffic_gb: int
    devices: int


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: set[int]
    cryptobot_token: str
    db_path: str
    public_base_url: str
    shop_name: str
    support_url: str
    channel_url: str
    trial: TrialCfg
    referral_percent: int
    topup_amounts: list[int] = field(default_factory=list)
    servers: list[Server] = field(default_factory=list)
    plans: list[Plan] = field(default_factory=list)
    stars_per_rub: float = 0.7

    def server(self, key: str) -> Server | None:
        for srv in self.servers:
            if srv.key == key:
                return srv
        return None

    def plan(self, plan_id: str) -> Plan | None:
        for plan in self.plans:
            if plan.id == plan_id:
                return plan
        return None

    def stars_price(self, plan_or_rub: Plan | int) -> int:
        if isinstance(plan_or_rub, Plan):
            if plan_or_rub.price_stars > 0:
                return plan_or_rub.price_stars
            rub = plan_or_rub.price_rub
        else:
            rub = int(plan_or_rub)
        return max(1, math.ceil(rub * self.stars_per_rub))


def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError as exc:
                raise RuntimeError(
                    f"Ошибка конфигурации: ADMIN_IDS содержит не число: {part!r}. "
                    "Укажите Telegram ID администраторов через запятую."
                ) from exc
    return ids


def _load_yaml(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        raise RuntimeError(
            f"Ошибка конфигурации: файл {config_path!r} не найден. "
            "Скопируйте config.example.yml в config.yml и заполните его."
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Ошибка конфигурации: файл {config_path!r} должен содержать YAML-словарь."
        )
    return data


def _build_servers(raw: object) -> list[Server]:
    servers: list[Server] = []
    if not isinstance(raw, list):
        raw = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        servers.append(
            Server(
                key=str(item.get("key", "")).strip(),
                name=str(item.get("name", "")).strip(),
                api_url=str(item.get("api_url", "")).rstrip("/"),
                api_token=str(item.get("api_token", "")),
                sub_url=str(item.get("sub_url", "")).rstrip("/"),
                verify_ssl=bool(item.get("verify_ssl", False)),
            )
        )
    return servers


def _build_plans(raw: object) -> list[Plan]:
    plans: list[Plan] = []
    if not isinstance(raw, list):
        raw = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        plans.append(
            Plan(
                id=str(item.get("id", "")).strip(),
                title=str(item.get("title", "")).strip(),
                days=int(item.get("days", 30)),
                traffic_gb=int(item.get("traffic_gb", 0)),
                devices=int(item.get("devices", 0)),
                price_rub=int(item.get("price_rub", 0)),
                price_stars=int(item.get("price_stars", 0)),
            )
        )
    return plans


@lru_cache
def get_settings() -> Settings:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    admin_ids_raw = os.getenv("ADMIN_IDS", "").strip()
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

    missing: list[str] = []
    if not bot_token:
        missing.append("BOT_TOKEN (токен бота от @BotFather)")
    if not admin_ids_raw:
        missing.append("ADMIN_IDS (ID администраторов через запятую)")
    if not public_base_url:
        missing.append("PUBLIC_BASE_URL (публичный адрес страницы подписки)")
    if missing:
        raise RuntimeError(
            "Ошибка конфигурации: в .env не заданы обязательные переменные:\n"
            + "\n".join(f"  - {name}" for name in missing)
            + "\nСкопируйте .env.example в .env и заполните эти значения."
        )

    admin_ids = _parse_admin_ids(admin_ids_raw)
    cryptobot_token = os.getenv("CRYPTOBOT_TOKEN", "").strip()
    db_path = os.getenv("DB_PATH", "data/shop.db").strip() or "data/shop.db"
    config_path = os.getenv("CONFIG_PATH", "config.yml").strip() or "config.yml"

    cfg = _load_yaml(config_path)

    trial_raw = cfg.get("trial") or {}
    if not isinstance(trial_raw, dict):
        trial_raw = {}
    trial = TrialCfg(
        enabled=bool(trial_raw.get("enabled", False)),
        days=int(trial_raw.get("days", 3)),
        traffic_gb=int(trial_raw.get("traffic_gb", 0)),
        devices=int(trial_raw.get("devices", 1)),
    )

    servers = _build_servers(cfg.get("servers"))
    plans = _build_plans(cfg.get("plans"))

    problems: list[str] = []
    if not servers:
        problems.append("не задан ни один сервер в секции servers")
    for srv in servers:
        if not srv.key or not srv.api_url or not srv.api_token or not srv.sub_url:
            problems.append(
                f"у сервера {srv.key or srv.name or '<без key>'!s} не заполнены "
                "key/api_url/api_token/sub_url"
            )
    if not plans:
        problems.append("не задан ни один тариф в секции plans")
    for plan in plans:
        if not plan.id or not plan.title:
            problems.append(f"у тарифа {plan.id or plan.title or '<без id>'!s} не заполнены id/title")
    if problems:
        raise RuntimeError(
            f"Ошибка конфигурации в {config_path}:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\nСверьтесь с config.example.yml."
        )

    topup_raw = cfg.get("topup_amounts") or []
    topup_amounts = [int(x) for x in topup_raw] if isinstance(topup_raw, list) else []

    return Settings(
        bot_token=bot_token,
        admin_ids=admin_ids,
        cryptobot_token=cryptobot_token,
        db_path=db_path,
        public_base_url=public_base_url,
        shop_name=str(cfg.get("shop_name", "VPN Shop")),
        support_url=str(cfg.get("support_url", "")),
        channel_url=str(cfg.get("channel_url", "") or ""),
        trial=trial,
        referral_percent=int(cfg.get("referral_percent", 0)),
        topup_amounts=topup_amounts,
        servers=servers,
        plans=plans,
        stars_per_rub=float(cfg.get("stars_per_rub", 0.7)),
    )
