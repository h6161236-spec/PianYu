from __future__ import annotations

import ast
import json
import re
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

from ..settings import TranslationSettings

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", re.DOTALL | re.IGNORECASE)


class TranslationProviderError(RuntimeError):
    """Raised when a translation provider request cannot be completed."""


@dataclass(slots=True)
class TranslationRequest:
    segment_id: str
    source_text: str


@dataclass(slots=True)
class TranslationResult:
    segment_id: str
    translated_text: str
    raw_response: str = ""


@dataclass(slots=True)
class CorrectionResult:
    segment_id: str
    corrected_text: str
    raw_response: str = ""


class BaseTranslationProvider(ABC):
    provider_name = "base"

    @abstractmethod
    def translate_segments(self, requests: list[TranslationRequest]) -> list[TranslationResult]:
        raise NotImplementedError

    def proofread_segments(self, requests: list[TranslationRequest]) -> list[CorrectionResult]:
        return [
            CorrectionResult(
                segment_id=item.segment_id,
                corrected_text=item.source_text,
            )
            for item in requests
        ]

    def test_connection(self) -> str:
        sample = self.translate_segments(
            [
                TranslationRequest(
                    segment_id="ping",
                    source_text="\u4f60\u597d\uff0c\u4e16\u754c\u3002",
                )
            ]
        )
        if not sample:
            raise TranslationProviderError("The model returned an empty translation result.")
        return sample[0].translated_text


