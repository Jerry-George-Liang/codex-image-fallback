#!/usr/bin/env python3
"""Discover image models and make one explicit image request using Codex settings."""

import argparse
import base64
import binascii
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request


MAX_BYTES = 128 * 1024 * 1024
DEFAULT_MODEL = "gpt-image-2"
IMAGE_ID = re.compile(
    r"gpt-image|dall-e|seedream|flux|imagen|ideogram|stable[-_]diffusion|"
    r"sdxl|qwen[-_]image|hunyuan[-_]image|gemini.*image", re.I
)


class Failure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_error_report(exc, secrets=()):
    """Keep actionable fields without logging response bodies or signed URLs."""
    def clean(value):
        text = str(value)
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"https?://[^\s\"'<>]+", "[URL omitted]", text)
        text = re.sub(r"(?i)bearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", text)
        return text[:1500]

    report = {"http_status": exc.code}
    headers = exc.headers or {}
    request_id = headers.get("x-request-id") or headers.get("request-id")
    if request_id:
        report["request_id"] = clean(request_id)
    try:
        data = json.loads(exc.read(16384))
        error = data.get("error", data) if isinstance(data, dict) else None
        if isinstance(error, dict):
            report["error"] = {k: clean(error[k]) for k in
                               ("code", "type", "message", "param", "request_id")
                               if isinstance(error.get(k), (str, int, float))}
        elif isinstance(error, str):
            report["error"] = {"message": clean(error)}
    except (OSError, ValueError):
        pass
    if not report.get("error"):
        report["error"] = {"message": "服务端未返回可解析的错误字段；原始正文已省略"}
    return report


def https_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
        raise Failure("只支持无用户名、密码及片段的 HTTPS URL")
    return p


def load_config(home, profile=None):
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        profile = profile or config.get("profile")
        selected = config.get("profiles", {}).get(profile, {}) if profile else {}
        if profile and not selected:
            raise Failure("指定的配置 profile 不存在或为空")
        provider = selected.get("model_provider", config.get("model_provider", "openai"))
        settings = config.get("model_providers", {}).get(provider, {})
        base = settings.get("base_url", "").rstrip("/")
        if not base:
            raise Failure("当前 provider 未配置 base_url；不会猜测地址")
        if https_url(base).query:
            raise Failure("base_url 不能包含查询参数")
        credentials = []
        notes = []
        auth_path = home / "auth.json"
        if auth_path.exists():
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
            key = auth.get("OPENAI_API_KEY")
            if isinstance(key, str) and key.strip():
                credentials.append(("auth.json:OPENAI_API_KEY", key.strip()))
            else:
                notes.append("auth.json 没有 API Key；OAuth tokens 不作为 API Key")
        else:
            notes.append("auth.json 不存在")
        env_name = settings.get("env_key")
        if env_name and os.environ.get(env_name, "").strip():
            credentials.append(("config.toml:env_key=" + env_name, os.environ[env_name].strip()))
        token = settings.get("experimental_bearer_token")
        if isinstance(token, str) and token.strip():
            credentials.append(("config.toml:experimental_bearer_token", token.strip()))
        unique = []
        for source, key in credentials:
            if key not in [item[1] for item in unique]:
                unique.append((source, key))
        if not unique:
            raise Failure("auth.json 和当前 provider 均没有可用 API 认证；请在本地配置，勿在聊天粘贴密钥")
        return {"base_url": base, "provider": provider, "profile": profile,
                "credentials": unique, "notes": notes}
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        raise Failure("无法解析 Codex 配置或认证文件；未输出文件内容") from None


def read_limited(response):
    content = response.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise Failure("响应超过 128 MiB，已停止读取")
    return content


def request_json(url, key, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    # No SDK retries, cookies, redirects, or ambient authentication handlers.
    with urllib.request.build_opener(NoRedirect).open(request, timeout=180) as response:
        try:
            return json.loads(read_limited(response))
        except ValueError:
            raise Failure("接口未返回有效 JSON，可能是网页或网关错误") from None


def model_items(data):
    items = data.get("data", data.get("models")) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise Failure("模型接口响应不是模型列表")
    result = []
    for item in items:
        if isinstance(item, str):
            item = {"id": item}
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            result.append(item)
    if items and not result:
        raise Failure("模型列表中没有可识别的模型 ID")
    return result


def image_candidate(item):
    if IMAGE_ID.search(item["id"]):
        return True
    for field in ("output_modalities", "supported_output_modalities", "capabilities"):
        value = item.get(field, [])
        if isinstance(value, dict):
            value = [k for k, enabled in value.items() if enabled]
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list) and any(v in ("image", "images", "image_generation") for v in value):
            return True
    return False


def discover(config):
    failures = list(config["notes"])
    for source, key in config["credentials"]:
        try:
            items = model_items(request_json(config["base_url"] + "/models", key))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                failures.append(source + ": HTTP " + str(exc.code))
                exc.close()
                continue
            raise
        return {"base_url": config["base_url"], "provider": config["provider"],
                "credential_source": source, "auth_notes": failures,
                "model_count": len(items), "models": [i["id"] for i in items],
                "image_candidates": [i["id"] for i in items if image_candidate(i)],
                "generation_verified": False}, key
    raise Failure("全部配置认证均返回 401/403；不是模型不存在，也不能靠换模型解决。" + "；".join(failures))


def discovery_report(report, model=DEFAULT_MODEL, list_models=False):
    if list_models:
        return report
    public = {k: report[k] for k in ("base_url", "provider", "credential_source", "auth_notes")}
    public.update({"model": model, "model_available": model in report["image_candidates"],
                   "generation_verified": False})
    return public


