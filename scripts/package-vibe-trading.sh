#!/usr/bin/env bash
set -euo pipefail

# 打包并发布 Vibe-Trading 金融投研引擎为 downloadable_local 分发包（模块「金融投研与量化交易」/ P2）。
#
# 设计目标：**一个脚本、不带参数即默认构建并发布当前机器能产出的所有平台架构**，所有上传参数
# 全部放配置文件 config.yaml（默认=生产）/ config-dev.yaml（本地）。日常发布只需：
#   scripts/package-vibe-trading.sh            # 默认读同目录 config.yaml（生产）
#   scripts/package-vibe-trading.sh --dev      # 读同目录 config-dev.yaml（本地/测试环境）
#
# 范式来源：huanxing-apps/film-engine/scripts/package-film-engine.sh（同为 Python + bundled venv）。
#
# ## 包结构契约（daemon domains/finance 的 engine.rs / driver.rs 依赖，勿擅改）
#
#   vibe-trading-<os>-<arch>-<version>.zip
#   ├── venv/            # bundled standalone Python + 依赖（无 symlink，可迁移）
#   └── agent/           # 引擎本体（fork 的 agent/ 整树，剔除开发垃圾）
#
#   - 生产安装由 hasn-local-runtime-artifact 展开到不可变 Revision；`current/` 只允许旧客户端迁移读取，
#     不再是运行权威。Package Adapter 从 ActiveRevision 获取根目录后使用
#     `venv/bin/python` + `agent/mcp_server.py`。
#   - 结构闸门：包内必须有 `agent/mcp_server.py` 与 `venv/bin/python`（本脚本与 install 侧同一硬校验）。
#   - 启动方式（daemon driver.rs）：
#       cd <RevisionBinding.root> && venv/bin/python agent/mcp_server.py --transport http --host 127.0.0.1 --port <动态>
#     **必须 --transport http**：上游 stdio 无条件开启 shell 工具且 env 闸对它无效，
#     等于把 bash 交给 swarm 里的 LLM。见 03 设计文档 §2.2/§2.3「安全选项 A」与
#     `agent/tests/test_transport_shell_gate.py`（守卫测试真调 main() 钉死这条）。
#   - `file-manifest.json` 在 zip 内，逐一声明普通文件的路径、大小、SHA-256 与执行权限；
#     `manifest.json` 在包外，使用 Ed25519 签名 schema v2，覆盖 ArtifactId、平台包、发布序列、
#     有效期、展开预算与撤销材料。daemon 先验签，再选择平台包并按逐文件清单 fail closed。
#
# ## 运行期配置：全部 env 注入，包内不带 config.yaml
#
# daemon 起子进程时注入（03 §3.3/§4）：
#   HOME=<owner 数据根>                    # 引擎按 ~ 展开即落进 owner 隔离目录（零改上游）
#   OPENAI_BASE_URL=<new-api 网关>         # 计费可审计：走网关 + owner key → 积分账本
#   OPENAI_API_KEY=<owner key>
#   VIBE_TRADING_ENABLE_SHELL_TOOLS=false  # 双保险（env_schema.py 里 default 本就是 False）
# 故包内**不放** config.yaml——放了反而会与 env 抢优先级、且成为跨 owner 残留的隐患。
#
# ## 依赖装法：用 uv.lock 导出，而不是 agent/requirements.txt
#
# `uv export --frozen` 从**锁文件**出带 hash 的 requirements——这正是验收 A9 那把锁的意义
# （fork 修掉上游 `slackify-markdown>=4.4.0` 的 npm 版本号谬误后才解得开）。仓内那份
# agent/requirements.txt 无 hash、与 pyproject 无强制同步，不作打包依据。
#
# extras 取舍（打包只带引擎真用得上的，IM 渠道 / dev / 券商 SDK 一律不带）：
#   - `ashare`（baostock）：**带**。它是 a_share 回退链里唯一免 token 的额外源，
#     且差集只有 baostock 自己、零连锁。不带的话 a_share 链声明 7 源实际只剩
#     tencent/eastmoney/akshare/tushare 4 源（mootdx 按验收 A3 刻意不进依赖，httpx<0.28 冲突），
#     而 macro/fund/futures 三链已是 akshare 独苗无兜底（验收 R9）。
#   - `channels`/`slack`/`feishu`/… ：不带。唤星自有 IM，引擎不需要对外发消息。
#   - `dev`：不带。测试依赖不进分发包。
#   - `mootdx`：不带（验收 A3 决策，见 agent/tests/test_availability_probe.py 的反向守卫）。
#
# ## 用法
#   scripts/package-vibe-trading.sh [选项] [aarch64|x86_64]
#   - 不带架构参数：构建+发布**当前 OS 默认全架构**（mac=aarch64+x86_64；linux/win=本机架构）。
#   - 带一个架构参数：只构建+发布该架构（单架构调试）。
#
# ## 配置（优先级 命令行 > 环境变量 > 配置文件 > 内置默认）
#   --dev             读同目录 config-dev.yaml（本地/测试）而非默认 config.yaml（生产）。
#   --env=<name>      dev → config-dev.yaml；prod/空 → config.yaml。
#   --config=<file>   显式指定配置文件路径（亦可用环境变量 VIBE_TRADING_ENGINE_CONFIG）。
#                     **YAML 扁平 key: value、# 注释，安全逐行解析（不 source、不引 YAML 库）**。
#                     config.yaml / config-dev.yaml 均已 .gitignore（含 admin_token），模板见 config.example.yaml。
#   --targets=<list>  目标架构列表（逗号或空格分隔）。配置键 targets。留空 = 当前 OS 默认全架构。
#   --version=<v>     包版本（配置键 version；留空则读仓根 pyproject.toml 的 version）。
#   --src=<dir>       引擎 agent 源码目录（配置键 src；默认 <引擎仓根>/agent）。
#   --out=<dir>       产物输出目录（配置键 out；默认 <引擎仓根>/.engine-build/vibe-trading）。
#   --publish=<url>   云端 API 基址（配置键 publish_url）。给了即开启发布：POST 引擎包到
#                     <url>/api/v1/hasn/app-catalogs/<pk>/engine-package，落公共桶 + 写
#                     迁移期 config_json.engine；服务端**权威**算 sha256/size、与本地交叉校验。
#                     全平台包完成并签名后，再 POST 同一 catalog 的 finance-engine-release，
#                     严格核对云端回写与本地 schema-v2 清单完全一致。
#   --defer-release-publish  只上传平台包并保留本地签名清单，不发布清单。跨 OS 构建时使用；
#                            协调器汇总全部平台、重新签名后，只能发布一次最终清单。
#   --app-pk=<id>     发布必填：云端应用目录行 ID（配置键 app_pk；app_id='finance' 那行主键）。
#   --admin-token=<t> 发布必填：管理端 JWT（配置键 admin_token；或环境变量 HASN_ADMIN_TOKEN）。
#   --base-url=<u>    仅手工上传场景：manifest.url=<base>/<包名>（配置键 base_url）。
#   --release-sequence=<n>  单调发布序列（配置键 release_sequence；生产可运行包必填）。
#   --issued-at=<time>      UTC RFC3339 签发时间（配置键 issued_at；多平台构建必须一致）。
#   --expires-at=<time>     UTC RFC3339 失效时间（配置键 expires_at；生产可运行包必填）。
#   --channel=<name>        发布通道（配置键 channel；默认 stable）。
#   --minimum-daemon-version=<v> 最低 daemon 版本（配置键 minimum_daemon_version；必填）。
#   --key-id=<id>           受信 Ed25519 公钥标识（配置键 key_id；必填）。
#   --signing-key=<path>    Ed25519 PKCS8 PEM 私钥路径（配置键 signing_key；必填，不写日志）。
#   --revocations=<path>    可选撤销材料 JSON 数组（配置键 revocations_file）。
#   --no-venv         **仅验证打包流水线**：跳过 venv 构建，产出 STRUCTURE-only 包（不可运行）。
#   --help
#
# ## 可移植性方案（照抄 film/reel 已验证范式）
#   - 自带 standalone Python：把 uv 托管的 python-build-standalone 解释器整树用 `rsync -aL`
#     （解引用所有 symlink）拷进 `venv/`，删掉 PEP 668 `EXTERNALLY-MANAGED` 标记，再用包内
#     python 把依赖装进其自身 site-packages。产物**无 symlink**、按二进制位置自寻 stdlib，
#     拷到任意机器/路径直接可跑。daemon 的 unpack_zip 不还原 symlink，故包内有 symlink = 坏包。
#   - 跨架构边界：`uv` 只能装「当前 OS」的 standalone Python；同 OS 跨架构（mac arm64↔x86_64）
#     可经 uv/Rosetta，跨 OS（linux/win）须在对应 OS 的机器/容器上分别跑本脚本，各自上传、合并 manifest。

