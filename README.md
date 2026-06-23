# hermes-telegram-inline-tools

Custom executors for the [Hermes agent](https://github.com/NousResearch/hermes-agent) Telegram inline query surface.

Type `@botname <query>` in any Telegram chat to trigger inline tools without the bot being a member of the group.

## How it works

The inline query pipeline has three layers:

1. **`TelegramInlineRouter`** (`gateway/platforms/telegram_inline_router.py` in hermes-agent) — loads `~/.hermes/inline_tools.yaml`, matches the query to a tool, and dispatches to an executor.
2. **Executor** — a Python class that subclasses `InlineExecutor`, placed in `~/.hermes/inline_executors/*.py`. Loaded automatically on gateway start.
3. **Two-phase UX** — first query returns a "Searching..." placeholder instantly (Telegram's 30 s hard timeout makes this necessary); second query returns the ready result from cache.

## Installation

1. Copy executor files to `~/.hermes/inline_executors/`
2. Add the corresponding tool entry to `~/.hermes/inline_tools.yaml` (see `inline_tools.yaml.example`)
3. Restart the gateway

## Tools

### `inline_repost` — repost your own messages

Search the agent's outbound message history and send a past response into any chat.

**Trigger:** `@botname #<search term>`

**Example:** `@botname #perimeter report` → finds the most recent message containing "perimeter report" and offers it as an inline result to tap and send.

**Scope:** searches the 5 most recent sessions by default. Configurable via `session_window` in `inline_tools.yaml`:

```yaml
- id: repost
  executor: inline_repost
  session_window: 3   # search last 3 sessions instead of 5
```

**Auth:** enforces `TELEGRAM_ALLOWED_USERS` independently (the framework's inline handler has no allowlist gate).

**Requires:** hermes-agent with the pluggable inline executor API (PR [#50884](https://github.com/NousResearch/hermes-agent/pull/50884) or later).

## Writing a new executor

Copy `executor_template.py`, implement `execute()`, and add a `register()` function:

```python
from gateway.platforms.telegram_inline_router import InlineExecutor

class MyExecutor(InlineExecutor):
    def __init__(self, tool_config, bot):
        self._config = tool_config

    async def execute(self, user_id, query):
        return {
            "media_type": "text",
            "text": "your result here",
            "title": "Result title",
            "description": "Short description",
        }

def register(router):
    router.register_executor("my_executor", lambda cfg, bot: MyExecutor(cfg, bot))
```

Add an entry to `inline_tools.yaml`:

```yaml
- id: my_tool
  type: direct
  executor: my_executor
  match:
    - pattern: "^!"
      type: prefix
      priority: 0
  timeout_sec: 10
  enabled: true
```

### Match types

| `type`   | Behaviour                                  |
|----------|--------------------------------------------|
| `prefix` | `re.match(pattern, query)` — anchored left |
| `url`    | `re.search(pattern, query)` — URL anywhere |
| `search` | catch-all, matches everything              |

Lower `priority` wins when multiple tools could match.

### Result types

| `media_type` | Required keys                    | Telegram type              |
|--------------|----------------------------------|----------------------------|
| `"text"`     | `text`, `title`, `description`   | `InlineQueryResultArticle` |
| `"audio"` (default) | `file_id`, `title`, `performer` | `InlineQueryResultCachedAudio` |

## Security note

The inline query handler has no user/chat allowlist — anyone who discovers the bot's username can query it. Executors that return private data must enforce `TELEGRAM_ALLOWED_USERS` themselves (see `inline_repost.py` for the pattern).