def download_image(url):
    p = https_url(url)
    # Reject private destinations; downloads never receive API keys or cookies.
    try:
        addresses = socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        raise Failure("图片下载域名无法解析；可从恢复文件重试获取") from None
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise Failure("图片下载地址不是公网地址")
    with urllib.request.build_opener(NoRedirect).open(url, timeout=90) as response:
        return read_limited(response)


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def fetch_result(result_file, out):
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if isinstance(result, dict) and result.get("kind") == "generation_pending":
        raise Failure("生成结果尚未确认，文件中没有图片；先核对服务端请求记录，不会重新生成")
    if isinstance(result, dict) and result.get("kind") == "generation_http_error":
        raise Failure("该文件是失败请求的诊断记录，没有可下载图片；不会重新生成")
    items = result.get("data") if isinstance(result, dict) else None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise Failure("响应未提供唯一 data 图片；保留恢复文件，不重新生成")
    item = items[0]
    if isinstance(item.get("b64_json"), str) and item["b64_json"]:
        try:
            data = base64.b64decode(item["b64_json"], validate=True)
        except (ValueError, binascii.Error):
            raise Failure("图片 Base64 无效；保留恢复文件") from None
    elif isinstance(item.get("url"), str):
        data = download_image(item["url"])
    else:
        raise Failure("响应没有 b64_json 或 url；保留恢复文件")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise Failure("获取内容不是可识别的 PNG/JPEG/WebP；保留恢复文件")
    actual = out.with_suffix(suffix)
    actual.parent.mkdir(parents=True, exist_ok=True)
    write_private(actual, data)
    result_file.unlink()
    return str(actual)


def generate(config, args):
    if args.expected_base and config["base_url"] != args.expected_base.rstrip("/"):
        raise Failure("服务地址与模型发现阶段不同，请重新发现模型并确认；未发送生成请求")
    if args.out.exists() or args.out.with_suffix(".response.json").exists():
        raise Failure("输出或恢复文件已存在；未发送生成请求，请使用新文件名")
    if any(args.out.with_suffix(ext).exists() for ext in (".png", ".jpg", ".webp")):
        raise Failure("同名图片已存在；未发送生成请求")
    prompt = args.prompt_file.read_text(encoding="utf-8").strip()
    if not prompt:
        raise Failure("提示词为空；未发送生成请求")
    report, key = discover(config)
    if args.model not in report["image_candidates"]:
        raise Failure("所用模型 " + args.model + " 不在当前生图候选目录中；未发送生成请求。请指定其他模型，或要求查看可用模型。")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    recovery = args.out.with_suffix(".response.json")
    # A crash or lost response must leave a readable marker, not an empty image result.
    pending = {"kind": "generation_pending", "model": args.model}
    write_private(recovery, json.dumps(pending).encode())
    payload = {"model": args.model, "prompt": prompt, "n": 1}
    if args.size:
        payload["size"] = args.size
    public = {k: report[k] for k in ("base_url", "credential_source", "auth_notes")}
    public.update({"model": args.model, "recovery_file": str(recovery)})
    print(json.dumps({"generation_starting": public}, ensure_ascii=False), flush=True)
    try:
        result = request_json(config["base_url"] + "/images/generations", key, payload)
    except urllib.error.HTTPError as exc:
        diagnostic = http_error_report(exc, [secret for _, secret in config["credentials"]])
        diagnostic.update({"kind": "generation_http_error", "model": args.model})
        with recovery.open("wb") as f:
            f.write(json.dumps(diagnostic, ensure_ascii=False).encode())
        exc.diagnostic_saved = True
        print(json.dumps({"generation_error": diagnostic, "diagnostic_file": str(recovery)},
                         ensure_ascii=False), file=sys.stderr)
        print("生成请求失败；不会自动重试。请根据错误详情及请求 ID 核对服务端记录。", file=sys.stderr)
        raise
    except Exception:
        print("生成结果未知或失败；不会自动重试 POST。请先核对服务端请求记录。", file=sys.stderr)
        raise
    encoded = json.dumps(result).encode()
    if any(secret.encode() in encoded for _, secret in config["credentials"]):
        raise Failure("响应含有认证密钥，拒绝落盘；请核对服务端记录，不重新生成")
    with recovery.open("wb") as f:
        f.write(encoded)
    public["output"] = fetch_result(recovery, args.out)
    public.pop("recovery_file")
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser())
    parser.add_argument("--profile")
    sub = parser.add_subparsers(dest="command", required=True)
    discovery = sub.add_parser("discover")
    discovery.add_argument("--model", default=DEFAULT_MODEL)
    discovery.add_argument("--list-models", action="store_true")
    gen = sub.add_parser("generate")
    gen.add_argument("--model", default=DEFAULT_MODEL)
    gen.add_argument("--prompt-file", type=Path, required=True)
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--expected-base", help="Optional service guard after a separate model discovery")
    gen.add_argument("--size")
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--result-file", type=Path, required=True)
    fetch.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    secrets = []
    try:
        if args.command == "fetch":
            result = {"output": fetch_result(args.result_file, args.out)}
        else:
            config = load_config(args.codex_home, args.profile)
            secrets = [secret for _, secret in config["credentials"]]
            if args.command == "discover":
                result = discovery_report(discover(config)[0], args.model, args.list_models)
            else:
                result = generate(config, args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except urllib.error.HTTPError as exc:
        if not getattr(exc, "diagnostic_saved", False):
            print(json.dumps(http_error_report(exc, secrets), ensure_ascii=False), file=sys.stderr)
        exc.close()
    except Failure as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError, TypeError, KeyError):
        print("文件、响应解析或网络错误；未输出密钥、URL 参数或响应内容。生成请求不会自动重试。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
