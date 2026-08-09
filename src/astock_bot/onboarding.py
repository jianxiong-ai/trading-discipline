from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .portfolio_store import PortfolioStoreError, _normalize_symbol


COMPANY_SURVEY_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/CompanySurveyAjax"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"
DEFAULT_LLM_MODEL = "deepseek-v4-flash"

SECTOR_CATALOG: dict[str, dict[str, Any]] = {
    "copper": {
        "label": "铜产业",
        "route": "沪铜、铜仓单、同行与公司公告",
        "keywords": ("铜矿", "铜冶炼", "阴极铜", "铜加工", "铜业"),
        "peers": ("601899.SH", "000630.SZ", "000878.SZ"),
        "drivers": ("铜价与库存变化", "矿山与冶炼盈利", "产量及资本开支"),
        "risks": ("铜价下行", "冶炼费承压", "海外项目与汇率波动"),
    },
    "insurance": {
        "label": "寿险",
        "route": "长端利率、权益市场、保险同行与公司公告",
        "keywords": ("人寿保险", "寿险", "保险业务", "保险行业"),
        "peers": ("601628.SH", "601601.SH", "601319.SH", "601318.SH"),
        "drivers": ("新业务价值与价值率", "长端利率", "投资收益与保费增速"),
        "risks": ("利率下行", "资本市场波动", "负债成本与偿付能力压力"),
    },
    "insurance_financial_group": {
        "label": "综合金融",
        "route": "保险与银行同行、长端利率、权益市场及公司公告",
        "keywords": ("综合金融", "保险、银行", "保险 银行", "金融服务集团"),
        "peers": ("601628.SH", "601601.SH", "601336.SH", "000001.SZ"),
        "drivers": ("寿险新业务价值", "资产质量与投资收益", "综合金融协同"),
        "risks": ("利率与权益市场波动", "地产及银行资产质量", "资本约束"),
    },
    "new_energy_vehicle": {
        "label": "新能源车",
        "route": "新能源车产销、汽车同行与公司公告",
        "keywords": ("新能源汽车", "汽车零部件", "智能驾驶", "线控制动", "底盘", "汽车电子", "动力电池"),
        "peers": ("002594.SZ", "601633.SH", "601238.SH"),
        "drivers": ("新能源车产销", "客户与订单放量", "单车价值量和产品升级"),
        "risks": ("行业价格竞争", "客户集中", "新项目量产不及预期"),
    },
    "satellite_communications": {
        "label": "航天通信",
        "route": "卫星通信产业事件、航天通信同行与公司公告",
        "keywords": ("卫星通信", "卫星互联网", "航天通信", "空间信息", "卫星应用", "卫星制造"),
        "peers": ("600118.SH", "601698.SH", "002465.SZ"),
        "drivers": ("卫星组网与发射进度", "商业化订单", "通信牌照与应用落地"),
        "risks": ("项目进度不确定", "订单确认周期长", "政策与技术路线变化"),
    },
    "semiconductor": {
        "label": "半导体",
        "route": "集成电路产量、电子信息制造业、同行与公司公告",
        "keywords": ("半导体", "集成电路", "芯片", "晶圆", "封装测试", "功率器件"),
        "peers": ("688981.SH", "603986.SH", "002371.SZ"),
        "drivers": ("行业景气与国产替代", "新品放量", "产能利用率和库存周期"),
        "risks": ("行业周期下行", "研发与制程迭代", "库存及客户集中"),
    },
    "optical_communications": {
        "label": "光通信",
        "route": "通信业月报、光纤光缆与光通信同行、公司公告",
        "keywords": ("光通信", "光纤", "光缆", "光模块", "海底电缆", "海缆"),
        "peers": ("600498.SH", "601869.SH", "600522.SH"),
        "drivers": ("算力网络与通信投资", "光纤光缆需求", "高端产品及海外订单"),
        "risks": ("运营商资本开支波动", "产品价格竞争", "海外交付与贸易风险"),
    },
    "generic": {
        "label": "待补充行业",
        "route": "技术面、公告、公司行动与两融；产业证据待补充",
        "keywords": (),
        "peers": (),
        "drivers": ("主营业务景气", "盈利兑现", "估值与催化"),
        "risks": ("产业证据尚未结构化", "同行样本不足", "公司经营不及预期"),
    },
}


@dataclass(frozen=True)
class OnboardingResult:
    symbol: str
    name: str
    sector: str
    peers: list[str]
    analysis_profile: dict[str, Any]


