#!/usr/bin/env python3
"""사진 식단 분석(analyze-photo)의 음식명·그램·kcal 정확도를 측정한다.

docs/adr/2026-06-23-vision-ai-photo-meal.md ADR-1은 "영양소 오차의 주요 원인은
계수가 아니라 그램 추정 오차"라는 가정으로 MFDS DB 조회 대신 Vision 직접 추정을
택했다. 그 가정은 아직 측정된 적이 없다. 이 스크립트가 그것을 숫자로 가른다.

사용법과 평가셋 스키마는 eval/README.md 참고.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import statistics
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── ENV 고정: app.config import 보다 반드시 먼저 ─────────────────────────────
# settings는 최초 import 시 한 번만 생성되는 프로세스 전역 싱글턴이다.
# tests/conftest.py가 dev를 고정하는 것과 같은 이유이고 방향만 반대다.
_DRY_RUN = "--dry-run" in sys.argv
os.environ["ENV"] = "dev" if _DRY_RUN else "prod"

# Windows 콘솔 기본 인코딩(cp949)에서 한글 출력이 깨지는 것을 막는다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

from app.config import settings  # noqa: E402
from app.services.claude_service import (  # noqa: E402
    _PRICING_USD_PER_1M,
    call_claude_vision,
    strip_json_code_block,
)
from app.services.mfds_service import search_food_mfds  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# 프롬프트 변주
# ─────────────────────────────────────────────────────────────────────────────

_JSON_SPEC = (
    "아래 JSON 형식으로만 응답하세요 (다른 텍스트 없이):\n"
    '{"detected_items": [{"name": "음식명(한국어)", "estimated_grams": 숫자, '
    '"kcal": 숫자, "protein_g": 숫자, "carb_g": 숫자, "fat_g": 숫자}], '
    '"ai_comment": "한 문장 한국어 코멘트"}\n\n'
    "음식이 감지되지 않으면 detected_items를 빈 배열로 반환하세요."
)

# prod: app/routers/ai_meal.py:analyze_photo 의 사본.
#       라우터를 바꾸면 이쪽도 같이 바꿔야 측정이 의미를 갖는다.
_PROD = (
    "이 사진에 있는 음식을 모두 감지하고 영양소를 추정해주세요. 식사 유형: {meal_type}\n\n"
    + _JSON_SPEC
)

# hinted: 그램 추정 근거를 명시적으로 요구한다. ADR-1이 "지배적"이라 한
#         그램 오차를 줄일 수 있는지 보는 대조군.
_HINTED = (
    "이 사진에 있는 음식을 모두 감지하고 영양소를 추정해주세요. 식사 유형: {meal_type}\n\n"
    "그램 추정 시 다음을 근거로 사용하세요.\n"
    "- 함께 찍힌 식기의 표준 크기 (밥공기 지름 약 11cm·1공기 210g, 국그릇 약 15cm, "
    "일반 접시 약 23cm, 젓가락 길이 약 22cm)\n"
    "- 1인분 표준량 (공깃밥 210g, 닭가슴살 1덩이 100~150g, 계란 1개 50g)\n"
    "- 접시를 채운 넓이만 보지 말고 음식의 높이(두께)를 함께 고려하세요. "
    "넓이만 보면 과대추정됩니다.\n\n"
    + _JSON_SPEC
)

PROMPTS: dict[str, str] = {"prod": _PROD, "hinted": _HINTED}

# call_claude_vision의 기본 모델. 결과 JSON에 무엇으로 측정했는지 남기기 위해 복제한다.
_DEFAULT_VISION_MODEL = "claude-opus-4-5-20251101"

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


# ─────────────────────────────────────────────────────────────────────────────
# Claude 호출 로그 수집 — 기존 계측(_log_claude_call)을 그대로 주워 쓴다
# ─────────────────────────────────────────────────────────────────────────────


class ClaudeCallCapture(logging.Handler):
    """logger "ai.claude"가 남기는 구조화 로그를 파싱해 모은다."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = json.loads(record.getMessage())
        except (json.JSONDecodeError, TypeError):
            return
        if payload.get("event") == "claude_call":
            self.records.append(payload)


# ─────────────────────────────────────────────────────────────────────────────
# 평가셋
# ─────────────────────────────────────────────────────────────────────────────


# kcal 정답을 어디서 얻었는지. MFDS에서 베낀 정답으로 MFDS arm을 채점하면
# 순환논증이 되므로 지표를 출처별로 갈라 본다.
KCAL_SOURCES = {"package", "scale+db", "mfds"}


@dataclass
class GtItem:
    name: str
    grams: float
    kcal: float | None = None
    kcal_source: str | None = None
    aliases: list[str] = field(default_factory=list)


@dataclass
class Sample:
    id: str
    image: Path
    meal_type: str
    kind: str
    items: list[GtItem]
    note: str = ""

    @property
    def total_kcal(self) -> float | None:
        """모든 아이템에 정답 kcal이 있을 때만 합계를 낸다."""
        if not self.items or any(i.kcal is None for i in self.items):
            return None
        return sum(i.kcal for i in self.items)  # type: ignore[misc]


