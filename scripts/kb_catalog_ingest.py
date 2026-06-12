#!/usr/bin/env python3
"""Каталог Школы (Google-таблица → CSV) → карточки + карточка-оглавление → эмбеддинг (TEI) →
SQL для pgvector. Запускать ПОСЛЕ чистки цен в таблице. Идемпотентно по source-тегам
('google-sheet:catalog' и 'catalog-index') — перезалив сносит старое и грузит заново.

  # 1) выгрузить вкладку «исправить» как CSV:
  curl -sL "https://docs.google.com/spreadsheets/d/1EkDFntg4XA-RZ4LrxHp9csUgrmJNpgFvvVyVsk_3iEU/export?format=csv&gid=1721549680" -o /tmp/kb_sheet.csv
  # 2) сгенерировать SQL (эмбеддер — из env; IP в публичный репо НЕ хардкодим):
  EMBEDDER_URL=http://<vm-ip>:8081 \
  EMBEDDER_TOKEN=$(ssh root@<vm-ip> 'grep EMBEDDER_TOKEN /opt/risuy-tei/.env|cut -d= -f2') \
  python3 scripts/kb_catalog_ingest.py /tmp/kb_sheet.csv
  # 3) применить owner-DSN (пароль не светим — twc-migrate тянет его из API сам):
  bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy gen_user \
    /tmp/kb_seed.sql /tmp/kb_index.sql /tmp/kb_test.sql

Пишет /tmp/kb_seed.sql (каталог + kb_enabled=1), /tmp/kb_index.sql (оглавление — ранжируется #1
на запрос-список), /tmp/kb_test.sql (3 retrieval-проверки). e5 ТРЕБУЕТ префикс passage:/query: — учтено.
"""
import csv, json, os, re, sys, urllib.request

EMB = os.environ.get("EMBEDDER_URL", "").rstrip("/")
TOKEN = os.environ.get("EMBEDDER_TOKEN", "")
CSV_PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/kb_sheet.csv"
if not EMB:
    raise SystemExit("Задайте EMBEDDER_URL (напр. http://<vm-ip>:8081) в окружении.")

# Поля карточки программы: колонка CSV → подпись в тексте
FIELDS = [
    ("positioning", "Позиционирование"), ("target", "Для кого"), ("goal", "Цель"),
    ("outcomes", "Результат"), ("curriculum", "Программа"), ("duration", "Длительность"),
    ("schedule", "Расписание"), ("promo", "Акция"), ("materials", "Материалы"),
    ("value_prop", "Чем ценен"), ("restrictions", "Ограничение (возраст)"),
    ("location_addr", "Адрес"),
]


def clean_title(t):
    t = (t or "").strip()
    t = re.split(r"\s*[-—]\s*Эскал", t)[0]               # срез внутренней служебной логики из title
    t = re.split(r"оставьте свои контакт", t, flags=re.I)[0]
    return t.strip(" -—\n\t")


def teachers(r):
    v = (r.get("teachers") or "").strip()
    if not v:
        return ""
    try:
        a = json.loads(v)                                 # в таблице — JSON-массив имён
        if isinstance(a, list):
            return ", ".join(str(x) for x in a)
    except Exception:
        pass
    return v


def price_full(r):
    p = []
    if (r.get("price_full") or "").strip():
        p.append(f"{r['price_full'].strip()} ₽")
    if (r.get("price_month") or "").strip():
        p.append(f"{r['price_month'].strip()} ₽/мес")
    if (r.get("price_note") or "").strip():
        p.append(r["price_note"].strip())
    return "; ".join(p)


def price_short(r):
    if (r.get("price_full") or "").strip():
        return f"{r['price_full'].strip()} ₽"
    pn = (r.get("price_note") or "").strip()
    return (pn[:40] + "…") if len(pn) > 42 else pn


def build_card(r):
    title = clean_title(r.get("title"))
    parts = []
    for key, label in FIELDS:
        v = (r.get(key) or "").strip().replace("\n", " ")
        if v:
            parts.append(f"{label}: {v}")
    if price_full(r):
        parts.append(f"Цена: {price_full(r)}")
    if teachers(r):
        parts.append(f"Преподаватели: {teachers(r)}")
    body = ". ".join(p.rstrip(". ") for p in parts)
    return title, (f"{title}. {body}".strip() if body else title)


def embed(texts):
    """Эмбеддинги через TEI, батчами по 32 (лимит клиентского батча)."""
    out = []
    for i in range(0, len(texts), 32):
        req = urllib.request.Request(
            EMB + "/embed",
            data=json.dumps({"inputs": texts[i:i + 32], "normalize": True}).encode(),
            headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}"},
        )
        out.extend(json.loads(urllib.request.urlopen(req, timeout=120).read()))
    return out


q = lambda s: s.replace("'", "''")
vlit = lambda v: "[" + ",".join(f"{x:.6f}" for x in v) + "]"