class StockOnboardingService:
    """Build a bounded research profile before a security enters the tracker."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        profile_fetcher: Callable[[str], dict[str, Any]] | None = None,
        llm_classifier: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.config = config or {}
        self.profile_fetcher = profile_fetcher or self._fetch_company_profile
        self.llm_classifier = llm_classifier or self._classify_with_llm

    @property
    def llm_api_key(self) -> str:
        # OPENAI_API_KEY remains a compatibility fallback for existing local deployments.
        return (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()

    @property
    def llm_available(self) -> bool:
        return bool(self.config.get("llm_enabled", True) and self.llm_api_key)

    @property
    def llm_model(self) -> str:
        return str(
            self.config.get("llm_model")
            or os.getenv("LLM_MODEL")
            or os.getenv("OPENAI_MODEL")
            or DEFAULT_LLM_MODEL
        )

    @property
    def llm_responses_url(self) -> str:
        base_url = str(
            self.config.get("llm_base_url")
            or os.getenv("LLM_BASE_URL")
            or DEFAULT_LLM_BASE_URL
        ).strip().rstrip("/")
        if base_url.endswith("/v1/responses"):
            return base_url
        if base_url.endswith("/v1"):
            return f"{base_url}/responses"
        return f"{base_url}/v1/responses"

    def onboard(
        self,
        symbol: str,
        *,
        use_llm: bool = False,
        manual_name: str = "",
        manual_sector: str = "",
        manual_peers: str = "",
    ) -> OnboardingResult:
        normalized = _normalize_symbol(symbol)
        profile_error = ""
        try:
            source = self.profile_fetcher(normalized)
        except Exception as exc:  # external source failures should become a clear UI state
            source = {}
            profile_error = str(exc)

        name = manual_name.strip() or str(source.get("name", "")).strip()
        if not name:
            raise PortfolioStoreError("未能自动识别公司名称，请展开“手动补充”填写名称后重试")

        rule = self._rule_classification(normalized, source)
        classification = dict(rule)
        source_kind = "rule"
        llm_status = "未使用"
        if use_llm:
            if not self.llm_available:
                llm_status = "未配置 LLM API 密钥，已使用规则归类"
            else:
                try:
                    proposal = self.llm_classifier(source, rule)
                    # LLM is intentionally limited to research prose. Industry and peers
                    # feed evidence routing and relative-strength calculations, so only
                    # deterministic rules or an explicit manual choice may set them.
                    classification = self._validated_llm_result(
                        proposal, rule, normalized, allow_routing_override=False
                    )
                    source_kind = "llm_review"
                    llm_status = f"已由 {self.llm_model} 复核研究描述；行业与同行仍使用规则路径"
                except Exception as exc:
                    llm_status = f"LLM 复核失败，已回退规则归类：{str(exc)[:80]}"

        if manual_sector:
            if manual_sector not in SECTOR_CATALOG:
                raise PortfolioStoreError("手动行业必须从页面选项中选择")
            classification = self._classification_for(manual_sector, normalized)
            source_kind = "manual"
        if manual_peers.strip():
            classification["peers"] = _parse_peer_text(manual_peers, normalized)

        sector = str(classification["sector"])
        catalog = SECTOR_CATALOG[sector]
        # Both exchanges now use official structured announcement queries. ETFs
        # remain not-applicable for company disclosures in the evidence layer.
        coverage = "full" if sector != "generic" else "basic"
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        analysis_profile = {
            "coverage": coverage,
            "coverage_label": "完整跟踪" if coverage == "full" else "待补齐公告/产业证据",
            "classification_source": source_kind,
            "classification_label": {"rule": "规则识别", "llm_review": "LLM 复核", "manual": "手动指定"}[source_kind],
            "confidence": str(classification.get("confidence", "medium")),
            "sector_label": catalog["label"],
            "source_industry": str(source.get("industry", "")).strip(),
            "business_summary": str(classification.get("business_summary") or source.get("business") or source.get("description") or "")[:420],
            "evidence_route": catalog["route"],
            "drivers": _clean_lines(classification.get("drivers") or catalog["drivers"], 4),
            "risks": _clean_lines(classification.get("risks") or catalog["risks"], 4),
            "monitoring_topics": _clean_lines(classification.get("monitoring_topics") or catalog["drivers"], 5),
            "rationale": str(classification.get("rationale", ""))[:240],
            "profile_source": str(source.get("source", "公开公司资料")),
            "profile_as_of": str(source.get("as_of", now[:10])),
            "profiled_at": now,
            "profile_error": profile_error[:160],
            "llm_status": llm_status,
            "signal_policy": (
                "技术、量能、产业、公司与市场证据完整通过后才评估首次建仓"
                if coverage == "full"
                else "仅记录基础研究；补齐交易所公告或产业证据路由前不触发首次建仓"
            ),
        }
        return OnboardingResult(
            symbol=normalized,
            name=name,
            sector=sector,
            peers=list(classification.get("peers") or []),
            analysis_profile=analysis_profile,
        )

    def _rule_classification(self, symbol: str, source: dict[str, Any]) -> dict[str, Any]:
        if symbol == "601318.SH":
            return self._classification_for("insurance_financial_group", symbol)
        text = " ".join(
            str(source.get(key, "")) for key in ("name", "full_name", "industry", "business", "description")
        )
        best_sector = "generic"
        best_score = 0
        for sector, item in SECTOR_CATALOG.items():
            if sector == "generic":
                continue
            score = sum(
                3 if keyword in str(source.get("industry", ""))
                else 2 if keyword in str(source.get("name", ""))
                else 1
                for keyword in item["keywords"] if keyword in text
            )
            if score > best_score:
                best_sector, best_score = sector, score
        # A single broad match is not enough to select an evidence route. It must
        # have at least two weighted points from a sector-specific keyword.
        if best_score < 2:
            best_sector = "generic"
        result = self._classification_for(best_sector, symbol)
        result["confidence"] = "high" if best_score >= 4 else "medium" if best_score >= 2 else "low"
        result["business_summary"] = str(source.get("business") or source.get("description") or "")[:420]
        result["rationale"] = (
            f"公开资料关键词与“{SECTOR_CATALOG[best_sector]['label']}”证据路由匹配"
            if best_sector != "generic"
            else "公开资料未能可靠匹配现有产业证据路由"
        )
        return result

    @staticmethod
    def _classification_for(sector: str, symbol: str) -> dict[str, Any]:
        item = SECTOR_CATALOG[sector]
        return {
            "sector": sector,
            "confidence": "medium" if sector != "generic" else "low",
            "peers": [peer for peer in item["peers"] if peer != symbol],
            "drivers": list(item["drivers"]),
            "risks": list(item["risks"]),
            "monitoring_topics": list(item["drivers"]),
            "business_summary": "",
            "rationale": "",
        }

    @staticmethod
    def _validated_llm_result(
        proposal: dict[str, Any],
        fallback: dict[str, Any],
        symbol: str,
        *,
        allow_routing_override: bool = False,
    ) -> dict[str, Any]:
        sector = str(proposal.get("sector", ""))
        if sector not in SECTOR_CATALOG:
            raise ValueError("返回了不受支持的行业")
        result = dict(fallback)
        allowed_fields = (
            "confidence", "business_summary", "drivers", "risks", "monitoring_topics", "rationale"
        )
        result.update({key: proposal[key] for key in allowed_fields if key in proposal})
        if allow_routing_override:
            result["sector"] = sector
        peers = []
        for peer in proposal.get("peers", []):
            try:
                normalized = _normalize_symbol(str(peer))
            except PortfolioStoreError:
                continue
            if normalized != symbol and normalized not in peers:
                peers.append(normalized)
        if allow_routing_override:
            result["peers"] = peers or StockOnboardingService._classification_for(sector, symbol)["peers"]
        if result.get("confidence") not in {"high", "medium", "low"}:
            result["confidence"] = "medium"
        return result

    def _fetch_company_profile(self, symbol: str) -> dict[str, Any]:
        market, code = symbol.split(".")[1], symbol.split(".")[0]
        url = f"{COMPANY_SURVEY_URL}?{urlencode({'code': market + code})}"
        timeout = float(self.config.get("profile_timeout_seconds", 8))
        retries = max(1, min(int(self.config.get("profile_retries", 2)), 3))
        rows: dict[str, Any] | list[dict[str, Any]] = []
        error: Exception | None = None
        for _ in range(retries):
            try:
                payload = _request_json(url, timeout=timeout)
                rows = payload.get("jbzl") or []
                if rows:
                    break
            except Exception as exc:
                error = exc
        if not rows:
            name = _fetch_tencent_name(symbol, timeout=timeout)
            if not name:
                raise ValueError(f"公开公司资料暂不可用{': ' + str(error) if error else ''}")
            return {
                "symbol": symbol,
                "name": name,
                "full_name": name,
                "industry": "",
                "business": name,
                "description": "",
                "source": "腾讯公开行情证券名称（公司资料待补充）",
                "source_url": f"{TENCENT_QUOTE_URL}{market.lower()}{code}",
                "as_of": datetime.now().astimezone().date().isoformat(),
            }
        item = rows if isinstance(rows, dict) else rows[0]
        returned_code = str(item.get("agdm", ""))
        if returned_code and returned_code != code:
            raise ValueError("公开资料证券代码不匹配")
        return {
            "symbol": symbol,
            "name": str(item.get("agjc", "")).strip(),
            "full_name": str(item.get("gsmc", "")).strip(),
            "industry": str(item.get("sshy") or item.get("sszjhhy") or "").strip(),
            "business": str(item.get("zyyw") or item.get("jyfw") or "").strip(),
            "description": str(item.get("gsjj", "")).strip(),
            "source": "东方财富 F10 公司概况（公开资料）",
            "source_url": url,
            "as_of": datetime.now().astimezone().date().isoformat(),
        }

    def _classify_with_llm(self, source: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
        api_key = self.llm_api_key
        if not api_key:
            raise ValueError("LLM_API_KEY 未配置")
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "sector": {"type": "string", "enum": list(SECTOR_CATALOG)},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "peers": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                "business_summary": {"type": "string"},
                "drivers": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                "risks": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                "monitoring_topics": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                "rationale": {"type": "string"},
            },
            "required": ["sector", "confidence", "peers", "business_summary", "drivers", "risks", "monitoring_topics", "rationale"],
        }
        allowed = {key: {"label": value["label"], "route": value["route"]} for key, value in SECTOR_CATALOG.items()}
        prompt = {
            "task": "复核A股标的研究归类。只能从给定行业键中选择；同行必须是A股6位代码加.SH/.SZ。未知时选generic。不要生成买卖建议、目标价或预测。",
            "company_profile": source,
            "rule_baseline": rule,
            "allowed_sectors": allowed,
        }
        body = {
            "model": self.llm_model,
            "input": [
                {"role": "system", "content": "你是公共股票研究资料归档助手。只整理可追踪的业务驱动、风险、同行与证据路由，不做投资决策。"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            "reasoning": {"effort": "low"},
            "text": {"format": {"type": "json_schema", "name": "stock_onboarding_profile", "strict": True, "schema": schema}},
        }
        request = Request(
            self.llm_responses_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        response = _request_json(request, timeout=float(self.config.get("llm_timeout_seconds", 30)))
        text = str(response.get("output_text", "")).strip()
        if not text:
            for output in response.get("output", []):
                for content in output.get("content", []):
                    if content.get("type") == "output_text":
                        text = str(content.get("text", "")).strip()
                        if text:
                            break
        if not text:
            raise ValueError("LLM 未返回结构化文本")
        return json.loads(text)


def sector_options() -> list[dict[str, str]]:
    return [{"key": key, "label": value["label"]} for key, value in SECTOR_CATALOG.items()]


def _request_json(request_or_url: Request | str, *, timeout: float) -> dict[str, Any]:
    request = request_or_url
    if isinstance(request_or_url, str):
        request = Request(request_or_url, headers={"User-Agent": "Mozilla/5.0 AStockDisciplineBot/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_tencent_name(symbol: str, *, timeout: float) -> str:
    code, market = symbol.split(".")
    request = Request(
        f"{TENCENT_QUOTE_URL}{market.lower()}{code}",
        headers={"User-Agent": "Mozilla/5.0 AStockDisciplineBot/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode("gb18030", errors="replace")
    except Exception:
        return ""
    match = re.search(r'="([^"]+)"', text)
    if not match:
        return ""
    fields = match.group(1).split("~")
    return fields[1].strip() if len(fields) > 2 else ""


def _parse_peer_text(value: str, symbol: str) -> list[str]:
    peers: list[str] = []
    for item in re.split(r"[,，\s]+", value.strip()):
        if not item:
            continue
        peer = _normalize_symbol(item)
        if peer != symbol and peer not in peers:
            peers.append(peer)
    return peers


def _clean_lines(value: Any, limit: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    result = []
    for item in value or []:
        text = str(item).strip()
        if text and text not in result:
            result.append(text[:90])
    return result[:limit]