# ---- 参数解析 -------------------------------------------------------------
ARCH_INPUT=""
TARGETS_INPUT="${VIBE_TRADING_ENGINE_TARGETS:-}"
VERSION="${VIBE_TRADING_ENGINE_VERSION:-}"
SRC="${VIBE_TRADING_AGENT_SRC:-}"
OUT_DIR="${VIBE_TRADING_ENGINE_OUT:-}"
BASE_URL="${VIBE_TRADING_ENGINE_BASE_URL:-}"
PUBLISH_URL="${VIBE_TRADING_ENGINE_PUBLISH_URL:-}"
APP_PK="${VIBE_TRADING_ENGINE_APP_PK:-}"
ADMIN_TOKEN="${HASN_ADMIN_TOKEN:-}"
RELEASE_SEQUENCE="${VIBE_TRADING_ENGINE_RELEASE_SEQUENCE:-}"
ISSUED_AT="${VIBE_TRADING_ENGINE_ISSUED_AT:-}"
EXPIRES_AT="${VIBE_TRADING_ENGINE_EXPIRES_AT:-}"
CHANNEL="${VIBE_TRADING_ENGINE_CHANNEL:-}"
MINIMUM_DAEMON_VERSION="${VIBE_TRADING_ENGINE_MINIMUM_DAEMON_VERSION:-}"
KEY_ID="${VIBE_TRADING_ENGINE_KEY_ID:-}"
SIGNING_KEY="${VIBE_TRADING_ENGINE_SIGNING_KEY:-}"
REVOCATIONS_FILE="${VIBE_TRADING_ENGINE_REVOCATIONS_FILE:-}"
DEFER_RELEASE_PUBLISH="${VIBE_TRADING_ENGINE_DEFER_RELEASE_PUBLISH:-}"
NO_VENV=0
ENV_NAME=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${VIBE_TRADING_ENGINE_CONFIG:-}"

