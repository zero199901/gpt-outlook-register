# 短效 Outlook 邮箱接码 + SUB2API 上传记录

## 1. 本次测试结论

本次使用 **短效 Outlook 邮箱** 完成了注册/登录链路，并通过 SUB2API 后端接口上传成功。

- 邮箱类型：`short-outlook`
- 领取邮箱：`g19w8pania@outlook.com`
- 代理：`socks5://127.0.0.1:7897`
- 出口 IP：`141.11.146.74 / HK`
- 邮箱 API 请求地址：`https://mail.no-replyca.xyz`
- 文档页：`https://email.manageh.shop/#api`
- SUB2API 面板：`https://sub.llmwc.com`
- SUB2API 导入模式：`Agent Identity`
- SUB2API 导入接口：`POST /api/v1/admin/accounts/import/codex-session`
- 上传结果：成功

---

## 2. 邮箱 API 配置

> 注意：不要把完整 API Key 写进代码仓库。这里用占位符表示。

```bash
export EMAIL_API_BASE="https://mail.no-replyca.xyz"
export EMAIL_API_KEY="YOUR_EMAIL_API_KEY"
```

### 2.1 查询余额

```bash
curl -k -sS "$EMAIL_API_BASE/api/user/quota?apikey=$EMAIL_API_KEY"
```

本次返回：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "quota_left": 500,
    "quota_total": 500,
    "quota_used": 0
  }
}
```

### 2.2 领取短效 Outlook 邮箱

```bash
curl -k -sS "$EMAIL_API_BASE/api/user/email?type=short-outlook&apikey=$EMAIL_API_KEY"
```

本次领取到：

```json
{
  "type": "short-outlook",
  "email": "g19w8pania@outlook.com",
  "cost": 1
}
```

### 2.3 查询最新邮件/验证码

```bash
EMAIL="g19w8pania@outlook.com"

curl -k -sS "$EMAIL_API_BASE/api/user/mail?email=$EMAIL&apikey=$EMAIL_API_KEY"
```

接口返回中验证码字段为：

```text
data.code
```

本次流程中：

- 第一次查到旧码：`634093`，OpenAI 返回 `wrong_email_otp_code`
- 自动重发后查到新码：`303820`，验证成功

---

## 3. 项目内新增的测试脚本

脚本路径：

```text
/Users/tbs/code/codex/注册机/gpt/gpt-outlook-register/email_api_short_outlook_test.py
```

作用：

1. 查询邮箱 API 余额
2. 领取 `short-outlook` 邮箱
3. 使用项目原有 `AuthFlow` 触发 OpenAI 邮箱 OTP
4. 轮询邮箱 API 获取验证码
5. 验证 OTP
6. 创建账户/获取凭证
7. 保存结果 JSON

运行命令：

```bash
cd /Users/tbs/code/codex/注册机/gpt/gpt-outlook-register

EMAIL_API_KEY="YOUR_EMAIL_API_KEY" \
OTP_TIMEOUT=240 \
WEBUI_ALLOW_LOGIN=1 \
python3 email_api_short_outlook_test.py \
  --type short-outlook \
  --proxy "socks5://127.0.0.1:7897"
```

本次输出结果文件：

```text
/Users/tbs/code/codex/注册机/gpt/gpt-outlook-register/email_api_result_g19w8pania.json
```

该文件是项目内部注册结果格式，不是前端手动导入用的 SUB2API 导出文件。

核心字段：

```json
{
  "email": "g19w8pania@outlook.com",
  "session_token": "...",
  "access_token": "...",
  "device_id": "32689b63-dc7a-49f5-b791-e0e68bfe7f50",
  "agent_runtime_id": "agent-ngywPNPrHLiLKBfMikIROM1j",
  "agent_private_key": "...",
  "_email_api_account": {
    "type": "short-outlook",
    "email": "g19w8pania@outlook.com",
    "cost": 1
  }
}
```

---

## 4. 关于“JSON 文件格式不对”的原因

页面提示：

```text
文件 sub2api_oauth_payload_g19w8pania.json 不是受支持的导出数据文件
文件 sub2api_import_codex_session_g19w8pania.json 不是受支持的导出数据文件
```

原因：

这些 JSON 是给 SUB2API 后端接口 POST 的 payload，不是 SUB2API 前端“导入导出数据文件”功能所识别的备份文件格式。

正确方式是调用后端接口上传，而不是在页面导入文件框里上传。

---

## 5. SUB2API 上传方式

### 5.1 配置

> 注意：不要把完整 `admin-*` API Key 写入仓库。这里用占位符。

```bash
export SUB2API_URL="https://sub.llmwc.com"
export SUB2API_KEY="YOUR_SUB2API_ADMIN_KEY"
```

### 5.2 使用项目导出函数上传

本次实际使用项目内函数：

```python
from webui import exporter