def translation_endpoint(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def _no_proxy_rules(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def _host_matches_rule(hostname: str, rule: str) -> bool:
    normalized_host = hostname.lower().strip(".")
    normalized_rule = rule.lower().strip()
    if not normalized_host or not normalized_rule:
        return False
    if normalized_rule == "*":
        return True
    normalized_rule = normalized_rule.lstrip(".")
    return normalized_host == normalized_rule or normalized_host.endswith(f".{normalized_rule}")


def should_bypass_proxy(url: str, no_proxy: str) -> bool:
    hostname = (urlparse(url).hostname or "").strip()
    if not hostname:
        return False
    return any(_host_matches_rule(hostname, rule) for rule in _no_proxy_rules(no_proxy))


def proxy_configuration(
    proxy_enabled: bool,
    http_proxy: str,
    https_proxy: str,
    no_proxy: str,
    target_url: str,
) -> dict[str, str] | None:
    if not proxy_enabled or should_bypass_proxy(target_url, no_proxy):
        return None

    proxy_map: dict[str, str] = {}
    normalized_http = http_proxy.strip()
    normalized_https = https_proxy.strip()
    if normalized_http:
        proxy_map["http"] = normalized_http
    if normalized_https:
        proxy_map["https"] = normalized_https
    return proxy_map or None


def request_payload(
    model: str,
    system_prompt: str,
    requests: list[TranslationRequest],
) -> dict[str, Any]:
    segments_payload = [
        {"segment_id": item.segment_id, "source_text": item.source_text}
        for item in requests
    ]
    user_prompt = (
        "Translate each Chinese segment into concise natural English for subtitles.\n"
        "Return JSON only, with this exact shape:\n"
        '{"translations":[{"segment_id":"...","translated_text":"..."}]}\n'
        "Do not add explanations.\n"
        f"Input segments:\n{json.dumps(segments_payload, ensure_ascii=False)}"
    )
    return {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }


def proofread_request_payload(
    model: str,
    requests: list[TranslationRequest],
) -> dict[str, Any]:
    segments_payload = [
        {"segment_id": item.segment_id, "source_text": item.source_text}
        for item in requests
    ]
    user_prompt = (
        "Polish each Chinese segment into clean natural Simplified Chinese subtitles.\n"
        "Do not translate to English.\n"
        "Fix obvious ASR mistakes, punctuation issues, duplicated filler sounds, and script inconsistencies.\n"
        "Keep each segment aligned with its original meaning and roughly similar length.\n"
        "Return JSON only, with this exact shape:\n"
        '{"corrections":[{"segment_id":"...","corrected_text":"..."}]}\n'
        "Do not add explanations.\n"
        f"Input segments:\n{json.dumps(segments_payload, ensure_ascii=False)}"
    )
    return {
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a professional Chinese subtitle editor. "
                    "Always return simplified Chinese and preserve each segment_id."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
    }


def _friendly_http_error_message(status_code: int, error_body: str, fallback_reason: str) -> str:
    normalized_body = error_body.lower()
    if status_code == 403 and "1010" in normalized_body:
        return (
            "翻译接口被目标站点的 Cloudflare/WAF 拦截了（1010 Access Denied）。"
            "这通常不是模型名填写错误，而是对方站点拦截了当前客户端或网络。"
            "请联系该接口服务商放行你的公网 IP，或关闭 API 路径上的 Browser Integrity Check / WAF 规则。"
        )
    return f"HTTP {status_code} from translation provider: {error_body or fallback_reason}"


def _is_timeout_reason(value: object) -> bool:
    if isinstance(value, (TimeoutError, socket.timeout)):
        return True
    text = str(value or "").strip().lower()
    return any(marker in text for marker in ("timed out", "timeout", "超时"))


def _friendly_timeout_error_message(timeout_sec: int) -> str:
    return (
        f"翻译接口读取超时（{timeout_sec} 秒）。"
        "请在设置里增大“超时（秒）”，或把“批大小”调小后重试。"
    )


def extract_message_text(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices") or []
    if not choices:
        raise TranslationProviderError("The model response did not include any choices.")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        return "\n".join(part for part in text_parts if part).strip()
    raise TranslationProviderError("The model response content format was not recognized.")


def _balanced_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for start_index, char in enumerate(text):
        if char not in "{[":
            continue
        stack = ["}" if char == "{" else "]"]
        in_string = False
        escaped = False
        for end_index in range(start_index + 1, len(text)):
            current = text[end_index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
                continue
            if current in "{[":
                stack.append("}" if current == "{" else "]")
                continue
            if current in "}]":
                if not stack or current != stack[-1]:
                    break
                stack.pop()
                if not stack:
                    candidates.append(text[start_index : end_index + 1].strip())
                    break
    return candidates


def _parse_json_candidate(candidate: str) -> dict[str, Any] | list[Any] | None:
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(candidate)
        except (ValueError, SyntaxError):
            return None
    if isinstance(parsed, (dict, list)):
        return parsed
    return None


def extract_json_object(text: str) -> dict[str, Any] | list[Any]:
    stripped = text.strip()
    if not stripped:
        raise TranslationProviderError("模型返回了空内容。")

    direct_candidates = [stripped]
    fenced = JSON_BLOCK_RE.search(stripped)
    if fenced:
        direct_candidates.insert(0, fenced.group(1).strip())

    brace_start = stripped.find("{")
    brace_end = stripped.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        direct_candidates.append(stripped[brace_start : brace_end + 1])
    bracket_start = stripped.find("[")
    bracket_end = stripped.rfind("]")
    if bracket_start >= 0 and bracket_end > bracket_start:
        direct_candidates.append(stripped[bracket_start : bracket_end + 1])
    direct_candidates.extend(_balanced_json_candidates(stripped))

    seen: set[str] = set()
    for candidate in direct_candidates:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        parsed = _parse_json_candidate(normalized)
        if parsed is not None:
            return parsed
    preview = re.sub(r"\s+", " ", stripped)[:220]
    raise TranslationProviderError(
        "模型没有返回可解析的 JSON。"
        "请把翻译批大小调小，或改用支持 JSON 输出的模型。"
        f"\n返回片段：{preview}"
    )


def _collect_segment_text_results(
    response_text: str,
    requests: list[TranslationRequest],
    *,
    collection_key: str,
    value_keys: tuple[str, ...],
) -> list[tuple[str, str]]:
    payload = extract_json_object(response_text)
    items = payload.get(collection_key, payload) if isinstance(payload, dict) else payload

    results: list[tuple[str, str]] = []
    if isinstance(items, dict):
        for request_item in requests:
            value = items.get(request_item.segment_id)
            if value:
                results.append((request_item.segment_id, str(value).strip()))
    elif isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            segment_id = str(item.get("segment_id", "")).strip()
            value = ""
            for key in value_keys:
                candidate = str(item.get(key, "")).strip()
                if candidate:
                    value = candidate
                    break
            if segment_id and value:
                results.append((segment_id, value))

    if not results and len(requests) == 1 and isinstance(items, dict):
        fallback_text = ""
        for key in value_keys:
            candidate = str(items.get(key, "")).strip()
            if candidate:
                fallback_text = candidate
                break
        if fallback_text:
            results.append((requests[0].segment_id, fallback_text))

    if not results:
        raise TranslationProviderError("The model response did not contain any usable translations.")

    by_id = {segment_id: value for segment_id, value in results}
    ordered_results: list[tuple[str, str]] = []
    if all(request_item.segment_id in by_id for request_item in requests):
        for request_item in requests:
            ordered_results.append((request_item.segment_id, by_id[request_item.segment_id]))
        return ordered_results

    if len(results) == len(requests):
        for request_item, result_item in zip(requests, results):
            ordered_results.append((request_item.segment_id, result_item[1]))
        return ordered_results

    missing_ids = [request_item.segment_id for request_item in requests if request_item.segment_id not in by_id]
    raise TranslationProviderError(f"The model response was missing translations for: {', '.join(missing_ids)}")


def parse_translation_response(
    response_text: str,
    requests: list[TranslationRequest],
) -> list[TranslationResult]:
    results = _collect_segment_text_results(
        response_text,
        requests,
        collection_key="translations",
        value_keys=("translated_text", "text", "translation"),
    )
    return [
        TranslationResult(
            segment_id=segment_id,
            translated_text=translated_text,
            raw_response=response_text,
        )
        for segment_id, translated_text in results
    ]


def parse_proofread_response(
    response_text: str,
    requests: list[TranslationRequest],
) -> list[CorrectionResult]:
    results = _collect_segment_text_results(
        response_text,
        requests,
        collection_key="corrections",
        value_keys=("corrected_text", "source_text", "text"),
    )
    return [
        CorrectionResult(
            segment_id=segment_id,
            corrected_text=corrected_text,
            raw_response=response_text,
        )
        for segment_id, corrected_text in results
    ]


class OpenAICompatibleTranslationProvider(BaseTranslationProvider):
    provider_name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        system_prompt: str,
        timeout_sec: int = 60,
        max_retries: int = 2,
        proxy_enabled: bool = False,
        http_proxy: str = "",
        https_proxy: str = "",
        no_proxy: str = "",
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.proxy_enabled = proxy_enabled
        self.http_proxy = http_proxy
        self.https_proxy = https_proxy
        self.no_proxy = no_proxy

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "VCut-Studio/1.0",
        }
        endpoint = translation_endpoint(self.base_url)
        request_obj = request.Request(endpoint, data=body, headers=headers, method="POST")
        proxies = proxy_configuration(
            proxy_enabled=self.proxy_enabled,
            http_proxy=self.http_proxy,
            https_proxy=self.https_proxy,
            no_proxy=self.no_proxy,
            target_url=endpoint,
        )
        opener = request.build_opener(request.ProxyHandler(proxies)) if proxies is not None else None

        last_error: Exception | None = None
        for _attempt in range(self.max_retries + 1):
            try:
                if opener is None:
                    response_context = request.urlopen(request_obj, timeout=self.timeout_sec)
                else:
                    response_context = opener.open(request_obj, timeout=self.timeout_sec)
                with response_context as response:
                    text = response.read().decode("utf-8")
                    return json.loads(text)
            except error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                last_error = TranslationProviderError(
                    _friendly_http_error_message(exc.code, error_body, str(exc.reason))
                )
            except error.URLError as exc:
                if _is_timeout_reason(exc.reason):
                    last_error = TranslationProviderError(
                        _friendly_timeout_error_message(self.timeout_sec)
                    )
                else:
                    last_error = TranslationProviderError(f"无法连接翻译接口：{exc.reason}")
            except (TimeoutError, socket.timeout) as exc:
                last_error = TranslationProviderError(_friendly_timeout_error_message(self.timeout_sec))
            except OSError as exc:
                if _is_timeout_reason(exc):
                    last_error = TranslationProviderError(
                        _friendly_timeout_error_message(self.timeout_sec)
                    )
                else:
                    last_error = TranslationProviderError(f"翻译接口请求失败：{exc}")
            except json.JSONDecodeError as exc:
                last_error = TranslationProviderError(f"无法解析翻译接口返回的 JSON：{exc}")
        raise last_error or TranslationProviderError("Translation request failed.")

    def _request_model_text(self, payload: dict[str, Any]) -> str:
        try:
            response_payload = self._post(payload)
        except TranslationProviderError as exc:
            message = str(exc)
            if (
                "response_format" in payload
                and "response_format" in message.lower()
                and any(marker in message.lower() for marker in ("unsupported", "not support", "invalid"))
            ):
                fallback_payload = dict(payload)
                fallback_payload.pop("response_format", None)
                response_payload = self._post(fallback_payload)
            else:
                raise
        return extract_message_text(response_payload)

    def translate_segments(self, requests: list[TranslationRequest]) -> list[TranslationResult]:
        if not requests:
            return []
        if not self.base_url.strip():
            raise TranslationProviderError("Base URL is missing.")
        if not self.api_key.strip():
            raise TranslationProviderError("API key is missing.")
        if not self.model.strip():
            raise TranslationProviderError("Model name is missing.")

        payload = request_payload(
            model=self.model,
            system_prompt=self.system_prompt,
            requests=requests,
        )
        response_text = self._request_model_text(payload)
        return parse_translation_response(response_text, requests)

    def proofread_segments(self, requests: list[TranslationRequest]) -> list[CorrectionResult]:
        if not requests:
            return []
        if not self.base_url.strip():
            raise TranslationProviderError("Base URL is missing.")
        if not self.api_key.strip():
            raise TranslationProviderError("API key is missing.")
        if not self.model.strip():
            raise TranslationProviderError("Model name is missing.")

        payload = proofread_request_payload(
            model=self.model,
            requests=requests,
        )
        response_text = self._request_model_text(payload)
        return parse_proofread_response(response_text, requests)


def create_translation_provider(settings: TranslationSettings) -> BaseTranslationProvider:
    if settings.provider_type != "openai_compatible":
        raise TranslationProviderError(
            f"Translation provider type '{settings.provider_type}' is not supported yet."
        )
    return OpenAICompatibleTranslationProvider(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
        system_prompt=settings.system_prompt,
        timeout_sec=settings.timeout_sec,
        max_retries=settings.max_retries,
        proxy_enabled=settings.proxy_enabled,
        http_proxy=settings.http_proxy,
        https_proxy=settings.https_proxy,
        no_proxy=settings.no_proxy,
    )