# Python 版本：与 fork 的 requires-python>=3.11 兼容；取 3.12（与 film/reel 一致，生态最稳）。
PY_VERSION="3.12"

# 打包时带上的 extras（理由见脚本头「extras 取舍」）。
PKG_EXTRAS=(--extra ashare)

# usage 打印脚本头注释（行 3 至「参数解析」分隔线），对 header 增长鲁棒（不硬编码行号）。
usage() { sed -n '3,/^# ----/p' "${BASH_SOURCE[0]}" | sed '/^# ----/d; s/^# \{0,1\}//'; }

# 安全解析 YAML 扁平配置文件（key: value，# 注释）——**逐行解析、不 source、不引 YAML 库**（防任意代码执行）。
# 仅取顶层 `key: value`（value 按首个冒号切分，故 URL 里的 :// 不受影响）。
# 仅填充「当前仍为空」的变量，故优先级 = 命令行 > 环境变量 > 配置文件 > 内置默认。
load_config_file() {
  local file="$1" line key value
  [[ -f "${file}" ]] || return 0
  echo "[vt-pkg] 读配置文件: ${file}"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"                       # 去尾部 CR（CRLF 文件兼容）
    line="${line#"${line%%[![:space:]]*}"}"    # 去前导空白
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    [[ "${line}" != *:* ]] && continue          # 非 key: value 行跳过
    key="${line%%:*}"; value="${line#*:}"       # 首个冒号切分（value 里的 :// 保留）
    key="${key//[[:space:]]/}"
    value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"  # 去 value 首尾空白
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"      # 去成对引号
    case "${key}" in
      targets) [[ -z "${TARGETS_INPUT}" ]] && TARGETS_INPUT="${value}" ;;
      publish_url) [[ -z "${PUBLISH_URL}" ]] && PUBLISH_URL="${value}" ;;
      app_pk) [[ -z "${APP_PK}" ]] && APP_PK="${value}" ;;
      admin_token) [[ -z "${ADMIN_TOKEN}" ]] && ADMIN_TOKEN="${value}" ;;
      version) [[ -z "${VERSION}" ]] && VERSION="${value}" ;;
      src) [[ -z "${SRC}" ]] && SRC="${value}" ;;
      out) [[ -z "${OUT_DIR}" ]] && OUT_DIR="${value}" ;;
      base_url) [[ -z "${BASE_URL}" ]] && BASE_URL="${value}" ;;
      release_sequence) [[ -z "${RELEASE_SEQUENCE}" ]] && RELEASE_SEQUENCE="${value}" ;;
      issued_at) [[ -z "${ISSUED_AT}" ]] && ISSUED_AT="${value}" ;;
      expires_at) [[ -z "${EXPIRES_AT}" ]] && EXPIRES_AT="${value}" ;;
      channel) [[ -z "${CHANNEL}" ]] && CHANNEL="${value}" ;;
      minimum_daemon_version) [[ -z "${MINIMUM_DAEMON_VERSION}" ]] && MINIMUM_DAEMON_VERSION="${value}" ;;
      key_id) [[ -z "${KEY_ID}" ]] && KEY_ID="${value}" ;;
      signing_key) [[ -z "${SIGNING_KEY}" ]] && SIGNING_KEY="${value}" ;;
      revocations_file) [[ -z "${REVOCATIONS_FILE}" ]] && REVOCATIONS_FILE="${value}" ;;
      defer_release_publish) [[ -z "${DEFER_RELEASE_PUBLISH}" ]] && DEFER_RELEASE_PUBLISH="${value}" ;;
      *) echo "[vt-pkg] ⚠ 配置文件未知键，忽略: ${key}" >&2 ;;
    esac
  done < "${file}"
  return 0
}

# 先扫一遍找 --config / --dev / --env（让命令行能指定配置文件/环境），再加载配置（不覆盖已设环境变量）。
for arg in "$@"; do
  case "${arg}" in
    --config=*) CONFIG_FILE="${arg#--config=}" ;;
    --dev) ENV_NAME="dev" ;;
    --env=*) ENV_NAME="${arg#--env=}" ;;
  esac
done
if [[ -z "${CONFIG_FILE}" ]]; then
  case "${ENV_NAME}" in
    dev | test | local) CONFIG_FILE="${SCRIPT_DIR}/config-dev.yaml" ;;
    *) CONFIG_FILE="${SCRIPT_DIR}/config.yaml" ;;
  esac
fi
load_config_file "${CONFIG_FILE}"

