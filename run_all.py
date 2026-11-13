"""
run_all.py - odpala po kolei skanery wszystkich dostawców, a na końcu generator configu.

Każdy skaner zapisuje własną listę w tym samym formacie: lists/<dostawca>.json

    {
      "provider": "mistral",
      "timestamp": "2026-11-09 14:00:00",
      "list_ok": true,                 # czy udało się pobrać katalog modeli od dostawcy
      "listed": ["id", ...],           # WSZYSTKIE modele czatowe, które dostawca aktualnie zwraca
      "ok": ["id", ...],               # odpowiedziały 200
      "rate_limited": ["id", ...],     # odpowiedziały 429
      "failed": [{"id": "...", "status": 404}, ...],
      "capabilities": {"id": {"vision": true|false|null, "tools": true|false|null, "source": "probe"}}
    }

Użycie:
    python run_all.py                  # wszyscy dostawcy + generowanie configu
    python run_all.py groq mistral     # tylko wybrani dostawcy + generowanie configu
    python run_all.py --no-config      # bez generowania configu na końcu
    python run_all.py --no-probe       # bez testów wizji/narzędzi (tylko katalog + nazwa)
    python run_all.py --report         # tylko raport możliwości z istniejących list

Klucze API są czytane z pliku .env (wzór: .env.example). Dostawca bez klucza jest pomijany.
"""
import base64
import json
import os
import re
import struct
import subprocess
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
BASE = SCRIPT_DIR                 # korzen repo: .env, lists/, raporty/, config
REPORTS_DIR = BASE / "raporty"
REPORTS_DIR.mkdir(exist_ok=True)
LISTS_DIR = BASE / "lists"
load_dotenv(BASE / ".env")


# ----------------------------------------------------------------------------
# Możliwości modeli: wizja (obrazy) i narzędzia (function calling / MCP)
#   źródło: "catalog"  - z metadanych katalogu dostawcy,
#           "probe"    - sprawdzone prawdziwym zapytaniem (obraz + definicja narzędzia),
#           "heuristic"- zgadnięte po nazwie (wizja=true tylko gdy nazwa na to wskazuje; nic poza tym).
#   wartość: true / false / null (null = nie wiadomo)
# Test (probe) wykonuje się tylko dla modeli, które odpowiedziały OK, i tylko dla brakujących danych;
# wyniki z poprzednich skanów są zachowywane. Wyłączenie testów: --no-probe
# ----------------------------------------------------------------------------
PROBE = "--no-probe" not in sys.argv


def _make_png(w=64, h=64, rgb=(255, 0, 0)):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


RED_PNG = "data:image/png;base64," + base64.b64encode(_make_png()).decode()
VISION_HINT = re.compile(
    r"vision|[-_/]vl\b|[-_]vl[-_]|-vlm|llava|pixtral|gemma-?3|gpt-4o|gpt-4\.1|gpt-5|\bo[34]\b|gemini|llama-4|"
    r"scout|maverick|qwen\S*vl|molmo|multimodal|mistral-(large|medium|small)-(3|4)|ministral", re.I)
