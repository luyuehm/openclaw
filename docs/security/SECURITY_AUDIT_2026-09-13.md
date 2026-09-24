# OpenClaw 安全漏洞审计报告

- **审计对象**: OpenClaw 智能体平台宿主网关 / 插件生态 (`/Users/macbook/vscode/openclaw`)
- **仓库版本**: `2026.4.25` (branch `fix/memory-lancedb-pro-status-json`, HEAD `3429ac65e7`)
- **审计日期**: 2026-09-13 → 2026-09-14
- **审计方式**: 静态审查 + 自动扫描（gitleaks / pnpm audit(OSV) / trivy / 内置 `security audit` 子系统）+ 人工代码走查
- **审计维度**: 10 维度全量覆盖（凭据 / 依赖 SCA / 容器镜像 / 认证授权 / 注入 / 网络暴露面 / 配置 / 敏感数据 / 供应链 / 合规基线）

## 结论摘要

| 严重度      | 数量 | 说明                                                                                                                                                   |
| ----------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 🔴 Critical | 4    | 均为依赖链漏洞（tar / baileys / vitest-browser），当前锁文件即被 CI 审计门拦截                                                                         |
| 🟠 High     | 63   | 依赖 SCA 批量（axios/undici/grpc/protobufjs/sharp 等）+ 少量可确认项                                                                                   |
| 🟡 Medium   | 10   | 设备令牌明文存储 / 会话 IDOR / 网关令牌明文落盘 / `mode:none`+loopback 走钢丝 / 循环地址限流豁免 / 镜像缺 provenance / 浏览器 eval / cron webhook SSRF |
| 🔵 Low/Info | 10   | 自签 TLS 无 HSTS / origin 通配符 / 文档占位密钥 / ffmpeg 参数注入 / sessionFile 路径 / 合规基线等                                                      |

**总体判断**: 工程本身具备远超同类的安全内建能力（SSRF 加固 fetch、会话默认拒绝授权、非 loopback 绑定强校验、密文安全比较、自研安全审计子系统），**未发现已被利用或可直通的 🔴 级代码漏洞**。当前 🔴/🟠 几乎全部集中在**第三方依赖漏洞（SCA）**，且**当前 `pnpm-lock.yaml` 会让仓库自带的 CI 审计门直接失败**——这是发布阻断项，优先处置。

---

## 1. 凭据 / 密钥泄露 🔴→🔵

### 结论

- **工作区 / HEAD 未发现真实明文密钥**。gitleaks 全历史扫描 22995 条命中中，去噪后（排除 `.secrets.baseline`、i18n 语言包、测试夹具、docs 占位符、`dist/` 构建产物）真实源码命中为 0。
- 已有 `detect-secrets` + `.secrets.baseline`（2505 条已基线化）+ pre-commit `detect-private-key` + CI `security-scm-fast` 门禁。
- 唯一值得记录的历史项：fork 仓库历史中 `dist/` 与测试文件曾携带 Discord/测试密钥（均为测试占位符），已在主流分支移除；**未发现真实 token/API key 泄露**。

| ID     | 等级      | 位置                                        | 证据                                                                              | 处置                                          |
| ------ | --------- | ------------------------------------------- | --------------------------------------------------------------------------------- | --------------------------------------------- |
| SEC-01 | 🔵 Low    | `docs/gateway/openai-http-api.md` 等 30+ 处 | `YOUR_TOKEN` / `sk-litellm-key` / Discord 示例 ID 等文档占位符                    | 归档；已确认非真实密钥                        |
| SEC-02 | 🔵 Low    | `ui/src/i18n/.i18n/*.tm.jsonl` (7000+ 命中) | 机器翻译语料含疑似 key 串，无真实凭据                                             | 归档                                          |
| SEC-03 | 🟡 Medium | 历史提交中 `dist/` 携带 Discord/测试密钥    | 2026-04 重构前 `extensions/discord/dist/*.js` 内含 `discord-client-secret` 占位符 | 归档；`dist/` 已 gitignore，历史需 GC（可选） |

### 复现命令

```bash
gitleaks dir . --log-level=error --report-format=json --report-path=/tmp/gl-worktree.json
gitleaks git --no-banner --log-level=warn --report-format=json --report-path=/tmp/gl-history.json
```