for arg in "$@"; do
  case "${arg}" in
    --config=* | --dev | --env=*) ;; # 已在上面处理
    --targets=*) TARGETS_INPUT="${arg#--targets=}" ;;
    --version=*) VERSION="${arg#--version=}" ;;
    --src=*) SRC="${arg#--src=}" ;;
    --out=*) OUT_DIR="${arg#--out=}" ;;
    --base-url=*) BASE_URL="${arg#--base-url=}" ;;
    --publish=*) PUBLISH_URL="${arg#--publish=}" ;;
    --app-pk=*) APP_PK="${arg#--app-pk=}" ;;
    --admin-token=*) ADMIN_TOKEN="${arg#--admin-token=}" ;;
    --release-sequence=*) RELEASE_SEQUENCE="${arg#--release-sequence=}" ;;
    --issued-at=*) ISSUED_AT="${arg#--issued-at=}" ;;
    --expires-at=*) EXPIRES_AT="${arg#--expires-at=}" ;;
    --channel=*) CHANNEL="${arg#--channel=}" ;;
    --minimum-daemon-version=*) MINIMUM_DAEMON_VERSION="${arg#--minimum-daemon-version=}" ;;
    --key-id=*) KEY_ID="${arg#--key-id=}" ;;
    --signing-key=*) SIGNING_KEY="${arg#--signing-key=}" ;;
    --revocations=*) REVOCATIONS_FILE="${arg#--revocations=}" ;;
    --defer-release-publish) DEFER_RELEASE_PUBLISH=1 ;;
    --no-venv) NO_VENV=1 ;;
    --help | -h) usage; exit 0 ;;
    aarch64 | arm64) ARCH_INPUT=aarch64 ;;
    x86_64 | amd64) ARCH_INPUT=x86_64 ;;
    *)
      echo "未知参数: ${arg}" >&2
      usage >&2
      exit 1
      ;;
  esac
done

DEFER_RELEASE_PUBLISH="${DEFER_RELEASE_PUBLISH:-0}"
case "${DEFER_RELEASE_PUBLISH}" in
  0 | 1) ;;
  *) echo "[vt-pkg] defer_release_publish 只允许 0 或 1" >&2; exit 1 ;;
esac
if [[ "${DEFER_RELEASE_PUBLISH}" == "1" && -z "${PUBLISH_URL}" ]]; then
  echo "[vt-pkg] --defer-release-publish 只适用于已开启的平台包上传" >&2
  exit 1
fi

if [[ "${NO_VENV}" == "1" && -n "${PUBLISH_URL}" ]]; then
  echo "[vt-pkg] --no-venv 产物不可运行，禁止发布到任何远程或本地发布端点" >&2
  exit 1
fi

# 目标 OS 由构建主机决定（跨 OS 的 Python 构建不在 bash 单机范围内；同 OS 跨架构可经 uv/Rosetta）。
case "$(uname -s)" in
  Darwin) OS_KEY=darwin; PY_OS=macos ;;
  Linux) OS_KEY=linux; PY_OS=linux ;;
  MINGW* | MSYS* | CYGWIN*) OS_KEY=win; PY_OS=windows ;;
  *) echo "不支持的构建主机 OS: $(uname -s)" >&2; exit 1 ;;
esac

default_targets_for_os() {
  case "${OS_KEY}" in
    darwin) echo "aarch64 x86_64" ;; # mac 可经 uv/Rosetta 跨架构
    linux)
      case "$(uname -m)" in
        aarch64 | arm64) echo "aarch64" ;;
        *) echo "x86_64" ;;
      esac ;;
    win) echo "x86_64" ;;
  esac
}

if [[ -n "${ARCH_INPUT}" ]]; then
  TARGETS="${ARCH_INPUT}"
elif [[ -n "${TARGETS_INPUT}" ]]; then
  TARGETS="${TARGETS_INPUT//,/ }" # 逗号亦可
else
  TARGETS="$(default_targets_for_os)"
fi
for t in ${TARGETS}; do
  case "${t}" in
    aarch64 | x86_64) ;;
    *) echo "[vt-pkg] 未知目标架构: ${t}（仅支持 aarch64 / x86_64）" >&2; exit 1 ;;
  esac
done

# ---- 全局前置校验（影响所有架构的，一次性 fail-fast）-----------------------
ENGINE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SRC="${SRC:-${ENGINE_ROOT}/agent}"
OUT_DIR="${OUT_DIR:-${ENGINE_ROOT}/.engine-build/vibe-trading}"

if [[ ! -f "${SRC}/mcp_server.py" ]]; then
  echo "找不到引擎源（缺 ${SRC}/mcp_server.py）；用 --src 指定" >&2
  exit 1
fi
if [[ ! -f "${ENGINE_ROOT}/pyproject.toml" || ! -f "${ENGINE_ROOT}/uv.lock" ]]; then
  echo "缺 ${ENGINE_ROOT}/pyproject.toml 或 uv.lock —— 依赖必须由锁文件导出（见脚本头「依赖装法」）" >&2
  exit 1
fi

# 版本：未显式给则从仓根 pyproject.toml 读首个 version 行。多架构必须同版本（manifest/云端约束）。
if [[ -z "${VERSION}" ]]; then
  VERSION="$(awk -F'"' '/^version[[:space:]]*=/{print $2; exit}' "${ENGINE_ROOT}/pyproject.toml")"