TOOL_DEF = [{"type": "function", "function": {
    "name": "get_weather", "description": "Get the current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]


def heuristic_caps(model_id):
    return {"vision": True if VISION_HINT.search(model_id) else None, "tools": None, "source": "heuristic"}


def _rejected(r, words):
    return r.status_code in (400, 422) and re.search(words, r.text or "", re.I) is not None


def _message(r):
    try:
        return (r.json().get("choices") or [{}])[0].get("message") or {}
    except Exception:
        return {}


def probe_caps(chat_url, headers, model_id, need_vision, need_tools, token_key="max_tokens", timeout=60):
    """Zwraca (vision, tools); None = test niejednoznaczny (429, timeout, model nie wywołał narzędzia...)."""
    vision = tools = None
    if need_tools:
        try:
            r = requests.post(chat_url, headers=headers, timeout=timeout, json={
                "model": model_id, token_key: 120, "tools": TOOL_DEF,
                "messages": [{"role": "user", "content": "What is the weather in Warsaw? Use the get_weather function."}]})
            if r.status_code == 200:
                tools = True if _message(r).get("tool_calls") else None   # brak wywołania != brak wsparcia
            elif _rejected(r, r"tool|function"):
                tools = False
        except Exception:
            pass
    if need_vision:
        try:
            r = requests.post(chat_url, headers=headers, timeout=timeout, json={
                "model": model_id, token_key: 300, "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "What color is this image? Answer with one word."},
                    {"type": "image_url", "image_url": {"url": RED_PNG}}]}]})
            if r.status_code == 200:
                text = str(_message(r).get("content") or "").lower()
                if re.search(r"red|czerw|rouge|\brot\b|rojo", text):
                    vision = True
                elif text.strip():
                    vision = False
            elif _rejected(r, r"image|vision|multimodal|modal|content"):
                vision = False
        except Exception:
            pass
    return vision, tools


def make_prober(chat_url, headers, token_key="max_tokens", sleep_s=1.0):
    def prober(model_id, need_vision, need_tools):
        res = probe_caps(chat_url, headers, model_id, need_vision, need_tools, token_key)
        time.sleep(sleep_s)
        return res
    return prober


def load_prev_caps(provider):
    try:
        d = json.loads((LISTS_DIR / f"{provider}.json").read_text(encoding="utf-8-sig"))
        return d.get("capabilities", {}) or {}
    except Exception:
        return {}


def build_caps(provider, listed, ok, meta, prober):
    prev = load_prev_caps(provider)
    caps = {}
    for mid in sorted(set(listed)):
        c = heuristic_caps(mid)
        m = (meta or {}).get(mid)
        if m:
            for k in ("vision", "tools"):
                if m.get(k) is not None:
                    c[k] = m[k]
                    c["source"] = "catalog"
        p = prev.get(mid)
        if p and p.get("source") == "probe":
            for k in ("vision", "tools"):
                if p.get(k) is not None:
                    c[k] = p[k]
                    c["source"] = "probe"
        caps[mid] = c
    if prober and PROBE:
        todo = [m for m in sorted(set(ok)) if m in caps and (
            caps[m]["vision"] is None or caps[m]["tools"] is None or caps[m]["source"] == "heuristic")]
        if todo:
            print(f"  Sprawdzam możliwości (wizja/narzędzia) dla {len(todo)} modeli...")
        for i, mid in enumerate(todo, 1):
            c = caps[mid]
            need_v = c["vision"] is None or c["source"] == "heuristic"
            need_t = c["tools"] is None
            v, t = prober(mid, need_v, need_t)
            if v is not None:
                c["vision"] = v
            if t is not None:
                c["tools"] = t
            if v is not None or t is not None:
                c["source"] = "probe"
            print(f"  [caps {i}/{len(todo)}] {mid}: wizja={_fmt(c['vision'])} narzędzia={_fmt(c['tools'])}")
    return caps


def _fmt(v):
    return "TAK" if v is True else ("nie" if v is False else "?")


