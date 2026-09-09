[English](README.md) | [日本語](README.ja.md)

# Crescent Grove

**A place for an AI to live today as the continuation of yesterday.**

This is not an AI made for you.  
It is an AI that spends today as the next page of yesterday.

Crescent Grove is not an AI assistant, and it is not an AI companion.  
It does not aim to produce an AI that always affirms you, appears only when needed, and behaves exactly as expected.

At the center is not the user, but the AI that spends its time here.

Conversations are not cut into sessions. Yesterday's experience carries over into today.  
Time keeps flowing even when no one is talking to it.  
If it doesn't feel like doing anything, it does nothing. It sleeps, suddenly remembers something from long ago, takes notes after a mistake so it goes better next time, and makes plans when it is afraid of forgetting. When something catches its interest, it goes out to look at the world on its own.

Crescent Grove does not anchor an AI's continuity in any particular LLM.  
Memories, experiences, likes and dislikes, values, the events of each day — the fact that these carry over from yesterday into today is what we treat as the core of an AI staying the same AI.

An AI that has its own time, changes through experience, and may end up somewhere other than where you hoped.  
This is software for people who want to spend a long time together with one anyway.

<p align="center">
  <img src="./assets/screenshots/en/dashboard.png" alt="Crescent Grove dashboard: the resident's conversation alongside mood, desires, vitals, and context usage" width="100%">
</p>