fi
if [[ -z "${VERSION}" ]]; then
  echo "无法确定版本（pyproject.toml 无 version，且未给 --version / VIBE_TRADING_ENGINE_VERSION）" >&2
  exit 1
fi

command -v uv >/dev/null 2>&1 || { echo "[vt-pkg] 需要 uv（导出锁文件依赖 / 安装 standalone Python）" >&2; exit 1; }
if [[ "${NO_VENV}" != "1" ]]; then
  command -v rsync >/dev/null 2>&1 || { echo "[vt-pkg] 需要 rsync 实体化拷贝解释器树（-aL 解引用 symlink）" >&2; exit 1; }
  CHANNEL="${CHANNEL:-stable}"
  [[ "${RELEASE_SEQUENCE}" =~ ^[1-9][0-9]*$ ]] || { echo "[vt-pkg] 可运行包必须提供正整数 release_sequence" >&2; exit 1; }
  [[ -n "${ISSUED_AT}" ]] || { echo "[vt-pkg] 可运行包必须提供 issued_at（UTC RFC3339，多平台一致）" >&2; exit 1; }
  [[ -n "${EXPIRES_AT}" ]] || { echo "[vt-pkg] 可运行包必须提供 expires_at（UTC RFC3339）" >&2; exit 1; }
  [[ -n "${MINIMUM_DAEMON_VERSION}" ]] || { echo "[vt-pkg] 可运行包必须提供 minimum_daemon_version" >&2; exit 1; }
  [[ -n "${KEY_ID}" ]] || { echo "[vt-pkg] 可运行包必须提供 key_id" >&2; exit 1; }
  [[ -n "${SIGNING_KEY}" && -f "${SIGNING_KEY}" ]] || { echo "[vt-pkg] 可运行包必须提供存在的 Ed25519 signing_key 文件" >&2; exit 1; }
  if [[ -n "${REVOCATIONS_FILE}" && ! -f "${REVOCATIONS_FILE}" ]]; then
    echo "[vt-pkg] revocations_file 不存在：${REVOCATIONS_FILE}" >&2
    exit 1
  fi
  if [[ -z "${PUBLISH_URL}" && -z "${BASE_URL}" ]]; then
    echo "[vt-pkg] 未走一键发布时必须提供 base_url，禁止把占位 URL 写进受签名清单" >&2
    exit 1
  fi
fi

# 发布前置：fail-fast，缺要素立即报错（别等打完几百 MB 才发现没法上传）。
if [[ -n "${PUBLISH_URL}" ]]; then
  [[ -n "${APP_PK}" ]] || { echo "[vt-pkg] 发布需要 --app-pk / VIBE_TRADING_ENGINE_APP_PK（云端应用目录行ID，app_id='finance' 那行主键）" >&2; exit 1; }
  [[ -n "${ADMIN_TOKEN}" ]] || { echo "[vt-pkg] 发布需要 --admin-token / HASN_ADMIN_TOKEN（管理端 JWT）" >&2; exit 1; }
  command -v curl >/dev/null 2>&1 || { echo "[vt-pkg] 发布需要 curl 上传引擎包" >&2; exit 1; }
fi

# 依赖清单：从 uv.lock 导出（--frozen = 不许就地重解析，锁什么装什么）。
# 一次导出、各架构复用：本引擎依赖无 sys_platform 分叉的架构差异，逐架构导会白跑几遍解析。
REQ_FILE="${OUT_DIR}/requirements-locked.txt"
mkdir -p "${OUT_DIR}"
echo "[vt-pkg] 从 uv.lock 导出依赖清单 → ${REQ_FILE}（extras: ${PKG_EXTRAS[*]}）"
( cd "${ENGINE_ROOT}" && uv export --frozen --no-emit-project --no-dev "${PKG_EXTRAS[@]}" -o "${REQ_FILE}" >/dev/null )
[[ -s "${REQ_FILE}" ]] || { echo "[vt-pkg] uv export 产出为空 —— 锁文件可能已损坏" >&2; exit 1; }
echo "[vt-pkg] 锁定依赖 $(grep -c '^[a-zA-Z0-9]' "${REQ_FILE}") 个"

echo "[vt-pkg] OS=${OS_KEY} 目标架构=[${TARGETS}] 版本=${VERSION} 源=${SRC}"
[[ "${NO_VENV}" == "1" ]] && echo "[vt-pkg] ⚠ --no-venv：仅验证流水线，产出 STRUCTURE-only 包（不可运行）"
[[ -n "${PUBLISH_URL}" ]] && echo "[vt-pkg] 发布开启 → ${PUBLISH_URL}（app-pk=${APP_PK}）" || echo "[vt-pkg] 未配 --publish/VIBE_TRADING_ENGINE_PUBLISH_URL：只打包不发布"

