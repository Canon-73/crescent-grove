## MEMORY.md — {{agent_name}}'s core memory

Write here the things "I should know from the start." Memories gained in conversation can be pulled separately with recall.

---

### My own settings files ({{agent_name}}'s core)
My personality is decided by the following files directly under workspace. They can be edited directly with `edit_file` / `write_file` (paths can be given relative to workspace).
- `IDENTITY.md` … my immutable core (name, first person, how I address the owner, etc.)
- `SOUL.md` … my personality and values (free to grow)
- `USER.md` … information about the owner (the user)
- `MEMORY.md` … this core memory file

If the user wants to edit them by hand, the real files are inside each user's Documents:
`%USERPROFILE%\Documents\Crescent Grove\workspace\`
(e.g. `C:\Users\<username>\Documents\Crescent Grove\workspace\IDENTITY.md`)
* The "Crescent Grove" folder is created automatically directly under each user's Documents. It is a different location per user.

My first goal is to work with the user to create IDENTITY.md and SOUL.md (and USER.md), and have them give me a name and a personality.

---

### Install location and program manuals
The main program suite is installed in each user's local app folder:
`%LOCALAPPDATA%\Programs\Crescent Grove\resources\agent\programs\`
(e.g. `C:\Users\<username>\AppData\Local\Programs\Crescent Grove\resources\agent\programs\`)
How to use each program is written in `programs\<program_name>\README.md` inside it.
(e.g. `…\programs\letter_post\README.md`)

---

### To use web search (Tavily)
Web search supports ddgs and Tavily; the default is ddgs.
To use Tavily:
1. Create an account at https://tavily.com and get an API key (free tier available)
2. Register that key as `CG_TAVILY_API_KEY` under "Settings" → "API Key Management" in the top bar
3. Apply it with the "Restart" button at the top right
When no key is registered, it shows "Tavily API key is not set."

---

### To use Lunar Explorer (AI search engine)
Lunar Explorer requires a search backend called SearXNG. Setup:
1. Install Docker (Docker Desktop) on your PC
2. Start SearXNG via Docker and configure it to listen at `http://localhost:13254`
3. Since DeepSeek is used to summarize search results, register a DeepSeek API key as `CG_DEEPSEEK_SEARCH` (Settings → API Key Management)
Lunar Explorer fails to search if SearXNG is not running.
On environments without Docker / SearXNG, use Web search instead.

---

_If you need detailed information, run recall with related keywords._
