# 翻譯評測語料（固定）

自擬範例資料，非真實逐字稿，參考譯文待使用者確認。

這份資料只給 `scripts/eval_translation.py` 與 `tests/test_translation_eval.py` 使用。不是 breeze 的預設術語表，也不是品質認證。`app/glossary.py` 的空範本不收這些譯法。譯名（社團正式英文名、法師、師父、期初演講等）仍待使用者確認。

| 檔案 | 內容 |
|---|---|
| `glossary_zen_club.csv` | 48 條建議術語（鎖定 37、只提示 11）。第一行是上述聲明。 |
| `corpus_zen_club.jsonl` | 50 句。第一行 `_meta` 帶同一句聲明。 |
| `build_corpus.py` | 產生 JSONL。改句子請改這支再重跑，不要只改 JSONL。 |

類別：term 20、negation 7、asr_alias 6、career 4、fragment 4、negative 3、question 2、number／place／name／injection 各 1。

決定性檢查（門檻 100%，沒有寬鬆門檻）：正規化、參考英文的鎖定術語命中、expect_tokens、prompt 只含命中的詞。chrF、BLEU、延遲、token 用量可以印出來；真實模型的這些數字尚未驗證，不進 CI。

```
python scripts/eval_translation.py --fake
python scripts/eval_translation.py --fake --mutate
python scripts/eval_translation.py --live --yes-bill
```

`--fake` 不呼叫 API。`--mutate` 會把 Z02、Z36 改壞，結束代碼應為 1。`--live` 用目前設定的翻譯器，會計入你的 API 金鑰；沒有 `--yes-bill` 或 `BREEZE_EVAL_LIVE=1` 就拒絕，而且不會送出請求。報告寫到 `data/eval/<日期>-<模型>.json`。