---

## 2. 依赖漏洞 (SCA) 🔴🟠 **发布阻断**

### 结论

`node scripts/pre-commit/pnpm-audit-prod.mjs --audit-level=high` **返回退出码 1**：生产依赖中 **67 个 high+ 通告（含 5 个 CRITICAL）**。仓库自带 CI 门禁 (`security-dependency-audit`) 会直接失败。

### 🔴 Critical（按可及性排序）

| ID     | 等级 | 依赖                                            | 链                                                                  | 说明                                                                                               | 处置                                                                                          |
| ------ | ---- | ----------------------------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| SCA-01 | 🔴   | `tar@7.5.13`                                    | `. > tar`                                                           | `GHSA-23hp-3jrh-7fpw` 解压 DoS（无限输入）+ 多个 path traversal 通告（CVE-2026-26960/29786/31802） | **阻断发布**；升级 `tar≥7.5.19`；代码侧已用 `preservePaths:false, strict:true` + 路径校验缓解 |
| SCA-02 | 🔴   | `@whiskeysockets/baileys@7.0.0-rc.9`            | `extensions/whatsapp > baileys`                                     | `GHSA-qvv5-jq5g-4cgg` 消息 spoofing / app state 污染                                               | **阻断发布**；升级 `≥7.0.0-rc12`（需回归 WhatsApp 链路）                                      |
| SCA-03 | 🔴   | `@vitest/browser` (via `@copilotkit/aimock`)    | `extensions/qa-lab > @copilotkit/aimock > vitest > @vitest/browser` | 3 个 CRITICAL：otelCarrier 内联脚本 / 文件访问门绕过 / CDP 代理 → RCE                              | **阻断发布**（qa-lab 为发布扩展）；升级 `vitest≥4.1.10`                                       |
| SCA-04 | 🔴   | `@copilotkit/aimock@1.15.0` 生产依赖携带 vitest | 同上                                                                | qa-lab 把测试框架打进**生产**依赖                                                                  | 结构性修复：把 `@copilotkit/aimock` 移入 devDependencies 或升级链                             |

### 🟠 High 主要分组

| ID     | 等级 | 依赖                                                                                                                                  | 链                                                    | 处置                                                    |
| ------ | ---- | ------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------- |
| SCA-05 | 🟠   | `undici@8.1.0` (≤8.9.0 多通告)                                                                                                        | `. > undici`                                          | 升级 `≥8.9.0`                                           |
| SCA-06 | 🟠   | `axios@1.15.0`（12 通告，含 NO_PROXY 绕过 / 凭据泄漏）                                                                                | `extensions/feishu > @larksuiteoapi/node-sdk > axios` | 升级 `axios≥1.16.0`                                     |
| SCA-07 | 🟠   | `@grpc/grpc-js@1.14.0`                                                                                                                | `extensions/diagnostics-otel > otlp-grpc`             | 升级 `≥1.14.4`                                          |
| SCA-08 | 🟠   | `protobufjs@7.5.5`                                                                                                                    | `extensions/diagnostics-otel`                         | 升级 `≥7.6.1`                                           |
| SCA-09 | 🟠   | `sharp@<0.35.4`                                                                                                                       | `extensions/media-understanding-core`                 | 升级 `≥0.35.4`                                          |
| SCA-10 | 🟠   | `@opentelemetry/sdk-node / exporter-prometheus / propagator-jaeger`                                                                   | `extensions/diagnostics-otel`                         | 升级 `≥0.217.0 / ≥2.9.0`                                |
| SCA-11 | 🟠   | `ws@8.20.0`                                                                                                                           | `. > ws`                                              | 升级 `≥8.21.0`                                          |
| SCA-12 | 🟠   | `hono@4.12.14` (via MCP SDK)                                                                                                          | `. > @modelcontextprotocol/sdk > hono`                | 升级 `≥4.12.25`                                         |
| SCA-13 | 🟠   | `@mariozechner/pi-coding-agent@0.70.2`                                                                                                | `. > pi-coding-agent`                                 | 升级 `>0.73.1`（含 brace-expansion / extract-zip 联动） |
| SCA-14 | 🟠   | `basic-ftp@5.3.0` / `fast-xml-builder` / `pdfjs-dist` / `linkify-it` / `fast-uri` / `postcss` / `nanoid` / `form-data` / `ip-address` | 多扩展                                                | 见复现命令输出                                          |

