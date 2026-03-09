# Skill: 开启 Verilog 实验 (Run Verilog Experiment)

## 启动实验前的检查清单

在启动实验前，**必须**向用户确认以下参数：

| 参数 | 必须确认 | 默认值 |
|------|----------|--------|
| 实验名 | ✅ | 无 |
| Repo 开始索引 (START_INDEX) | ✅ | 0 |
| Repo 结束索引 (END_INDEX) | ✅ | 无 |
| 并行数量 (CONCURRENCY) | ✅ | 无 |
| 模型 (MODEL) | ✅ | 无 |
| Actor 模型 | ✅ | 同 MODEL（支持多个，逗号分隔） |
| Validation Runs | ✅ | 4（支持每个模型不同次数，逗号分隔） |
| PR Timeout | ⚠️ 建议 | 600 分钟 |
| Task Type | ⚠️ 建议 | all (new_feature + bugfix) |
| GitHub Token | ✅ | 需要用户提供 |

## 关键规则

### GitHub Token 检查
每次启动实验前，**必须先检查** GitHub Token 的 rate limit：
```bash
curl -s -H "Authorization: token <TOKEN>" https://api.github.com/rate_limit | python3 -c "
import json, sys
data = json.load(sys.stdin)
core = data['resources']['core']
search = data['resources']['search']
graphql = data['resources'].get('graphql', {})
print(f'Core API:   {core[\"remaining\"]}/{core[\"limit\"]}')
print(f'Search API: {search[\"remaining\"]}/{search[\"limit\"]}')
if graphql:
    print(f'GraphQL:    {graphql[\"remaining\"]}/{graphql[\"limit\"]}')
"
```
确保 `remaining` > 预计使用量 (每个 repo 约消耗 50-100 次请求)

### 并发数设置
用户可以自定义并发数 (CONCURRENCY)：
- 完全并行：并发数 = END_INDEX - START_INDEX（如 11 个 repo，并发数 = 11）
- 分批运行：并发数 < repo 数量（如 50 个 repo，并发数 = 10，分 5 批运行）

### 分批运行的调度逻辑

当 **repo 数量 > 并发数** 时，脚本会自动分批：
- 例如：50 个 repo，并发数 10 → 分成 5 批
- 每批之间间隔 **7 小时**自动启动下一批
- 脚本需要**持续运行** 28+ 小时才能启动所有批次

**⚠️ 重要**：Claude Code 的后台任务与会话绑定，退出 Claude Code 或关闭电脑会导致后台任务被终止，后续批次无法启动。

### 长时间分批实验：使用 nohup 或 tmux

当分批运行需要长时间等待时，**必须使用 nohup 或 tmux**，不要使用 Claude Code 的后台任务功能。

#### 方式 1：nohup（推荐，更简单）

```bash
nohup bash -c '
source /home/chenzhirong/mshrl/.venv/bin/activate && \
eval $(curl -fsSL proxy.msh.work:3128/env --noproxy proxy.msh.work) && \
export GITHUB_TOKEN="ghp_xxx" && \
cd /home/chenzhirong/VeriMine && \
bash verilog_mining/run_verilog.sh \
    实验名 起始索引 结束索引 模型 flash模型 并发数 \
    PR_TOP_N 要求code+test PR超时 Repo超时 Actor模型 验证次数
' > ~/verilog-experiment-name.log 2>&1 &

# 查看进程
ps aux | grep run_verilog

# 查看日志
tail -f ~/verilog-experiment-name.log
```

#### 方式 2：tmux（可交互查看）

```bash
# 创建 tmux 会话
tmux new -s verilog-experiment

# 在 tmux 里运行实验
source /home/chenzhirong/mshrl/.venv/bin/activate && \
eval $(curl -fsSL proxy.msh.work:3128/env --noproxy proxy.msh.work) && \
export GITHUB_TOKEN="ghp_xxx" && \
cd /home/chenzhirong/VeriMine && \
bash verilog_mining/run_verilog.sh ...

# 退出 tmux（进程继续运行）：按 Ctrl+B, 然后按 D

# 重新连接查看
tmux attach -t verilog-experiment
```

## 与 C++ 实验的关键差异

| 方面 | C++ Pipeline | Verilog Pipeline |
|------|-------------|-----------------|
| Docker 镜像 | 每 repo 独立镜像 `swe_factory_cpp:{id}` | **单一共享镜像** `eda-sandbox:agent` |
| 仓库获取 | Docker 镜像内预装 | **sandbox 内 git clone** |
| 构建工具 | cmake, make, meson | **iverilog, verilator, cocotb** |
| 测试框架 | gtest, catch2, ctest | **cocotb, UVM, VUnit, 原生 testbench** |
| 文件扩展名 | .cpp, .cc, .h, .hpp | **.v, .vh, .sv, .svh** |
| 禁止工具 | GPU/CUDA | **商业 EDA (VCS, Questa, Xcelium)** |
| 测试结果解析 | gtest/ctest/pytest | **$display PASS/FAIL, cocotb XML, UVM report** |
| 脚本 | `run_cpp.sh` | **`run_verilog.sh`** |
| env 名称 | `swe-factory-cpp` | **`eda-sandbox`** |
| Task Type | 仅 new_feature | **new_feature + bugfix** |

