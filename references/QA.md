# 验证记录

验证日期：2026-11-10。环境：macOS arm64，Python 3.12。运行时仅使用 Python 标准库。技能格式检查使用 skill-creator 的校验器及其 PyYAML 依赖，PyYAML 不属于本技能运行依赖。

## 可重复的离线验证

在仓库目录运行：

```sh
python3 -B scripts/test_provider_images.py -v
python3 scripts/provider_images.py generate --help
```

29 项测试通过；所有测试使用临时配置、虚构密钥、内存图片和模拟网络，没有真实模型请求。

| 范围 | 验证结果 |
| --- | --- |
| TOML、provider、profile、URL 前缀 | 解析正常，保留已有接口路径前缀 |
| 认证 | auth.json 优先；401/403 后回退当前 provider 认证；重复密钥去重；OAuth 不作为 API Key |
| 模型 | 默认 gpt-image-2；显式指定保持精确 ID；平时隐藏列表；缺失或聊天模型不发生成请求 |
| 生成 | 一次配置读取、正常认证下一次模型 GET 和一次生成 POST；n=1；不发送 quality/complexity |
| 地址与文件保护 | 显式 expected-base 不匹配及输出存在时停止，不覆盖文件 |
| 图片获取 | Base64 解码与无认证 URL 下载；失败保留恢复文件，恢复不重新生图 |
| 中断 | 超时、无效响应、用户中断留下可解析的 pending 记录；fetch 明确拒绝误当成图片结果 |
| 错误 | 保留 HTTP 状态、错误字段及请求 ID；密钥及签名 URL 脱敏；非 JSON 原始正文不输出 |
| 网络与文件 | API 禁止重定向；图片下载不携带认证且拒绝私网；图片与恢复文件权限 600 |

技能名称、frontmatter、引用文件及 Python 语法检查通过。使用说明、UI 元数据、脚本和测试的默认模型一致。

## 真实生成证据

开发期间在当时配置的 HTTPS 服务使用 `gpt-image-2`，仅提交 model、prompt、n=1，未传 size、quality、complexity，生成写实橘猫成功。结果通过图片查看工具检查后，才把该模型设为默认值。

另一个模型的参数兼容测试未通过，不能据此推断全部模型可用。本仓库不包含个人配置、实际认证、服务域名、签名 URL、历史请求 ID、本机绝对路径或生成结果；以上记录仅说明验证边界。

## 未覆盖与限制

- 未验证所有服务、账户、模型、尺寸、价格或实际扣费；不把离线测试当成新的真实生成成功。
- 仅支持同步 `/models` 和 `/images/generations`，结果形状为 `data[0].b64_json` 或 `data[0].url`。
- 配置读取不代表已经识别运行中 Codex 的启动覆盖、托管配置或所有认证存储；profile 使用本地 TOML 的 profiles 表。
- 模型 ID 与输出能力筛选可能漏掉命名或元数据特殊的图片模型；无候选时不会猜测模型发起收费试用。
- 不支持新图生成之外的编辑、异步轮询、图片下载重定向和非 PNG/JPEG/WebP 输出。
- 本次发布只复核离线测试，没有额外执行收费生成，也没有页面测试；当前没有配置远程 CI。