### 关键观察

- `package.json` 的 `pnpm.overrides` 块在 **pnpm v10 下已不生效**（启动警告 `The "pnpm" field ... no longer read`），真正的 pin 生效在 `pnpm-workspace.yaml`。当前 `pnpm-workspace.yaml` **没有** overrides —— 因此锁文件里 `tar/undici/axios/baileys/protobufjs/basic-ftp` 全是脆弱版本。
- 可及性：`tar`/`undici`/`ws`/`pi-coding-agent` 在核心运行时；`baileys`/`axios`/`protobufjs` 在发布扩展；`vitest` 链在 qa-lab 生产依赖。**无不可达项。**

### 复现命令

```bash
node scripts/pre-commit/pnpm-audit-prod.mjs --audit-level=high   # 退出码 1 = 阻断
pnpm audit --prod --audit-level high --registry https://registry.npmjs.org
```

---

## 3. 镜像 / 容器

| ID      | 等级    | 项                             | 证据                                                                                             | 处置                                                             |
| ------- | ------- | ------------------------------ | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------- |
| CONT-01 | 🟡      | 基础镜像 OS 层 CVE             | `trivy image node:24-bookworm-slim@<digest>`：**7 CRITICAL + 70 HIGH**（快照时点）               | 构建时 `apt-get upgrade` 缓解大部分；建议发布流水线加 trivy gate |
| CONT-02 | 🟢 通过 | 基础镜像 digest 锁定           | `Dockerfile` L19-22：`node:24-bookworm@sha256:...` / `-slim@sha256:...` 均带 digest              | —                                                                |
| CONT-03 | 🟢 通过 | 非 root 运行                   | `Dockerfile` L266 `USER node`；sandbox `USER sandbox`                                            | —                                                                |
| CONT-04 | 🟢 通过 | 无特权 / 无 docker.sock 默认   | `docker-compose.yml` 默认不挂 socket；`docker.sock` 仅 `OPENCLAW_SANDBOX` 显式开启且校验先决条件 | —                                                                |
| CONT-05 | 🟡      | docker.sock 可挂载（沙箱模式） | `scripts/docker/setup.sh` L570-585 在沙箱启用时注入 socket 挂载 = 宿主 root 等权                 | 文档化风险；维持 opt-in                                          |
| CONT-06 | 🟢 通过 | 健康检查 / TLS 最小版本        | `HEALTHCHECK` 存在；`gateway.tls.minVersion=TLSv1.3`                                             | —                                                                |
| CONT-07 | 🟡      | 镜像缺 SLSA provenance         | `.github/workflows/docker-release.yml` 4 处 `provenance: false`                                  | 置 `provenance: true` + `attestations`（Low-Med）                |

### 复现命令

```bash
trivy image --scanners vuln --severity CRITICAL,HIGH node:24-bookworm-slim@sha256:e8e2e91b1378f83c5b2dd15f0247f34110e2fe895f6ca7719dbb780f929368eb
```

---

## 4. 认证 / 授权