## 完整参数列表

```bash
bash verilog_mining/run_verilog.sh \
    <实验名>           # 1. EXPERIMENT_NAME: 如 verilog-task-gen-v1
    <起始索引>         # 2. START_INDEX: 通常为 0
    <结束索引>         # 3. END_INDEX: repo 数量
    <模型>             # 4. MODEL: PR 挖掘模型
    <flash模型>        # 5. FLASH_MODEL: 快速模型，通常同 MODEL
    <并发数>           # 6. CONCURRENCY: ⚠️ 建议 = END_INDEX - START_INDEX
    <PR_TOP_N>         # 7. PR_DISCOVERY_TOP_N: 默认 3
    <要求code+test>    # 8. REQUIRE_CODE_AND_TEST: true / false
    <PR超时>           # 9. PR_TIMEOUT: 分钟，建议 600
    <Repo超时>         # 10. REPO_TIMEOUT: 分钟，默认 9999
    <Actor模型>        # 11. ACTOR_MODELS: 验证用模型（逗号分隔多个）
    <验证次数>         # 12. VALIDATION_RUNS: 每个 actor 模型运行次数（逗号分隔）
```

**注意**：与 C++ 的 run_cpp.sh 相比，少了 LANGUAGE 参数（因为固定是 Verilog/SystemVerilog）。

## 多 Actor 模型 + 不同验证次数

### 核心语法

```bash
# 参数 11: Actor 模型（逗号分隔）
"model1,model2,model3"

# 参数 12: 验证次数（逗号分隔，与 Actor 模型一一对应）
"4,1,2"
```

### 匹配规则

| Actor 模型 | Validation Runs | 结果 |
|-----------|-----------------|------|
| `"modelA,modelB"` | `"4,1"` | modelA 跑 4 次，modelB 跑 1 次 |
| `"modelA,modelB,modelC"` | `"4"` | 所有模型都跑 4 次（自动扩展） |
| `"modelA,modelB,modelC"` | `"4,2"` | modelA 跑 4 次，modelB 跑 2 次，modelC 跑 2 次 |

## 标准启动模板

### 示例 1：试水 - 11 个 repo，全部并行，跳过 Phase 8

```bash
source /home/chenzhirong/mshrl/.venv/bin/activate && \
eval $(curl -fsSL proxy.msh.work:3128/env --noproxy proxy.msh.work) && \
export GITHUB_TOKEN="ghp_xxx" && \
cd /home/chenzhirong/VeriMine && \
bash verilog_mining/run_verilog.sh \
    verilog-test-v1 \
    0 \
    11 \
    cpp-task-rollout-2 \
    cpp-task-rollout-2 \
    11 \
    3 \
    true \
    600 \
    9999 \
    "" \
    0
```

**说明**：Actor 模型为空、验证次数为 0 = 跳过 Phase 8（Real Test Validation）

### 示例 2：正式实验 - 单 Actor 模型

```bash
source /home/chenzhirong/mshrl/.venv/bin/activate && \
eval $(curl -fsSL proxy.msh.work:3128/env --noproxy proxy.msh.work) && \
export GITHUB_TOKEN="ghp_xxx" && \
cd /home/chenzhirong/VeriMine && \
bash verilog_mining/run_verilog.sh \
    verilog-rollout-v1 \
    0 \
    11 \
    cpp-task-rollout-2 \
    cpp-task-rollout-2 \
    11 \
    3 \
    true \
    600 \
    9999 \
    cpp-task-rollout-2 \
    4
```

### 示例 3：多 Actor 模型 + 不同验证次数

```bash
source /home/chenzhirong/mshrl/.venv/bin/activate && \
eval $(curl -fsSL proxy.msh.work:3128/env --noproxy proxy.msh.work) && \
export GITHUB_TOKEN="ghp_xxx" && \
cd /home/chenzhirong/VeriMine && \
bash verilog_mining/run_verilog.sh \
    verilog-multi-model-v1 \
    0 \
    11 \
    cpp-task-rollout-1 \
    cpp-task-rollout-1 \
    11 \
    3 \
    true \
    600 \
    9999 \
    "cpp-task-rollout-1,claude-opus-4-5-20251101" \
    "4,1"
```

### 示例 4：50 个 repo，分批运行（用 nohup）

```bash
nohup bash -c '
source /home/chenzhirong/mshrl/.venv/bin/activate && \
eval $(curl -fsSL proxy.msh.work:3128/env --noproxy proxy.msh.work) && \
export GITHUB_TOKEN="ghp_xxx" && \
cd /home/chenzhirong/VeriMine && \
bash verilog_mining/run_verilog.sh \
    verilog-50repo-v1 \
    0 \
    50 \
    cpp-task-rollout-2 \
    cpp-task-rollout-2 \
    10 \
    3 \
    true \
    600 \
    9999 \
    cpp-task-rollout-2 \
    4
' > ~/verilog-50repo-v1.log 2>&1 &
```

