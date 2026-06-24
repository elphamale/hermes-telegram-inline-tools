# hermes-telegram-inline-tools

Custom executors for the Hermes agent Telegram inline query surface.

Type `@botname <query>` in any Telegram chat to trigger inline tools without the bot being a member of the group.

## How it works

The inline query pipeline has three layers:

1. **`TelegramInlineRouter`** (`gateway/platforms/telegram_inline_router.py` in hermes-agent) — loads `~/.hermes/inline_tools.yaml`, matches the query to a tool, and dispatches to an executor.
2. **Executor** — a Python class that subclasses `InlineExecutor`, placed in `~/.hermes/inline_executors/*.py`. Loaded automatically on gateway start.
3. **Executors own their full result lifecycle.** Return `List[InlineQueryResult]` — ready-to-send Telegram objects. For slow operations, return a stub immediately.

## Installation

1. Copy executor files to `~/.hermes/inline_executors/`
2. Add the corresponding tool entry to `~/.hermes/inline_tools.yaml` (see `inline_tools.yaml.example`)
3. Restart the gateway

## Tools

### `inline_repost` — repost your own messages

Search the agent's outbound message history and send a past response into any chat.

**Trigger:** `@botname #<search term>`

**Example:** `@botname #release notes` → finds the most recent message containing "release notes" and offers it as an inline result to tap and send.

**Scope:** searches the 5 most recent sessions by default. Configurable via `session_window` in `inline_tools.yaml`:

```yaml
- id: repost
  executor: inline_repost
  session_window: 3   # search last 3 sessions instead of 5
```

**Auth:** enforces `TELEGRAM_ALLOWED_USERS` independently (the adapter-level inline handler has no allowlist gate — see Security note).

**Requires:** hermes-agent with the pluggable inline executor API (`InlineExecutor` ABC + `register_executor()`).

## Classifier

Query routing uses a two-stage model implemented in `inline_classifier.py`. On each inline query the classifier first checks whether any enabled tool declares a `prefix:` field that matches the query start — if so, only those tools are dispatched (O(1), no model inference). For queries that don't match any prefix, the classifier encodes the query with [fastembed](https://github.com/qdrant/fastembed) (`BAAI/bge-small-en-v1.5`) and computes cosine similarity against each tool's `description:` embedding, dispatching only tools above a configurable threshold. Both stages merge results in FILO order (last-registered tool wins on tie). Tool embeddings are built once at plugin init and rebuilt whenever `inline_tools.yaml` mtime changes.

## Writing a new executor

Copy `executor_template.py`, implement `execute()`, and add a `register()` function:

```python
from gateway.platforms.telegram_inline_router import InlineExecutor
from telegram import InlineQueryResultArticle, InputTextMessageContent

class MyExecutor(InlineExecutor):
    def __init__(self, tool_config, bot):
        self._config = tool_config

    async def execute(self, user_id, query):
        return [
            InlineQueryResultArticle(
                id="result",
                title="Result title",
                description="Short description",
                input_message_content=InputTextMessageContent(message_text="your result here"),
            )
        ]

def register(router):
    router.register_executor("my_executor", lambda cfg, bot: MyExecutor(cfg, bot))
```

Add an entry to `inline_tools.yaml`:

```yaml
- id: my_tool
  type: direct
  executor: my_executor
  description: "What this tool does, used by the classifier"
  prefix: "!"
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

Executors return `List[InlineQueryResult]` — Telegram objects passed directly to `iq.answer()`.

| Telegram type                  | Common constructor args                                        |
|-------------------------------|----------------------------------------------------------------|
| `InlineQueryResultArticle`    | `id`, `title`, `description`, `input_message_content`         |
| `InlineQueryResultCachedAudio`| `id`, `audio_file_id`, `caption`                              |

## Security note

Auth is enforced at the adapter level. Executors accessing sensitive data may add a second check as defense-in-depth.

The inline handler has a ~10 s empirical deadline; the adapter enforces 6.5 s via `INLINE_RESPONSE_DEADLINE`. Executors that need longer should return a stub result immediately and manage their own background state.