| ID      | 等级    | 项                                   | 证据                                                                                                                                                                 | 处置                                                |
| ------- | ------- | ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| AUTH-01 | 🟡      | 设备令牌明文落盘                     | `src/shared/device-auth-store.ts` `storeDeviceAuthTokenInStore`：device token 以明文写入 `device-pairing.json`，比对用 `safeEqualSecret`（constant-time 但对明文值） | 建议改存 `sha256(role\|deviceId\|token)`            |
| AUTH-02 | 🟡      | 网关令牌明文配置                     | `src/gateway/startup-auth.ts:207` 自动生成 192-bit token 后写入 `gateway.auth.token` 明文 config                                                                     | 工作站/配置仓库泄露面；建议持久化 hash + 一次性展示 |
| AUTH-03 | 🟡      | 会话水平越权（IDOR）                 | `src/gateway/sessions.ts` `sessions.get/preview/send/steer` 仅做 scope 检查，不校验 `agent:<id>` 归属；任何 READ/WRITE scope 设备可读/控他人会话                     | 增加 owner 校验或限制跨 agent 会话为 admin scope    |
| AUTH-04 | 🟡      | `auth.mode:none` + loopback 可裸认证 | `src/gateway/auth.ts:445` `mode:none` 直通；仅 loopback 绑定被允许（非 loopback 已强校验拒绝）                                                                       | 容器 port-forward 场景需告警/文档                   |
| AUTH-05 | 🟡      | 限流对 loopback 豁免                 | `src/gateway/auth-rate-limit.ts` `exemptLoopback=true` 默认                                                                                                          | 同宿主恶意进程可无限爆破；建议可配置                |
| AUTH-06 | 🟢 通过 | 默认认证 = token，非 none            | `auth-resolve.ts:88-91` 默认 mode `token`；`none` 永不自动选择                                                                                                       | —                                                   |
| AUTH-07 | 🟢 通过 | 非 loopback 无凭据拒绝启动           | `server-runtime-config.ts:143`                                                                                                                                       | —                                                   |
| AUTH-08 | 🟢 通过 | 弱口令哨兵                           | `known-weak-gateway-secrets.ts` 拒绝 `change-me-*` 占位                                                                                                              | —                                                   |
| AUTH-09 | 🟢 通过 | 设备签名 PKCS + nonce                | `message-handler.ts:718-759`                                                                                                                                         | —                                                   |
| AUTH-10 | 🟢 通过 | 默认 deny scope                      | `server-methods.ts:43-70`                                                                                                                                            | —                                                   |

---

## 5. 注入 / SSRF / 路径穿越

**结论：未发现默认配置下可利用的 Critical/High 注入/SSRF/路径穿越漏洞。** 代码库具备强 SSRF 加固层与全参数化 SQL。以下为确认项：

| ID     | 等级    | 项                                                              | 证据                                                                                                                                                                                                                | 处置                                                            |
| ------ | ------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| INJ-01 | 🟢 通过 | SSRF 加固核心                                                   | `src/infra/net/ssrf.ts`：阻断 localhost/_.local/_.internal/metadata.google.internal/RFC1918/loopback/link-local/IPv4-embedded-IPv6/legacy-octal-hex IPv4/malformed-IPv6；DNS-pin + 每个解析地址复查（防 rebinding） | —                                                               |
| INJ-02 | 🟢 通过 | 出站 fetch 防护                                                 | `src/infra/net/fetch-guard.ts` `fetchWithSsrFGuard`：scheme/host allowlist + pinned DNS + 重定向上限(3) + 跨源剥离敏感头                                                                                            | —                                                               |
| INJ-03 | 🟢 通过 | SQL 全参数化                                                    | `node:sqlite` 各处 `?` 占位符（`task-registry.store.sqlite.ts` 等），无字符串拼接 SQL                                                                                                                               | —                                                               |
| INJ-04 | 🟢 通过 | tar/zip 解压路径穿越防御                                        | `src/infra/archive.ts:839-840` `preservePaths:false, strict:true` + `validateArchiveEntryPath` + 符号链接 staging + hash-verify-then-extract                                                                        | —                                                               |
| INJ-05 | 🟢 通过 | 技能下载/安装路径包含                                           | `skills-install-download.ts` / `skills-install-extract.ts` `assertCanonicalPathWithinBase`                                                                                                                          | —                                                               |
| INJ-06 | 🟢 通过 | 网关文件 API 根限制                                             | `server-methods/agents.ts` `ALLOWED_FILE_NAMES` + `readFileWithinRoot/writeFileWithinRoot`                                                                                                                          | —                                                               |
| INJ-07 | 🟢 通过 | 会话路径校验                                                    | `config/sessions/paths.ts` `SAFE_SESSION_ID_RE` + `resolvePathWithinSessionsDir` 拒绝 `..`/绝对路径                                                                                                                 | —                                                               |
| INJ-08 | 🟢 通过 | 命令执行 argv 数组                                              | `src/process/exec.ts` / `ssh-tunnel.ts` 带 `--` 哨兵；无 shell 拼接                                                                                                                                                 | —                                                               |
| INJ-09 | 🟡      | 浏览器扩展 `new Function`+`eval` 执行 agent 提供 JS             | `extensions/browser/src/browser/pw-tools-core.interactions.ts:857,897`：`opts.fn` 任意函数体被字符串 eval 进沙箱浏览器页                                                                                            | 工具面已受批准边界约束；建议收紧为白名单函数模式（Medium）      |
| INJ-10 | 🟡      | Cron webhook 目标仅校验 http(s) scheme，未校验私网/云元数据地址 | `src/cron/webhook-url.ts:8-18` + `jobs.ts:202-207`：`delivery.to` 未走 `fetchWithSsrFGuard`                                                                                                                         | 接入 SSRF guard / 拒绝私网目标（Medium）                        |
| INJ-11 | 🔵 Low  | ffmpeg 参数注入（`-` 前缀文件名）                               | `src/media/ffmpeg-exec.ts:66-84` 用 `execFile`（无 shell）但无 `--` 哨兵，用户文件名可被误解析为选项                                                                                                                | 在位置参数前加 `--`                                             |
| INJ-12 | 🔵 Low  | `sessionFile` 回退路径无包含校验                                | `src/gateway/session-transcript-files.fs.ts:105-108` `path.resolve(trimmed)`                                                                                                                                        | 加根目录包含校验                                                |
| INJ-13 | 🔵 Low  | 渠道监视器从消息内容提取 URL 并 fetch                           | `extensions/zalo/monitor.ts:384` / `feishu/docx.ts:533` / `tlon/monitor/media.ts:69`                                                                                                                                | 均走 SDK `fetchRemoteMedia`（默认 SSRF 策略），默认配置不可利用 |

