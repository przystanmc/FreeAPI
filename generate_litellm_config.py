"""
generate_litellm_config.py - buduje gateway-config.yaml dla LiteLLM z "lepkiego" stanu modeli.

Zasady:
  * model trafia do configu, gdy choć raz odpowiedział OK (status 200),
  * późniejszy rate limit (429) albo błąd NIE usuwa go z configu,
  * model znika dopiero, gdy dostawca w ogóle przestał go zwracać w swoim katalogu
    (czyli został wycofany); wtedy zapisujemy to w removed_models.log.

Stan jest trzymany w models_state.json.

Użycie:
    python generate_litellm_config.py                # zapis do gateway-config.yaml (+ .bak)
    python generate_litellm_config.py --out test.yaml
"""
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

BASE = Path(__file__).resolve().parent   # korzen repo: lists/, models_state.json, gateway-config.yaml
REPORTS = BASE / "raporty"
REPORTS.mkdir(exist_ok=True)
LISTS_DIR = BASE / "lists"
STATE_PATH = BASE / "models_state.json"
REMOVED_LOG = REPORTS / "removed_models.log"
DEFAULT_OUT = BASE / "gateway-config.yaml"

# kolejność = kolejność sekcji w configu
PROVIDERS = {
    "gemini": {"prefix": "gemini", "key": "GEMINI_API_KEY"},
    "mistral": {"prefix": "mistral", "key": "MISTRAL_API_KEY"},
    # api_base opcjonalny: ustaw OLLAMA_API_BASE w .env, jeśli LiteLLM ma łączyć się z własnym serwerem Ollama
    "ollama": {"prefix": "ollama", "key": "OLLAMA_API_KEY",
               "api_base": os.getenv("OLLAMA_API_BASE")},
    "openai": {"prefix": "openai", "key": "OPENAI_API_KEY"},
    "openrouter": {"prefix": "openrouter", "key": "OPENROUTER_API_KEY"},
    "groq": {"prefix": "groq", "key": "GROQ_API_KEY"},
    # NVIDIA NIM: prefiks nvidia_nim, nazwy w gatewayu dostają "nv-", żeby nie kolidować z innymi
    "nvidia": {"prefix": "nvidia_nim", "key": "NVIDIA_API_KEY", "name_prefix": "nv-"},
    # Cloudflare Workers AI przez endpoint zgodny z OpenAI; api_base bierze z .env (CLOUDFLARE_AI_BASE)
    "cloudflare": {"prefix": "openai", "key": "CLOUDFLARE_API_TOKEN",
                   "api_base": "os.environ/CLOUDFLARE_AI_BASE", "name_prefix": "cf-"},
}

BASE_CONFIG = {
    "general_settings": {
        "background_health_checks": False,
        "health_check_interval": 300,
        "debug": False,
        "num_retries": 2,
    },
    "litellm_settings": {
        "num_retries": 2,
        # przykład fallbacków (odkomentuj i dostosuj do swoich modeli):
        # "fallbacks": [{"model-a": ["model-b", "model-c"]}],
    },
}


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


# ----------------------------------------------------------------------------
# Aktualizacja stanu na podstawie list z run_all.py
# ----------------------------------------------------------------------------
def update_state(state, provider, data):
    st = state.setdefault(provider, {})
    t = now()
    ok = set(data.get("ok", []))
    rate_limited = set(data.get("rate_limited", []))
    failed = {f["id"]: f.get("status") for f in data.get("failed", [])}

    for mid in ok:
        entry = st.setdefault(mid, {"first_ok": t})
        entry["last_ok"] = t
        entry["last_status"] = "ok"
        entry.pop("seeded", None)
    for mid in rate_limited:
        if mid in st:
            st[mid]["last_status"] = "rate_limit"
    for mid, status in failed.items():
        if mid in st:
            st[mid]["last_status"] = f"failed:{status}"

    caps = data.get("capabilities", {}) or {}
    for mid, entry in st.items():
        c = caps.get(mid)
        if c and c.get("source") in ("probe", "catalog"):
            for k in ("vision", "tools"):
                if c.get(k) is not None:
                    entry[k] = c[k]
    removed = []
    listed = set(data.get("listed", []))
    if data.get("list_ok") and listed:
        removed = sorted(m for m in st if m not in listed)
        for mid in removed:
            del st[mid]
    elif st:
        print(f"  [!] {provider}: pusty/niepewny katalog - nic nie usuwam.")
    return removed


# ----------------------------------------------------------------------------
# Budowa configu
# ----------------------------------------------------------------------------
def create_entry(model_name, cfg, info=None):
    params = {"model": f"{cfg['prefix']}/{model_name}",
              "api_key": f"os.environ/{cfg['key']}"}
    if cfg.get("api_base"):
        params["api_base"] = cfg["api_base"]
    entry = {
        "model_name": cfg.get("name_prefix", "")
        + model_name.split("/")[-1].replace(":", "-"),
        "litellm_params": params,
    }
    mi = {}
    if (info or {}).get("vision") is not None:
        mi["supports_vision"] = bool(info["vision"])
    if (info or {}).get("tools") is not None:
        mi["supports_function_calling"] = bool(info["tools"])
    if mi:
        entry["model_info"] = mi
    return entry


def main():
    out_path = DEFAULT_OUT
    if "--out" in sys.argv:
        out_path = Path(sys.argv[sys.argv.index("--out") + 1])
        if not out_path.is_absolute():
            out_path = BASE / out_path

    state = read_json(STATE_PATH) if STATE_PATH.exists() else {}
    for p in PROVIDERS:
        state.setdefault(p, {})

    removed_all = []
    for provider in PROVIDERS:
        list_path = LISTS_DIR / f"{provider}.json"
        if not list_path.exists():
            print(f"- {provider}: brak listy w lists/ - zostaje poprzedni stan "
                  f"({len(state[provider])} modeli)")
            continue
        data = read_json(list_path)
        removed = update_state(state, provider, data)
        removed_all += [(provider, m) for m in removed]
        print(f"- {provider}: lista z {data.get('timestamp')} | "
              f"ok={len(data.get('ok', []))} 429={len(data.get('rate_limited', []))} "
              f"błędy={len(data.get('failed', []))} | w configu={len(state[provider])}"
              + (f" | usunięte (wycofane): {', '.join(removed)}" if removed else ""))

    if removed_all:
        with open(REMOVED_LOG, "a", encoding="utf-8") as f:
            for provider, mid in removed_all:
                f.write(f"{now()}\t{provider}\t{mid}\n")

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)

    all_models = []
    for provider, cfg in PROVIDERS.items():
        ids = sorted(state[provider])
        if ids:
            all_models.append({"_comment": f"##### {provider} #####"})
            all_models.extend(create_entry(mid, cfg, state[provider][mid]) for mid in ids)

    config = dict(BASE_CONFIG)
    config["model_list"] = all_models
    text = yaml.dump(config, sort_keys=False, allow_unicode=True)
    text = text.replace("- _comment: '##### ", "\n# ===== ").replace(" #####'", " =====")

    if out_path.exists():
        shutil.copyfile(out_path, out_path.with_name(out_path.name + ".bak"))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)

    total = sum(len(state[p]) for p in PROVIDERS)
    print(f"\nGotowe: {total} modeli zapisano do {out_path.name}")


if __name__ == "__main__":
    main()