# ---- 单架构处理：staging → venv → 逐文件清单 → zip → 上传包 → 更新签名清单 ------
process_one_arch() {
  local ARCH_KEY="$1"
  local OS_ARCH="${OS_KEY}-${ARCH_KEY}"
  echo
  echo "========== [${OS_ARCH}] =========="

  local STAGE="${OUT_DIR}/stage-${OS_ARCH}"
  rm -rf "${STAGE}"
  mkdir -p "${STAGE}/agent"
  # 剔除：缓存、字节码、测试、运行期产物（runs/sessions 是引擎跑出来的，不该进分发包）、
  # editable 安装残留（egg-info/.venv）、VCS。
  local COPY_EXCLUDES=(
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache'
    --exclude='.ruff_cache' --exclude='tests' --exclude='.git'
    --exclude='.venv' --exclude='*.egg-info'
    --exclude='runs/*' --exclude='sessions/*'
  )
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "${COPY_EXCLUDES[@]}" "${SRC}/" "${STAGE}/agent/"
  else
    cp -R "${SRC}/" "${STAGE}/agent/"
    ( cd "${STAGE}/agent" && rm -rf .venv .git tests .pytest_cache .ruff_cache *.egg-info \
        && find . -name '__pycache__' -type d -prune -exec rm -rf {} + \
        && find . -name '*.pyc' -delete \
        && find runs sessions -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true )
  fi
  # staging 结构闸门：与 daemon install 侧同一硬校验，提前在打包侧失败而非等下载后才发现坏包。
  [[ -f "${STAGE}/agent/mcp_server.py" ]] || { echo "[vt-pkg] staging 异常：缺 agent/mcp_server.py" >&2; exit 1; }

  # venv：自带 standalone Python（relocatable）+ rsync -aL 实体化 symlink。详见脚本头。
  if [[ "${NO_VENV}" != "1" ]]; then
    local PY_SPEC="cpython-${PY_VERSION}-${PY_OS}-${ARCH_KEY}"
    local VENV="${STAGE}/venv"
    echo "[vt-pkg] 安装目标架构 standalone Python: ${PY_SPEC}"
    uv python install "${PY_SPEC}"
    local PY_DIR_ROOT PY_HOME
    PY_DIR_ROOT="$(uv python dir 2>/dev/null || echo "${HOME}/.local/share/uv/python")"
    PY_HOME="$(ls -d "${PY_DIR_ROOT}"/cpython-"${PY_VERSION}".*-"${PY_OS}"-"${ARCH_KEY}"-none 2>/dev/null | sort -V | tail -1)"
    if [[ -z "${PY_HOME}" || ! -x "${PY_HOME}/bin/python${PY_VERSION}" ]]; then
      echo "[vt-pkg] 定位 standalone Python 安装根失败（PY_DIR_ROOT=${PY_DIR_ROOT}，期望 cpython-${PY_VERSION}.*-${PY_OS}-${ARCH_KEY}-none）" >&2
      exit 1
    fi
    echo "[vt-pkg] 实体化拷贝解释器树 → ${VENV}（rsync -aL 解引用 symlink）"
    rm -rf "${VENV}"
    rsync -aL "${PY_HOME}/" "${VENV}/"
    echo "[vt-pkg] 移除 PEP 668 EXTERNALLY-MANAGED 标记（引擎私有 python，允许装依赖）"
    find "${VENV}" -name EXTERNALLY-MANAGED -delete
    echo "[vt-pkg] 按锁文件安装依赖到自身 site-packages"
    "${VENV}/bin/python${PY_VERSION}" -m pip install --no-input --no-warn-script-location -r "${REQ_FILE}"

    # 启动契约闸门：daemon 按 `venv/bin/python` 起进程（不带版本号后缀）。
    # standalone 解释器树自带 bin/python，缺了说明上游布局变了 —— 必须打包侧就炸。
    [[ -x "${VENV}/bin/python" ]] || { echo "[vt-pkg] ✗ 缺 venv/bin/python（daemon locator 契约）" >&2; exit 1; }

    # symlink 守卫：daemon unpack_zip 不还原 symlink，包内任何 symlink 都会损坏。
    if find "${STAGE}" -type l | grep -q .; then
      echo "[vt-pkg] ✗ 包内仍存在 symlink（daemon 解压不还原 symlink → 包会损坏）：" >&2
      find "${STAGE}" -type l >&2
      exit 1
    fi

    # 冒烟：用包内 python 真的 import 一次引擎注册表 —— 证明依赖装全了、且 shell 工具默认关闭。
    # macOS arm64 构建 x86_64 时通过 Rosetta 执行；其他无法本机执行的跨架构包留给目标构建机。
    local HOST_ARCH; HOST_ARCH="$(uname -m)"; [[ "${HOST_ARCH}" == "arm64" ]] && HOST_ARCH="aarch64"
    run_registry_smoke() {
      ( cd "${STAGE}/agent" && "$@" - <<'PY'
import sys

from src.tools import build_registry

names = set(build_registry().tool_names)
if not names:
    sys.exit("[vt-pkg] ✗ 冒烟失败：注册表为空")
for shell_tool in ("bash", "background_run"):
    if shell_tool in names:
        sys.exit(f"[vt-pkg] ✗ 冒烟失败：默认注册表暴露了 shell 工具 {shell_tool}")
print(f"[vt-pkg] ✓ 冒烟通过：{len(names)} 个工具，无 shell 工具")
PY
      )
    }
    if [[ "${ARCH_KEY}" == "${HOST_ARCH}" ]]; then
      echo "[vt-pkg] 冒烟：包内 python 载入工具注册表 + 核对 shell 闸"
      run_registry_smoke "${STAGE}/venv/bin/python" || exit 1
    elif [[ "${OS_KEY}" == "darwin" && "${HOST_ARCH}" == "aarch64" && "${ARCH_KEY}" == "x86_64" ]] \
      && arch -x86_64 /usr/bin/true >/dev/null 2>&1; then
      echo "[vt-pkg] 冒烟：通过 Rosetta 载入 x86_64 工具注册表 + 核对 shell 闸"
      run_registry_smoke arch -x86_64 "${STAGE}/venv/bin/python" || exit 1
    else
      echo "[vt-pkg] 当前主机不能执行 ${OS_ARCH} 包，冒烟必须由对应目标构建机补齐" >&2
    fi
  fi

  # 逐文件清单在归档前生成；扫描会拒绝 symlink、hardlink 与特殊文件。
  local FILE_META_JSON FILE_MANIFEST_SHA256 INSTALLED_SIZE
  FILE_META_JSON="$(uv run --frozen python "${SCRIPT_DIR}/vibe_release_manifest.py" \
    build-file-manifest --root="${STAGE}")"
  FILE_MANIFEST_SHA256="$(FILE_META_JSON="${FILE_META_JSON}" python3 - <<'PY'
import json
import os

print(json.loads(os.environ["FILE_META_JSON"])["file_manifest_sha256"])
PY
)"
  INSTALLED_SIZE="$(FILE_META_JSON="${FILE_META_JSON}" python3 - <<'PY'
