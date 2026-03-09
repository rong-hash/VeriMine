# Skill: 分析 Rollout 结果 (Analyze Verilog Rollout Stats)

## 用途

分析 Verilog PR 挖掘实验的 rollout 输出目录，统计成功生成的 task 数量及其 model validation 结果。

## 使用方式

```bash
/analyze-rollout <rollout输出目录路径>
```

**示例：**
```bash
/analyze-rollout /mnt/workspace/verilog-task-gen-v1/output
```

## 运行脚本

```bash
source /home/chenzhirong/mshrl/.venv/bin/activate && \
cd /home/chenzhirong/VeriMine && \
python verilog_mining/filter_valid_tasks.py <rollout输出目录路径> --report report.json
```

或者直接用 Python 分析：

```bash
source /home/chenzhirong/mshrl/.venv/bin/activate && \
python3 << 'PYEOF'
import json
from pathlib import Path

output_dir = Path("<rollout输出目录路径>")

# 找到所有 task 目录
task_dirs = sorted(output_dir.rglob("task_*"))
task_dirs = [d for d in task_dirs if d.is_dir() and (d / "task.json").exists()]

print(f"Total task folders: {len(task_dirs)}")

# 统计
has_summary = 0
has_validation = 0
validation_pass = 0
has_score_1 = 0
has_variation = 0
all_zero = 0
task_types = {"new_feature": 0, "bugfix": 0, "unknown": 0}

for td in task_dirs:
    # Check task.json for task_type
    tj = td / "task.json"
    if tj.exists():
        meta = json.loads(tj.read_text())
        tt = meta.get("task_type", "unknown")
        task_types[tt] = task_types.get(tt, 0) + 1

    # Check commit_test_validation
    cv = td / "commit_test_validation" / "result.json"
    if cv.exists():
        has_summary += 1
        data = json.loads(cv.read_text())
        val = data.get("validation", "UNKNOWN")
        if val == "PASS":
            validation_pass += 1

    # Check validate_difficulty (Phase 8)
    vd = td / "validate_difficulty" / "summary.json"
    if vd.exists():
        has_validation += 1
        summary = json.loads(vd.read_text())
        for model_name, model_data in summary.items():
            scores = model_data.get("scores", [])
            if scores:
                if max(scores) >= 1.0:
                    has_score_1 += 1
                if len(set(scores)) > 1:
                    has_variation += 1
                if all(s == 0 for s in scores):
                    all_zero += 1

print(f"\n📊 SUMMARY STATISTICS")
print(f"{'='*60}")
print(f"Total task folders:                          {len(task_dirs)}")
print(f"Tasks with validation result:                {has_summary}")
print(f"Tasks with PASS validation:                  {validation_pass}")
print(f"Tasks with model validation (Phase 8):       {has_validation}")
print(f"\n📂 TASK TYPES:")
for tt, count in task_types.items():
    print(f"  {tt}: {count}")
print(f"\n📈 MODEL VALIDATION (if Phase 8 ran):")
print(f"Tasks with score = 1.0:                      {has_score_1}")
print(f"Tasks with variation:                        {has_variation}")
print(f"Tasks with all scores = 0:                   {all_zero}")
PYEOF
```

## 统计指标说明

### 基础统计

| 指标 | 说明 |
|------|------|
| Total task folders | 输出目录中所有 task_* 文件夹数量 |
| Tasks with validation result | 有 commit_test_validation/result.json 的 task |
| Tasks with PASS validation | Phase 7 验证通过（base 失败 + target 成功） |
| Tasks with model validation | Phase 8 actor model 验证完成 |

### Task Type 统计

| 指标 | 说明 |
|------|------|
| new_feature | 新增 RTL 模块/接口/功能的任务 |
| bugfix | 修复 RTL 逻辑错误/时序问题的任务 |

### Model Validation 统计（Phase 8，如果运行了）

| 指标 | 说明 | 意义 |
|------|------|------|
| **Tasks with score = 1.0** | 验证中任意一次得分为1.0 | 模型能完全解决 |
| **Tasks with variation** | 验证得分不全相同 | 任务具有一定难度 |
| **Tasks with all scores = 0** | 验证全部为0 | 任务可能过难或有问题 |

### 判断任务有效的条件

1. **必须文件**：task.json, task.md, test.patch, code.patch, run-tests.sh
2. **Phase 7 验证通过**：commit_test_validation/result.json 中 validation = "PASS"
3. **含义**：base + test.patch 失败，base + test.patch + code.patch 成功

## 输出结构说明

Verilog Rollout 输出目录结构：

```
output/
├── verilog-0/                     # Sandbox 容器（repo 级别）
│   ├── task_a1b2c3d4/             # Task 文件夹（PR 级别）
│   │   ├── task.json              # 元数据（repo, pr_number, task_type）
│   │   ├── task.md                # Query（用户需求描述）
│   │   ├── code.patch             # RTL 实现代码补丁
│   │   ├── test.patch             # 测试/testbench 补丁
│   │   ├── run-tests.sh           # 一键仿真脚本
│   │   ├── generate_query/        # Phase 2 输出
│   │   ├── quality_check/         # Phase 3 输出
│   │   ├── organize_tests_unified/# Phase 4 输出
│   │   ├── test_environment_validation/  # Phase 5 输出
│   │   ├── test_query_validation/ # Phase 6 输出
│   │   ├── commit_test_validation/# Phase 7 输出（关键！）
│   │   │   └── result.json        # base vs target 验证结果
│   │   └── validate_difficulty/   # Phase 8 输出（如果运行了）
│   │       └── {model_name}/
│   │           ├── run_1/
│   │           ├── run_2/
│   │           └── summary.json
│   ├── pr_summary.json            # Repo 级别的 PR 摘要
│   └── task_generator.log         # 日志
├── verilog-1/
...
```

## 快速验证命令

### 检查有多少 task 目录
```bash
find <output_dir> -name "task.json" -path "*/task_*/task.json" | wc -l
```

### 检查 Phase 7 验证结果
```bash
find <output_dir> -name "result.json" -path "*/commit_test_validation/*" -exec python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
val = data.get('validation', 'UNKNOWN')
target_pr = data.get('target_commit', {}).get('pass_rate', 0)
base_pr = data.get('base_commit', {}).get('pass_rate', 0)
print(f'{sys.argv[1]}: {val} (target={target_pr:.0%}, base={base_pr:.0%})')
" {} \;
```

### 检查 task type 分布
```bash
find <output_dir> -name "task.json" -path "*/task_*/task.json" -exec python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
print(f'{data.get(\"task_type\", \"unknown\"):15s} PR#{data.get(\"pr_number\", \"?\")} {data.get(\"repo\", \"\")}')
" {} \; | sort | uniq -c | sort -rn
```

## 相关文件

| 文件 | 说明 |
|------|------|
| `verilog_mining/filter_valid_tasks.py` | 任务过滤和报告脚本 |
| `verilog_mining/run_verilog.sh` | 启动 rollout 的脚本 |
| `verilog_mining/verilog_task_generator.py` | 核心任务生成器 |
| `.claude/skills/run-experiment/SKILL.md` | 运行实验的 skill |
