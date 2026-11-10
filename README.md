# Codex Image Fallback

在 Codex 没有提供内置 `image_gen` 工具，或提示需要另配 `OPENAI_API_KEY` 时，复用本地 Codex 的服务地址和 API 认证生成图片。

- 默认使用 `gpt-image-2`，用户指定其他模型时按指定值执行。
- 平时不展示模型列表、不要求选择；只有用户要求时才列出。
- 一条命令完成配置读取、模型检查、单次生成和图片保存。
- 不发送 `quality`、`complexity`，支持用户指定 `size`。
- 失败保留脱敏诊断；结果未知或下载失败时不自动重复生图。

## 安装与使用

需要 Python 3.11 或更高版本，运行和测试只使用标准库，无需安装 Python 依赖。

首次安装到尚不存在的技能目录：

```sh
git clone https://github.com/Jerry-George-Liang/codex-image-fallback.git ~/.codex/skills/codex-image-fallback
```

在能发现该技能的 Codex 会话中输入：

```text
使用 $codex-image-fallback 生成一只窗边晒太阳的橘猫，写实风格。
```

只查看候选模型：

```text
使用 $codex-image-fallback 查看当前服务有哪些生图模型，不要生成图片。
```

## 配置来源

默认读取 `$CODEX_HOME`，未设置时读取 `~/.codex`：

1. 从 `config.toml` 当前 `model_provider` 对应配置读取 `base_url`，保留 `/v1`、`/coding` 等已有前缀。
2. 优先使用 `auth.json` 的 `OPENAI_API_KEY`。
3. 缺少 API Key 或模型查询返回 401/403 时，回退到该 provider 的 `env_key` 环境变量、`experimental_bearer_token`。

OAuth 登录 tokens 不当作 API Key。不会修改 Codex 配置或要求把密钥粘贴到聊天；认证只发送到用户配置的 HTTPS 地址。服务必须支持相应的 OpenAI 兼容模型列表和图片生成接口，列出模型不等于已经验证生成权限。

## 命令行

在仓库目录执行，先将图片描述写入 UTF-8 文本文件：

```sh
python3 scripts/provider_images.py generate \
  --prompt-file /absolute/path/prompt.txt \
  --out /absolute/path/result.png
```

默认一张图片。需要其他模型或尺寸时增加 `--model MODEL_ID`、`--size WIDTHxHEIGHT`。输出文件必须不重名；实际保存扩展名按 PNG、JPEG 或 WebP 内容确定。

仅查看模型列表，不生成图片：

```sh
python3 scripts/provider_images.py discover --list-models
```

如果在独立查询模型之后才生成，可使用 `--expected-base` 锁定当时的服务地址；普通生成无需先运行 discover。自定义配置目录使用全局参数，例如 `python3 scripts/provider_images.py --codex-home /absolute/codex-home generate ...`。

生成响应已返回、只有图片获取失败时，可恢复获取：

```sh
python3 scripts/provider_images.py fetch \
  --result-file /absolute/path/result.response.json \
  --out /absolute/path/result.png
```

恢复不会重新提交生成请求。`generation_pending` 表示结果尚未确认，应先查服务端请求记录；HTTP 错误诊断文件没有可下载图片。恢复文件可能包含 Base64 或签名 URL，不应发布。

## 验证与限制

```sh
python3 -B scripts/test_provider_images.py -v
```

29 项离线测试覆盖配置解析、认证回退、模型选择、单次生成、结果获取、中断状态及诊断脱敏。开发时已实测 `gpt-image-2` 不传质量或复杂度字段生成成功；这不保证其他服务、模型或账户可用。

只支持同步新图生成，不支持图片编辑或异步任务轮询。不自动重试收费 POST；HTTP 失败也不作为未扣费的证明。详情见 [技能说明](SKILL.md) 和 [QA](references/QA.md)。