import json
import os

print(json.loads(os.environ["FILE_META_JSON"])["installed_size"])
PY
)"

  # 打包 zip（顶层 venv/ + agent/ + file-manifest.json）+ sha256 + size。
  local PKG_NAME="vibe-trading-${OS_ARCH}-${VERSION}.zip"
  local PKG_PATH="${OUT_DIR}/${PKG_NAME}"
  rm -f "${PKG_PATH}"
  echo "[vt-pkg] 打包 → ${PKG_PATH}"
  uv run --frozen python "${SCRIPT_DIR}/vibe_release_manifest.py" build-archive \
    --root="${STAGE}" \
    --archive="${PKG_PATH}" >/dev/null

  local SHA256 SIZE
  if command -v sha256sum >/dev/null 2>&1; then
    SHA256="$(sha256sum "${PKG_PATH}" | awk '{print $1}')"
  else
    SHA256="$(shasum -a 256 "${PKG_PATH}" | awk '{print $1}')"
  fi
  SIZE="$(wc -c < "${PKG_PATH}" | tr -d ' ')"

  # 再从最终 zip 读取逐文件清单逐项复验，避免“扫描后、打包前”漂移或归档工具引入额外入口。
  uv run --frozen python "${SCRIPT_DIR}/vibe_release_manifest.py" verify-archive \
    --archive="${PKG_PATH}" \
    --file-manifest-sha256="${FILE_MANIFEST_SHA256}" >/dev/null

  local MANIFEST="${OUT_DIR}/manifest.json"
  local PKG_URL="${BASE_URL:+${BASE_URL%/}/${PKG_NAME}}"

  echo "[vt-pkg] 完成 ${OS_ARCH}: ${PKG_PATH}（sha256=${SHA256:0:12}… size=${SIZE}）"

  # 一键发布：POST 引擎包到云端 admin 端点（落公共桶 + 写 config_json.engine + push）。
  if [[ -n "${PUBLISH_URL}" ]]; then
    local ENDPOINT="${PUBLISH_URL%/}/api/v1/hasn/app-catalogs/${APP_PK}/engine-package"
    echo "[vt-pkg] 发布 → ${ENDPOINT}（os_arch=${OS_ARCH} version=${VERSION}）"
    # 服务端权威算 sha256（交叉校验）+ size，落公共桶，写 config_json.engine，push platform_config。
    # token 经 Authorization 头（不进 URL/日志）；-sS 静默但报错，-w 附 HTTP 码。
    local HTTP_BODY_FILE="${OUT_DIR}/.publish-resp-${OS_ARCH}.json" HTTP_CODE
    HTTP_CODE="$(curl -sS -o "${HTTP_BODY_FILE}" -w '%{http_code}' \
      -X POST "${ENDPOINT}" \
      -H "Authorization: Bearer ${ADMIN_TOKEN}" \
      -F "file=@${PKG_PATH};type=application/zip" \
      -F "os_arch=${OS_ARCH}" \
      -F "version=${VERSION}" \
      -F "sha256=${SHA256}" || echo "000")"
    if [[ "${HTTP_CODE}" != "200" ]]; then
      echo "[vt-pkg] ✗ 发布失败（HTTP ${HTTP_CODE}）：" >&2
      cat "${HTTP_BODY_FILE}" >&2 2>/dev/null || true
      echo >&2
      rm -f "${HTTP_BODY_FILE}"
      exit 1
    fi
    # 解析统一信封并取服务端实际分配的稳定 URL；签名清单禁止写上传前猜测的地址。
    PKG_URL="$(RESP_FILE="${HTTP_BODY_FILE}" OS_ARCH="${OS_ARCH}" python3 - <<'PY'