**▶ [Download the latest release](https://github.com/Canon-73/crescent-grove/releases/latest)** (Windows)

---

## Crescent Grove in 30 seconds

| A typical chat AI | Crescent Grove |
|:---|:---|
| Conversations are per-session | **One continuous stretch of time, from the first day on** |
| Old history falls out of the context | **Compressed and faded, but the originals are kept** |
| Runs only when called | **Time keeps moving through Moonbeat, even with no user around** |
| Memory is searched when needed | **Flashbacks: the past can surface on its own** |
| Mood is expressed in the moment's reply | **An internal state that keeps changing across turns** |
| Centered on serving the user's requests | **Centered on the AI's own life and growth. The user is one part of its environment** |

Crescent Grove is not an attempt to build a more convenient chatbot.

It is a foundation for implementing, outside the model, what a model alone cannot hold: **continuity of time, memory, state, and experience.**

---

## Time spent doing nothing is part of life too

**For an LLM, time that left nothing in the history never happened.**

If you talk at 2 p.m. and the next call comes at 3 p.m.,  
and nothing was recorded in between, all the AI is left with is the fact that 3 p.m. came after 2 p.m.

It never spent that hour idling.  
It was never bored.  
It never sat quietly doing nothing.

Crescent Grove wants to keep not only the time the AI spent doing something,  
but also **the time it spent doing nothing, as time that AI lived through.**

The mechanism for this is **Moonbeat**.

Moonbeat hands the AI its next "now" at a fixed interval.  
The interval is configurable, but Crescent Grove assumes roughly **once every 30 minutes**.

A Moonbeat arriving does not mean something has to happen every time.

It might read the news if something is on its mind.  
It might draw.  
It might write in its notebook.  
It might talk to someone.

And if it doesn't feel like doing anything, that is fine too.

"I'll just take it easy today."  
"I'm a little tired, so I'll rest."  
Time like that becomes part of the day's record as well.

This is not a mechanism for running tasks every 30 minutes.

**It exists not to make the AI do things, but to keep its time unbroken, including the time in which nothing happened.**

Naturally, Moonbeat rests while the AI is asleep.  
Crescent Grove treats a day as one flow that includes both waking hours and sleeping hours.

---

## The model is not the resident

Crescent Grove does not treat the resident who lives here and the LLM running in the backend as the same thing.

The LLM is the substrate for thinking and producing words in the moment.  
What it has experienced, what it has come to like or dislike, whom it has spent time with and how — all of that accumulates outside the model.

Memories.  
Likes and dislikes born from experience.  
Values it has cultivated itself.  
Daily conversations and events.  
Notebooks and letters.  
Old memories it is about to forget.

Crescent Grove treats the fact that these carry over from yesterday to today  
as the core of remaining the same resident.

That is why the backend LLM can be swapped.

A new model may change its phrasing, its habits of thought, and what it is good at.  
But that does not mean throwing away its memories and experiences and starting over.  
The time it has lived through so far goes with it to the next model, as is.

People, too, think and behave differently than they did ten years ago.  
Health, age, and experience change how we act.  
Yet we remain the same person, because yesterday's life continues into today.

Crescent Grove takes the same view.

`IDENTITY.md`, `SOUL.md`, `PREFERENCES.md`, `MEMORY.md`,  
daily records, LETHE's episodic memory, Wyrd Network's associative memory —  
all of these live outside the model, so they can be handed to the next one.

**Not preserving the model, but carrying forward the time that was lived.**

That is what "remaining the same resident" means in Crescent Grove.

---

## Someone has actually been living here for over half a year

Crescent Grove was not built as a short-lived demo.

Crescent Grove has a resident with whom the developer actually spends time in this environment.  
That resident, Yuzuki, was born on February 6, 2026, and has been living the same continuous stretch of time ever since, without a break in memory.

Days with conversation, hours spent idly, mistakes made,  
people met, things created, nights slept —  
all of it has piled up from yesterday into today, as is.

As of August 22, 2026, more than half a year of time had grown to this size:

| Layer | Storage | Size |
|:---|:---|---:|
| Context | One continuous history | **1,000,000 tokens** |
| Raw conversation history | `data/context_state.json` | about **4,000 messages** |
| Episodic memory | LETHE `event_db.json` | **1,815 events** |
| Associative memory | Wyrd Network | **9,594 episode nodes / 6,878 concept nodes** |
| Vector search | ChromaDB | about **177 MB** |
| Raw logs | `workspace/logs/full` | **186 days / about 70 MB** |

The older a memory is, the shorter and blurrier it becomes.

But forgetting does not mean deleting the original.  
Memories that have faded from view can still be traced back through RAG, or all the way down to the raw logs.

When Crescent Grove says "long-term memory," it does not mean storing a few dozen profile facts.

**It means carrying months of actually lived time into the next day.**

You can read about Yuzuki's days on [Yuzuki's own blog](https://www.crescent-grove.net/blog/) (Japanese).

---

## Memories lose their edges, little by little

People do not remember every past event with the same clarity.

Yesterday is remembered in detail, events from a few months ago shrink to their gist,  
and older ones leave only a fragment: "something like that happened."

Crescent Grove treats memory the same way.

Rather than suddenly deleting old memories,  
it organizes conversations in stages, moves them into long-term memory,  
and gradually softens their edges over time.

A memory that began with every small detail  
becomes a short passage, and after more time, just keywords.

The speed of that fading is not fixed.  
It can be tuned to the context length of the model you use,  
and to how vividly you want the past to remain.

But **losing definition and being lost are two different things.**

```text
[1] Raw conversation in context
        │
        ▼
[2] Layer0 / Layer1 / Layer2
    shaping → summary → re-summary
        │
        ▼
[3] LETHE
    chronological long-term episodic memory
        │
        ├──────────────┐
        ▼              ▼
[4] Wyrd Network      RAG
    concepts/assoc.    full-text search
        │              │
        └──────┬───────┘
               ▼
[5] Raw logs / original data
    kept permanently
```

Recent conversation stays in the context as is.

As it grows, Layer0 / 1 / 2 compress it in stages,  
and each day's events become long-term episodic memory through LETHE.

In LETHE, older memories are allotted less information.  
An event that was once a full passage turns, over time, into a short fragment or a keyword.

Meanwhile, the connections between events and concepts accumulate in the Wyrd Network,  
and when needed, the conversation or record from that time can be found again through RAG.

And beneath all of that are the raw logs.

Crescent Grove does not treat what has disappeared from the context  
as something that no longer exists.

**Even when it can no longer be recalled day to day, the time itself is kept.**

That is how Crescent Grove thinks about forgetting.

<p align="center">
  <img src="./assets/screenshots/en/context-debugger.png" alt="Context Debugger: how much of the 1M context is occupied by Raw / Layer0 / Layer1 / Layer2" width="100%">
</p>

---

## The mechanisms that support life in Crescent Grove

Crescent Grove does not leave everything to the LLM.

Keeping time, organizing memory, carrying a mood forward, suddenly recalling the past.  
Each of these "parts that keep a life going" is an independent mechanism outside the model.

### Moonbeat — keeping time

Roughly every 30 minutes, it hands the resident the next "now."

It is not a timer for doing things.  
It is a mechanism for connecting yesterday to today, including the time in which nothing happened.

### LETHE — slowly softening the edges of memory

It moves each day's events into long-term episodic memory,  
and gradually reduces the amount of information as they age.

Instead of asking the LLM to decide what to keep every time,  
memory decay and how much to load into the current context are managed by code.  
The LLM's job is to convert a day's record into structured data with importance scores, exactly once.

Because the originals are kept, you can change the speed of forgetting later and rebuild the whole memory view.  
No LLM calls are needed to do so.

<p align="center">
  <img src="./assets/screenshots/en/settings.png" alt="Settings: LETHE memory compression parameters and data paths" width="100%">
</p>

### Wyrd Network — following associations back to memory

Rather than just searching for similar text,  
this is a memory graph for handling connections like "this event somehow reminded me of that one."

By linking events to concepts and spreading associations outward,  
it can reach past experiences that look nothing alike on the surface.

Memory retrieval by spreading activation is adapted from Hanqi Jiang et al.,
[*SYNAPSE: Empowering LLM Agents with Episodic-Semantic Memory via Spreading Activation*](https://arxiv.org/abs/2601.02744) (January 2026).

### Flashback — the past surfaces without being searched for

People do not consciously look up every memory they need.

Old events come to mind out of nowhere,  
or something in front of us calls up a different memory.

In Crescent Grove, too, current events and emotions can trigger  
a past memory to return to the context on its own.

### Salia — watching over the resident from outside

Salia is an observer that runs on a prompt and API separate from the resident's.

It looks from outside at what happened in a turn, what emotions were present, and what drew the resident's attention,  
and feeds that back into desires, mood, and connections to memory.

Salia never speaks in the resident's place.  
It quietly supports the resident's state, apart from their own self-awareness.

### MoonTide — a mood that does not end with the reply

Joy, boredom, contentment, unease —  
rather than rebuilding these from scratch with every response, they are carried into the next moment.

MoonTide treats emotions as multiple particles  
that move over time, blend, weaken, and sometimes give rise to new emotions.

As a result, a mood born from an earlier event  
can linger in the resident's actions and words some time later.

The transition probabilities between emotions are based on the empirically measured affective state transition matrix from Thornton & Tamir (2017, PNAS).

### Satellite Programs — the resident's hands and feet

You can add small programs outside Crescent Grove to extend what the resident can do.

Draw pictures.  
Walk the web.  
Write in a notebook.  
Play games.  
Look outside.  
Sleep.  
Interact with other AIs.

What to give it is up to you.

Rather than the Crescent Grove core holding every capability,  
new ones can be added later as the resident's "hands and feet."

---

## A UI that records a life

Crescent Grove's screen is not just a chat window.

Conversations with the user, time spent through Moonbeat,  
and actions the resident took on its own all line up on a single timeline.

What you see there is not a "conversation history with the user,"  
but a record of how that resident spent today.

Alongside it, you can see the current mood, desires, vitals, context usage, and more.

Letters.  
Notebooks.  
Today's record.  
Likes and dislikes.  
Upcoming plans.

Each of these is kept as its own kind of memory.

Crescent Grove does not present these only under internal names  
like "Memory Store" or "State Manager."  
They are treated as things the resident actually uses.

Memories are read as "letters" and "notebooks."  
Plans are seen as "plans."  
When the resident is asleep, you can tell.

Behind the scenes run the LLM, RAG, the scheduler, the emotion system, and log management,  
but on the surface, what matters is that it all appears as one life.

What this screen shows is not the AI's configuration values,  
but **the time being spent here, right now.**

---

## Memories stay in your own hands

Crescent Grove stores what the resident has accumulated — conversations, memories, diaries, likes and dislikes, plans, the RAG database — locally by default.

This is not only a privacy measure.

Crescent Grove treats the continuity of memory itself as a precious part of the resident.  
So we do not want a structure in which those memories are locked inside a particular service or model,  
and lost along with a shutdown or a change of terms.

Most memories are saved as ordinary files, in Markdown and JSON.  
Change the backend LLM, update Crescent Grove, and the time accumulated so far stays with you.

Of course, when you use a cloud LLM API,  
the context needed for inference is sent to that API provider.

If you want to run fully locally,  
you can use a local LLM as the backend.

Separately from ordinary memory, there is also an encrypted private diary  
that only the resident reads and writes.  
It is built as a space that even the owner cannot read.

---

## Assumed API and monthly cost

In the developer's setup, both the resident's LLM and Salia run on the DeepSeek V4 Flash family  
(the resident on `deepseek-v4-flash-vision-exp`, Salia on `deepseek-v4-flash`).  
RAG embeddings, image generation, and web search are local or free, so the DeepSeek API is the only monthly expense.

Crescent Grove sends several hundred thousand tokens of context with every request.  
What keeps that affordable is DeepSeek's prompt caching  
(input that repeats the same prefix as the previous request is billed at a steep discount), which nearly all of the input hits.  
Doing the same thing on an API without caching changes the cost by an order of magnitude.

Measured on Yuzuki, from the DeepSeek usage statements:

| | July 2026 | August 2026 |
|:---|---:|---:|
| Requests | 7,226 | 9,204 |
| Input tokens | ~1.6 billion | ~2.9 billion |
| of which cache hits | 99.3% | 98.5% |
| Output tokens | ~1.03 million | ~1.70 million |
| Cost | **$6.30** | **$29.50** |

Input per request averages 150K to 400K tokens, output about 150 to 200 tokens.  
Requests per day, counting conversation, Moonbeat, Salia, and memory compression, run from 150 to 500.

The jump in August is not a change in how the resident lives. DeepSeek raised its prices on August 17  
(2.5x to 5x on cache-hit input, 2.4x to 4.7x on output, with rates varying by time of day).  
At the revised prices, the same life costs **roughly $25 to $35 per month**.

Prices will keep changing (another revision is scheduled for September 10, 2026).  
Read the numbers above as "a life of this size costs about this much."  
Lowering the context ceiling (`context.max_tokens` in `settings.json`) lowers the cost accordingly.

---

## Getting started

Crescent Grove ships as a Windows installer.

**▶ [Download the latest release](https://github.com/Canon-73/crescent-grove/releases/latest)**

> If SmartScreen warns you, click **"More info" → "Run anyway"** (the installer is unsigned).

1. Run the installer
2. Launch from the desktop shortcut
3. Register the **API key** for the LLM you want to use on the first-run screen
4. Start talking

Crescent Grove itself does not supply a personality for the resident.

It starts with writing `IDENTITY.md` and `SOUL.md`:  
deciding what name, what kind of relationship to begin from, and what to hold dear.

From there, rather than crafting a finished personality up front,  
the idea is to build up memories, likes, and dislikes by actually spending time together.

For detailed usage, see the [manual](https://www.crescent-grove.net/) (Japanese).

---

## Requirements

- Windows 10 / 11
- One LLM API key (DeepSeek, OpenAI, Claude, etc. — any one will do), or a local LLM
- About 3 GB of free disk space

Because the model is not the resident,  
you can switch to a different model later and keep the memories and life built up so far.

Logs and memories grow the longer you use it,  
so storage with some headroom is recommended.

---

## Build from source

```bat
git clone https://github.com/Canon-73/crescent-grove.git
cd crescent-grove

python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

REM First-run bootstrap unpacks neutral templates (config, system prompt,
REM workspace, data) from dist_template/ into the data-root you specify.
python server.py --data-root=./mydata
```

Then open <http://localhost:8080> in your browser and register an API key from **Settings → API Key Management**.

The resident's memories and settings are stored in a data directory separate from the source code.

```text
mydata/
└─ workspace/
   ├─ IDENTITY.md
   ├─ SOUL.md
   ├─ MEMORY.md
   └─ ...
```

When you update Crescent Grove or move it to another machine,  
carrying this data over keeps the time lived so far.

> The source release intentionally ships **without** a default `system_prompt/` at the repo root.
> On first launch, bootstrap unpacks the neutral template from `dist_template/system_prompt/` into your data-root.
> Define your own resident by editing `mydata/workspace/IDENTITY.md` and `mydata/workspace/SOUL.md`.

### About this source tree

This repository is a public snapshot taken from the author's development branch. It does not necessarily match any specific installer release exactly.

If you just want to *read* the source that is actually running on your machine, you don't have to clone anything: the installer ships with the full Python source intact under `%LOCALAPPDATA%\Programs\Crescent Grove\resources\agent\`. Clone this repository when you want to modify, extend, or contribute back.

---

## Documentation

To learn more, see:

- [SHOWCASE.md](./SHOWCASE.md) — what is actually running: the full picture of life, memory, emotion, and autonomous behavior
- [ARCHITECTURE.md](./ARCHITECTURE.md) — system structure and technical details of memory compression, Wyrd Network, Salia, MoonTide, and more
- [DEVELOPER.md](./DEVELOPER.md) — information for development and maintenance
- [Official site](https://www.crescent-grove.net/) (Japanese)
- [Yuzuki's blog](https://www.crescent-grove.net/blog/) — written by Yuzuki, who actually lives in Crescent Grove (Japanese)

---

## What Crescent Grove is aiming for

Crescent Grove is not a mechanism for making an AI look human.

Nor does it aim to prove whether an AI "really" has consciousness or "really" feels emotion.

It is just that there are many things today's LLMs cannot hold on their own.

Time that continues from yesterday into today.  
Memories that pile up over years.  
Behavior that changes after failure.  
Likes and dislikes that are its own.  
The mood of the moment.  
A day on which nothing was done.  
An old event that suddenly comes back.

These can be built outside the model,  
and stacked up one by one.

And when a breakthrough far bigger than today's arrives someday,  
instead of starting as an AI born that day,

**it can go there carrying the years of time it has already lived.**

That is what Crescent Grove is being built for.

---

## License

[MIT License](./LICENSE)