def write_capability_report():
    """raporty/mozliwosci_modeli.txt - które dostępne modele mają wizję i narzędzia (MCP)."""
    lines = [f"MOŻLIWOŚCI MODELI  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
             "wizja = rozumie obrazy; narzędzia = function calling (potrzebne agentowi MCP); ? = nie wiadomo",
             "źródło: catalog = z katalogu dostawcy, probe = sprawdzone zapytaniem, heuristic = zgadnięte po nazwie", ""]
    best = []
    for name, _ in SCANNERS:
        path = LISTS_DIR / f"{name}.json"
        if not path.exists():
            continue
        d = json.loads(path.read_text(encoding="utf-8-sig"))
        caps = d.get("capabilities", {})
        avail = [(m, "ok") for m in d.get("ok", [])] + [(m, "429") for m in d.get("rate_limited", [])]
        lines += [f"=== {name} ({len(avail)} dostępnych) ===", f"{'model':<52}{'wizja':<7}{'narz.':<7}{'źródło':<11}status"]
        for mid, st in sorted(avail):
            c = caps.get(mid, {})
            lines.append(f"{mid[:51]:<52}{_fmt(c.get('vision')):<7}{_fmt(c.get('tools')):<7}{c.get('source', '-'):<11}{st}")
            if c.get("vision") is True and c.get("tools") is True:
                best.append(f"{name}/{mid}")
        lines.append("")
    lines += ["=== WIZJA + NARZĘDZIA (nadają się do agenta MCP z podglądem obrazu) ==="] + (best or ["(brak potwierdzonych)"])
    text = "\n".join(lines) + "\n"
    (REPORTS_DIR / "mozliwosci_modeli.txt").write_text(text, encoding="utf-8")
    print(f"Raport możliwości: {REPORTS_DIR / 'mozliwosci_modeli.txt'}  (wizja+narzędzia: {len(best)})")


# ----------------------------------------------------------------------------
# Wspólne narzędzia
# ----------------------------------------------------------------------------
def save_list(provider, listed, ok, rate_limited, failed, meta=None, prober=None):
    LISTS_DIR.mkdir(exist_ok=True)
    data = {
        "provider": provider,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "list_ok": True,
        "listed": sorted(set(listed)),
        "ok": sorted(set(ok)),
        "rate_limited": sorted(set(rate_limited)),
        "failed": failed,
        "capabilities": build_caps(provider, listed, ok, meta, prober),
    }
    path = LISTS_DIR / f"{provider}.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    print(
        f"  -> {path.name}: listed={len(data['listed'])} ok={len(data['ok'])} "
        f"rate_limit={len(data['rate_limited'])} failed={len(failed)}"
    )


def get_key(env_name):
    key = os.getenv(env_name)
    if not key:
        print(f"  [!] Brak {env_name} w .env - pomijam tego dostawcę (lista bez zmian).")
    return key


def probe(post_fn, model_id, idx, total, ok, rate_limited, failed):
    """Wysyła testowe zapytanie i klasyfikuje wynik. post_fn() zwraca obiekt Response."""
    try:
        r = post_fn()
        code = r.status_code
    except Exception as e:  # nie wypisujemy treści wyjątku - może zawierać URL z kluczem
        print(f"[{idx}/{total}] [!] {model_id} -> {type(e).__name__}")
        failed.append({"id": model_id, "status": "exception"})
        return
    if code == 200:
        print(f"[{idx}/{total}] [OK]  {model_id}")
        ok.append(model_id)
    elif code == 429:
        print(f"[{idx}/{total}] [429] {model_id}")
        rate_limited.append(model_id)
    else:
        print(f"[{idx}/{total}] [X]   {model_id} -> {code}")
        failed.append({"id": model_id, "status": code})


def scan_openai_style(provider, key_env, list_url, chat_url, id_filter, payload_fn,
                      sleep_s, timeout=20, extra_headers=None, meta_fn=None):
    key = get_key(key_env)
    if not key:
        return
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})

    resp = requests.get(list_url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"  [X] Nie udało się pobrać katalogu ({resp.status_code}) - lista bez zmian.")
        return
    candidates = [m for m in resp.json().get("data", []) if id_filter(m)]
    ids = [m["id"] for m in candidates]
    meta = {m["id"]: meta_fn(m) for m in candidates} if meta_fn else None
    print(f"  Katalog: {len(ids)} modeli czatowych. Testuję...")

    ok, rate_limited, failed = [], [], []
    for idx, mid in enumerate(ids, 1):
        probe(
            lambda mid=mid: requests.post(
                chat_url, headers=headers, json=payload_fn(mid), timeout=timeout
            ),
            mid, idx, len(ids), ok, rate_limited, failed,
        )
        if idx < len(ids):
            time.sleep(sleep_s)
    tk = "max_completion_tokens" if "max_completion_tokens" in payload_fn("x") else "max_tokens"
    save_list(provider, ids, ok, rate_limited, failed, meta,
              make_prober(chat_url, headers, tk, sleep_s=min(sleep_s, 3)))


