#!/usr/bin/env python3
"""Mizan fiyat bekçisi.

Resmi fiyat sayfalarını okur, mizan/fiyatlar.json ile karşılaştırır.

Güvenlik kuralları (yanlış fiyat yayınlamamak için):
  * Bir fiyat değişikliği, iki AYRI kontrolde aynı değer görülmeden yayınlanmaz.
  * Yeni fiyat eskisinin %80'i ile 2,5 katı arasında değilse yayınlanmaz, "şüpheli" sayılır.
  * Sayfa okunamazsa ya da fiyat bulunamazsa hiçbir şey değişmez, sorun raporlanır.

Yalnızca Python standart kütüphanesini kullanır.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "mizan" / "fiyatlar.json"
SOURCES = ROOT / "mizan" / "kaynaklar.json"
STATE = ROOT / "mizan" / "bekci_durum.json"
PROBLEMS = ROOT / "bekci_sorun.md"

TZ = dt.timezone(dt.timedelta(hours=3))  # Türkiye
PRICE_RE = r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
MIN_RATIO, MAX_RATIO = 0.8, 2.5

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def parse_price(raw: str) -> float | None:
    s = raw.strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def fetch_text(url: str) -> str:
    """Sayfayı indirir; etiketleri temizlenmiş, boşlukları sadeleşmiş metin döner.
    Script içerikleri de korunur (fiyatlar bazen sayfa verisinin içindedir)."""
    last_error = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.5",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as res:
                body = res.read().decode(res.headers.get_content_charset() or "utf-8", "replace")
            text = re.sub(r"<(script|style)[^>]*>", " ", body, flags=re.I)
            text = re.sub(r"</(script|style)>", " ", text, flags=re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = html.unescape(text)
            text = text.replace("\\u20ba", "₺").replace("\\u00a0", " ")
            text = text.replace(" ", " ").replace(" ", " ")
            return re.sub(r"\s+", " ", text)
        except Exception as e:  # ağ hatası, 403, zaman aşımı
            last_error = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{type(last_error).__name__}: {last_error}")


def extract(text: str, item: dict) -> float | None:
    """Bir planın fiyatını metinden çıkarır.

    item["pattern"]: bir yakalama grubu içeren düzenli ifade (group ile hangisi seçilir), ya da
    item["anchor"] + item["index"]: anchor'dan sonraki 'window' karakter içindeki n'inci fiyat.
    """
    flags = re.I | re.S
    if "pattern" in item:
        m = re.search(item["pattern"].replace("{PRICE}", PRICE_RE), text, flags)
        if not m:
            return None
        return parse_price(m.group(item.get("group", 1)))
    if "anchor" in item:
        m = re.search(item["anchor"], text, flags)
        if not m:
            return None
        window = text[m.end(): m.end() + int(item.get("window", 400))]
        unit = item.get("unit", r"(?:TL|₺)")
        prices = re.findall(rf"(?:₺\s?{PRICE_RE}|{PRICE_RE}\s?{unit})", window)
        values = [parse_price(a or b) for a, b in prices]
        values = [v for v in values if v is not None]
        idx = int(item.get("index", 0))
        return values[idx] if idx < len(values) else None
    return None


def bump_version(old: str, today: str) -> str:
    date, _, n = old.partition(".")
    if date == today:
        return f"{today}.{(int(n) if n.isdigit() else 0) + 1}"
    return f"{today}.1"


def fmt(v: float | None, cur: str = "TRY") -> str:
    if v is None:
        return "—"
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"₺{s}" if cur == "TRY" else f"{s} {cur}"


def main() -> int:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    pending = state.setdefault("pending", {})
    run_id = os.environ.get("GITHUB_RUN_ID", "local")

    services = {s["id"]: s for s in catalog["services"]}
    today = now().strftime("%Y-%m-%d")
    rows, problems, changed, verified_services = [], [], [], {}

    for src in sources["checks"]:
        url = src["url"]
        try:
            text = fetch_text(url)
        except Exception as e:
            for item in src["plans"]:
                rows.append((item["service"], item["plan"], "—", "—", "sayfa okunamadı"))
                verified_services[item["service"]] = False
            problems.append(f"- **Sayfa okunamadı:** {url} ({e})")
            continue

        for item in src["plans"]:
            sid, pid = item["service"], item["plan"]
            service = services.get(sid)
            plan = next((p for p in (service or {}).get("plans", []) if p["id"] == pid), None)
            if plan is None:
                problems.append(f"- **Katalogda yok:** {sid}/{pid}")
                continue
            cur = plan.get("currency", "TRY")
            listed = float(plan["price"])
            found = extract(text, item)
            key = f"{sid}/{pid}"

            if found is None:
                rows.append((sid, pid, fmt(listed, cur), "—", "fiyat bulunamadı"))
                problems.append(f"- **Fiyat bulunamadı:** {service['name']} {plan['name']} — {url}")
                verified_services[sid] = False
                continue

            if abs(found - listed) < 0.005:
                rows.append((sid, pid, fmt(listed, cur), fmt(found, cur), "doğru"))
                pending.pop(key, None)
                verified_services.setdefault(sid, True)
                continue

            verified_services[sid] = False
            ratio = found / listed if listed else 0
            if not (MIN_RATIO <= ratio <= MAX_RATIO):
                rows.append((sid, pid, fmt(listed, cur), fmt(found, cur), "şüpheli, yayınlanmadı"))
                problems.append(
                    f"- **Şüpheli fiyat:** {service['name']} {plan['name']}: listede {fmt(listed, cur)}, "
                    f"sayfada {fmt(found, cur)} göründü (kampanya olabilir). {url}"
                )
                continue

            prev = pending.get(key)
            if prev and abs(prev["price"] - found) < 0.005 and prev.get("run") != run_id:
                plan["previousPrice"] = listed
                plan["price"] = found
                plan["changedAt"] = today
                service["verifiedAt"] = today
                service["verification"] = "resmi"
                pending.pop(key, None)
                changed.append(f"{service['name']} {plan['name']}: {fmt(listed, cur)} → {fmt(found, cur)}")
                rows.append((sid, pid, fmt(listed, cur), fmt(found, cur), "GÜNCELLENDİ"))
            else:
                pending[key] = {"price": found, "first_seen": today, "run": run_id}
                rows.append((sid, pid, fmt(listed, cur), fmt(found, cur), "değişti, 2. kontrol bekleniyor"))

    # Tüm planları doğru çıkan servislerin doğrulama tarihi haftada bir yenilenir.
    refreshed = []
    for sid, ok in verified_services.items():
        s = services.get(sid)
        if not ok or s is None:
            continue
        last = s.get("verifiedAt", "")
        try:
            age = (dt.date.fromisoformat(today) - dt.date.fromisoformat(last[:10])).days
        except ValueError:
            age = 999
        if age >= 7:
            s["verifiedAt"] = today
            s["verification"] = "resmi"
            refreshed.append(sid)

    if changed or refreshed:
        catalog["listVersion"] = bump_version(catalog.get("listVersion", ""), today)
        catalog["updatedAt"] = now().isoformat(timespec="seconds")
        CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    state["run_id"] = run_id
    state["last_run"] = now().isoformat(timespec="seconds")

    # Özet tablo (GitHub Actions iş özeti ve konsol)
    lines = [
        f"## Mizan fiyat bekçisi · {now().strftime('%d.%m.%Y %H:%M')}",
        "",
        "| Servis | Plan | Listede | Resmi sayfada | Durum |",
        "|---|---|---|---|---|",
        *[f"| {a} | {b} | {c} | {d} | {e} |" for a, b, c, d, e in rows],
        "",
    ]
    if changed:
        lines += ["### Yayınlanan değişiklikler", *[f"- {c}" for c in changed], ""]
    summary = "\n".join(lines)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary)

    # Sorun raporu: yalnızca içerik değiştiyse yazılır (aynı sorun için her gün bildirim gelmesin).
    problem_text = "\n".join(sorted(set(problems)))
    problem_hash = hashlib.sha1(problem_text.encode()).hexdigest() if problems else ""
    if problems and problem_hash != state.get("last_problem_hash"):
        PROBLEMS.write_text(
            "Fiyat bekçisi bazı fiyatları otomatik doğrulayamadı. Bunlar için listede hiçbir şey "
            "değiştirilmedi; uygulama mevcut fiyatları göstermeye devam ediyor.\n\n"
            + problem_text
            + "\n\nAyrıntılı tablo: Actions › Mizan fiyat bekçisi › son çalışma özeti.\n",
            encoding="utf-8",
        )
    state["last_problem_hash"] = problem_hash

    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