import json
import os
import sys

with open(os.environ["RESP_FILE"], encoding="utf-8") as fh:
    env = json.load(fh)
code = env.get("code")
if code not in (0, 200):
    print(f"[vt-pkg] ✗ 云端业务失败 code={code} msg={env.get('msg')}", file=sys.stderr)
    sys.exit(1)
engine = env.get("data") or {}
pkgs = engine.get("packages") or {}
package = pkgs.get(os.environ["OS_ARCH"]) or {}
url = package.get("url")
if not isinstance(url, str) or not url:
    print("[vt-pkg] ✗ 云端响应缺少已发布包稳定 URL", file=sys.stderr)
    sys.exit(1)
print(url)
PY
)" || exit 1
    echo "[vt-pkg] ✓ 已发布 ${OS_ARCH}，稳定 URL 已进入待签名清单"
    rm -f "${HTTP_BODY_FILE}"
  fi

  if [[ "${NO_VENV}" == "1" ]]; then
    echo "[vt-pkg] STRUCTURE-only 包已验证；不生成可被生产安装器接受的 manifest.json"
    return 0
  fi

  uv run --frozen python "${SCRIPT_DIR}/vibe_release_manifest.py" upsert-package \
    --manifest="${MANIFEST}" \
    --version="${VERSION}" \
    --release-sequence="${RELEASE_SEQUENCE}" \
    --channel="${CHANNEL}" \
    --issued-at="${ISSUED_AT}" \
    --expires-at="${EXPIRES_AT}" \
    --minimum-daemon-version="${MINIMUM_DAEMON_VERSION}" \
    --key-id="${KEY_ID}" \
    --signing-key="${SIGNING_KEY}" \
    --platform="${OS_ARCH}" \
    --url="${PKG_URL}" \
    --sha256="${SHA256}" \
    --compressed-size="${SIZE}" \
    --installed-size-limit="${INSTALLED_SIZE}" \
    --file-manifest-sha256="${FILE_MANIFEST_SHA256}" \
    --revocations-file="${REVOCATIONS_FILE}" >/dev/null
  echo "[vt-pkg] 签名清单已更新：${MANIFEST}（${OS_ARCH}，release_sequence=${RELEASE_SEQUENCE}）"
}


publish_release_manifest() {
  local MANIFEST="${OUT_DIR}/manifest.json"
  local ENDPOINT="${PUBLISH_URL%/}/api/v1/hasn/app-catalogs/${APP_PK}/finance-engine-release"
  local HTTP_BODY_FILE="${OUT_DIR}/.publish-release-resp.json" HTTP_CODE
  echo "[vt-pkg] 发布签名清单 → ${ENDPOINT}（release_sequence=${RELEASE_SEQUENCE}）"
  HTTP_CODE="$(curl -sS -o "${HTTP_BODY_FILE}" -w '%{http_code}' \
    -X POST "${ENDPOINT}" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -F "manifest=@${MANIFEST};type=application/json" || echo "000")"
  if [[ "${HTTP_CODE}" != "200" ]]; then
    echo "[vt-pkg] ✗ 签名清单发布失败（HTTP ${HTTP_CODE}）：" >&2
    cat "${HTTP_BODY_FILE}" >&2 2>/dev/null || true
    echo >&2
    rm -f "${HTTP_BODY_FILE}"
    exit 1
  fi
  if ! uv run --frozen python "${SCRIPT_DIR}/vibe_release_manifest.py" verify-publish-response \
    --manifest="${MANIFEST}" \
    --response="${HTTP_BODY_FILE}" >/dev/null; then
    rm -f "${HTTP_BODY_FILE}"
    exit 1
  fi
  rm -f "${HTTP_BODY_FILE}"
  echo "[vt-pkg] ✓ 签名清单已由云端持久化并进入平台配置"
}


# ---- 主流程：清旧 manifest（本次产一份干净的；跨机构建由发布协调器合并并重签）+ 遍历目标 ----
rm -f "${OUT_DIR}/manifest.json"
for arch in ${TARGETS}; do
  process_one_arch "${arch}"
done

echo
if [[ "${NO_VENV}" == "1" ]]; then
  echo "[vt-pkg] ✅ STRUCTURE-only 验证完成：架构 [${TARGETS}] 版本 ${VERSION}；无生产 manifest"
else
  if [[ -n "${PUBLISH_URL}" && "${DEFER_RELEASE_PUBLISH}" != "1" ]]; then
    publish_release_manifest
  elif [[ "${DEFER_RELEASE_PUBLISH}" == "1" ]]; then
    echo "[vt-pkg] 已按跨 OS 模式推迟清单发布；协调器须汇总全部平台、重新签名后只发布一次最终清单。"
  fi
  echo "[vt-pkg] ✅ 全部完成：架构 [${TARGETS}] 版本 ${VERSION}。受签名 manifest: ${OUT_DIR}/manifest.json"
fi