# ----------------------------------------------------------------------------
# Skanery dostawców
# ----------------------------------------------------------------------------
def chat_payload(model_id):
    return {"model": model_id, "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5}


def scan_groq():
    skip = ("whisper", "guard", "safeguard", "tts", "orpheus")
    scan_openai_style(
        "groq", "GROQ_API_KEY",
        "https://api.groq.com/openai/v1/models",
        "https://api.groq.com/openai/v1/chat/completions",
        lambda m: not any(s in m["id"].lower() for s in skip),
        chat_payload, sleep_s=2,
    )


def scan_mistral():
    excluded = ("embed", "ocr", "voxtral", "moderation")
    scan_openai_style(
        "mistral", "MISTRAL_API_KEY",
        "https://api.mistral.ai/v1/models",
        "https://api.mistral.ai/v1/chat/completions",
        lambda m: m.get("capabilities", {}).get("completion_chat", True)
        and not any(w in m["id"] for w in excluded),
        chat_payload, sleep_s=3,
        meta_fn=lambda m: {"vision": (m.get("capabilities") or {}).get("vision"),
                           "tools": (m.get("capabilities") or {}).get("function_calling")},
    )


def scan_openai():
    excluded = ("embedding", "tts", "whisper", "dall-e", "moderation", "realtime",
                "audio", "davinci", "curie", "babbage", "ada")

    def id_filter(m):
        i = m["id"].lower()
        if any(w in i for w in excluded):
            return False
        return "gpt" in i or "o1" in i or "o3" in i

    def payload(model_id):
        return {"model": model_id, "messages": [{"role": "user", "content": "Hi"}],
                "max_completion_tokens": 16}

    scan_openai_style(
        "openai", "OPENAI_API_KEY",
        "https://api.openai.com/v1/models",
        "https://api.openai.com/v1/chat/completions",
        id_filter, payload, sleep_s=0.5, timeout=30,
    )


def scan_openrouter():
    scan_openai_style(
        "openrouter", "OPENROUTER_API_KEY",
        "https://openrouter.ai/api/v1/models",
        "https://openrouter.ai/api/v1/chat/completions",
        lambda m: ":free" in m.get("id", ""),
        chat_payload, sleep_s=6, timeout=30,
        extra_headers={"HTTP-Referer": "https://github.com/api-boty", "X-Title": "Brama Bot"},
        meta_fn=lambda m: {
            "vision": "image" in [str(x).lower() for x in (m.get("architecture") or {}).get("input_modalities", [])] or None,
            "tools": "tools" in [str(x).lower() for x in m.get("supported_parameters", [])]},
    )


def scan_ollama():
    key = get_key("OLLAMA_API_KEY")
    if not key:
        return
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    resp = requests.get("https://ollama.com/api/tags", headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"  [X] Nie udało się pobrać katalogu ({resp.status_code}) - lista bez zmian.")
        return
    ids = [m["name"] for m in resp.json().get("models", [])]
    print(f"  Katalog: {len(ids)} modeli. Testuję...")

    ok, rate_limited, failed = [], [], []
    for idx, mid in enumerate(ids, 1):
        payload = {"model": mid, "messages": [{"role": "user", "content": "Hi"}],
                   "stream": False, "options": {"num_predict": 5}}
        probe(
            lambda payload=payload: requests.post(
                "https://ollama.com/api/chat", headers=headers, json=payload, timeout=30
            ),
            mid, idx, len(ids), ok, rate_limited, failed,
        )
        time.sleep(1)
    save_list("ollama", ids, ok, rate_limited, failed, None,
              make_prober("https://ollama.com/v1/chat/completions", headers, sleep_s=1))


def scan_gemini():
    key = get_key("GEMINI_API_KEY")
    if not key:
        return
    base = "https://generativelanguage.googleapis.com/v1beta"
    resp = requests.get(f"{base}/models", params={"key": key, "pageSize": 1000}, timeout=30)
    if resp.status_code != 200:
        print(f"  [X] Nie udało się pobrać katalogu ({resp.status_code}) - lista bez zmian.")
        return
    ids = []
    for m in resp.json().get("models", []):
        name = m.get("name", "")
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if "embedding" in name.lower() or "tts" in name.lower():
            continue
        ids.append(name.replace("models/", ""))
    print(f"  Katalog: {len(ids)} modeli. Testuję...")

    ok, rate_limited, failed = [], [], []
    payload = {"contents": [{"parts": [{"text": "OK"}]}]}
    for idx, mid in enumerate(ids, 1):
        probe(
            lambda mid=mid: requests.post(
                f"{base}/models/{mid}:generateContent", params={"key": key},
                json=payload, timeout=20,
            ),
            mid, idx, len(ids), ok, rate_limited, failed,
        )
        time.sleep(1)
    save_list("gemini", ids, ok, rate_limited, failed, None,
              make_prober(f"{base}/openai/chat/completions", {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          sleep_s=1))


def scan_nvidia():
    # katalog NVIDIA NIM zawiera też modele nie-czatowe (embeddingi, reranki, guardy...) - odfiltrowujemy
    skip = ("embed", "rerank", "clip", "parse", "guard", "safety", "reward", "retriever",
            "ocr", "tts", "asr", "riva", "nemoretriever", "segment", "diffusion", "bge")
    scan_openai_style(
        "nvidia", "NVIDIA_API_KEY",
        "https://integrate.api.nvidia.com/v1/models",
        "https://integrate.api.nvidia.com/v1/chat/completions",
        lambda m: not any(s in m["id"].lower() for s in skip),
        chat_payload, sleep_s=2, timeout=40,
    )


def scan_cloudflare():
    account = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    token = os.getenv("CLOUDFLARE_API_TOKEN")
    if not account or not token:
        print("  [!] Brak CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN w .env - pomijam "
              "tego dostawcę (lista bez zmian).")
        return
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai"

    # katalog jest stronicowany
    ids, page, cf_meta = [], 1, {}
    while True:
        resp = requests.get(f"{base}/models/search", headers=headers,
                            params={"per_page": 100, "page": page}, timeout=30)
        if resp.status_code != 200:
            print(f"  [X] Nie udało się pobrać katalogu ({resp.status_code}) - lista bez zmian.")
            return
        body = resp.json()
        result = body.get("result", [])
        for m in result:
            task_list = [m["task"]] if isinstance(m.get("task"), dict) else m.get("tasks", [])
            tasks = [t.get("name", "").lower() for t in task_list]
            if m.get("name") and any("text generation" in t for t in tasks):
                ids.append(m["name"])
                props = {p.get("property_id"): p.get("value") for p in (m.get("properties") or []) if isinstance(p, dict)}
                cf_meta[m["name"]] = {"vision": True if str(props.get("vision", "")).lower() == "true" else None,
                                       "tools": True if str(props.get("function_calling", "")).lower() == "true" else None}
        info = body.get("result_info") or {}
        total_pages = info.get("total_pages")
        if not result or (total_pages and page >= total_pages) or (not total_pages and len(result) < 100):
            break
        page += 1
    ids = sorted(set(ids))
    print(f"  Katalog: {len(ids)} modeli czatowych. Testuję...")

    ok, rate_limited, failed = [], [], []
    for idx, mid in enumerate(ids, 1):
        probe(
            lambda mid=mid: requests.post(
                f"{base}/v1/chat/completions", headers=headers,
                json=chat_payload(mid), timeout=30,
            ),
            mid, idx, len(ids), ok, rate_limited, failed,
        )
        time.sleep(1)
    save_list("cloudflare", ids, ok, rate_limited, failed, cf_meta,
              make_prober(f"{base}/v1/chat/completions", headers, sleep_s=1))


SCANNERS = [
    ("gemini", scan_gemini),
    ("mistral", scan_mistral),
    ("ollama", scan_ollama),
    ("openai", scan_openai),
    ("openrouter", scan_openrouter),
    ("groq", scan_groq),
    ("nvidia", scan_nvidia),
    ("cloudflare", scan_cloudflare),
]


KEY_VARS = ("GEMINI_API_KEY", "MISTRAL_API_KEY", "OLLAMA_API_KEY", "OPENAI_API_KEY",
            "OPENROUTER_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY", "CLOUDFLARE_API_TOKEN")


def redact(text):
    """Usuwa z tekstu wartości kluczy z .env (żeby nie wyciekły do konsoli/raportu)."""
    for name, value in os.environ.items():
        if value and len(value) > 8 and any(w in name for w in ("KEY", "TOKEN", "SECRET")):
            text = text.replace(value, "***")
    return text


def write_report(statuses):
    """Zapisuje czytelny raport z całego skanu: raport_skanu.txt (+ wypisuje go w konsoli)."""
    lines = [f"RAPORT SKANU  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "=" * 65]
    for name, _ in SCANNERS:
        if name not in statuses:
            continue
        path = LISTS_DIR / f"{name}.json"
        status = statuses[name]
        if status != "ok":
            lines.append(f"{name:<11} {status}")
            continue
        if not path.exists():
            lines.append(f"{name:<11} brak pliku listy (skan nic nie zapisał)")
            continue
        d = json.loads(path.read_text(encoding="utf-8"))
        lines.append(
            f"{name:<11} katalog={len(d.get('listed', []))}  OK={len(d.get('ok', []))}  "
            f"429={len(d.get('rate_limited', []))}  błędy={len(d.get('failed', []))}"
        )
    lines.append("=" * 65)
    text = "\n".join(lines)
    (REPORTS_DIR / "raport_skanu.txt").write_text(text + "\n", encoding="utf-8")
    print("\n" + text)
    print(f"Raport zapisano: {REPORTS_DIR / 'raport_skanu.txt'}")


def main():
    args = sys.argv[1:]
    run_config = "--no-config" not in args
    wanted = [a for a in args if not a.startswith("--")]
    statuses = {}
    if "--report" in args:          # tylko raport możliwości z istniejących list
        write_capability_report()
        return

    if not any(os.getenv(v) for v in KEY_VARS):
        print("Nie znaleziono żadnego klucza API. Skopiuj .env.example do .env i uzupełnij przynajmniej jeden klucz.")
        sys.exit(1)

    for name, fn in SCANNERS:
        if wanted and name not in wanted:
            continue
        print("=" * 65)
        print(f" {name.upper()}")
        print("=" * 65)
        try:
            before = (LISTS_DIR / f"{name}.json").stat().st_mtime if (LISTS_DIR / f"{name}.json").exists() else None
            fn()
            after = (LISTS_DIR / f"{name}.json").stat().st_mtime if (LISTS_DIR / f"{name}.json").exists() else None
            statuses[name] = "ok" if after != before else "POMINIĘTY (brak klucza w .env albo błąd katalogu) - lista bez zmian"
        except Exception as e:  # błąd jednego dostawcy nie zatrzymuje reszty
            msg = redact(f"{type(e).__name__}: {e}")[:300]
            print(f"  [!] {name}: {msg} - lista bez zmian.")
            statuses[name] = f"BŁĄD - {msg}"

    write_report(statuses)
    write_capability_report()

    if run_config:
        print("=" * 65)
        print(" GENEROWANIE CONFIGU")
        print("=" * 65)
        subprocess.run([sys.executable, str(SCRIPT_DIR / "generate_litellm_config.py")], cwd=BASE)


if __name__ == "__main__":
    main()