def load_dataset(path: Path) -> list[Sample]:
    if not path.exists():
        raise SystemExit(f"평가셋이 없다 — {path}\neval/README.md의 스키마를 참고해 만들어라.")

    base = path.parent
    samples: list[Sample] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("//"):
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path}:{lineno} JSON 파싱 실패 — {e}") from e

        image = (base / d["image"]).resolve()
        if not image.exists():
            raise SystemExit(f"{path}:{lineno} 사진 없음 — {image}")
        if image.suffix.lower() not in _MEDIA_TYPES:
            raise SystemExit(f"{path}:{lineno} 지원하지 않는 확장자 — {image.name}")

        items = []
        for i in d["items"]:
            src = i.get("kcal_source")
            if src is not None and src not in KCAL_SOURCES:
                raise SystemExit(
                    f"{path}:{lineno} kcal_source가 '{src}'다. "
                    f"{sorted(KCAL_SOURCES)} 중 하나여야 한다."
                )
            if i.get("kcal") is not None and src is None:
                print(
                    f"경고: {path}:{lineno} '{i['name']}'에 kcal은 있는데 kcal_source가 없다. "
                    "MFDS arm 비교가 순환인지 판별할 수 없다.",
                    file=sys.stderr,
                )
            items.append(
                GtItem(
                    name=i["name"],
                    grams=float(i["grams"]),
                    kcal=float(i["kcal"]) if i.get("kcal") is not None else None,
                    kcal_source=src,
                    aliases=list(i.get("aliases", [])),
                )
            )
        samples.append(
            Sample(
                id=str(d["id"]),
                image=image,
                meal_type=d["meal_type"],
                kind=d.get("kind", "single"),
                items=items,
                note=d.get("note", ""),
            )
        )

    if not samples:
        raise SystemExit(f"{path}: 유효한 샘플이 없다")
    return samples


# ─────────────────────────────────────────────────────────────────────────────
# 음식명 매칭
# ─────────────────────────────────────────────────────────────────────────────


def normalize(name: str) -> str:
    """괄호 안 조리법과 공백을 떼고 비교용 문자열을 만든다.

    "닭가슴살(구이)" 와 "닭 가슴살" 을 같게 본다.
    """
    out: list[str] = []
    depth = 0
    for ch in name:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and not ch.isspace():
            out.append(ch.lower())
    return "".join(out)


def match_score(gt: GtItem, pred_name: str) -> int:
    """2 = 이름/별칭 일치, 1 = 부분 문자열 포함, 0 = 불일치."""
    p = normalize(pred_name)
    if not p:
        return 0
    candidates = {normalize(gt.name)} | {normalize(a) for a in gt.aliases}
    candidates.discard("")
    if p in candidates:
        return 2
    for c in candidates:
        if c in p or p in c:
            return 1
    return 0


def assign(gt_items: list[GtItem], preds: list[dict]) -> list[tuple[int, int, int]]:
    """정답과 예측을 점수 높은 순으로 1:1 배정한다. (gt_idx, pred_idx, score) 목록."""
    pairs: list[tuple[int, int, int]] = []
    for gi, gt in enumerate(gt_items):
        for pi, pred in enumerate(preds):
            s = match_score(gt, str(pred.get("name", "")))
            if s > 0:
                pairs.append((s, gi, pi))
    pairs.sort(key=lambda t: -t[0])

    used_gt: set[int] = set()
    used_pred: set[int] = set()
    matched: list[tuple[int, int, int]] = []
    for s, gi, pi in pairs:
        if gi in used_gt or pi in used_pred:
            continue
        used_gt.add(gi)
        used_pred.add(pi)
        matched.append((gi, pi, s))
    return matched


def ape(pred: float, truth: float) -> float | None:
    """절대 백분율 오차. 정답이 0 이하면 정의되지 않는다."""
    if truth <= 0:
        return None
    return abs(pred - truth) / truth * 100.0


