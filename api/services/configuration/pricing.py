"""Provider model pricing catalog.

cost_per_min_usd: estimated cost for 1 min of voice AI usage
native_cost_display: human-readable provider pricing unit

LLM assumption: ~500 input + 200 output tokens/min of conversation
TTS assumption: ~500 characters/min of generated speech
STT: direct per-minute rate from provider
Realtime: per-minute rate
"""

# ─── Pricing catalog ─────────────────────────────────────────────────────────

_PRICING: dict[str, tuple[float, str]] = {
    # ── OpenAI LLM ──────────────────────────────────────────────────────────
    "gpt-4o":                       (0.0033,  "$2.50/M in · $10.00/M out tokens"),
    "gpt-4o-2024-11-20":            (0.0033,  "$2.50/M in · $10.00/M out tokens"),
    "gpt-4o-2024-08-06":            (0.0033,  "$2.50/M in · $10.00/M out tokens"),
    "gpt-4o-2024-05-13":            (0.0050,  "$5.00/M in · $15.00/M out tokens"),
    "gpt-4o-mini":                  (0.00020, "$0.15/M in · $0.60/M out tokens"),
    "gpt-4o-mini-2024-07-18":       (0.00020, "$0.15/M in · $0.60/M out tokens"),
    "gpt-4-turbo":                  (0.0110,  "$10.00/M in · $30.00/M out tokens"),
    "gpt-4-turbo-2024-04-09":       (0.0110,  "$10.00/M in · $30.00/M out tokens"),
    "gpt-4":                        (0.0270,  "$30.00/M in · $60.00/M out tokens"),
    "gpt-4-0613":                   (0.0270,  "$30.00/M in · $60.00/M out tokens"),
    "gpt-3.5-turbo":                (0.00055, "$0.50/M in · $1.50/M out tokens"),
    "gpt-3.5-turbo-0125":           (0.00055, "$0.50/M in · $1.50/M out tokens"),
    "o1":                           (0.0195,  "$15.00/M in · $60.00/M out tokens"),
    "o1-2024-12-17":                (0.0195,  "$15.00/M in · $60.00/M out tokens"),
    "o1-mini":                      (0.0039,  "$3.00/M in · $12.00/M out tokens"),
    "o1-mini-2024-09-12":           (0.0039,  "$3.00/M in · $12.00/M out tokens"),
    "o1-preview":                   (0.0225,  "$15.00/M in · $60.00/M out tokens"),
    "o3":                           (0.0330,  "$10.00/M in · $40.00/M out tokens"),
    "o3-mini":                      (0.0015,  "$1.10/M in · $4.40/M out tokens"),
    "o4-mini":                      (0.0015,  "$1.10/M in · $4.40/M out tokens"),
    # ── OpenAI TTS ──────────────────────────────────────────────────────────
    "tts-1":                        (0.0075,  "$15.00/M characters"),
    "tts-1-hd":                     (0.0150,  "$30.00/M characters"),
    "tts-1-1106":                   (0.0075,  "$15.00/M characters"),
    "tts-1-hd-1106":                (0.0150,  "$30.00/M characters"),
    # ── OpenAI STT ──────────────────────────────────────────────────────────
    "whisper-1":                    (0.0060,  "$0.006/min audio"),
    # ── OpenAI Realtime ─────────────────────────────────────────────────────
    "gpt-4o-realtime-preview":      (0.0600,  "$0.06/min audio in · $0.24/min out"),
    "gpt-4o-realtime-preview-2024-12-17": (0.0600, "$0.06/min audio in · $0.24/min out"),
    "gpt-4o-mini-realtime-preview": (0.0100,  "$0.01/min audio in · $0.02/min out"),
    # ── Anthropic ───────────────────────────────────────────────────────────
    "claude-haiku-4-5":                   (0.0012, "$0.80/M in · $4.00/M out tokens"),
    "claude-haiku-4-5-20251001":          (0.0012, "$0.80/M in · $4.00/M out tokens"),
    "claude-haiku-3-5":                   (0.0012, "$0.80/M in · $4.00/M out tokens"),
    "claude-haiku-3-5-20241022":          (0.0012, "$0.80/M in · $4.00/M out tokens"),
    "claude-sonnet-4-6":                  (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-sonnet-4-5":                  (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-sonnet-4-5-20250929":         (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-sonnet-3-7":                  (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-sonnet-3-7-20250219":         (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-sonnet-3-5":                  (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-sonnet-3-5-20241022":         (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-opus-4-1":                    (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-opus-4-1-20250805":           (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-opus-4-5":                    (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-opus-4-5-20251101":           (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-opus-4-6":                    (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-opus-4-7":                    (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-opus-4-8":                    (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-3-opus-20240229":             (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "claude-3-5-sonnet-20241022":         (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "claude-3-5-haiku-20241022":          (0.0012, "$0.80/M in · $4.00/M out tokens"),
    # ── Google Gemini ────────────────────────────────────────────────────────
    "gemini-2.5-pro":               (0.00219, "$1.25/M in · $10.00/M out tokens"),
    "gemini-2.5-pro-preview-05-06": (0.00219, "$1.25/M in · $10.00/M out tokens"),
    "gemini-2.5-flash":             (0.00023, "$0.15/M in · $0.60/M out tokens"),
    "gemini-2.5-flash-preview-04-17": (0.00023, "$0.15/M in · $0.60/M out tokens"),
    "gemini-2.0-flash":             (0.00010, "$0.075/M in · $0.30/M out tokens"),
    "gemini-2.0-flash-001":         (0.00010, "$0.075/M in · $0.30/M out tokens"),
    "gemini-2.0-flash-lite":        (0.000044, "$0.0375/M in · $0.15/M out tokens"),
    "gemini-2.0-flash-lite-001":    (0.000044, "$0.0375/M in · $0.15/M out tokens"),
    "gemini-2.0-flash-lite-preview-02-05": (0.000044, "$0.0375/M in · $0.15/M out tokens"),
    "gemini-1.5-pro":               (0.00163, "$1.25/M in · $5.00/M out tokens"),
    "gemini-1.5-pro-001":           (0.00163, "$1.25/M in · $5.00/M out tokens"),
    "gemini-1.5-pro-002":           (0.00163, "$1.25/M in · $5.00/M out tokens"),
    "gemini-1.5-flash":             (0.00010, "$0.075/M in · $0.30/M out tokens"),
    "gemini-1.5-flash-001":         (0.00010, "$0.075/M in · $0.30/M out tokens"),
    "gemini-1.5-flash-002":         (0.00010, "$0.075/M in · $0.30/M out tokens"),
    "gemini-1.5-flash-8b":          (0.000056, "$0.0375/M in · $0.15/M out tokens"),
    "gemini-1.5-flash-8b-001":      (0.000056, "$0.0375/M in · $0.15/M out tokens"),
    "deep-research-max-preview-04-2026": (0.0500, "$7.00/M in · $35.00/M out tokens"),
    "deep-research-preview-04-2026":     (0.0300, "$3.50/M in · $17.50/M out tokens"),
    "deep-research-pro-preview-12-2025": (0.0300, "$3.50/M in · $17.50/M out tokens"),
    "antigravity-preview-05-2026":       (0.0219, "$1.25/M in · $10.00/M out tokens"),
    # ── Google TTS ──────────────────────────────────────────────────────────
    "google-tts-standard":          (0.0020,  "$4.00/M characters"),
    "google-tts-wavenet":           (0.0080,  "$16.00/M characters"),
    "google-tts-neural2":           (0.0080,  "$16.00/M characters"),
    "google-tts-studio":            (0.0320,  "$64.00/M characters"),
    # ── Google STT ──────────────────────────────────────────────────────────
    "google-stt-standard":          (0.0040,  "$0.004/min audio"),
    "google-stt-enhanced":          (0.0090,  "$0.009/min audio"),
    "google-stt-chirp":             (0.0160,  "$0.016/min audio"),
    # ── Google Realtime ─────────────────────────────────────────────────────
    "gemini-2.0-flash-exp":         (0.0200,  "Experimental — est. $0.02/min"),
    "gemini-2.0-flash-live-001":    (0.0150,  "$0.015/min audio stream"),
    # ── Groq ────────────────────────────────────────────────────────────────
    "llama3-8b-8192":               (0.000041, "$0.05/M in · $0.08/M out tokens"),
    "llama3-70b-8192":              (0.000453, "$0.59/M in · $0.79/M out tokens"),
    "llama-3.1-8b-instant":         (0.000041, "$0.05/M in · $0.08/M out tokens"),
    "llama-3.1-70b-versatile":      (0.000453, "$0.59/M in · $0.79/M out tokens"),
    "llama-3.3-70b-versatile":      (0.000453, "$0.59/M in · $0.79/M out tokens"),
    "llama-3.3-70b-specdec":        (0.000453, "$0.59/M in · $0.79/M out tokens"),
    "llama-3.2-1b-preview":         (0.000008, "$0.04/M in · $0.04/M out tokens"),
    "llama-3.2-3b-preview":         (0.000030, "$0.06/M in · $0.06/M out tokens"),
    "llama-3.2-11b-vision-preview": (0.000059, "$0.18/M in · $0.18/M out tokens"),
    "llama-3.2-90b-vision-preview": (0.000476, "$0.90/M in · $0.90/M out tokens"),
    "mixtral-8x7b-32768":           (0.000168, "$0.24/M in · $0.24/M out tokens"),
    "gemma-7b-it":                  (0.000035, "$0.07/M in · $0.07/M out tokens"),
    "gemma2-9b-it":                 (0.000100, "$0.20/M in · $0.20/M out tokens"),
    "llama-guard-3-8b":             (0.000041, "$0.20/M in · $0.20/M out tokens"),
    "whisper-large-v3":             (0.0001,   "$0.111/hr audio"),
    "whisper-large-v3-turbo":       (0.0001,   "$0.04/hr audio"),
    "distil-whisper-large-v3-en":   (0.0001,   "$0.02/hr audio"),
    # ── xAI / Grok ──────────────────────────────────────────────────────────
    "grok-2":                       (0.0030,  "$2.00/M in · $10.00/M out tokens"),
    "grok-2-latest":                (0.0030,  "$2.00/M in · $10.00/M out tokens"),
    "grok-2-1212":                  (0.0030,  "$2.00/M in · $10.00/M out tokens"),
    "grok-3":                       (0.0045,  "$3.00/M in · $15.00/M out tokens"),
    "grok-3-latest":                (0.0045,  "$3.00/M in · $15.00/M out tokens"),
    "grok-3-fast":                  (0.0015,  "$5.00/M in · $25.00/M out tokens"),
    "grok-3-mini":                  (0.00040, "$0.30/M in · $0.50/M out tokens"),
    "grok-beta":                    (0.0075,  "$5.00/M in · $15.00/M out tokens"),
    "grok-vision-beta":             (0.0075,  "$5.00/M in · $15.00/M out tokens"),
    # ── xAI TTS ─────────────────────────────────────────────────────────────
    "eve":                          (0.0030,  "xAI TTS — est. $0.006/min"),
    "ara":                          (0.0030,  "xAI TTS — est. $0.006/min"),
    "rex":                          (0.0030,  "xAI TTS — est. $0.006/min"),
    "sal":                          (0.0030,  "xAI TTS — est. $0.006/min"),
    "leo":                          (0.0030,  "xAI TTS — est. $0.006/min"),
    # ── ElevenLabs TTS ──────────────────────────────────────────────────────
    "eleven_flash_v2_5":            (0.000040, "$0.08/M characters"),
    "eleven_flash_v2":              (0.000040, "$0.08/M characters"),
    "eleven_turbo_v2_5":            (0.000040, "$0.08/M characters"),
    "eleven_turbo_v2":              (0.000040, "$0.08/M characters"),
    "eleven_multilingual_v2":       (0.000090, "$0.18/M characters"),
    "eleven_multilingual_v1":       (0.000090, "$0.18/M characters"),
    "eleven_monolingual_v1":        (0.000090, "$0.18/M characters"),
    # ── Deepgram STT ────────────────────────────────────────────────────────
    # Nova-2 tier — $0.0043/min (general/media use cases)
    "nova-2":                       (0.0043,  "$0.0043/min audio"),
    "nova-2-general":               (0.0043,  "$0.0043/min audio"),
    "nova-2-conversationalai":      (0.0043,  "$0.0043/min audio"),
    "nova-2-voicemail":             (0.0043,  "$0.0043/min audio"),
    "nova-2-video":                 (0.0043,  "$0.0043/min audio"),
    # Nova-2 tier — $0.0059/min (specialized use cases)
    "nova-2-meeting":               (0.0059,  "$0.0059/min audio"),
    "nova-2-phonecall":             (0.0059,  "$0.0059/min audio"),
    "nova-2-finance":               (0.0059,  "$0.0059/min audio"),
    # Nova-2 medical — $0.0100/min
    "nova-2-medical":               (0.0100,  "$0.0100/min audio"),
    # Nova-3 tier — $0.0059/min
    "nova-3":                       (0.0059,  "$0.0059/min audio"),
    "nova-3-general":               (0.0059,  "$0.0059/min audio"),
    # Nova (legacy) — $0.0059/min
    "nova":                         (0.0059,  "$0.0059/min audio"),
    # Enhanced tier — $0.0145/min (prefix covers enhanced-*)
    "enhanced":                     (0.0145,  "$0.0145/min audio"),
    # Base tier — $0.0125/min (prefix covers base-*)
    "base":                         (0.0125,  "$0.0125/min audio"),
    # Whisper (Deepgram-hosted) — $0.0200/min
    "whisper":                      (0.0200,  "$0.0200/min audio"),
    # Short-form model names returned by Deepgram live API
    "conversationalai":             (0.0043,  "$0.0043/min audio"),
    "general":                      (0.0059,  "$0.0059/min audio"),
    "meeting":                      (0.0059,  "$0.0059/min audio"),
    "finance":                      (0.0059,  "$0.0059/min audio"),
    "phonecall":                    (0.0059,  "$0.0059/min audio"),
    "voicemail":                    (0.0043,  "$0.0043/min audio"),
    # ── Deepgram TTS (Aura) ──────────────────────────────────────────────────
    "aura-asteria-en":              (0.0075,  "$15.00/M characters"),
    "aura-luna-en":                 (0.0075,  "$15.00/M characters"),
    "aura-stella-en":               (0.0075,  "$15.00/M characters"),
    "aura-athena-en":               (0.0075,  "$15.00/M characters"),
    "aura-hera-en":                 (0.0075,  "$15.00/M characters"),
    "aura-orion-en":                (0.0075,  "$15.00/M characters"),
    "aura-arcas-en":                (0.0075,  "$15.00/M characters"),
    "aura-perseus-en":              (0.0075,  "$15.00/M characters"),
    "aura-angus-en":                (0.0075,  "$15.00/M characters"),
    "aura-orpheus-en":              (0.0075,  "$15.00/M characters"),
    "aura-helios-en":               (0.0075,  "$15.00/M characters"),
    "aura-zeus-en":                 (0.0075,  "$15.00/M characters"),
    "aura-2-thalia-en":             (0.0060,  "$12.00/M characters"),
    "aura-2-andromeda-en":          (0.0060,  "$12.00/M characters"),
    # ── Cartesia TTS ─────────────────────────────────────────────────────────
    "sonic":                        (0.000045, "$0.09/M characters"),
    "sonic-2":                      (0.000045, "$0.09/M characters"),
    "sonic-english":                (0.000045, "$0.09/M characters"),
    "sonic-multilingual":           (0.000070, "$0.14/M characters"),
    "sonic-preview":                (0.000045, "$0.09/M characters"),
    # ── AssemblyAI STT ───────────────────────────────────────────────────────
    "best":                         (0.0065,  "$0.0065/min audio"),
    "nano":                         (0.0020,  "$0.0020/min audio"),
    "slam-1":                       (0.0065,  "$0.0065/min audio"),
    # ── Gladia STT ───────────────────────────────────────────────────────────
    "gladia-v2":                    (0.0068,  "$0.0068/min audio"),
    "fast":                         (0.0036,  "$0.0036/min audio"),
    "accurate":                     (0.0068,  "$0.0068/min audio"),
    # ── Speechmatics STT ─────────────────────────────────────────────────────
    "speechmatics-enhanced":        (0.0120,  "$0.0120/min audio"),
    "speechmatics-standard":        (0.0060,  "$0.0060/min audio"),
    # ── Rime TTS ─────────────────────────────────────────────────────────────
    "arcas":                        (0.000050, "$0.10/M characters"),
    "mist":                         (0.000050, "$0.10/M characters"),
    # ── Azure OpenAI LLM ─────────────────────────────────────────────────────
    "gpt-4o-azure":                 (0.0033,  "$2.50/M in · $10.00/M out tokens"),
    "gpt-4o-mini-azure":            (0.00020, "$0.15/M in · $0.60/M out tokens"),
    # ── AWS Bedrock ──────────────────────────────────────────────────────────
    "anthropic.claude-3-haiku-20240307-v1:0":  (0.0012, "$0.80/M in · $4.00/M out tokens"),
    "anthropic.claude-3-sonnet-20240229-v1:0": (0.0045, "$3.00/M in · $15.00/M out tokens"),
    "anthropic.claude-3-opus-20240229-v1:0":   (0.0225, "$15.00/M in · $75.00/M out tokens"),
    "amazon.titan-text-lite-v1":               (0.00019, "$0.15/M in · $0.20/M out tokens"),
    "amazon.titan-text-express-v1":            (0.00063, "$0.20/M in · $0.65/M out tokens"),
    "meta.llama3-8b-instruct-v1:0":            (0.000041, "$0.30/M in · $0.60/M out tokens"),
    "meta.llama3-70b-instruct-v1:0":           (0.000453, "$2.65/M in · $3.50/M out tokens"),
    # ── MiniMax ──────────────────────────────────────────────────────────────
    "abab6.5-chat":                 (0.00080, "$0.80/M tokens"),
    "abab6.5s-chat":                (0.00080, "$0.80/M tokens"),
    "abab5.5-chat":                 (0.00015, "$0.15/M tokens"),
    "speech-01-turbo":              (0.000050, "$0.10/M characters"),
    "speech-01-hd":                 (0.000100, "$0.20/M characters"),
    # ── Sarvam STT ───────────────────────────────────────────────────────────
    "saarika:v2":                   (0.0040,  "$0.004/min audio"),
    "saaras:v2":                    (0.0040,  "$0.004/min audio"),
    # ── OpenRouter (pass-through — varies by model) ───────────────────────────
    "openai/gpt-4o":                (0.0033,  "via OpenRouter · $2.50/M in · $10.00/M out"),
    "openai/gpt-4o-mini":           (0.00020, "via OpenRouter · $0.15/M in · $0.60/M out"),
    "anthropic/claude-3.5-sonnet":  (0.0045,  "via OpenRouter · $3.00/M in · $15.00/M out"),
    "google/gemini-2.0-flash":      (0.00010, "via OpenRouter · $0.075/M in · $0.30/M out"),
    "meta-llama/llama-3.3-70b-instruct": (0.000453, "via OpenRouter · $0.59/M in · $0.79/M out"),
    # ── Speaches (self-hosted / free) ─────────────────────────────────────────
    "speaches-tts":                 (0.0000,  "Self-hosted · no per-unit cost"),
    "speaches-stt":                 (0.0000,  "Self-hosted · no per-unit cost"),
    # ── Mistral AI LLM ───────────────────────────────────────────────────────
    # Assumption: 500 in + 200 out tokens/min
    "mistral-large-latest":         (0.0022,  "$2.00/M in · $6.00/M out tokens"),
    "mistral-large-2411":           (0.0022,  "$2.00/M in · $6.00/M out tokens"),
    "mistral-medium-latest":        (0.0009,  "$0.40/M in · $2.00/M out tokens"),
    "mistral-medium-3":             (0.0009,  "$0.40/M in · $2.00/M out tokens"),
    "mistral-small-latest":         (0.00011, "$0.10/M in · $0.30/M out tokens"),
    "mistral-small-3.1-24b":        (0.00011, "$0.10/M in · $0.30/M out tokens"),
    "mistral-nemo":                 (0.00011, "$0.15/M in · $0.15/M out tokens"),
    "open-mistral-7b":              (0.000035, "$0.25/M in · $0.25/M out tokens"),
    "open-mixtral-8x7b":            (0.00042, "$0.70/M in · $0.70/M out tokens"),
    "open-mixtral-8x22b":           (0.00126, "$2.00/M in · $6.00/M out tokens"),
    "codestral-latest":             (0.00033, "$0.30/M in · $0.90/M out tokens"),
    "codestral-2501":               (0.00033, "$0.30/M in · $0.90/M out tokens"),
    # ── Together AI LLM ──────────────────────────────────────────────────────
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": (0.00062, "$0.88/M in · $0.88/M out tokens"),
    "meta-llama/Llama-3.1-405B-Instruct-Turbo": (0.00245, "$3.50/M in · $3.50/M out tokens"),
    "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo": (0.000126, "$0.18/M in · $0.18/M out tokens"),
    "meta-llama/Llama-3.2-11B-Vision-Instruct-Turbo": (0.000126, "$0.18/M in · $0.18/M out tokens"),
    "Qwen/Qwen2.5-72B-Instruct-Turbo": (0.00084, "$1.20/M in · $1.20/M out tokens"),
    "Qwen/Qwen2.5-7B-Instruct-Turbo": (0.000042, "$0.06/M in · $0.06/M out tokens"),
    "deepseek-ai/DeepSeek-V3":      (0.000875, "$1.25/M in · $1.25/M out tokens"),
    "mistralai/Mixtral-8x7B-Instruct-v0.1": (0.00042, "$0.60/M in · $0.60/M out tokens"),
    "mistralai/Mistral-7B-Instruct-v0.3": (0.000126, "$0.18/M in · $0.18/M out tokens"),
    "google/gemma-2-27b-it":        (0.00056, "$0.80/M in · $0.80/M out tokens"),
    # ── Cerebras LLM ─────────────────────────────────────────────────────────
    "llama3.3-70b":                 (0.000665, "$0.85/M in · $1.20/M out tokens"),
    "llama3.1-70b":                 (0.000420, "$0.60/M in · $0.60/M out tokens"),
    "llama3.1-8b":                  (0.000070, "$0.10/M in · $0.10/M out tokens"),
    "llama3.1-405b":                (0.00175,  "$2.50/M in · $2.50/M out tokens"),
    # ── Fireworks AI LLM ─────────────────────────────────────────────────────
    "accounts/fireworks/models/llama-v3p3-70b-instruct": (0.000630, "$0.90/M in · $0.90/M out tokens"),
    "accounts/fireworks/models/llama-v3p1-8b-instruct":  (0.000126, "$0.18/M in · $0.18/M out tokens"),
    "accounts/fireworks/models/llama-v3p1-405b-instruct": (0.00210, "$3.00/M in · $3.00/M out tokens"),
    "accounts/fireworks/models/mixtral-8x7b-instruct":   (0.000350, "$0.50/M in · $0.50/M out tokens"),
    "accounts/fireworks/models/qwen2p5-72b-instruct":    (0.000630, "$0.90/M in · $0.90/M out tokens"),
    "accounts/fireworks/models/deepseek-v3":             (0.000630, "$0.90/M in · $0.90/M out tokens"),
    "accounts/fireworks/models/gemma2-9b-it":            (0.000126, "$0.18/M in · $0.18/M out tokens"),
    # ── Cohere LLM ───────────────────────────────────────────────────────────
    "command-r-plus-08-2024":       (0.003250, "$2.50/M in · $10.00/M out tokens"),
    "command-r-plus":               (0.003250, "$2.50/M in · $10.00/M out tokens"),
    "command-r-08-2024":            (0.000195, "$0.15/M in · $0.60/M out tokens"),
    "command-r":                    (0.000195, "$0.15/M in · $0.60/M out tokens"),
    "command-light":                (0.000110, "$0.10/M in · $0.30/M out tokens"),
    "command-r7b-12-2024":          (0.000049, "$0.0375/M in · $0.15/M out tokens"),
    # ── AWS Polly TTS ────────────────────────────────────────────────────────
    # Assumption: 500 chars/min · Standard $4/M · Neural $16/M
    "neural":                       (0.0080,  "AWS Polly Neural · $16.00/M characters"),
    "long-form":                    (0.0080,  "AWS Polly Long-Form Neural · $16.00/M characters"),
    "standard":                     (0.0020,  "AWS Polly Standard · $4.00/M characters"),
    "generative":                   (0.0300,  "AWS Polly Generative · $60.00/M characters"),
    # ── Azure TTS ────────────────────────────────────────────────────────────
    # Neural at $16/M chars; standard at $4/M chars; HD at $64/M chars
    "azure-tts-neural":             (0.0080,  "Azure Neural TTS · $16.00/M characters"),
    "azure-tts-standard":           (0.0020,  "Azure Standard TTS · $4.00/M characters"),
    # ── PlayHT TTS ───────────────────────────────────────────────────────────
    # Play3.0-mini: $1/M chars · PlayHT2.0-turbo: $2.50/M chars · PlayHT2.0: $4.50/M chars
    "Play3.0-mini":                 (0.000500, "$1.00/M characters"),
    "PlayHT2.0-turbo":              (0.001250, "$2.50/M characters"),
    "PlayHT2.0":                    (0.002250, "$4.50/M characters"),
    "PlayDialog":                   (0.002250, "$4.50/M characters"),
    # ── Neets.ai TTS ─────────────────────────────────────────────────────────
    # ~$0.80/M chars · cheapest non-self-hosted
    "style-diff-500":               (0.000400, "$0.80/M characters"),
    "ar-diff-50k":                  (0.000400, "$0.80/M characters"),
    "vits":                         (0.000400, "$0.80/M characters"),
    # ── Azure Speech STT ─────────────────────────────────────────────────────
    "azure-speech-realtime":        (0.0167,  "$0.0167/min audio"),
    "realtime":                     (0.0167,  "Azure Speech STT · $0.0167/min audio"),
    # ── AWS Transcribe STT ────────────────────────────────────────────────────
    "aws-transcribe-general":       (0.0240,  "$0.024/min audio"),
    "aws-transcribe-phone-call":    (0.0240,  "$0.024/min audio"),
    "phone-call":                   (0.0240,  "AWS Transcribe Phone Call · $0.024/min audio"),
    "dictation":                    (0.0240,  "AWS Transcribe Dictation · $0.024/min audio"),
}


def get_model_pricing(model_id: str) -> tuple[float, str] | None:
    """Return (cost_per_min_usd, native_cost_display) for a model, or None if unknown.

    Tries exact match first, then prefix matching for versioned model IDs
    or Deepgram voice model variants like "aura-asteria-en-custom".
    """
    if model_id in _PRICING:
        return _PRICING[model_id]
    for key, val in _PRICING.items():
        if model_id.startswith(key) or key.startswith(model_id):
            return val
    return None
