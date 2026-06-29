# Исследование: приватный РФ-резидентный LLM-бэкенд (Gemma + альтернативы), 2026-06-29

Контекст: risuy-ecosystem (РФ, 152-ФЗ) выбирает приватный РФ-бэкенд вместо трансгранич. Timeweb Gateway→DeepSeek.
Timeweb-поддержка для приватного размещения предложила ТОЛЬКО Gemma. Решение владельца: **Gemma — претендент №1**
(узкое горлышко — КОНТЕКСТ, см. план `docs/superpowers/plans/2026-06-29-private-rf-llm-backend.md`).
Исследование — многоагентное, web, со ссылками. Цифры — 2026.

## A. Gemma (Google open-weights)
- **Gemma 4** (релиз 02.04.2026): размеры E2B/E4B/12B/26B-MoE(~4B активных)/31B; **контекст 128k (E2B/E4B) /
  256k (12B/26B/31B)**; **Apache 2.0** (коммерция/self-host/fine-tune без порогов); **нативный function-calling**
  (спецтокены + JSON-schema) на всех размерах. [ai.google.dev/gemma/docs/core], [opensource.googleblog.com/2026/03/gemma-4]
- **Gemma 3**: 1B/4B/12B/27B; контекст 128k (4B+); кастомная (отзывная) лицензия Gemma Terms + Prohibited Use
  Policy (наследуется производными); tool-calling через промпт/парсинг (не нативный). [huggingface.co/blog/gemma3], [ai.google.dev/gemma/terms]
- **VRAM (Q4)**: 31B ~17.5 ГБ, 26B-MoE ~14.4 ГБ, 12B ~6.7 ГБ; Gemma 3 27B QAT int4 ~14 ГБ → одна 24ГБ-карта.
  [developers.googleblog.com/.../gemma-3-quantized]
- ⚠️ **Русский**: официальных русско-дисагрегированных метрик нет; в одном сравнении Gemma 3 — последняя по
  мультиязычности среди open. Агрегатно сильна (27B Global-MMLU-Lite 75.1). → нужен эвал на промпте Лии.
  [arxiv.org/html/2503.19786v1], [ai.rs/.../gemma-4-vs-qwen-3-5-vs-llama-4]
- Prohibited Use Policy — без гео/санкционных ограничений против РФ (тематические запреты). [ai.google.dev/gemma/prohibited_use_policy]

## B. Альтернативы
- **T-pro 2.0** (Т-Банк, Apache 2.0, рус-дотюн Qwen3-32B): **лучший русский среди open** (MERA 0.66,
  Arena-Hard-Ru 91.1); 32k native → 128k RoPE; tool-calling от Qwen3. [arxiv.org/abs/2512.10430]
- **Qwen3** (Apache 2.0, 0.6–235B): сильный tool-calling (Qwen-Agent/MCP/vLLM), 128k+, 119 языков; русский
  слабее дотюна T-pro. [github.com/QwenLM/qwen3]
- **Llama 3.x / Saiga**: кастомная лицензия Meta (порог 700M MAU, AUP против проф-советов, атрибуция) — риск. [wcr.legal/llama-3-license]
- **DeepSeek self-host**: 671B → ~376 ГБ VRAM Q4 (5–6×H100) — нереалистично/дорого; дешёвый API = трансгран.
- **GigaChat** (Сбер, managed РФ, FSTEC): 0.2 ₽/1К; function-calling на Pro/MAX; ⚠️ оферта ЗАПРЕЩАЕТ ПДн в
  запросах без enterprise-договора. [developers.sber.ru/.../tariffs], [securegpt.ru/.../ai-personal-data-legal-risks]
- **YandexGPT** (managed РФ, FSTEC УЗ-3): Lite ~0.2/0.4 ₽/1К, 5 Pro 128k ~2/6 ₽/1К; ⚠️ **на Timeweb — 32k вход**;
  стандартная оферта резервирует права на запросы. [geoscout.pro/.../yandexgpt-dlya-biznesa]

## C. РФ-хостинг и инференс
- **GPU-аренда РФ** (открытый прайс): RTX 3090 24ГБ ~37–68 ₽/час (~27–49к ₽/мес), RTX 4090 ~84 ₽/час (~60к),
  A100 80ГБ ~160–255к ₽/мес. Timeweb/Selectel/Cloud.ru — карты есть (L4/A5000/A6000/A100/H100), цена за заявкой.
  [immers.cloud/gpu], [intelion.cloud], [timeweb.cloud/services/gpu], [selectel.ru/.../gpu]
- **Стек**: **vLLM** — дефолт для прод (OpenAI `/v1/chat/completions` + tool-calling + continuous batching);
  SGLang — сильная альтернатива (agent/RAG); Ollama — только дев; TGI — maintenance mode. [docs.vllm.ai/.../tool_calling]
- ⚠️ **Tool-calling на reasoning-моделях** (Gemma 4/Qwen3): при «думании» tool-call уходит в reasoning_content →
  гасить thinking (`enable_thinking:false`) + правильный parser; запинить версию vLLM (баги). [github.com/vllm-project/vllm#39056], [insiderllm.com/.../function-calling-local-llms]
- **Экономика**: self-host ~30–60к ₽/мес + DevOps; точка безубыточности vs GigaChat (0.2 ₽/1К) ≈ **~2000
  диал/день**. При 100–500 диал/день managed дешевле → self-host оправдан **compliance, не деньгами**.
- ⚠️ **Дефицит GPU в РФ 2026** (санкции): высокие ставки, риск нехватки; одна карта = SPOF.

## D. 152-ФЗ-логика
- **self-host в РФ-ЦОД = самый чистый**: нет трансграна (ст.12 неприменима), нет стороннего обработчика →
  **договор поручения (ч.3 ст.6) и отдельное согласие НЕ нужны** (risuy сам обработчик). [vc.ru/ai/2982765]
- **managed РФ (GigaChat/YandexGPT)**: трансгран решён (серверы РФ), но это сторонний обработчик → **нужны DPA
  ч.3 ст.6 + отдельное согласие (с 01.09.2025 отдельным документом) + гарантия неиспользования для обучения**;
  стандартные оферты обоих НЕ дают этого для ПДн. [securegpt.ru], [robokassa.com/.../izmeneniya-v-152-fz]
- Штрафы: трансгран без уведомления до 6 млн ₽ (повтор 18 млн); утечки до 15 млн + оборотные 1–3% за повтор.
  [consultant.ru/legalnews/28492]
- ⚠️ self-host НЕ снимает прочие обязанности оператора: локализация первичной БД (ч.5 ст.18, уже ОК — кластер в
  РФ), аттестация ИСПДн, уведомление РКН, ретеншн.
- ⚠️ Экспортный контроль США на веса Gemma поверх лицензии — отдельный непроверенный пласт (Apache 2.0 гео не
  ограничивает, но EAR/OFAC применяются независимо) — уточнить отдельно.

## Вывод
Узкое горлышко — **контекст + следование длинному промпту** (промпт Лии 20k символов; YandexGPT-Timeweb 32k).
**Gemma 4** (256k, Apache 2.0, нативный tool-calling, self-host в РФ) — лучший ответ на это + чистый по 152-ФЗ.
Русский у Gemma — слабее Qwen/T-pro по бенчам, но по оценке владельца приемлем; **обязателен эвал на промпте Лии**
до прод. Запасные: Qwen3-32B/T-pro 2.0 (лучший русский), managed YandexGPT/GigaChat (быстро, но 32k + DPA).