**说明**：50 个 repo，每批 10 个并行，分 5 批运行，每批间隔 7 小时。

## 参数说明表

| # | 参数名 | 示例值 | 说明 |
|---|--------|--------|------|
| 1 | 实验名 | verilog-task-gen-v1 | 唯一标识符 |
| 2 | 起始索引 | 0 | 从第几个 repo 开始 |
| 3 | 结束索引 | 11 | 到第几个 repo 结束 |
| 4 | 模型 | cpp-task-rollout-2 | PR 挖掘和任务生成模型 |
| 5 | Flash 模型 | cpp-task-rollout-2 | 快速处理模型 |
| 6 | **并发数** | **11** | **建议 = 结束索引 - 起始索引** |
| 7 | PR Top N | 3 | 每个 repo 选取前 N 个 PR |
| 8 | 要求 Code+Test | true | PR 必须同时包含代码和测试 |
| 9 | PR 超时 | 600 | 单个 PR 处理超时（分钟） |
| 10 | Repo 超时 | 9999 | 单个 repo 所有 PR 超时 |
| 11 | **Actor 模型** | `"model1,model2"` | 验证阶段使用的模型（**逗号分隔多个**） |
| 12 | **验证次数** | `"4,1"` | 每个 actor 模型运行次数（**逗号分隔，与模型对应**） |

## 输入数据

Repo 列表位于：`verilog_mining/data/repo_list.jsonl`

当前已有 11 个 repo（来自 VeriMine pipeline 筛选）。如需更多 repo：
1. 设置 GITHUB_TOKEN
2. 修改 `config.json` 降低 `min_stars`（如改为 50）
3. 运行 `python -m hwrepo_pipeline --config config.json`
4. 运行 `bash verilog_mining/scripts/filter_repos.sh`

## 可用模型列表

| 模型名 | 类型 | 说明 |
|--------|------|------|
| cpp-task-rollout-1 | 内部部署 | Claude Code 兼容 |
| cpp-task-rollout-2 | 内部部署 | Claude Code 兼容 |
| claude-opus-4-5-20251101 | Qianxun | Opus 4.5 |
| claude-sonnet-4-5-20250929 | Qianxun | Sonnet 4.5 |
| infinigence/kimi-k2.5 | Qianxun | Kimi K2.5 |

## 监控批次状态

启动实验后，从日志中找到数字格式的 batch_id（如 `62219`），然后运行：

```bash
source /home/chenzhirong/mshrl/.venv/bin/activate && \
eval $(curl -fsSL proxy.msh.work:3128/env --noproxy proxy.msh.work) && \
python << 'EOF'
import asyncio
from agentgym.rollout.rollout_batch_manager import RolloutBatchManager

async def check(batch_id):
    manager = RolloutBatchManager()
    p = await manager.progress(batch_id)
    print(f'Batch {batch_id}:')
    print(f'  Status: {p.batch_status}')
    print(f'  Running: {p.running_tasks}')
    print(f'  Succeeded: {p.succeeded_tasks}')
    print(f'  Failed: {p.failed_tasks}')

asyncio.run(check(62219))  # 替换为实际的数字 batch_id
EOF
```

**注意**：batch_id 必须是数字（如 `62219`），不是字符串名称。

## 常见错误

| 错误 | 原因 | 解决 |
|------|------|------|
| 并发数设置不当 | CONCURRENCY 与 repo 数量不匹配 | 试水阶段建议 CONCURRENCY = repo 数量 |
| GitHub rate limit | Token 配额用尽 | 先检查 limit，或等待 reset |
| `agentgym` not found | 未激活 venv | `source /home/chenzhirong/mshrl/.venv/bin/activate` |
| 网络超时 | 未启动代理 | `eval $(curl -fsSL proxy.msh.work:3128/env --noproxy proxy.msh.work)` |
| batch_id 422 错误 | 用了字符串格式的 batch name | 使用数字格式的 batch_id |
| 分批实验后续批次未启动 | 关闭电脑/退出 Claude Code | 使用 nohup 或 tmux 运行长时间实验 |
| 商业 EDA 工具报错 | PR 依赖 VCS/Questa | 正常现象，pipeline 会自动跳过这类 PR |
| iverilog 编译失败 | 缺少 -g2012 标志 | 检查 run-tests.sh 是否使用 `-g2012` |
| cocotb 找不到 | sandbox 未安装 cocotb | 检查 setup_env.sh 是否安装了 cocotb |

## Verilog 特有注意事项

1. **eda-sandbox 镜像**：所有 repo 使用同一个 `eda-sandbox:agent` 镜像，镜像内预装 iverilog、verilator
2. **cocotb 需要 pip 安装**：setup_env.sh 会在 sandbox 内安装 cocotb
3. **仓库克隆**：entrypoint.sh 会在 sandbox 内 git clone 仓库（与 C++ 不同，C++ 用的是预构建的 Docker 镜像）
4. **Task Type**：支持 `new_feature`、`bugfix`、`all` 三种类型，默认 `all` 同时挖掘两种
5. **SystemVerilog 支持**：iverilog 需要 `-g2012` 标志才能支持 SystemVerilog 语法