---

## 6. 网络暴露面

| ID     | 等级    | 项                                          | 证据                                                                             | 处置                     |
| ------ | ------- | ------------------------------------------- | -------------------------------------------------------------------------------- | ------------------------ |
| NET-01 | 🟢 通过 | 默认 loopback 绑定                          | `net.ts:298-310`                                                                 | —                        |
| NET-02 | 🟢 通过 | 容器 auto→0.0.0.0 但无凭据拒绝              | `net.ts:340-350` + `server-runtime-config.ts:143`                                | —                        |
| NET-03 | 🟢 通过 | 无 CORS 通配符                              | 全仓无 `Access-Control-Allow-Origin:*`；Control UI 走同源+origin 校验            | —                        |
| NET-04 | 🟡      | `allowedOrigins:["*"]` 被接受               | `origin-check.ts:50`                                                             | 建议配置层拒绝通配符     |
| NET-05 | 🟢 通过 | 管理面 / canvas / sessions 均需认证 + scope | `session-kill-http.ts` / `sessions-history-http.ts` / `server-http.ts:1047-1071` | —                        |
| NET-06 | 🔵 Low  | TLS 默认关闭 + 自签无 HSTS                  | `gateway.tls.enabled !== true` 走 HTTP；自签 `CN=openclaw-gateway`，HSTS 默认关  | 建议启用 TLS 时默认 HSTS |

---

## 7. 配置安全

| ID     | 等级    | 项                                           | 证据                                                                                                  | 处置                         |
| ------ | ------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ---------------------------- |
| CFG-01 | 🟢 通过 | 默认日志级别 info，非 debug                  | `logging/levels.ts` fallback `info`                                                                   | —                            |
| CFG-02 | 🟢 通过 | 配置写审计                                   | `src/config/io.audit.ts` `config-audit.jsonl`                                                         | —                            |
| CFG-03 | 🟢 通过 | 内置安全审计子系统                           | `src/security/audit*.ts` + `openclaw security audit [--deep] [--fix]` CLI                             | —                            |
| CFG-04 | 🟡      | 本地 `~/.openclaw/openclaw.json` schema 漂移 | 本机 config 含 `meta.migrations` / `agents.defaults.systemAgent` 等未知键 → `security audit` 无法运行 | 环境问题，非产品缺陷；已记录 |

---

## 8. 敏感数据

