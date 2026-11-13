# Skaner darmowych modeli AI (i ich możliwości)

Jeden skrypt łączy się z API wielu dostawców, pobiera katalog modeli, wysyła do każdego krótkie zapytanie testowe
i sprawdza, **które modele faktycznie odpowiadają** oraz **co potrafią**:

- **wizja** - czy model rozumie obrazy,
- **narzędzia** - czy obsługuje function calling (potrzebne m.in. agentom MCP).

Wyniki trafiają do czytelnych raportów i list JSON. Opcjonalnie skrypt buduje też gotowy config dla
[LiteLLM](https://github.com/BerriAI/litellm).

Obsługiwani dostawcy: Google Gemini, Mistral, Ollama Cloud, Groq, OpenRouter (tylko modele `:free`),
NVIDIA NIM, Cloudflare Workers AI i OpenAI.

## Szybki start

1. Zainstaluj Pythona 3.9+.
2. Skopiuj `.env.example` jako `.env` i wpisz klucze API (wystarczy jeden, brakujący dostawca jest pomijany).
3. Uruchom:

```
python run_all.py
```

Na Windowsie możesz zamiast tego kliknąć dwukrotnie **`start.bat`**. Sam utworzy środowisko `.venv`, zainstaluje zależności,
przy pierwszym uruchomieniu utworzy `.env` i otworzy go w Notatniku, a potem odpali skan.

Bez `start.bat`:

```
pip install -r requirements.txt
python run_all.py
```

## Skąd wziąć klucze API

| Dostawca | Zmienna w `.env` | Utwórz klucz | Limity / zużycie |
|---|---|---|---|
| Google Gemini | `GEMINI_API_KEY` | https://aistudio.google.com/apikey | https://aistudio.google.com/rate-limit |
| Mistral | `MISTRAL_API_KEY` | https://console.mistral.ai/api-keys | https://admin.mistral.ai/subscription |
| Ollama Cloud | `OLLAMA_API_KEY` | https://ollama.com/settings/keys | https://ollama.com/settings |
| Groq | `GROQ_API_KEY` | https://console.groq.com/keys | https://console.groq.com/dashboard/metrics |
| OpenRouter | `OPENROUTER_API_KEY` | https://openrouter.ai/keys | https://openrouter.ai/activity |
| NVIDIA NIM | `NVIDIA_API_KEY` | https://build.nvidia.com/settings/api-keys | https://build.nvidia.com |
| Cloudflare Workers AI | `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` | https://dash.cloudflare.com/profile/api-tokens | panel Cloudflare -> AI -> Workers AI |
| OpenAI | `OPENAI_API_KEY` | https://platform.openai.com/api-keys | https://platform.openai.com/usage |

Uwagi:

- **Cloudflare**: token musi mieć uprawnienie do Workers AI (szablon „Workers AI" przy tworzeniu tokena). `ACCOUNT_ID` jest
  widoczny w panelu Cloudflare i w adresie URL po zalogowaniu.
- **OpenAI** to płatne API. Skaner wysyła minimalne zapytania (kilka tokenów), ale mimo to zużywa niewielki budżet.
- Limity darmowych planów zmieniają się - aktualne wartości sprawdzisz w panelach z ostatniej kolumny.

## Użycie

```
python run_all.py                  # wszyscy dostawcy + generowanie configu LiteLLM
python run_all.py groq mistral     # tylko wybrani (gemini mistral ollama openai openrouter groq nvidia cloudflare)
python run_all.py --no-config      # bez generowania configu LiteLLM na końcu
python run_all.py --no-probe       # bez testów wizji/narzędzi (tylko katalog + test odpowiedzi)
python run_all.py --report         # tylko raport możliwości z już zapisanych list
```

Pełny skan trwa dłuższą chwilę: między zapytaniami są przerwy, żeby nie wpadać w limity (np. OpenRouter ma 6 s na model).

## Co powstaje po skanie

| Plik | Zawartość |
|---|---|
| `lists/<dostawca>.json` | wszystkie modele z katalogu, te które odpowiedziały (`ok`), z limitem 429, z błędem, plus wizja/narzędzia |
| `raporty/raport_skanu.txt` | podsumowanie skanu per dostawca |
| `raporty/mozliwosci_modeli.txt` | tabela: model / wizja / narzędzia / źródło danych; na końcu lista modeli z wizją **i** narzędziami |
| `models_state.json` | „pamięć" modeli (co kiedykolwiek działało) - używana przy generowaniu configu |
| `gateway-config.yaml` | opcjonalny config LiteLLM zbudowany z powyższego |

Wszystkie te pliki są generowane lokalnie i są w `.gitignore`.

### Jak ustalane są możliwości modeli

Kolumna „źródło" w raporcie mówi, skąd pochodzi informacja:

- `catalog` - z metadanych katalogu dostawcy (np. Mistral, OpenRouter, Cloudflare),
- `probe` - sprawdzone prawdziwym zapytaniem: obraz testowy (model ma nazwać kolor) oraz definicja narzędzia,
- `heuristic` - zgadnięte po nazwie modelu (tylko wizja); `?` oznacza „nie wiadomo".

Test wykonuje się tylko dla modeli, które odpowiedziały poprawnie, i tylko dla brakujących danych. Wyniki z poprzednich
skanów są zachowywane.

## Config LiteLLM (opcjonalnie)

Na końcu `run_all.py` uruchamia `generate_litellm_config.py`, który zapisuje `gateway-config.yaml`. Zasady:

- model trafia do configu, gdy **choć raz** odpowiedział poprawnie,
- późniejszy rate limit (429) lub błąd go **nie usuwa**,
- model znika tylko wtedy, gdy dostawca przestał go zwracać w katalogu,
- klucze są w configu wyłącznie jako odwołania `os.environ/NAZWA`, nigdy jako wartości.

Dla Cloudflare ustaw w `.env` także `CLOUDFLARE_AI_BASE`, a dla własnego serwera Ollama `OLLAMA_API_BASE`
(patrz `.env.example`). Jeśli config LiteLLM Cię nie interesuje, używaj `--no-config`.

## Struktura repozytorium

```
run_all.py                    # skaner wszystkich dostawców (punkt startowy)
generate_litellm_config.py    # budowa configu LiteLLM (wywoływany przez run_all.py)
start.bat                     # uruchomienie jednym kliknięciem na Windowsie
requirements.txt
.env.example
```
