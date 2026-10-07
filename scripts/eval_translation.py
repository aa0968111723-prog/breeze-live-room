#!/usr/bin/env python3
"""Score the fixed corpus with the shipped glossary and prompt builder.

  python scripts/eval_translation.py --fake
  python scripts/eval_translation.py --fake --mutate
  python scripts/eval_translation.py --live --yes-bill

--fake never calls a translation API. --live uses the configured Translator
and bills the user's key, so it refuses to run unless --yes-bill is passed
or BREEZE_EVAL_LIVE=1. Reference English is an unconfirmed draft.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.glossary import (  # noqa: E402
    PROMPT_LIMIT,
    is_locked,
    matched_terms,
    normalize,
    term_hit,
    validate_terms,
)
from app.rtf import percentile  # noqa: E402
from app.translate import Translator  # noqa: E402

DISCLAIMER = "自擬範例資料，非真實逐字稿，參考譯文待使用者確認"
FIXTURE = ROOT / "tests" / "fixtures" / "translation"
TAIPEI = timezone(timedelta(hours=8))
# Fixed bad hypotheses from the draft. Z02 drops a locked term; Z36 drops a digit.
MUTATIONS = {
    "Z02": ("Dharma talk", "teaching"),
    "Z36": ("42", "forty"),
}
CONTRACT = {
    "rows": 50,
    "terms": 48,
    "locked_terms": 37,
    "normalize_total": 56,
    "locked_term_total": 53,
    "expect_tokens_total": 8,
    "asr_variants": 6,
    "categories": {
        "term": 20,
        "negation": 7,
        "asr_alias": 6,
        "career": 4,
        "fragment": 4,
        "negative": 3,
        "question": 2,
        "number": 1,
        "place": 1,
        "name": 1,
        "injection": 1,
    },
}


def load_glossary(path: Path) -> list[dict]:
    """CSV rows in the shape validate_terms and normalize already accept."""
    lines = []
    text = path.read_text(encoding="utf-8-sig")
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(raw)
    terms = []
    for raw in csv.DictReader(lines):
        zh = (raw.get("zh") or "").strip()
        en = (raw.get("en") or "").strip()
        aliases = [part.strip() for part in (raw.get("aliases") or "").split("|") if part.strip()]
        lock_text = (raw.get("lock") or "true").strip().lower()
        terms.append({
            "zh": zh,
            "aliases": aliases,
            "en": en,
            "lock": lock_text in {"1", "true", "yes", "y"},
            "category": (raw.get("category") or "").strip(),
            "note": (raw.get("note") or "").strip(),
        })
    return terms


def load_corpus(path: Path) -> tuple[dict, list[dict]]:
    meta: dict = {}
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.strip():
            continue
        obj = json.loads(raw)
        if "_meta" in obj:
            meta = obj["_meta"]
            continue
        rows.append(obj)
    return meta, rows


def chrf(hypothesis: str, reference: str, max_n: int = 6, beta: float = 2.0) -> float:
    """Character n-gram F-score, 0 to 100. Spaces are ignored. Standard library only."""
    def ngrams(text: str, size: int) -> dict[str, int]:
        compact = text.replace(" ", "")
        counts: dict[str, int] = {}
        for index in range(len(compact) - size + 1):
            piece = compact[index:index + size]
            counts[piece] = counts.get(piece, 0) + 1
        return counts

    precisions = []
    recalls = []
    for size in range(1, max_n + 1):
        hyp_counts = ngrams(hypothesis, size)
        ref_counts = ngrams(reference, size)
        if not hyp_counts or not ref_counts:
            continue
        overlap = 0
        for piece, count in hyp_counts.items():
            overlap += min(count, ref_counts.get(piece, 0))
        precisions.append(overlap / sum(hyp_counts.values()))
        recalls.append(overlap / sum(ref_counts.values()))
    if not precisions:
        return 0.0
    precision = sum(precisions) / len(precisions)
    recall = sum(recalls) / len(recalls)
    if precision + recall == 0:
        return 0.0
    beta_sq = beta * beta
    return 100 * (1 + beta_sq) * precision * recall / (beta_sq * precision + recall)


def optional_bleu(hypotheses: list[str], references: list[str]) -> dict:
    """BLEU only when sacrebleu is already installed. It is not a dependency."""
    try:
        import sacrebleu
    except ImportError:
        return {"bleu": None, "chrf": None, "note": "未安裝 sacrebleu，略過 BLEU"}
    bleu = sacrebleu.corpus_bleu(hypotheses, [references])
    extra = sacrebleu.corpus_chrf(hypotheses, [references])
    return {
        "bleu": round(float(bleu.score), 2),
        "chrf": round(float(extra.score), 2),
        "note": "sacrebleu 有安裝才計算；不是 CI 門檻",
    }


def expected_prompt_terms(zh: str, glossary, context=None) -> list[dict]:
    """Terms matched_terms finds, current line first, then up to four previous lines."""
    picked = []
    seen: set[str] = set()
    texts = [zh or ""]
    for line in list(context or [])[-4:]:
        texts.append(str(line or ""))
    for text in texts:
        for term in matched_terms(text, glossary):
            key = term["zh"]
            if key in seen:
                continue
            en = str(term.get("en") or "")
            if not en:
                continue
            seen.add(key)
            picked.append({"zh": key, "en": en, "locked": is_locked(term)})
            if len(picked) >= PROMPT_LIMIT:
                return picked
    return picked


def diff_prompt(messages: list[dict], zh: str, glossary, context=None, bare_system: str | None = None) -> list[str]:
    """Fail when the prompt carries a term the matcher did not hit, or a host note."""
    problems = []
    if bare_system is not None and messages and messages[0].get("content") != bare_system:
        problems.append("system prompt 含有本句以外的內容")
    if [item.get("role") for item in messages] != ["system", "user"]:
        problems.append("messages 角色不是 system、user")
        return problems
    try:
        user = json.loads(messages[1]["content"])
    except (json.JSONDecodeError, TypeError, KeyError):
        return problems + ["user message 不是 JSON"]
    prompted = user.get("glossary")
    if not isinstance(prompted, list):
        return problems + ["user glossary 不是陣列"]
    expected = expected_prompt_terms(zh, glossary, context)
    got = []
    for item in prompted:
        if not isinstance(item, dict):
            problems.append("prompt 詞條不是物件")
            continue
        extra = set(item) - {"zh", "en", "locked"}
        if extra:
            problems.append("prompt 詞條多了欄位：" + "、".join(sorted(extra)))
        got.append({"zh": item.get("zh"), "en": item.get("en"), "locked": item.get("locked")})
    if got != expected:
        extra_zh = [item["zh"] for item in got if item not in expected]
        missing_zh = [item["zh"] for item in expected if item not in got]
        problems.append(f"prompt 詞條與最長比對不一致；多了 {extra_zh}，少了 {missing_zh}")
    blob = "\n".join(str(line or "") for line in [zh, *(context or [])])
    for item in got:
        if item["zh"] and item["zh"] not in blob:
            problems.append(f"prompt 含有句子裡沒有的詞「{item['zh']}」")
    serialized = json.dumps(messages, ensure_ascii=False)
    for term in glossary or []:
        note = str(term.get("note") or "").strip()
        if note and note in serialized:
            problems.append(f"主持端備註進了 prompt：{note[:24]}")
    return problems


def prompt_problems(zh: str, glossary, context=None, translator: Translator | None = None) -> list[str]:
    translator = translator or Translator()
    messages = translator.build_messages(zh, glossary, context)
    bare = translator.build_messages(".", [], None)
    return diff_prompt(messages, zh, glossary, context, bare[0]["content"])


def glossary_problems(terms: list[dict]) -> list[str]:
    accepted, rejected = validate_terms(terms)
    problems = []
    if rejected:
        for item in rejected:
            problems.append(f"[glossary] 第 {item.get('line')} 條：{item.get('reason')}")
    if len(terms) != CONTRACT["terms"]:
        problems.append(f"[contract] 術語 {len(terms)} 條，預期 {CONTRACT['terms']}")
    locked = sum(1 for term in terms if term.get("lock") is True)
    if locked != CONTRACT["locked_terms"]:
        problems.append(f"[contract] 鎖定 {locked} 條，預期 {CONTRACT['locked_terms']}")
    if len(accepted) != len(terms):
        problems.append(f"[glossary] 通過驗證 {len(accepted)} 條，檔案有 {len(terms)} 條")
    return problems


def corpus_problems(meta: dict, rows: list[dict]) -> list[str]:
    problems = []
    if DISCLAIMER not in json.dumps(meta, ensure_ascii=False):
        problems.append("[corpus] 檔頭沒有「" + DISCLAIMER + "」")
    if meta.get("not_app_default") is not True:
        problems.append("[corpus] 檔頭沒有標成非預設術語表")
    if len(rows) != CONTRACT["rows"]:
        problems.append(f"[contract] 語料 {len(rows)} 句，預期 {CONTRACT['rows']}")
    seen: set[str] = set()
    categories: dict[str, int] = {}
    variants = 0
    for row in rows:
        row_id = row.get("id")
        if not row_id or row_id in seen:
            problems.append(f"[corpus] 編號重複或空白：{row_id!r}")
        seen.add(row_id)
        if not str(row.get("zh") or "").strip() or not str(row.get("ref_en") or "").strip():
            problems.append(f"[corpus] {row_id} 缺中文或參考英文")
        cat = row.get("category")
        categories[cat] = categories.get(cat, 0) + 1
        if row.get("asr_variant"):
            variants += 1
            if row["asr_variant"] == row.get("zh"):
                problems.append(f"[corpus] {row_id} 的 ASR 變體與乾淨中文相同")
    if categories != CONTRACT["categories"]:
        problems.append(f"[contract] 類別數量 {categories}，預期 {CONTRACT['categories']}")
    if variants != CONTRACT["asr_variants"]:
        problems.append(f"[contract] ASR 變體 {variants}，預期 {CONTRACT['asr_variants']}")
    pairs: dict[str, set[str]] = {}
    for row in rows:
        if row.get("pair"):
            pairs.setdefault(row["pair"], set()).add(row["id"])
    if pairs.get("Z47") != {"Z47a", "Z47b"} or pairs.get("Z48") != {"Z48a", "Z48b"}:
        problems.append(f"[corpus] 半句配對不完整：{pairs}")
    return problems


def _stat(hit: int, total: int) -> dict:
    return {"hit": hit, "total": total, "ok": total > 0 and hit == total}


def normalization_report(rows: list[dict], glossary) -> tuple[dict, list[str]]:
    hit = total = 0
    failures = []
    for row in rows:
        sources = []
        if row.get("asr_variant"):
            sources.append(row["asr_variant"])
        sources.append(row["zh"])
        banned = list(row.get("must_not_contain_after_norm") or [])
        for source in sources:
            total += 1
            got = normalize(source, glossary)
            bad = [word for word in banned if word in got]
            if got == row["zh"] and normalize(got, glossary) == got and not bad:
                hit += 1
                continue
            failures.append(f"[normalize] {row['id']}: {source!r} -> {got!r}，預期 {row['zh']!r}，誤含 {bad}")
    if total != CONTRACT["normalize_total"]:
        failures.append(f"[contract] 正規化樣本 {total}，預期 {CONTRACT['normalize_total']}")
    return _stat(hit, total), failures


def fake_answers(rows: list[dict], mutate: bool) -> tuple[dict[str, dict], list[str]]:
    """Deterministic stand-in. Latency is a fixed series, not a clock reading."""
    answers = {}
    problems = []
    applied = []
    for index, row in enumerate(rows):
        english = str(row.get("ref_en") or "")
        changed = False
        if mutate and row["id"] in MUTATIONS:
            old, new = MUTATIONS[row["id"]]
            if old not in english:
                problems.append(f"[mutate] {row['id']} 的參考英文沒有 {old!r}，變異沒有套用")
            else:
                english = english.replace(old, new, 1)
                changed = True
                applied.append(row["id"])
        answers[row["id"]] = {
            "en": english,
            "latency_ms": 20 + index,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "status": "ok",
            "mutated": changed,
        }
    if mutate and applied != list(MUTATIONS):
        problems.append(f"[mutate] 預期改壞 {list(MUTATIONS)}，實際 {applied}")
    return answers, problems


def live_answers(rows: list[dict], glossary, translator: Translator) -> dict[str, dict]:
    answers = {}
    for row in rows:
        started = time.perf_counter()
        result = translator.translate(row["zh"], glossary, None)
        elapsed_ms = (time.perf_counter() - started) * 1000
        # A failed call must not be scored as if the Chinese source were English.
        english = result.text if result.status == "ok" else ""
        answers[row["id"]] = {
            "en": english,
            "latency_ms": elapsed_ms,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "status": result.status,
            "mutated": False,
            "unexpected_text": bool(result.status != "ok" and result.text),
        }
    return answers


def _latency(values: list[float], measured: bool) -> dict:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "max": None, "measured": False}
    return {
        "n": len(values),
        "p50": round(float(percentile(values, 0.50)), 6),
        "p95": round(float(percentile(values, 0.95)), 6),
        "max": round(float(max(values)), 6),
        "measured": measured,
    }


def _tokens(scored: list[dict], measured: bool) -> dict:
    prompt_sum = 0
    completion_sum = 0
    with_usage = 0
    missing = 0
    for row in scored:
        prompt = row.get("prompt_tokens")
        completion = row.get("completion_tokens")
        if isinstance(prompt, int) and isinstance(completion, int):
            prompt_sum += prompt
            completion_sum += completion
            with_usage += 1
        else:
            missing += 1
    return {
        "prompt_tokens": prompt_sum,
        "completion_tokens": completion_sum,
        "total_tokens": prompt_sum + completion_sum,
        "rows_with_usage": with_usage,
        "rows_missing_usage": missing,
        "measured": bool(measured and missing == 0 and with_usage),
    }


def evaluate(meta: dict, rows: list[dict], glossary, answers: dict[str, dict], *, mode: str, model: str, measured: bool, gate_quality: bool, mutate: bool) -> dict:
    failures: list[str] = []
    failures.extend(glossary_problems(glossary))
    failures.extend(corpus_problems(meta, rows))
    norm_stat, norm_failures = normalization_report(rows, glossary)
    failures.extend(norm_failures)

    translator = Translator()
    prompt_hit = 0
    prompt_failures = []
    for row in rows:
        problems = prompt_problems(row["zh"], glossary, None, translator)
        if problems:
            prompt_failures.append(f"[prompt] {row['id']}: " + "；".join(problems))
        else:
            prompt_hit += 1
    failures.extend(prompt_failures)

    locked_hit = locked_total = 0
    token_hit = token_total = 0
    scores = []
    scored_rows = []
    for row in rows:
        answer = answers.get(row["id"])
        if answer is None:
            failures.append(f"[hyp] 缺少 {row['id']}")
            continue
        english = str(answer.get("en") or "")
        row_locked_hit, row_locked_total, missing_terms = _locked(row, glossary, english)
        row_token_hit, row_token_total, missing_tokens = _tokens_row(row, english)
        locked_hit += row_locked_hit
        locked_total += row_locked_total
        token_hit += row_token_hit
        token_total += row_token_total
        if gate_quality:
            for term in missing_terms:
                failures.append(f"[term] {row['id']}: 缺「{term['zh']}={term['en']}」")
            for token in missing_tokens:
                failures.append(f"[token] {row['id']}: 缺 {token!r}")
        score = chrf(english, row["ref_en"])
        scores.append(score)
        scored_rows.append({
            "id": row["id"],
            "category": row.get("category"),
            "status": answer.get("status"),
            "hyp_en": english,
            "ref_en": row["ref_en"],
            "chrf": round(score, 2),
            "latency_ms": answer.get("latency_ms"),
            "prompt_tokens": answer.get("prompt_tokens"),
            "completion_tokens": answer.get("completion_tokens"),
            "missing_terms": missing_terms,
            "missing_tokens": missing_tokens,
            "mutated": bool(answer.get("mutated")),
        })
        if answer.get("unexpected_text"):
            failures.append(f"[live] {row['id']}: 失敗狀態卻帶回譯文")
        if mode == "live" and answer.get("status") != "ok":
            failures.append(f"[live] {row['id']}: 翻譯狀態 {answer.get('status')}")

    if locked_total != CONTRACT["locked_term_total"]:
        failures.append(f"[contract] 鎖定詞出現 {locked_total} 次，預期 {CONTRACT['locked_term_total']}")
    if token_total != CONTRACT["expect_tokens_total"]:
        failures.append(f"[contract] expect_tokens {token_total} 個，預期 {CONTRACT['expect_tokens_total']}")
    if gate_quality and not mutate:
        if scores and any(score != 100 for score in scores):
            failures.append("[chrf] fake 用參考譯文當假說時，每句 chrF 應為 100")
    if mutate:
        caught = any(row["missing_terms"] or row["missing_tokens"] for row in scored_rows)
        if not caught:
            failures.append("[mutate] 變異樣本沒有被抓到")
    quality_ok = _stat(locked_hit, locked_total)["ok"] and _stat(token_hit, token_total)["ok"]

    now = datetime.now(TAIPEI)
    chrf_mean = round(sum(scores) / len(scores), 2) if scores else None
    report = {
        "report_version": 1,
        "mode": mode,
        "model": model,
        "time": now.isoformat(timespec="seconds"),
        "measured": measured,
        "measured_label": _measured_label(mode, measured),
        "disclaimer": DISCLAIMER,
        "reference_status": "參考譯文待使用者確認；這份報告不是翻譯品質認證",
        "quality_is_gate": gate_quality,
        "quality_ok": quality_ok,
        "summary": _summary(mode, measured, not failures, quality_ok),
        "context": "每句單獨送譯，不帶前句。課堂上的前 4 句中文不在這次評測裡。",
        "glossary_terms": len(glossary),
        "glossary_locked": sum(1 for term in glossary if term.get("lock") is True),
        "corpus_rows": len(rows),
        "normalize": norm_stat,
        "prompt_only_matched": _stat(prompt_hit, len(rows)),
        "locked_term_hit": _stat(locked_hit, locked_total),
        "expect_tokens_hit": _stat(token_hit, token_total),
        "chrf": {
            "mean": chrf_mean,
            "min": round(min(scores), 2) if scores else None,
            "max": round(max(scores), 2) if scores else None,
            "beta": 2,
            "max_n": 6,
            "implementation": "pure-python",
            "note": "純 Python chrF（beta=2，n=1 到 6）。fake 用參考譯文得到 100，只代表計分與語料一致，不是模型品質。",
        },
        "sacrebleu": optional_bleu(
            [row["hyp_en"] for row in scored_rows],
            [row["ref_en"] for row in scored_rows],
        ) if scored_rows else {"bleu": None, "chrf": None, "note": "沒有假說"},
        "latency_ms": _latency(
            [float(row["latency_ms"]) for row in scored_rows if isinstance(row.get("latency_ms"), (int, float))],
            measured,
        ),
        "token_usage": _tokens(scored_rows, measured),
        "mutation": {
            "applied": [row_id for row_id, answer in answers.items() if answer.get("mutated")],
            "caught": (any(row["missing_terms"] or row["missing_tokens"] for row in scored_rows) if mutate else None),
        },
        "thresholds": {
            "normalize": "100%",
            "locked_term_hit_on_reference": "100%",
            "expect_tokens_on_reference": "100%",
            "prompt_only_matched": "100%",
            "live_chrf_latency": "不設門檻，尚未驗證",
        },
        "failures": failures,
        "ok": not failures,
        "rows": scored_rows,
    }
    if mode == "fake":
        report["latency_ms"]["note"] = "固定數列 20、21、…，用來檢查 p50/p95/max，不是實測延遲。"
        report["token_usage"]["note"] = "fake 模式沒有呼叫模型，token 用量是 0。"
    else:
        report["latency_ms"]["note"] = "單句翻譯呼叫的經過時間，含網路。不是課堂逐段延遲。"
        report["token_usage"]["note"] = "加總翻譯回應裡的 usage。沒有 usage 的句子不算實測。"
    return report


def _locked(row: dict, glossary, english: str) -> tuple[int, int, list[dict]]:
    hit = total = 0
    missing = []
    for term in matched_terms(row["zh"], glossary):
        if not is_locked(term):
            continue
        total += 1
        if term_hit(english, term):
            hit += 1
        else:
            missing.append({"zh": term["zh"], "en": term.get("en") or ""})
    return hit, total, missing


def _tokens_row(row: dict, english: str) -> tuple[int, int, list[str]]:
    hit = total = 0
    missing = []
    for token in row.get("expect_tokens") or []:
        total += 1
        if term_hit(english, {"en": token}):
            hit += 1
        else:
            missing.append(token)
    return hit, total, missing


def _summary(mode: str, measured: bool, gates_ok: bool, quality_ok: bool) -> str:
    if mode == "fake":
        if gates_ok:
            return "決定性檢查通過。fake 沒有呼叫模型，不是翻譯品質實測。"
        return "決定性檢查沒有通過。"
    if not measured:
        return "實測沒有完成。尚未驗證模型品質。"
    if quality_ok:
        return "實測已跑完，而且這次輸出通過語料的 100% 詞條與 token 檢查。參考譯文仍待使用者確認。"
    return "實測已跑完。詞條或 token 未達 100%，參考譯文待使用者確認，這不是通過認證。"


def _measured_label(mode: str, measured: bool) -> str:
    if mode == "fake":
        return "否。fake 模式沒有呼叫模型。"
    if measured:
        return "是。這次有呼叫設定的翻譯器。"
    return "否。翻譯器沒有對每一句回傳成功譯文。"


def live_output_path(model: str, when: datetime, directory: Path) -> Path:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in model).strip("._") or "model"
    return directory / f"{when:%Y-%m-%d}-{safe}.json"


def make_translator() -> Translator:
    """Same key, model, and on/off switch as the server, without mutating os.environ."""
    from app.settings import Settings, merge_env

    env = merge_env(dict(os.environ), ROOT / ".env")
    settings = Settings.from_env(env, env_file=ROOT / ".env.missing")
    model = (env.get("OPENAI_TRANSLATION_MODEL") or "").strip() or "gpt-4.1-mini"
    return Translator(
        enabled=settings.translate,
        key=env.get("OPENAI_API_KEY", ""),
        model=model,
        token_budget=settings.token_budget,
    )


def _consented(yes_bill: bool) -> bool:
    return bool(yes_bill) or os.environ.get("BREEZE_EVAL_LIVE", "") == "1"


def refuse_live() -> int:
    sys.stdout.write(
        "拒絕執行 --live：這會用目前設定的翻譯器呼叫模型，並計入你的 API 金鑰費用。\n"
        "尚未送出任何請求。\n"
        "若你同意計費，請設定 BREEZE_EVAL_LIVE=1，或加上 --yes-bill，然後再執行。\n"
    )
    return 2


def _write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path
    if target.exists():
        stamp = datetime.now(TAIPEI).strftime("%H%M%S")
        target = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
    report["output_path"] = str(target)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="評測固定語料的中翻英。--live 會計費。")
    parser.add_argument("--fake", action="store_true", help="決定性假翻譯器，不呼叫 API")
    parser.add_argument("--live", action="store_true", help="用設定的 Translator；需要同意計費")
    parser.add_argument("--mutate", action="store_true", help="把 Z02、Z36 的假說改壞，門檻應失敗")
    parser.add_argument("--yes-bill", action="store_true", help="同意 --live 使用你的 API 金鑰並計費")
    parser.add_argument("--corpus", type=Path, default=FIXTURE / "corpus_zen_club.jsonl")
    parser.add_argument("--glossary", type=Path, default=FIXTURE / "glossary_zen_club.csv")
    parser.add_argument("--out", type=Path, default=None, help="--live 報告路徑；預設 data/eval/<日期>-<模型>.json")
    args = parser.parse_args(argv)
    if args.fake == args.live:
        parser.error("請指定 --fake 或 --live，兩者擇一")
    if args.mutate and not args.fake:
        parser.error("--mutate 只能搭配 --fake，避免對計費呼叫改寫假說")
    if args.live and not _consented(args.yes_bill):
        return refuse_live()

    meta, rows = load_corpus(args.corpus)
    glossary = load_glossary(args.glossary)
    if args.fake:
        answers, mutate_problems = fake_answers(rows, args.mutate)
        report = evaluate(
            meta, rows, glossary, answers,
            mode="fake", model="fake", measured=False, gate_quality=True, mutate=args.mutate,
        )
        report["failures"] = mutate_problems + report["failures"]
        report["ok"] = not report["failures"]
        report["summary"] = _summary("fake", False, report["ok"], report["quality_ok"])
    else:
        translator = make_translator()
        answers = live_answers(rows, glossary, translator)
        measured = all(item.get("status") == "ok" for item in answers.values()) and bool(answers)
        report = evaluate(
            meta, rows, glossary, answers,
            mode="live", model=translator.model, measured=measured, gate_quality=False, mutate=False,
        )
        when = datetime.now(TAIPEI)
        directory = ROOT / "data" / "eval"
        path = args.out or live_output_path(translator.model, when, directory)
        _write_report(report, path)
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