def _num(value: Any) -> float:
    """모델이 문자열이나 null로 준 숫자를 안전하게 float으로 만든다."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def density(kcal: float | None, grams: float | None) -> float | None:
    """kcal/g. 그램이 0 이하거나 kcal이 없으면 정의되지 않는다."""
    if kcal is None or grams is None or grams <= 0:
        return None
    return kcal / grams


# ──────────────────────────────────────────────────────────────────────────────
# Option A arm — 음식명 → MFDS 조회
# ──────────────────────────────────────────────────────────────────────────────

# 같은 음식명을 여러 사진에서 다시 묻지 않는다. 공공 API라 무료지만 느리다.
_MFDS_CACHE: dict[str, dict[str, Any]] = {}

_ARMS = ("pred", "oracle")
_RULES = ("median", "oracle")

_MFDS_PAGE_SIZE = 100
_MFDS_MAX_PAGES = 5


async def mfds_lookup(query: str) -> dict[str, Any]:
    """음식명으로 MFDS를 조회해 **정확 일치** 레코드들의 밀도(kcal/g)를 모은다.

    부분 일치는 쓰지 않는다. MFDS 부분검색은 관련도순이 아니라서 "닭가슴살"의
    첫 결과가 "샌드위치_닭가슴살"(240 kcal/100g)이다. 틀린 음식의 계수를
    쓰느니 조회 실패로 세는 쪽이 정직하다.

    정확 일치도 여러 건 나온다 — "비빔밥"은 8건이고 112~320 kcal/100g로
    흩어진다. 그 모호성 자체가 Option A의 실제 성능이라 후보 수와 스프레드를
    함께 남긴다.
    """
    key = normalize(query)
    if not key:
        return {"densities": [], "names": []}
    if key in _MFDS_CACHE:
        return _MFDS_CACHE[key]

    densities: list[float] = []
    names: list[str] = []
    for page in range(1, _MFDS_MAX_PAGES + 1):
        try:
            items, total = await search_food_mfds(
                query, page=page, size=_MFDS_PAGE_SIZE
            )
        except Exception:  # noqa: BLE001 — 조회 실패는 미검출과 같게 센다
            break
        if not items:
            break
        for it in items:
            if normalize(it.name) != key:
                continue
            d = density(it.kcal, it.serving_size_g)
            if d:
                densities.append(d)
                names.append(it.name)
        if page * _MFDS_PAGE_SIZE >= total:
            break

    out = {"densities": densities, "names": names}
    _MFDS_CACHE[key] = out
    return out


async def add_mfds_arm(scored: list[dict[str, Any]]) -> None:
    """Option A(음식명 → MFDS 조회)를 같은 사진·같은 그램 추정 위에 얹는다.

    ADR-1은 Option A와 Option B(Vision 직접 추정)의 비교였는데, 하니스는 B만
    쟀다. 두 arm이 Vision의 그램 추정을 공유하므로 차이는 계수에서만 나온다.
    Claude 호출은 늘지 않는다.

    조회 이름 2가지 × 선택 규칙 2가지를 모두 계산한다.

    - 이름 `pred`   : Vision이 감지한 이름 (Option A의 충실한 재현)
    - 이름 `oracle` : 정답 이름 (이름을 완벽히 맞혔을 때)
    - 규칙 `median` : 정확 일치 후보들의 중앙값 (현실적으로 고를 수 있는 값)
    - 규칙 `oracle` : 정답 kcal에 가장 가까운 후보 (Option A의 상한)

    오라클 규칙으로도 Option B를 못 이기면 ADR-1은 결정적으로 입증된다.
    """
    for sc in scored:
        for d in sc["items"]:
            pg = _num(d.get("pred_grams"))
            gt_kcal = d.get("gt_kcal")
            queries = {"pred": d.get("pred_name"), "oracle": d.get("gt_name")}

            for arm in _ARMS:
                hit = await mfds_lookup(str(queries[arm] or ""))
                ds = hit["densities"]
                d[f"mfds_{arm}_n"] = len(ds)
                d[f"mfds_{arm}_names"] = hit["names"][:3]
                d[f"mfds_{arm}_spread"] = (
                    round(max(ds) / min(ds), 2) if len(ds) > 1 and min(ds) > 0 else None
                )

                chosen = {
                    "median": statistics.median(ds) if ds else None,
                    # 상한: 후보 중 정답 kcal에 가장 가까운 레코드를 신이 골라줬다면
                    "oracle": (
                        min(ds, key=lambda x: abs(x * pg - gt_kcal))
                        if ds and gt_kcal is not None and pg > 0
                        else None
                    ),
                }
                for rule in _RULES:
                    dens = chosen[rule]
                    kcal = None if dens is None or pg <= 0 else dens * pg
                    d[f"mfds_{arm}_{rule}_kcal"] = None if kcal is None else round(kcal, 1)
                    a = None if kcal is None or gt_kcal is None else ape(kcal, gt_kcal)
                    d[f"mfds_{arm}_{rule}_ape"] = None if a is None else round(a, 2)


# ─────────────────────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────────────────────


async def analyze(
    sample: Sample, prompt_key: str, model: str | None, max_tokens: int
) -> dict[str, Any]:
    """사진 1장을 분석한다. 실패는 예외로 올리지 않고 결과에 담는다."""
    # str.format을 쓰면 안 된다 — 프롬프트에 담긴 JSON 예시의 중괄호를
    # 포맷 필드로 해석해 KeyError가 난다.
    prompt = PROMPTS[prompt_key].replace("{meal_type}", sample.meal_type)
    try:
        data = sample.image.read_bytes()
        raw = await call_claude_vision(
            image_base64=base64.b64encode(data).decode("ascii"),
            media_type=_MEDIA_TYPES[sample.image.suffix.lower()],
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
        )
        parsed = json.loads(strip_json_code_block(raw))
        preds = parsed.get("detected_items", [])
        if not isinstance(preds, list):
            return {"id": sample.id, "error": "detected_items가 배열이 아니다", "preds": []}
        preds = [p for p in preds if isinstance(p, dict)]
        return {"id": sample.id, "error": None, "preds": preds}
    except Exception as e:  # noqa: BLE001 — 한 장 실패가 전체를 멈추면 안 된다
        return {"id": sample.id, "error": f"{type(e).__name__}: {e}", "preds": []}


def score_sample(sample: Sample, preds: list[dict]) -> dict[str, Any]:
    matched = assign(sample.items, preds)

    grams_apes: list[float] = []
    kcal_apes: list[float] = []
    density_apes: list[float] = []
    detail: list[dict[str, Any]] = []

    for gi, pi, s in matched:
        gt = sample.items[gi]
        pred = preds[pi]
        pred_grams = _num(pred.get("estimated_grams"))
        pred_kcal = _num(pred.get("kcal"))

        g_ape = ape(pred_grams, gt.grams)
        k_ape = ape(pred_kcal, gt.kcal) if gt.kcal is not None else None

        # 밀도(kcal/g) 오차 = 그램 오차를 소거한 순수 계수 오차.
        # kcal 오차는 그램 오차와 계수 오차의 합이라 부호가 반대면 상쇄된다.
        # ADR-1이 "계수가 아니라 그램"이라고 한 주장은 이 지표로만 갈린다.
        gt_density = density(gt.kcal, gt.grams)
        pred_density = density(pred_kcal, pred_grams)
        d_ape = (
            None
            if gt_density is None or pred_density is None
            else ape(pred_density, gt_density)
        )

        if g_ape is not None:
            grams_apes.append(g_ape)
        if k_ape is not None:
            kcal_apes.append(k_ape)
        if d_ape is not None:
            density_apes.append(d_ape)
        detail.append(
            {
                "gt_name": gt.name,
                "pred_name": pred.get("name"),
                "match": "exact" if s == 2 else "partial",
                "gt_grams": gt.grams,
                "pred_grams": pred.get("estimated_grams"),
                "grams_ape": None if g_ape is None else round(g_ape, 2),
                "gt_kcal": gt.kcal,
                "pred_kcal": pred.get("kcal"),
                "kcal_ape": None if k_ape is None else round(k_ape, 2),
                "kcal_source": gt.kcal_source,
                "gt_density": None if gt_density is None else round(gt_density, 4),
                "pred_density": None if pred_density is None else round(pred_density, 4),
                "density_ape": None if d_ape is None else round(d_ape, 2),
            }
        )

    # 미검출·과검출 항목도 남긴다 — 어떤 음식을 놓치는지가 개선 단서다
    missed = [sample.items[i].name for i in range(len(sample.items))
              if i not in {gi for gi, _, _ in matched}]
    spurious = [preds[i].get("name") for i in range(len(preds))
                if i not in {pi for _, pi, _ in matched}]

    gt_total = sample.total_kcal
    total_ape = (
        ape(sum(_num(p.get("kcal")) for p in preds), gt_total)
        if gt_total is not None
        else None
    )

    return {
        "id": sample.id,
        "kind": sample.kind,
        "tp": len(matched),
        "exact": sum(1 for _, _, s in matched if s == 2),
        "fp": len(preds) - len(matched),
        "fn": len(sample.items) - len(matched),
        "grams_apes": grams_apes,
        "kcal_apes": kcal_apes,
        "density_apes": density_apes,
        "total_kcal_ape": None if total_ape is None else round(total_ape, 2),
        "missed": missed,
        "spurious": spurious,
        "items": detail,
    }


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mape": None, "median": None, "p90": None}
    s = sorted(values)
    return {
        "n": len(s),
        "mape": round(statistics.fmean(s), 2),
        "median": round(statistics.median(s), 2),
        "p90": round(s[min(len(s) - 1, int(len(s) * 0.9))], 2),
    }


def aggregate(scored: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(s["tp"] for s in scored)
    exact = sum(s["exact"] for s in scored)
    fp = sum(s["fp"] for s in scored)
    fn = sum(s["fn"] for s in scored)

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    return {
        "photos": len(scored),
        "name": {
            "tp": tp,
            "exact": exact,
            "fp": fp,
            "fn": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        },
        "grams": _stats([v for s in scored for v in s["grams_apes"]]),
        "item_kcal": _stats([v for s in scored for v in s["kcal_apes"]]),
        "density": _stats([v for s in scored for v in s["density_apes"]]),
        "total_kcal": _stats(
            [s["total_kcal_ape"] for s in scored if s["total_kcal_ape"] is not None]
        ),
        "mfds": {arm: _arm_stats(scored, arm) for arm in _ARMS},
    }


def _arm_stats(scored: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    """Option A arm의 kcal 오차 + 조회 실패율 + 후보 모호성.

    조회 실패율과 모호성 자체가 측정값이다 — ADR-1의 "복합 한식은 DB 매칭
    불가능"이 그 주장이고, kind별로 갈라 보면 검증된다.
    """
    looked = [d for s in scored for d in s["items"] if f"mfds_{arm}_n" in d]
    hits = [d for d in looked if d[f"mfds_{arm}_n"]]
    spreads = [
        d[f"mfds_{arm}_spread"] for d in hits if d.get(f"mfds_{arm}_spread") is not None
    ]

    out: dict[str, Any] = {
        "lookups": len(looked),
        "miss": len(looked) - len(hits),
        "miss_rate": (
            round((len(looked) - len(hits)) / len(looked), 4) if looked else None
        ),
        "candidates_mean": (
            round(statistics.fmean([d[f"mfds_{arm}_n"] for d in hits]), 2)
            if hits
            else None
        ),
        "spread_median": round(statistics.median(spreads), 2) if spreads else None,
    }
    for rule in _RULES:
        key = f"mfds_{arm}_{rule}_ape"
        out[rule] = _stats([d[key] for d in looked if d.get(key) is not None])
    return out


def by_kind(scored: list[dict[str, Any]]) -> dict[str, Any]:
    kinds: dict[str, list[dict[str, Any]]] = {}
    for s in scored:
        kinds.setdefault(s["kind"], []).append(s)
    return {k: aggregate(v) for k, v in sorted(kinds.items())}


def by_kcal_source(scored: list[dict[str, Any]]) -> dict[str, Any]:
    """kcal 정답 출처별 분해.

    정답 kcal을 MFDS에서 베낀 항목은 MFDS arm이 당연히 이긴다 — 순환논증이다.
    출처를 갈라야 Option A의 우위가 실재하는지 판별된다.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for s in scored:
        for d in s["items"]:
            groups.setdefault(d.get("kcal_source") or "(미기재)", []).append(d)

    def pick(ds: list[dict[str, Any]], key: str) -> list[float]:
        return [d[key] for d in ds if d.get(key) is not None]

    return {
        src: {
            "items": len(ds),
            "kcal": _stats(pick(ds, "kcal_ape")),
            "density": _stats(pick(ds, "density_ape")),
            "mfds_pred_kcal": _stats(pick(ds, "mfds_pred_median_ape")),
        }
        for src, ds in sorted(groups.items())
    }


