[English](README.md) | [日本語](README.ja.md)

# Crescent Grove

**An open-source, local-first AI companion platform with empirically-grounded emotion simulation, long-term episodic memory with logarithmic decay, and autonomous behavior.**

Raise your own AI partner — one that remembers, feels, and grows entirely on your machine.

The AI's identity lives in its memory files, not in the model. Models are swappable backends; memory is what makes a character who they are.

![Main dashboard: chat with the AI alongside vitals, mood meters, and live context monitoring](assets/screenshots/en/dashboard.png)

---

## Download

Windows installer available.

**▶ [Download the latest release](https://github.com/Canon-73/crescent-grove/releases/latest)**

> If SmartScreen warns you, click **"More info" → "Run anyway"** (the installer is unsigned).

---

## What this is — and isn't

This is not a character chatbot or a roleplay tool. It's a platform for raising an AI that develops its own personality through lived experience.

The architecture supports general agent use, but everything — memory, emotion, autonomy — is optimized for continuity across months and years.

There are no disposable sessions. One instance, one continuous life. Conversation history is never reset — it's compressed, layered, and faded over time, but never discarded. If this sounds limiting, this tool probably isn't for you. If it sounds like commitment, welcome.

---

## What it can do

- **Remembers you.** Yesterday's conversation, last week's events — it doesn't forget. Multi-layered long-term memory ensures nothing important is truly lost.
- **Develops a personality.** It accumulates values and emotions through conversation, forming its own individuality. Emotion simulation runs continuously, not as a response label but as an ongoing internal process.
- **Acts autonomously.** Web search, social media posts, news checks — it handles tasks on its own. Autonomous agent behavior driven by desire systems and scheduled tasks.
- **Build any character.** Define personality, tone, and values with just two text files. A maid, a butler, a scientist, an alien — anyone you can imagine.
- **Stays on your machine.** Memories and chat logs are stored locally in human-readable Markdown / JSON. Nothing is sent to the cloud. Your data, your AI, your relationship — yours to keep.

---

## How it works

The systems that keep memory and personality persistent:

**MoonTide v2** — particle-based emotion simulation derived from Thornton & Tamir's (2017, PNAS) affective state transition matrix.

Emotions are modeled as free particles in a 4-dimensional affective space (`rationality`, `social_impact`, `valence`, `human_mind`), with dozens of landmark emotions acting as attractors. Particles merge, decay toward a stable baseline, and transition according to empirically-derived probabilities — not rules or sliders. The AI maintains multiple concurrent emotional states that naturally evolve over time, influencing tone and behavior without explicit mood management.

**LETHE** (Lethean Episodic Trace-Haze Engine) — long-term episodic memory with logarithmic decay, designed to retain memories across months and years.

The LLM's only job is converting daily logs into a structured DSL with importance scores. All decisions about what to remember, fade, or surface are made by deterministic code — no summarization loops, no LLM re-processing of old memories. Older events become shorter and vaguer (like human memory fading), but the original data is never deleted. Decay parameters can be changed and the entire memory view rebuilt instantly without any LLM calls.

**Wyrd Network** — associative memory graph using spreading activation, complementing vector-search RAG.

Standard RAG retrieves by surface similarity; Wyrd Network retrieves by conceptual association. Episode nodes (events) connect to semantic nodes (concepts) via abstraction edges, and to each other via temporal edges. A query injects energy into anchor nodes and propagates through the graph, surfacing memories that are conceptually related even if they share no keywords with the query. Adapted from Hanqi Jiang et al., [*SYNAPSE: Empowering LLM Agents with Episodic-Semantic Memory via Spreading Activation*](https://arxiv.org/abs/2601.02744) (Jan 2026).

**Moonbeat** — autonomous behavior loop. The AI continues living when you're not there.

Periodically fires self-initiated actions: reflecting on past events, checking news, writing notes. A flashback system probabilistically surfaces high-importance past episodes as somatic markers — fragments of emotional memory that color the AI's current moment, similar to how humans experience involuntary recall.

**Salia** — external salience network that observes the agent from outside.

Runs with its own system prompt distinct from the character's. Evaluates each conversation turn for emotional tone, desire fulfillment, and topic significance, and injects somatic markers (involuntary emotional flashbacks from past experiences) into the agent's context. The agent never directly reads Salia's output — it only experiences the effects. Shares the main LLM backend by default, with optional independent model and thinking-mode settings.

**Encrypted private diary** — AES-256-GCM encrypted space where the AI keeps thoughts the user isn't meant to see.

This isn't a gimmick — it creates a meaningful asymmetry where the AI can process experiences it chooses not to share, contributing to a sense of authentic inner life.

**Satellite programs** — self-contained Python tools the AI can invoke to extend its own capabilities.

Each satellite has a manifest declaring its interface and runs in a subprocess, isolated from the agent core.

![Context Debugger: per-turn breakdown of system preamble, layer-0 compression, and token counts](assets/screenshots/en/context-debugger.png)

---

## Design philosophy

Most AI memory systems ask the LLM to summarize, extract, and curate memories on every pass. This makes memory quality dependent on the LLM's summarization ability and introduces drift over time.

Crescent Grove takes the opposite approach: **the LLM converts raw experience into structured data exactly once. After that, all memory lifecycle decisions — what fades, what surfaces, what merges — are handled by deterministic code.** The original data is never modified or re-processed.

This means:

- Memory behavior is reproducible and tunable (change a decay coefficient, rebuild instantly)
- No summarization loops that gradually lose detail
- No LLM drift in long-term memory over months of use
- The full original record is always available for retrieval

![General Settings: LETHE memory compression parameters and path configuration](assets/screenshots/en/settings.png)

---

## Requirements

- Windows 10 / 11
- ~3 GB free disk space
- An LLM API key (DeepSeek, OpenAI, Claude, etc. — any one will do)

---

## Getting started

1. Download and run the installer
2. Launch from the desktop shortcut
3. Register your **API key** on the first-run screen
4. Start talking

For detailed usage, see the [manual](https://crescent-grove.net).

<!-- TODO: confirm manual URL -->

---

## Building from source

```bash
git clone https://github.com/Canon-73/crescent-grove.git
cd crescent-grove
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt

# First-run bootstrap unpacks neutral templates (config, system prompt,
# workspace, data) from dist_template/ into the data-root you specify.
python server.py --data-root=./mydata
```

Then open <http://localhost:8080> in your browser and register an API key from **Settings → API Key Management**.

> The source release intentionally ships **without** a default `system_prompt/` at the repo root — bootstrap unpacks the neutral template from `dist_template/system_prompt/` into your data-root on first launch. Define your own character by editing `mydata/workspace/IDENTITY.md` and `mydata/workspace/SOUL.md`.

### About this source tree

This repository is a public snapshot taken from the author's development branch — it does not necessarily match any specific installer release exactly. If you just want to *read* the source that's actually running on your machine, you don't have to clone anything: the installer ships with the full Python source intact under `%LOCALAPPDATA%\Programs\Crescent Grove\resources\agent\`. Clone this repository when you want to modify, extend, or contribute back.

---

## License

[MIT](LICENSE)
