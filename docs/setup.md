# 部署与验证

## 离线验证

```bash
python examples/demo.py
python examples/run_tests.py
```

测试包含证据校验、承诺识别与任务存储、钱包并发与结算。两条命令都使用 Python 3.12，可在没有 AstrBot、QQ 和模型凭据的情况下运行。若 Windows 缺少 IANA 时区数据，先安装 `tzdata`。

## AstrBot 接入

源环境基于 AstrBot 4.29.3。先用一个独立测试实例验证 OneBot QQ 私聊能收发，并选好可用的聊天模型。插件使用 AstrBot 实例中的 `httpx`、`aiofiles` 和 Pillow。下文的路径从 AstrBot 实例中包含 `data/` 的目录算起。

### 1. 安装插件和示例人格

把发布包 `plugins/` 下的九个插件目录复制进 AstrBot 的 `data/plugins/`。然后按下表放置示例文件。缺少目标父目录时先创建。

| 发布包里的文件 | 放到 AstrBot 实例里的位置 |
| --- | --- |
| [`examples/persona.example.md`](../examples/persona.example.md) | `data/plugin_data/mengyu_contact/persona.md` |
| [`examples/contact.config.example.json`](../examples/contact.config.example.json) | `data/plugin_data/mengyu_contact/config.json`，需要调整默认行为时再复制 |

可以在 `persona.md` 用自己的虚构角色设定替换示例正文。若使用 AstrBot 人格管理，创建 ID 为 `mengyu-portfolio` 的人格，或用 `MENGYU_PERSONA_ID` 指向自定义人格 ID。插件先读取对应的人格，找不到时读取本地 `persona.md`。

### 2. 配置测试私聊

`MENGYU_OWNER_ID` 填给机器人发私聊的**测试 QQ 发送者 ID**。适配器实例 ID 为 `napcat` 时，插件默认按 `napcat:FriendMessage:<发送者 ID>` 匹配。若实例 ID 不同，就用 `MENGYU_OWNER_ORIGIN` 写完整会话来源。来源的最后一段必须和发送者 ID 一致。

Windows PowerShell 示例，先替换 `YOUR_TEST_QQ_ID` 和实际的适配器实例 ID，再从同一个终端用原来的方式启动 AstrBot。

```powershell
$env:MENGYU_OWNER_ID = 'YOUR_TEST_QQ_ID'
$env:MENGYU_OWNER_ORIGIN = "napcat:FriendMessage:$($env:MENGYU_OWNER_ID)"
$env:MENGYU_TIMEZONE = 'UTC'
```

Bash 示例

```bash
export MENGYU_OWNER_ID='YOUR_TEST_QQ_ID'
export MENGYU_OWNER_ORIGIN="napcat:FriendMessage:${MENGYU_OWNER_ID}"
export MENGYU_TIMEZONE='UTC'
```

Docker 或系统服务部署时，把同名变量写入该容器或服务的环境配置并重启，使 AstrBot 进程取得它们。如果插件拿不到当前私聊选用的模型，可用 `MENGYU_CHAT_PROVIDER` 填 AstrBot 里的**模型提供方 ID**作为备用。

### 3. 启动和确认

1. 用配置好的 QQ 账号给机器人发一条私聊，再发 `/主动联系 状态`。这条指令有返回时，说明会话已命中插件的来源和账号检查。
2. 想试主动消息时，发送 `/主动联系 开启`。关闭时用 `/主动联系 关闭`。新实例默认关闭主动联系，首次启动可在示例配置里设置 `proactive_enabled`。已有状态以私聊指令设置的开关为准。
3. 换一条测试消息触发较长回复，检查文字和附件是否实际收到了。上线前再核对适配器返回的分段成功、失败和未知回执。包内验证覆盖离线逻辑，QQ 实机结果以你的适配器和模型联调为准。

## 从全新数据开始

新部署用自己的空白 `data/plugin_data/`。任务、状态和虚拟账本会在对应插件启用后按需创建。备份旧实例时，将聊天、日志、图片、凭据和运行数据库留在实例数据目录。配置示例中的占位值只在本机副本里替换。

| 部署时可自定义的内容 | 写法 |
| --- | --- |
| 角色称呼、兴趣、说话方式 | 改本地 `persona.md` 或 AstrBot 人格。默认有三位虚构好友。想在首次启动前替换显示名，可在 `data/plugin_data/mengyu_friends/friends.json` 写 `{"friends":[{"id":"lan","name":"示例书友"}]}`。可用的示例 ID 是 `lan`、`man`、`zhou`。已有好友数据库继续使用已建入的数据。 |
| 本地时区与场景天气 | `MENGYU_TIMEZONE` 填 IANA 时区，例如 `UTC`。给虚构场景选定城市后，设 `MENGYU_WEATHER_CITY`，例如 `Helsinki`。留空时跳过天气查询。 |
| 角色周期模拟 | 默认停用。需要验证虚构日程时，可设 `MENGYU_FICTIONAL_CYCLE_START`，例如 `2020-01-01`。格式为 `YYYY-MM-DD`，模拟从所填日期开始。 |
| 角色照与场景图 | 用文字描述虚构角色或物件，例如“生成一张虚构角色在书店翻书的生活照”。生活照用 `image_mode=live`，海报或插画用 `image_mode=artwork`。生活照按活动和场景核对，发送前会检查照片类文案是否有实际附件。 |

## 可选组件

| 组件 | 所需配置 |
| --- | --- |
| 模拟钱包 | 将 [钱包示例](../plugins/mengyu_economy/config.example.json) 复制为 `data/plugin_data/mengyu_economy/config.json`。样例里的余额、留存额和五类预算都是虚构金额，单位为分。按需改值，设 `enabled` 为 `true`。两项会话字段留空可以运行模拟结算。想从聊天里查询钱包，把 `owner_id` 和 `owner_origin` 改成和测试私聊一样的值。详见[钱包说明](../plugins/mengyu_economy/README.md)。 |
| 图像与原生搜索，可单独接入 | 在本机创建 `credentials.json`，写 `{"cpa_key":"REPLACE_WITH_YOUR_SERVICE_KEY"}` 并更换占位值。`MENGYU_SERVICE_CREDENTIALS` 指向这个文件的绝对路径。`MENGYU_SERVICE_URL` 填服务根地址，例如 `http://127.0.0.1:8317`。图像需要 `/v1/images/generations` 返回 PNG 的 `data[0].b64_json`。原生搜索需要 `/v1/responses` 支持 `web_search` 并返回来源引文。按服务提供方支持的模型填 `MENGYU_IMAGE_MODEL`、`MENGYU_SEARCH_MODEL`。 |
| 语音合成 | 将 `integrations/mengyu_breeze_voice` 单独加入 `data/plugins`。在 AstrBot 的插件配置里填写自己的 `api_key`、`voice_id`，再把 `enabled` 切成 `true`。需要代理时在 `proxy` 填自己的代理地址。 |
| 音频分析 | `integrations/mengyu_audio_listener` 是 Windows 适配器。启用前在自己的 `MENGYU_AUDIO_ROOT` 下准备 `analyze.py`、`.venv/Scripts/python.exe` 与 `bin/ffmpeg.exe`。适配器传入音频路径和片段参数，并从标准输出的 `@@MENGYU_AUDIO@@` 标记后读取 JSON。参数和返回值见 [`backend.py`](../integrations/mengyu_audio_listener/backend.py)。只需要文字、图像或语音合成时，可保持音频分析未安装。 |

凭据、角色资料和运行数据放在各自的本机实例中。分享部署示例时保留占位符和合成数据。