def cost_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [r["latency_ms"] for r in records if r.get("latency_ms") is not None]
    costs = [r["cost_usd"] for r in records if r.get("cost_usd") is not None]
    return {
        "calls": len(records),
        "errors": sum(1 for r in records if r.get("error")),
        "latency_ms_mean": round(statistics.fmean(latencies), 1) if latencies else None,
        "latency_ms_p90": (
            round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.9))], 1)
            if latencies
            else None
        ),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in records),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in records),
        "cost_usd_total": round(sum(costs), 6) if costs else None,
        "cost_unpriced_calls": len(records) - len(costs),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 출력
# ─────────────────────────────────────────────────────────────────────────────


def _fmt(v: Any) -> str:
    return "-" if v is None else str(v)


def _width(s: str) -> int:
    """한글·전각 문자를 2칸으로 세는 표시폭."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    """표시폭 기준으로 s를 width에 맞춰 채운다. str.format은 한글을 1칸으로 세서 어긋난다."""
    fill = " " * max(0, width - _width(s))
    return fill + s if right else s + fill


def interpret(result: dict[str, Any]) -> list[str]:
    """ADR-1의 가정에 비추어 결과를 읽는다."""
    m = result["metrics"]
    out: list[str] = []
    g = m["grams"]["mape"]
    ik = m["item_kcal"]["mape"]
    dm = m["density"]["mape"]
    f1 = m["name"]["f1"]

    if f1 < 0.7:
        out.append(
            f"음식명 F1이 {f1:.3f}로 낮다. 설계(DB조회 vs 직접추정)와 무관한 상위 문제이니 "
            "프롬프트·모델을 먼저 손봐야 한다. 아래 오차율은 매칭된 항목만의 값이라 낙관적이다."
        )

    src = result.get("by_kcal_source", {})
    circular = src.get("mfds", {}).get("items", 0)
    unlabeled = src.get("(미기재)", {}).get("items", 0)
    if circular:
        out.append(
            f"kcal 정답이 MFDS 출처인 항목이 {circular}개다. 그 항목에서 Option A가 이기는 것은 "
            "순환논증이다. 아래 판정은 package/scale+db 항목 기준으로 읽어라."
        )
    if unlabeled:
        out.append(
            f"kcal_source가 비어 있는 항목이 {unlabeled}개다. 순환 여부를 판별할 수 없으니 "
            "라벨을 채워라."
        )

    if g is None or dm is None:
        out.append(
            "그램 또는 kcal 정답이 부족해 ADR-1 가정을 판정할 수 없다. items[].kcal을 더 채워라."
        )
        return out

    # ① 계수 오차 vs 그램 오차 — ADR-1의 핵심 주장
    if dm < g * 0.5:
        out.append(
            f"밀도(kcal/g) MAPE {dm}%가 그램 MAPE {g}%의 절반 미만이다 — 오차가 그램에서 "
            "지배적으로 나온다. ADR-1의 근거가 성립한다. 개선 지점은 그램 추정 프롬프트다."
        )
    elif dm > g:
        out.append(
            f"밀도 MAPE {dm}%가 그램 MAPE {g}%보다 크다 — 계수 오차가 그램 오차보다 크다. "
            "ADR-1의 '그램 오차가 지배적' 전제가 깨진다."
        )
    else:
        out.append(
            f"밀도 MAPE {dm}%와 그램 MAPE {g}%가 같은 크기다 — 계수 오차가 무시할 수준이 아니다. "
            "ADR-1의 근거가 약해진다."
        )

    if ik is not None and ik < min(dm, g) * 0.8:
        out.append(
            f"kcal MAPE {ik}%가 그램({g}%)·밀도({dm}%) 오차보다 작다 — 두 오차가 서로 "
            "상쇄되고 있다. kcal MAPE만 보면 정확해 보이는 착시이니 그 수치로 판정하지 마라."
        )

    # ② Option A vs Option B 정면 비교 (그램 추정을 공유하므로 차이는 계수뿐)
    arm = m["mfds"]["pred"]
    real = arm["median"]["mape"]
    best = arm["oracle"]["mape"]

    if real is None:
        out.append(
            "MFDS 정확 일치가 한 건도 없어 Option A와 비교할 수 없다 "
            "(--no-mfds이거나 MFDS_API_KEY 미설정)."
        )
    elif ik is not None:
        if real < ik * 0.8:
            out.append(
                f"현실 조건(감지명 → 후보 중앙값)에서 Option A {real}%가 현행 "
                f"Option B {ik}%보다 유의하게 작다 — DB 조회가 더 정확하다. "
                "ADR-1의 '두 방식 정확도 차이 미미'가 반증된다."
            )
        elif real > ik * 1.2:
            out.append(
                f"현실 조건에서 Option A {real}%가 현행 Option B {ik}%보다 크다 — "
                "Vision 직접 추정이 더 정확하다. ADR-1을 이 측정 결과로 유지한다."
            )
        else:
            out.append(
                f"현실 조건에서 Option A {real}%와 Option B {ik}%가 비슷하다 — "
                "ADR-1의 '차이 미미' 주장이 측정으로 확인된다."
            )

        if best is not None:
            if best > ik:
                out.append(
                    f"레코드를 신이 골라준 상한({best}%)으로도 Option B {ik}%를 "
                    "못 이긴다 — ADR-1이 결정적으로 입증된다. DB를 어떻게 붙여도 "
                    "Vision 직접 추정보다 낫지 않다."
                )
            elif real is not None and real > best * 1.5:
                out.append(
                    f"상한은 {best}%인데 현실은 {real}%다 — DB 조회는 원리상 더 "
                    "정확하지만 어느 레코드를 고를지 정하지 못해 그 이득을 다 "
                    "까먹는다. ADR-1의 결론은 맞되 근거는 '계수 차이 미미'가 "
                    "아니라 '이름→레코드 매칭 불가'로 고쳐 적어야 한다."
                )

    # ③ 매칭 모호성 — ADR-1의 두 번째 근거
    mr = arm["miss_rate"]
    if mr is not None:
        comp = (
            result.get("by_kind", {})
            .get("composite", {})
            .get("mfds", {})
            .get("pred", {})
            .get("miss_rate")
        )
        line = f"MFDS 정확일치 실패율 전체 {mr:.1%}"
        if comp is None:
            line += (
                " · composite에 MFDS를 조회할 매칭 항목이 없어 ADR-1의 '복합 한식' "
                "근거는 미검증으로 남는다."
            )
        elif comp > mr * 1.5:
            line += (
                f" · composite {comp:.1%} — 복합 한식에서 두드러진다. "
                "ADR-1의 '복합 한식은 DB 매칭 불가' 주장이 뒷받침된다."
            )
        else:
            line += (
                f" · composite {comp:.1%} — 복합 한식이 특별히 나쁘지 않다. "
                "ADR-1의 '복합 한식 처리 용이' 근거는 약하다."
            )
        out.append(line)

    sp = arm["spread_median"]
    if sp is not None and sp > 1.5:
        out.append(
            f"정확 일치 후보끼리도 kcal이 중앙값 기준 {sp}배 흩어진다 "
            f"(평균 {arm['candidates_mean']}건). 같은 이름에 서로 다른 레코드가 "
            "공존하므로, Option A를 쓰려면 계수 정확도 이전에 레코드 선택 "
            "문제부터 풀어야 한다."
        )

    return out


def render(result: dict[str, Any]) -> None:
    m = result["metrics"]
    print()
    if result["dry_run"]:
        print("!! --dry-run: mock 응답이므로 아래 지표는 무의미하다 (파이프라인 점검용)")
    print(f"평가셋 {m['photos']}장 · prompt={result['prompt']} · model={result['model']}")
    print("=" * 64)

    n = m["name"]
    print("\n[음식명 검출]")
    print(f"  precision {n['precision']:.3f}   recall {n['recall']:.3f}   F1 {n['f1']:.3f}")
    print(f"  매칭 {n['tp']} (완전일치 {n['exact']}) · 과검출 {n['fp']} · 미검출 {n['fn']}")

    print("\n[오차율 %]                  n     MAPE   median      p90")
    for label, key in (
        ("그램 (아이템)", "grams"),
        ("kcal (아이템)", "item_kcal"),
        ("밀도 kcal/g (아이템)", "density"),
        ("kcal (사진 합계)", "total_kcal"),
    ):
        s = m[key]
        print(
            f"  {_pad(label, 22)} {s['n']:>4}  {_fmt(s['mape']):>7}  "
            f"{_fmt(s['median']):>7}  {_fmt(s['p90']):>7}"
        )

    print("\n[kind별 분해]")
    print(
        f"  {'kind':<12} {_pad('장수', 4, True)} {_pad('명F1', 7, True)} "
        f"{_pad('그램MAPE', 9, True)} {'kcalMAPE':>9}"
    )
    for kind, s in result["by_kind"].items():
        print(
            f"  {kind:<12} {s['photos']:>4} {s['name']['f1']:>7.3f} "
            f"{_fmt(s['grams']['mape']):>9} {_fmt(s['item_kcal']['mape']):>9}"
        )

    print("\n[ADR-1 정면 비교 — 같은 사진·같은 그램 추정 위에서]")
    print(
        f"  {_pad('방식', 26)} {_pad('kcalMAPE', 9, True)} "
        f"{_pad('조회실패', 9, True)} {_pad('후보수', 7, True)} "
        f"{_pad('모호도', 7, True)} {'n':>4}"
    )
    b = m["item_kcal"]
    print(
        f"  {_pad('Option B: Vision 직접 (현행)', 26)} {_fmt(b['mape']):>9} "
        f"{'-':>9} {'-':>7} {'-':>7} {b['n']:>4}"
    )
    for arm, rule, label in (
        ("pred", "median", "A: 감지명 → 중앙값 (현실)"),
        ("pred", "oracle", "A: 감지명 → 오라클 (상한)"),
        ("oracle", "median", "A: 정답명 → 중앙값"),
        ("oracle", "oracle", "A: 정답명 → 오라클"),
    ):
        st = m["mfds"][arm]
        miss = "-" if st["miss_rate"] is None else f"{st['miss_rate']:.1%}"
        cand = _fmt(st["candidates_mean"])
        sp = "-" if st["spread_median"] is None else f"{st['spread_median']}x"
        print(
            f"  {_pad(label, 26)} {_fmt(st[rule]['mape']):>9} {miss:>9} "
            f"{cand:>7} {sp:>7} {st[rule]['n']:>4}"
        )
    print("  * 네 행 모두 Vision의 그램 추정을 공유한다 — 차이는 계수에서만 나온다.")
    print("  * 오라클 = 정확일치 후보 중 정답 kcal에 가장 가까운 레코드 (달성 불가능한 상한).")

    if result.get("by_kcal_source"):
        print("\n[kcal 정답 출처별]")
        print(
            f"  {_pad('출처', 12)} {_pad('항목', 5, True)} {_pad('kcalMAPE', 9, True)} "
            f"{_pad('밀도MAPE', 9, True)} {'A:MFDS':>9}"
        )
        for src, st in result["by_kcal_source"].items():
            print(
                f"  {_pad(src, 12)} {st['items']:>5} {_fmt(st['kcal']['mape']):>9} "
                f"{_fmt(st['density']['mape']):>9} "
                f"{_fmt(st['mfds_pred_kcal']['mape']):>9}"
            )
        if "mfds" in result["by_kcal_source"]:
            print("  ! 'mfds' 출처 항목의 A:MFDS 열은 순환논증이다. 판정에서 빼라.")

    c = result["cost"]
    print("\n[호출 비용]")
    print(f"  호출 {c['calls']}건 (실패 {c['errors']}건)")
    print(f"  지연 평균 {_fmt(c['latency_ms_mean'])}ms · p90 {_fmt(c['latency_ms_p90'])}ms")
    print(f"  토큰 in {c['input_tokens']:,} / out {c['output_tokens']:,}")
    print(f"  비용 {_fmt(c['cost_usd_total'])} USD")
    if c["cost_unpriced_calls"]:
        print(
            f"  ! {c['cost_unpriced_calls']}건은 단가 미등록으로 비용 집계에서 빠졌다.\n"
            f"    claude_service._PRICING_USD_PER_1M 에 '{result['model']}' 단가를 "
            "채우면 잡힌다."
        )

    missed = [(s["id"], s["missed"]) for s in result["samples"] if s["missed"]]
    if missed:
        print("\n[미검출 음식]")
        for sid, names in missed[:15]:
            print(f"  {sid}: {', '.join(names)}")

    errs = [s for s in result["samples"] if s.get("error")]
    if errs:
        print(f"\n[분석 실패 {len(errs)}건]")
        for s in errs:
            print(f"  {s['id']}: {s['error']}")

    print("\n[해석]")
    for line in interpret(result):
        print(f"  - {line}")
    print()


def _arm(m: dict[str, Any]) -> dict[str, Any]:
    """이 스크립트 변경 이전에 저장된 결과에는 mfds arm이 없다."""
    return m.get("mfds", {}).get("pred", {})


def compare(paths: list[Path]) -> None:
    runs = []
    for p in paths:
        if not p.exists():
            raise SystemExit(f"결과 파일이 없다 — {p}")
        runs.append(json.loads(p.read_text(encoding="utf-8")))

    def head(r: dict[str, Any]) -> str:
        return f"{r['prompt']}/{r['model'][:16]}"

    print()
    print(_pad("지표", 24) + "".join(f"{head(r):>24}" for r in runs))
    print("=" * (24 + 24 * len(runs)))

    rows: list[tuple[str, Any]] = [
        ("음식명 F1", lambda m: f"{m['name']['f1']:.3f}"),
        ("음식명 precision", lambda m: f"{m['name']['precision']:.3f}"),
        ("음식명 recall", lambda m: f"{m['name']['recall']:.3f}"),
        ("그램 MAPE %", lambda m: _fmt(m["grams"]["mape"])),
        ("kcal MAPE % (아이템)", lambda m: _fmt(m["item_kcal"]["mape"])),
        ("밀도 MAPE % (계수)", lambda m: _fmt(m.get("density", {}).get("mape"))),
        ("kcal MAPE % (합계)", lambda m: _fmt(m["total_kcal"]["mape"])),
        (
            "Option A MAPE % (현실)",
            lambda m: _fmt(_arm(m).get("median", {}).get("mape")),
        ),
        (
            "Option A MAPE % (상한)",
            lambda m: _fmt(_arm(m).get("oracle", {}).get("mape")),
        ),
        (
            "MFDS 정확일치 실패율",
            lambda m: (
                "-"
                if _arm(m).get("miss_rate") is None
                else f"{_arm(m)['miss_rate']:.1%}"
            ),
        ),
    ]
    for label, get in rows:
        print(_pad(label, 24) + "".join(f"{get(r['metrics']):>24}" for r in runs))
    print(
        _pad("지연 평균 ms", 24)
        + "".join(f"{_fmt(r['cost']['latency_ms_mean']):>24}" for r in runs)
    )
    print(
        _pad("비용 USD", 24)
        + "".join(f"{_fmt(r['cost']['cost_usd_total']):>24}" for r in runs)
    )
    print()


# ─────────────────────────────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> dict[str, Any]:
    samples = load_dataset(args.dataset)

    capture = ClaudeCallCapture()
    claude_logger = logging.getLogger("ai.claude")
    claude_logger.addHandler(capture)
    claude_logger.setLevel(logging.INFO)

    sem = asyncio.Semaphore(args.concurrency)

    async def one(s: Sample) -> dict[str, Any]:
        async with sem:
            r = await analyze(s, args.prompt, args.model, args.max_tokens)
            status = f"실패: {r['error']}" if r["error"] else f"{len(r['preds'])}개 감지"
            print(f"  [{r['id']}] {status}", flush=True)
            return r

    print(f"사진 {len(samples)}장 분석 (동시 {args.concurrency})")
    raw_results = await asyncio.gather(*(one(s) for s in samples))

    by_id = {s.id: s for s in samples}
    scored: list[dict[str, Any]] = []
    for r in raw_results:
        sc = score_sample(by_id[r["id"]], r["preds"])
        sc["error"] = r["error"]
        scored.append(sc)

    if not args.no_mfds:
        print("Option A arm — MFDS 조회 중 (Claude 호출 없음)")
        await add_mfds_arm(scored)

    return {
        "prompt": args.prompt,
        "model": args.model or _DEFAULT_VISION_MODEL,
        "dry_run": args.dry_run,
        "mfds_arm": not args.no_mfds,
        "dataset": str(args.dataset),
        "metrics": aggregate(scored),
        "by_kind": by_kind(scored),
        "by_kcal_source": by_kcal_source(scored),
        "cost": cost_summary(capture.records),
        "samples": scored,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="사진 식단 분석 정확도 평가")
    p.add_argument("--dataset", type=Path, help="평가셋 JSONL 경로")
    p.add_argument("--prompt", choices=sorted(PROMPTS), default="prod")
    p.add_argument("--model", default=None, help="미지정 시 call_claude_vision 기본 모델")
    p.add_argument("--max-tokens", type=int, default=800)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument(
        "--no-mfds",
        action="store_true",
        help="Option A(MFDS 조회) arm을 건너뛴다. ADR-1 정면 비교가 빠진다.",
    )
    p.add_argument("--out", type=Path, help="결과 JSON 저장 경로")
    p.add_argument("--dry-run", action="store_true", help="ENV=dev, mock 응답 - 지표는 무의미")
    p.add_argument("--yes-real-api", action="store_true", help="실제 API 호출 동의 (비용 발생)")
    p.add_argument("--compare", type=Path, nargs="+", help="저장된 결과 JSON들을 비교만 한다")
    args = p.parse_args()

    if args.compare:
        compare(args.compare)
        return

    if not args.dataset:
        p.error("--dataset 또는 --compare 중 하나는 필요하다")
    if not args.dry_run and not args.yes_real_api:
        p.error(
            "실제 API를 호출하면 비용이 발생한다. 점검만 하려면 --dry-run, "
            "측정하려면 --yes-real-api 를 붙여라."
        )

    if not args.dry_run and not args.no_mfds and settings.mfds_api_key == "mock-key":
        print(
            "경고: MFDS_API_KEY가 mock-key다. Option A arm이 mock DB로 계산되어 "
            "ADR-1 비교가 무의미해진다. 키를 넣거나 --no-mfds 를 붙여라.",
            file=sys.stderr,
        )

    model = args.model or _DEFAULT_VISION_MODEL
    if not args.dry_run and model not in _PRICING_USD_PER_1M:
        print(
            f"경고: '{model}' 단가가 _PRICING_USD_PER_1M 에 없어 비용이 집계되지 않는다.",
            file=sys.stderr,
        )

    result = asyncio.run(run(args))
    render(result)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"결과 저장: {args.out}\n")


if __name__ == "__main__":
    main()