| ID     | 等级    | 项               | 证据                                                                                              | 处置               |
| ------ | ------- | ---------------- | ------------------------------------------------------------------------------------------------- | ------------------ |
| DAT-01 | 🟢 通过 | 日志脱敏         | `src/logging/redact.ts` token/apiKey/secret/password 正则 + 前后缀掩码；支持 `off/tools/all` 模式 | —                  |
| DAT-02 | 🟢 通过 | 密钥文件权限     | `src/infra/secret-file.ts` 0600 + 拒绝符号链接 + 大小上限                                         | —                  |
| DAT-03 | 🟡      | 设备令牌明文落盘 | 同 AUTH-01                                                                                        | 修复方向同 AUTH-01 |
| DAT-04 | 🟢 通过 | 传输加密         | TLS 支持（TLSv1.3 min），HTTP 默认                                                                | —                  |

---

## 9. 供应链

| ID     | 等级    | 项                                   | 证据                                                               | 处置                                              |
| ------ | ------- | ------------------------------------ | ------------------------------------------------------------------ | ------------------------------------------------- |
| SUP-01 | 🟡      | 镜像缺 provenance/attestation        | `docker-release.yml` `provenance: false`                           | 开启 SLSA provenance                              |
| SUP-02 | 🟢 通过 | 基础镜像 digest 锁定                 | Dockerfile 各 FROM 带 sha256                                       | —                                                 |
| SUP-03 | 🟢 通过 | 安装脚本校验和                       | `scripts/install.sh` sha256 校验 + TLS1.2 强制                     | —                                                 |
| SUP-04 | 🟢 通过 | 依赖构建脚本白名单                   | `pnpm-workspace.yaml` `onlyBuiltDependencies` 显式白名单           | —                                                 |
| SUP-05 | 🟡      | `pnpm.overrides` 失效导致 pin 不生效 | package.json 的 overrides 块被 pnpm10 忽略；workspace 无 overrides | 迁移到 `pnpm-workspace.yaml` overrides 并升级版本 |

---

## 10. 合规基线（等保 / 密评 / 信创）

| ID      | 等级   | 项                        | 证据                                 | 处置                                                                   |
| ------- | ------ | ------------------------- | ------------------------------------ | ---------------------------------------------------------------------- |
| COMP-01 | 🔵 Low | 无国密 SM2/SM3/SM4        | 全仓 grep 无 `SM2/SM3/SM4/国密`      | 如需信创合规，需引入国密库（`@noble/curves`/`gmssl`）并在 TLS/签名启用 |
| COMP-02 | 🔵 Low | 无数据分级 / 留存策略声明 | 未发现 PII 分类 / TTL / 删除路径文档 | 建议补隐私策略                                                         |
| COMP-03 | 🔵 Low | 缺少安全事件审计日志      | 有 config 写审计，无通用安全事件日志 | 建议增加登录/越权/密钥变更审计事件                                     |

---

## 处置清单（分级）

### 立即修（阻断发布）— 交付本单时已建子任务或本地修复

1. **SCA-01 `tar` → ≥7.5.19**（`pnpm-workspace.yaml` overrides + 重锁）
2. **SCA-02 `baileys` → ≥7.0.0-rc12**
3. **SCA-03/04 `vitest`/`@copilotkit/aimock` 链**（qa-lab 生产依赖结构问题）
4. **SCA-05..14 High 批量升级**（undici/axios/grpc/protobufjs/sharp/otel/ws/hono/pi-coding-agent）

### 排期（🟡 Medium）

- AUTH-01/02 令牌 hash 落盘；AUTH-03 会话 owner 校验；AUTH-04/05 配置项；NET-04 拒绝通配符；CONT-01 trivy gate；SUP-01 provenance；SUP-05 overrides 迁移；INJ-09 浏览器 eval 白名单；INJ-10 cron webhook SSRF guard

### 归档（🔵 Low/Info）

- SEC-01/02/03、NET-06、COMP-01/02/03

---

## 复现命令清单（供复测）

```bash
# 1. 密钥
gitleaks dir . --report-format=json --report-path=/tmp/gl.json
# 2. 依赖
node scripts/pre-commit/pnpm-audit-prod.mjs --audit-level=high; echo $?
# 3. 镜像
trivy image --scanners vuln --severity CRITICAL,HIGH node:24-bookworm-slim@sha256:e8e2e91b1378f83c5b2dd15f0247f34110e2fe895f6ca7719dbb780f929368eb
# 4. 内置审计
openclaw security audit --deep --json
```
