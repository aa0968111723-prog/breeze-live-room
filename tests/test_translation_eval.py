"""Deterministic gates for the fixed translation corpus.

The suite never calls OpenAI. --live is refused unless a test stubs the HTTP
opener after an explicit consent flag. Reference English is not a certificate.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "translation"
DISCLAIMER = "自擬範例資料，非真實逐字稿，參考譯文待使用者確認"
SCRIPT = ROOT / "scripts" / "eval_translation.py"


def _load():
    spec = importlib.util.spec_from_file_location("eval_translation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EV = _load()


def _run(args, env=None):
    merged = dict(os.environ)
    merged.pop("OPENAI_API_KEY", None)
    merged.pop("BREEZE_EVAL_LIVE", None)
    merged["PYTHONPATH"] = str(ROOT)
    merged["PYTHONIOENCODING"] = "utf-8"
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=merged,
        check=False,
    )


def _report(proc):
    assert proc.stdout.strip(), proc.stderr
    return json.loads(proc.stdout)


class _Body:
    def __init__(self, raw: bytes):
        self.raw = raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_disclaimer_is_on_the_header_and_readme():
    readme = (FIXTURE / "README.md").read_text(encoding="utf-8")
    glossary = (FIXTURE / "glossary_zen_club.csv").read_text(encoding="utf-8")
    corpus = (FIXTURE / "corpus_zen_club.jsonl").read_text(encoding="utf-8")
    assert DISCLAIMER in readme
    assert glossary.startswith("# " + DISCLAIMER)
    meta = json.loads(corpus.splitlines()[0])["_meta"]
    assert meta["disclaimer"] == DISCLAIMER
    assert meta["not_app_default"] is True


def test_fixture_is_not_the_product_default():
    from app.glossary import TEMPLATE_CSV

    assert TEMPLATE_CSV == "zh,aliases,en,lock,category,note\n"
    pieces = []
    for path in (ROOT / "app").rglob("*"):
        if path.suffix in {".py", ".html", ".js", ".csv", ".md"}:
            pieces.append(path.read_text(encoding="utf-8"))
    app_text = "\n".join(pieces)
    assert "Leadership Zen Club" not in app_text
    assert "glossary_zen_club" not in app_text
    assert DISCLAIMER not in app_text


def test_builder_matches_the_committed_corpus(tmp_path):
    out = tmp_path / "corpus.jsonl"
    proc = subprocess.run(
        [sys.executable, str(FIXTURE / "build_corpus.py"), "--out", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.read_text(encoding="utf-8") == (FIXTURE / "corpus_zen_club.jsonl").read_text(encoding="utf-8")


def test_corpus_and_glossary_stay_consistent():
    meta, rows = EV.load_corpus(FIXTURE / "corpus_zen_club.jsonl")
    glossary = EV.load_glossary(FIXTURE / "glossary_zen_club.csv")
    assert EV.corpus_problems(meta, rows) == []
    assert EV.glossary_problems(glossary) == []
    norm, failures = EV.normalization_report(rows, glossary)
    assert failures == []
    assert norm == {"hit": 56, "total": 56, "ok": True}
    locked = tokens = 0
    for row in rows:
        _hit, total, missing = EV._locked(row, glossary, row["ref_en"])
        locked += total
        assert missing == []
        _hit, total, missing = EV._tokens_row(row, row["ref_en"])
        tokens += total
        assert missing == []
    assert locked == 53
    assert tokens == 8
    assert sum(1 for term in glossary if term["lock"]) == 37
    assert len(glossary) == 48


def test_chrf_matches_a_hand_calculated_pair():
    assert EV.chrf("ab", "ab") == 100
    assert EV.chrf("禪學社", "禪學社") == 100
    # n=1 overlap 1/2; n=2 overlap 0. Mean precision = mean recall = 0.25. F2 = 25.
    assert EV.chrf("ab", "ac") == 25
    assert EV.chrf("", "ab") == 0


def test_fake_report_passes_every_gate():
    meta, rows = EV.load_corpus(FIXTURE / "corpus_zen_club.jsonl")
    glossary = EV.load_glossary(FIXTURE / "glossary_zen_club.csv")
    answers, problems = EV.fake_answers(rows, mutate=False)
    assert problems == []
    report = EV.evaluate(
        meta, rows, glossary, answers,
        mode="fake", model="fake", measured=False, gate_quality=True, mutate=False,
    )
    assert report["ok"] is True
    assert report["failures"] == []
    assert report["measured"] is False
    assert report["quality_ok"] is True
    assert "不是翻譯品質實測" in report["summary"]
    assert report["normalize"]["ok"] is True
    assert report["prompt_only_matched"] == {"hit": 50, "total": 50, "ok": True}
    assert report["locked_term_hit"] == {"hit": 53, "total": 53, "ok": True}
    assert report["expect_tokens_hit"] == {"hit": 8, "total": 8, "ok": True}
    assert report["chrf"]["mean"] == 100
    assert report["latency_ms"]["measured"] is False
    assert report["latency_ms"]["n"] == 50
    assert report["token_usage"]["measured"] is False
    assert report["token_usage"]["total_tokens"] == 0
    assert DISCLAIMER in report["disclaimer"]
    z01 = next(row for row in report["rows"] if row["id"] == "Z01")
    assert z01["missing_terms"] == []


def test_prompt_contains_only_the_longest_match():
    glossary = EV.load_glossary(FIXTURE / "glossary_zen_club.csv")
    translator = __import__("app.translate", fromlist=["Translator"]).Translator()
    z01 = "歡迎大家來到領袖禪學社的社課。"
    messages = translator.build_messages(z01, glossary, None)
    assert EV.diff_prompt(messages, z01, glossary, None, messages[0]["content"]) == []
    user = json.loads(messages[1]["content"])
    assert [item["zh"] for item in user["glossary"]] == ["領袖禪學社", "社課"]
    assert user["glossary"][0]["en"] == "Leadership Zen Club"
    assert all("note" not in item for item in user["glossary"])
    leaked = json.loads(messages[1]["content"])
    leaked["glossary"].append({"zh": "法鼓山", "en": "Dharma Drum Mountain", "locked": True})
    messages[1]["content"] = json.dumps(leaked, ensure_ascii=False)
    problems = EV.diff_prompt(messages, z01, glossary, None, messages[0]["content"])
    assert any("法鼓山" in problem for problem in problems)
    with_context = translator.build_messages(z01, glossary, ["法鼓山"])
    context_user = json.loads(with_context[1]["content"])
    assert [item["zh"] for item in context_user["glossary"]] == ["領袖禪學社", "社課", "法鼓山"]
    assert EV.prompt_problems(z01, glossary, ["法鼓山"], translator) == []
    note = next(term["note"] for term in glossary if term["zh"] == "禪學社")
    assert note
    assert note not in with_context[1]["content"]


def test_mutation_drops_one_locked_term_and_one_token():
    meta, rows = EV.load_corpus(FIXTURE / "corpus_zen_club.jsonl")
    glossary = EV.load_glossary(FIXTURE / "glossary_zen_club.csv")
    answers, problems = EV.fake_answers(rows, mutate=True)
    assert problems == []
    report = EV.evaluate(
        meta, rows, glossary, answers,
        mode="fake", model="fake", measured=False, gate_quality=True, mutate=True,
    )
    assert report["ok"] is False
    assert report["mutation"]["caught"] is True
    assert report["locked_term_hit"] == {"hit": 52, "total": 53, "ok": False}
    assert report["expect_tokens_hit"] == {"hit": 7, "total": 8, "ok": False}
    assert report["normalize"]["ok"] is True
    assert report["prompt_only_matched"]["ok"] is True
    assert report["chrf"]["mean"] < 100
    assert any("[term] Z02:" in item and "Dharma talk" in item for item in report["failures"])
    assert any("[token] Z36:" in item and "42" in item for item in report["failures"])
    z02 = next(row for row in report["rows"] if row["id"] == "Z02")
    assert z02["missing_terms"] == [{"zh": "開示", "en": "Dharma talk"}]
    assert "sitting meditation" not in json.dumps(z02["missing_terms"])


def test_cli_fake_exits_0_and_does_not_write_a_report():
    folder = ROOT / "data" / "eval"
    before = set(folder.glob("*")) if folder.exists() else set()
    proc = _run(["--fake"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["ok"] is True
    assert report["mode"] == "fake"
    assert report["measured"] is False
    assert report["model"] == "fake"
    assert "計費" not in proc.stdout
    after = set(folder.glob("*")) if folder.exists() else set()
    assert after == before


def test_cli_mutated_sample_exits_1():
    proc = _run(["--fake", "--mutate"])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["ok"] is False
    assert report["mutation"]["caught"] is True
    assert report["locked_term_hit"]["hit"] == 52
    assert report["expect_tokens_hit"]["hit"] == 7
    assert report["normalize"]["hit"] == 56


def test_cli_live_refuses_without_consent(monkeypatch, capsys):
    calls = []

    def opener(req, timeout=40):
        calls.append(req.full_url)
        raise AssertionError("不該送出請求")

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.delenv("BREEZE_EVAL_LIVE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    code = EV.main(["--live"])
    message = capsys.readouterr().out
    assert code == 2
    assert calls == []
    assert "計入你的 API 金鑰費用" in message
    assert "尚未送出任何請求" in message
    with pytest.raises(SystemExit) as raised:
        EV.main(["--live", "--mutate", "--yes-bill"])
    assert raised.value.code == 2
    assert calls == []


def test_cli_fake_never_opens_the_network(monkeypatch):
    calls = []

    def opener(req, timeout=40):
        calls.append(req.full_url)
        raise AssertionError("fake 不該送出請求")

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("BREEZE_EVAL_LIVE", "1")
    assert EV.main(["--fake", "--yes-bill"]) == 0
    assert calls == []


def test_cli_live_consent_uses_a_stub_and_writes_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BREEZE_TRANSLATE", "1")
    calls = []

    def opener(req, timeout=40):
        calls.append(req.full_url)
        assert "api.openai.com" in req.full_url
        payload = {
            "choices": [{"message": {"content": "stub english"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
        return _Body(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.delenv("BREEZE_EVAL_LIVE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("OPENAI_TRANSLATION_MODEL", "gpt-test-eval")
    out = tmp_path / "live.json"
    code = EV.main(["--live", "--yes-bill", "--out", str(out)])
    assert calls, "同意計費後應該呼叫翻譯器（這裡是測試替身）"
    assert len(calls) == 50
    assert code == 0, out.read_text(encoding="utf-8") if out.exists() else calls
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["measured"] is True
    assert saved["model"] == "gpt-test-eval"
    assert saved["mode"] == "live"
    assert saved["quality_is_gate"] is False
    assert saved["quality_ok"] is False
    assert "不是通過認證" in saved["summary"]
    assert saved["token_usage"]["prompt_tokens"] == 150
    assert saved["token_usage"]["completion_tokens"] == 100
    assert saved["token_usage"]["total_tokens"] == 250
    assert saved["token_usage"]["measured"] is True
    assert saved["latency_ms"]["n"] == 50
    assert saved["latency_ms"]["p50"] <= saved["latency_ms"]["p95"] <= saved["latency_ms"]["max"]
    assert saved["locked_term_hit"]["ok"] is False
    assert "參考譯文待使用者確認" in saved["reference_status"]
    assert DISCLAIMER in saved["disclaimer"]
    assert saved["output_path"] == str(out)


def test_live_env_flag_is_also_consent(tmp_path, monkeypatch):
    calls = []

    def opener(req, timeout=40):
        calls.append(req.full_url)
        payload = {"choices": [{"message": {"content": "stub"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return _Body(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setenv("BREEZE_EVAL_LIVE", "1")
    monkeypatch.setenv("BREEZE_TRANSLATE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("OPENAI_TRANSLATION_MODEL", "gpt-test-eval")
    code = EV.main(["--live", "--out", str(tmp_path / "from-env.json")])
    assert code == 0
    assert len(calls) == 50


def test_failed_translation_is_not_scored_as_english():
    meta, rows = EV.load_corpus(FIXTURE / "corpus_zen_club.jsonl")
    glossary = EV.load_glossary(FIXTURE / "glossary_zen_club.csv")

    class Bad:
        def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
            from app.translate import TranslateResult
            return TranslateResult("這是中文不該當英文", "error")

    answers = EV.live_answers(rows[:1], glossary, Bad())
    assert answers["Z01"]["en"] == ""
    assert answers["Z01"]["unexpected_text"] is True
    report = EV.evaluate(
        meta, rows[:1], glossary, answers,
        mode="live", model="bad", measured=False, gate_quality=False, mutate=False,
    )
    assert report["ok"] is False
    assert any("失敗狀態卻帶回譯文" in item for item in report["failures"])
    assert report["rows"][0]["hyp_en"] == ""


def test_bleu_is_skipped_when_sacrebleu_is_missing(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "sacrebleu":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    assert EV.optional_bleu(["hello"], ["hello"])["bleu"] is None
    assert "略過 BLEU" in EV.optional_bleu(["hello"], ["hello"])["note"]


def test_fake_latency_uses_the_app_percentile():
    from app.rtf import percentile

    values = [float(20 + index) for index in range(50)]
    meta, rows = EV.load_corpus(FIXTURE / "corpus_zen_club.jsonl")
    glossary = EV.load_glossary(FIXTURE / "glossary_zen_club.csv")
    answers, _problems = EV.fake_answers(rows, mutate=False)
    report = EV.evaluate(
        meta, rows, glossary, answers,
        mode="fake", model="fake", measured=False, gate_quality=True, mutate=False,
    )
    assert report["latency_ms"]["p50"] == round(float(percentile(values, 0.50)), 6)
    assert report["latency_ms"]["p95"] == round(float(percentile(values, 0.95)), 6)
    assert report["latency_ms"]["max"] == 69


def test_cli_requires_one_mode():
    proc = _run([])
    assert proc.returncode != 0
    both = _run(["--fake", "--live"])
    assert both.returncode != 0
    mutated_live = _run(["--live", "--mutate", "--yes-bill"])
    assert mutated_live.returncode != 0
    assert "api.openai.com" not in (mutated_live.stderr + mutated_live.stdout)
