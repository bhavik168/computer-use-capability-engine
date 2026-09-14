"""Gemini adapter for the discovery loop.

Everything provider-specific about talking to a model lives in this file: the message
shape, the role names, how a tool is declared, how a tool result is threaded back, and how
thinking is configured. The loop above it sees only `Decision` objects.

The one thing that is *not* adapted is the tool schema. Gemini's `parameters_json_schema`
accepts the same JSON Schema the grounding layer already builds, enum included, so the
constraint that makes the model schema-incapable of naming an off-screen element survives
the provider change untouched. That mattered enough to check before choosing this approach.
"""

from __future__ import annotations

import json
import logging
import os

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from engine.discovery.llm_client import Decision, LLMClient, LLMError

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-pro"
# -1 asks Gemini to size its own thinking budget per turn — the closest equivalent to
# adaptive thinking. A fixed number here would either starve a hard screen or pay for
# reasoning on a trivial one.
DYNAMIC_THINKING = -1


class GeminiClient(LLMClient):
    def __init__(self, model: str | None = None, max_output_tokens: int = 4000) -> None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise LLMError(
                "GEMINI_API_KEY is not set. Discovery needs a real model; replay does not."
            )
        self.client = genai.Client(api_key=api_key)
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.max_output_tokens = max_output_tokens
        self.calls = 0
        self._system_prompt = ""
        self._history: list[types.Content] = []
        self._pending_call_name: str | None = None

    # ------------------------------------------------------------------ conversation

    def start(self, system_prompt: str) -> None:
        self._system_prompt = system_prompt
        self._history = []
        self._pending_call_name = None

    def decide(self, observation: str, tool_schema: dict) -> Decision:
        self._history.append(
            types.Content(role="user", parts=[types.Part(text=observation)])
        )

        declaration = types.FunctionDeclaration(
            name=tool_schema["name"],
            description=tool_schema["description"],
            # Passed through verbatim: this is the grounded enum, not a suggestion.
            parameters_json_schema=tool_schema["input_schema"],
        )
        config = types.GenerateContentConfig(
            # Gemini carries the system prompt as configuration rather than as a message,
            # which also keeps it out of the turn history entirely.
            system_instruction=self._system_prompt,
            max_output_tokens=self.max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=DYNAMIC_THINKING),
            tools=[types.Tool(function_declarations=[declaration])],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    # ANY + an explicit name is Gemini's forced tool use: the model must
                    # answer with a call to `act`, never with prose.
                    mode=types.FunctionCallingConfigMode.ANY,
                    allowed_function_names=[tool_schema["name"]],
                )
            ),
            # The SDK will happily execute functions for us; that would take the grounding
            # and policy checks out of the loop, which is the opposite of what we want.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        self.calls += 1
        try:
            response = self.client.models.generate_content(
                model=self.model, contents=self._history, config=config
            )
        except genai_errors.ClientError as exc:
            raise LLMError(f"Gemini rejected the request: {exc}") from exc
        except genai_errors.ServerError as exc:
            raise LLMError(f"Gemini server error: {exc}") from exc
        except genai_errors.APIError as exc:
            raise LLMError(f"Gemini call failed: {exc}") from exc

        call = self._extract_call(response)
        # Echo the model's own turn back into history, or the next turn loses the thread.
        self._history.append(
            types.Content(role="model", parts=[types.Part(function_call=call)])
        )
        self._pending_call_name = call.name

        args = dict(call.args or {})
        log.debug("gemini proposed: %s", args)
        return Decision(
            action=args.get("action", ""),
            rationale=args.get("rationale"),
            element_id=args.get("element_id"),
            text=args.get("text"),
            url=args.get("url"),
            goal_reached=bool(args.get("goal_reached")),
            goal_evidence=args.get("goal_evidence"),
            raw=args,
        )

    def record_result(self, message: str, ok: bool = True) -> None:
        """Thread the outcome back as a function response.

        Gemini correlates a response to a call by **function name**, not by an id — there is
        no equivalent of `tool_use_id`. With one tool in play that is unambiguous, and the
        name is carried from the call that is actually outstanding rather than hardcoded.
        There is also no `is_error` flag, so failure is stated in the payload where the
        model will read it.
        """
        name = self._pending_call_name
        if name is None:
            log.warning("record_result called with no outstanding tool call; ignoring")
            return
        self._history.append(
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=name,
                            response={"ok": ok, "result": message},
                        )
                    )
                ],
            )
        )
        self._pending_call_name = None

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _extract_call(response) -> types.FunctionCall:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            raise LLMError(f"Gemini returned no candidates (finish: {response!r})")

        candidate = candidates[0]
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "function_call", None):
                return part.function_call

        # Forced tool use should make this unreachable; when it is not, say why rather than
        # letting a None propagate into the loop.
        finish = getattr(candidate, "finish_reason", None)
        text = getattr(response, "text", None)
        raise LLMError(
            f"Gemini returned no function call despite forced tool use "
            f"(finish_reason={finish}, text={json.dumps(text)[:200]})"
        )
