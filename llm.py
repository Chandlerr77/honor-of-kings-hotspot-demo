"""Model access for the pipeline.

Claude (official anthropic SDK) is preferred because it can search the web while
verifying a hotspot. Any OpenAI-compatible endpoint also works for matching and
script writing; without web search, factual claims stay 存疑 for human review.

Provider is picked from environment variables, first match wins:
  ANTHROPIC_API_KEY                         -> Claude, model LLM_MODEL or claude-opus-5-5
  LLM_API_KEY + LLM_BASE_URL + LLM_MODEL    -> any OpenAI-compatible endpoint
  DEEPSEEK_API_KEY                          -> DeepSeek, model LLM_MODEL or deepseek-flash, with web search
  GEMINI_API_KEY / DASHSCOPE_API_KEY / MOONSHOT_API_KEY / ARK_API_KEY / OPENAI_API_KEY

Gemini's free tier works for matching and writing; its Google Search grounding is paid-tier only,
so verification runs without web search there.
"""
import json
import os
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

CLAUDE_MODEL = "claude-opus-5-5"
COMPATIBLE = [  # (key env, base url, default model; None means LLM_MODEL is required)
    ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.5-flash-lite"),
    ("DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("MOONSHOT_API_KEY", "https://api.moonshot.cn/v1", "moonshot-v1-32k"),
    ("ARK_API_KEY", "https://ark.cn-beijing.volces.com/api/v3", None),  # 豆包：LLM_MODEL 填推理接入点 ID
    ("OPENAI_API_KEY", "https://api.openai.com/v1", None),
]


class ModelError(RuntimeError):
    pass


SLEEP = time.sleep   # replaced in tests
RETRIES = 3          # rate-limit retries; free tiers throttle hard


def parse_json(text):
    """Read a JSON object from model text, tolerating code fences or a preamble."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ModelError("模型没有返回 JSON")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as error:
        raise ModelError(f"模型返回的 JSON 无法解析：{error}") from None


class Claude:
    web_search = True

    def __init__(self, model):
        try:
            import anthropic
        except ImportError:
            raise ModelError("已配置 ANTHROPIC_API_KEY，但未安装 anthropic：pip install -r requirements.txt") from None
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.name = f"Claude · {model}"

    def _create(self, **kwargs):
        try:
            return self.client.beta.messages.create(
                model=self.model, max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except self.anthropic.RateLimitError as error:
            raise ModelError("Claude 限流，稍后重试") from error
        except self.anthropic.APIStatusError as error:
            raise ModelError(f"Claude 返回 HTTP {error.status_code}") from error
        except self.anthropic.APIConnectionError as error:
            raise ModelError("连不上 Claude API") from error

    @staticmethod
    def _text(response):
        if response.stop_reason == "refusal":
            raise ModelError("Claude 拒绝了这次请求")
        return "\n".join(b.text for b in response.content if b.type == "text")

    def research(self, system, prompt, max_searches=4):
        """Free-form answer with web search; returns notes text."""
        messages = [{"role": "user", "content": prompt}]
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
        for _ in range(4):
            response = self._create(system=system, messages=messages, tools=tools,
                                    output_config={"effort": "medium"})
            if response.stop_reason != "pause_turn":
                return self._text(response)
            messages.append({"role": "assistant", "content": response.content})
        raise ModelError("联网核验轮次过多，已中止")

    def json(self, system, prompt, schema):
        response = self._create(
            system=system, messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}})
        return parse_json(self._text(response))


class Compatible:
    web_search = False
    extra = {}           # provider-specific request fields
    timeout = 180

    def __init__(self, key, base_url, model, label):
        self.key, self.base_url, self.model = key, base_url.rstrip("/"), model
        self.name = f"{label} · {model}"

    def _message(self, body):
        """Send and return the assistant message; retry once on blank content (a documented DeepSeek quirk)."""
        for _ in range(2):
            data = self._post({"model": self.model, **self.extra, **body})
            try:
                message = data["choices"][0]["message"]
            except (KeyError, IndexError, TypeError):
                raise ModelError(f"{self.name} 返回格式异常") from None
            if data["choices"][0].get("finish_reason") == "content_filter":
                raise ModelError(f"{self.name} 触发内容过滤")
            if str(message.get("content") or "").strip():
                return message
        raise ModelError(f"{self.name} 连续返回空内容")

    def json(self, system, prompt, schema):
        message = self._message({
            "messages": [
                {"role": "system", "content": system + "\n\n只输出一个 json 对象，符合以下 JSON Schema：\n"
                 + json.dumps(schema, ensure_ascii=False)},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        })
        return parse_json(message["content"])

    def _post(self, body):
        """POST with backoff on 429/503; drop JSON mode once if the endpoint rejects it."""
        attempt = 0
        while True:
            req = Request(self.base_url + "/chat/completions", data=json.dumps(body).encode(),
                          headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            try:
                with urlopen(req, timeout=self.timeout) as response:
                    return json.load(response)
            except HTTPError as error:
                code, wait = error.code, error.headers.get("Retry-After") if error.headers else None
                error.close()
                if code == 400 and "response_format" in body:
                    body = {k: v for k, v in body.items() if k != "response_format"}
                    continue
                if code in (429, 500, 503) and attempt < RETRIES:
                    attempt += 1
                    SLEEP(float(wait) if wait and wait.replace(".", "", 1).isdigit() else 10 * 2 ** attempt)
                    continue
                hint = "（免费档限流，稍后会在下一轮继续）" if code == 429 else ""
                raise ModelError(f"{self.name} 返回 HTTP {code}{hint}") from None
            except OSError as error:
                raise ModelError(f"连不上 {self.name}：{type(error).__name__}") from None


class DeepSeek(Compatible):
    """deepseek-flash with DeepSeek's server-side web search (runs inside one request, tool calls not billed).

    Web search cannot be combined with JSON mode, so verification is two calls: research notes with
    the sources actually retrieved, then a JSON pass over those notes - the same shape as Claude.
    """
    web_search = True
    extra = {"thinking": {"type": "disabled"}}   # plain search; deep thinking search is slower and costlier
    timeout = 600                                 # search requests can run for minutes

    def __init__(self, key, model):
        super().__init__(key, "https://api.deepseek.com", model, "DeepSeek")

    def research(self, system, prompt, max_searches=4):
        message = self._message({
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "tools": [{"type": "deepseek_web_search"}, {"type": "deepseek_read_url"}],
        })
        sources = {}
        for note in message.get("annotations") or []:
            cite = note.get("url_citation") or {}
            if str(cite.get("url", "")).startswith("http"):
                sources[cite["url"]] = cite.get("title") or cite["url"]
        for step in message.get("server_tool_calls") or []:
            for call in step.get("calls") or []:
                for source in call.get("sources") or []:
                    if str(source.get("url", "")).startswith("http"):
                        sources.setdefault(source["url"], source.get("title") or source.get("site_name") or source["url"])
        listed = "\n".join(f"- {title} {url}" for url, title in list(sources.items())[:15])
        return message["content"] + ("\n\n本次实际检索到的来源（只能引用这些链接）：\n" + listed if listed else
                                     "\n\n本次没有检索到可引用的来源。")


def provider():
    """Return the configured model client, or None when no key is set."""
    model = os.getenv("LLM_MODEL", "").strip()
    if os.getenv("ANTHROPIC_API_KEY"):
        return Claude(model or CLAUDE_MODEL)
    if os.getenv("LLM_API_KEY") and os.getenv("LLM_BASE_URL") and model:
        return Compatible(os.environ["LLM_API_KEY"], os.environ["LLM_BASE_URL"], model, "自定义模型")
    if os.getenv("DEEPSEEK_API_KEY"):
        return DeepSeek(os.environ["DEEPSEEK_API_KEY"], model or "deepseek-flash")
    for env, base, default in COMPATIBLE:
        if os.getenv(env):
            if not (model or default):
                raise ModelError(f"已配置 {env}，还需要设置 LLM_MODEL")
            return Compatible(os.environ[env], base, model or default, env.removesuffix("_API_KEY").title())
    return None