res = exporter.export_to_sub2api(cred, cfg, log_fn=log)
```

因为注册结果里包含：

```text
agent_runtime_id
agent_private_key
```

所以项目自动选择 **Agent Identity 模式**，上传接口为：

```text
POST https://sub.llmwc.com/api/v1/admin/accounts/import/codex-session
```

### 5.3 本次上传日志

```text
[SUB2API] 使用 Agent Identity 模式导入 g19w8pania@outlook.com
[SUB2API] 第 1/3 次上传 g19w8pania@outlook.com (group_ids=[2])...
[SUB2API] ✅ 上传成功 g19w8pania@outlook.com (Agent Identity, created=0,updated=0)
```

返回结果：

```json
{
  "ok": true,
  "email": "g19w8pania@outlook.com",
  "account_id": "created=0,updated=0",
  "message": "SUB2API Agent Identity 上传成功 created=0,updated=0"
}
```

---

## 6. 可复用上传命令

```bash
cd /Users/tbs/code/codex/注册机/gpt/gpt-outlook-register

SUB2API_URL="https://sub.llmwc.com" \
SUB2API_KEY="YOUR_SUB2API_ADMIN_KEY" \
python3 - <<'PY'
import json
import os
from pathlib import Path
from webui import exporter

cred_path = Path("/Users/tbs/code/codex/注册机/gpt/gpt-outlook-register/email_api_result_g19w8pania.json")
cred = json.loads(cred_path.read_text(encoding="utf-8"))

cfg = {
    "sub2api_url": os.environ["SUB2API_URL"],
    "sub2api_api_key": os.environ["SUB2API_KEY"],
    "sub2api_group_ids": "2",
    "sub2api_timeout": "60",
}

def log(msg, lvl="info"):
    print(f"[{lvl}] {msg}", flush=True)

res = exporter.export_to_sub2api(cred, cfg, log_fn=log)
print(json.dumps(res, ensure_ascii=False, indent=2))
PY
```

---

## 7. 相关文件

```text
/Users/tbs/code/codex/注册机/gpt/gpt-outlook-register/email_api_short_outlook_test.py
/Users/tbs/code/codex/注册机/gpt/gpt-outlook-register/email_api_result_g19w8pania.json
/Users/tbs/code/codex/注册机/gpt/gpt-outlook-register/sub2api_import_codex_session_g19w8pania.json
/Users/tbs/code/codex/注册机/gpt/gpt-outlook-register/sub2api_oauth_payload_g19w8pania.json
```

推荐优先使用：

```text
email_api_result_g19w8pania.json + exporter.export_to_sub2api()
```

因为它会自动判断是否走 Agent Identity 模式。

---

## 8. 后续批量化建议

后续可以把流程固定为：

1. 使用邮箱 API 领取 `short-outlook`
2. 使用代理触发注册/登录
3. 邮箱 API 轮询 `data.code`
4. OTP 验证成功后保存内部结果 JSON
5. 调用 `exporter.export_to_sub2api()` 上传到 SUB2API

批量命令模板：

```bash
EMAIL_API_KEY="YOUR_EMAIL_API_KEY" \
SUB2API_URL="https://sub.llmwc.com" \
SUB2API_KEY="YOUR_SUB2API_ADMIN_KEY" \
PROXY="socks5://127.0.0.1:7897" \
OTP_TIMEOUT=240 \
WEBUI_ALLOW_LOGIN=1 \
python3 email_api_short_outlook_test.py \
  --type short-outlook \
  --proxy "$PROXY"
```

然后对生成的 `email_api_result_*.json` 调用 SUB2API 上传函数即可。