rows = [r for r in csv.DictReader(open(CSV_PATH, encoding="utf-8"))
        if any((v or "").strip() for v in r.values())]

# ── 1) Карточки каталога (одна на программу) ──
cards = []
for r in rows:
    title, text = build_card(r)
    if len((text or "").strip()) >= 15:
        cards.append((title, text))
vecs = embed([f"passage: {t}" for _, t in cards])
assert len(vecs) == len(cards), f"{len(vecs)} != {len(cards)}"
seed = ["delete from kb_documents where source='google-sheet:catalog';",
        "do $$ declare doc uuid; begin",
        "insert into kb_documents(title,source,role_tag,content,created_by) values "
        f"('Каталог программ Школы Лесова','google-sheet:catalog',null,'Каталог из Google-таблицы ({len(cards)} карточек)','script') returning id into doc;",
        "insert into kb_chunks(document_id,chunk_index,content,embedding,metadata) values"]
vals = []
for i, ((title, text), v) in enumerate(zip(cards, vecs)):
    meta = json.dumps({"role_tag": "", "title": title[:120], "source": "google-sheet:catalog"}, ensure_ascii=False)
    vals.append(f"(doc,{i},'{q(text)}','{vlit(v)}'::vector,'{q(meta)}'::jsonb)")
seed.append(",\n".join(vals) + ";")
seed.append("end $$;")
seed.append("insert into app_settings(key,value) values('kb_enabled','1') on conflict (key) do update set value=excluded.value;")
open("/tmp/kb_seed.sql", "w", encoding="utf-8").write("\n".join(seed))

# ── 2) Карточка-оглавление (полный перечень по категориям → #1 на запрос-список) ──
cats = {"Очные курсы (Санкт-Петербург)": [], "Онлайн-курсы": [], "Видеоуроки и записи": [], "Консультации": []}
for r in rows:
    t = clean_title(r.get("title"))
    if not t or "Школа рисунка" in t or "Здравствуйте" in t:
        continue                                          # пропустить интро-строку
    low, p = t.lower(), price_short(r)
    entry = f"«{t}»" + (f" — {p}" if p else "")
    if "консультаци" in low:
        cats["Консультации"].append(entry)
    elif any(k in low for k in ("видео", "мини-курс", "сборник", "спринт", "набро")):
        cats["Видеоуроки и записи"].append(entry)
    elif "онлайн" in low:
        cats["Онлайн-курсы"].append(entry)
    else:
        cats["Очные курсы (Санкт-Петербург)"].append(entry)
lines = ["Полный список программ Школы рисунка и живописи им. Лесова — все направления (по каждому есть отдельная карточка с деталями):"]
for cat, items in cats.items():
    if items:
        lines.append(f"{cat}: " + "; ".join(items) + ".")
lines.append("Это полный перечень направлений школы. Если спрашивают «какие курсы есть» — назови основные из всех категорий, не ограничивайся одной.")
index_text = "\n".join(lines)
ivec = embed([f"passage: {index_text}"])[0]
imeta = json.dumps({"role_tag": "", "title": "Полный список программ", "source": "catalog-index"}, ensure_ascii=False)
open("/tmp/kb_index.sql", "w", encoding="utf-8").write(
    "delete from kb_documents where source='catalog-index';\n"
    "do $$ declare doc uuid; begin\n"
    "insert into kb_documents(title,source,role_tag,content,created_by) "
    f"values('Полный список программ','catalog-index',null,'{q(index_text)}','script') returning id into doc;\n"
    "insert into kb_chunks(document_id,chunk_index,content,embedding,metadata) "
    f"values(doc,0,'{q(index_text)}','{vlit(ivec)}'::vector,'{q(imeta)}'::jsonb);\n"
    "end $$;"
)

# ── 3) Проверочные retrieval-запросы (для глаз — top-3 на вопрос) ──
TESTQS = ["сколько стоит подготовка в Академию Репина", "есть ли курсы рисования для детей",
          "какие у вас есть курсы расскажите все"]
tvecs = embed([f"query: {x}" for x in TESTQS])
tsql = [f"select '{q(x)}' as вопрос, metadata->>'title' as нашлось, "
        f"round((embedding <=> '{vlit(tv)}'::vector)::numeric,3) as дист "
        f"from kb_chunks order by embedding <=> '{vlit(tv)}'::vector limit 3;"
        for x, tv in zip(TESTQS, tvecs)]
open("/tmp/kb_test.sql", "w", encoding="utf-8").write("\n".join(tsql))

print(f"✅ карточек: {len(cards)} | оглавление: 1 | размерность: {len(vecs[0])}")
print("   файлы: /tmp/kb_seed.sql, /tmp/kb_index.sql, /tmp/kb_test.sql")
print("   применить: bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy gen_user /tmp/kb_seed.sql /tmp/kb_index.sql /tmp/kb_test.sql")
