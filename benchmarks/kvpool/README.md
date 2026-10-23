# KVPool SLO 开箱测试

`slo_search.py` 用于寻找指定延迟限制下，最高达标的并发数及对应吞吐。例如，在 **TTFT P90 < 10s、TPOT P90 < 20ms** 的要求下，自动调整并发并保存各轮结果。默认不复测边界，也可按需开启 confirm。

工具通过命令行调用 `aisbench_test.py`，可以与原项目放在不同目录。它自身只依赖 Python 3.9+ 标准库；执行压测的环境仍需具备原项目和 AISBench 所需依赖。

本目录为 KVPool 特性提供前缀缓存工作负载的 SLO 开箱测试入口。服务和存储后端需事先按 [KV Cache Pool 部署指南](../../docs/source/user_guide/feature_guide/kv_cache_pool_ascend_store.md) 启动；脚本只负责发压、搜索和记录，不启动或修改模型服务，也不会自动配置 KVPool。

执行链路为 `slo_search.py → aisbench_auto_tools_prefix/aisbench_test.py → AISBench → 模型服务`。外部依赖项目为 [aisbench_auto_tools_prefix](https://github.com/rayn-zzz/aisbench_auto_tools_prefix)，本目录不包含该项目或 AISBench。使用之前需在压测环境中单独准备并配置它们。

## 1. 运行前准备

1. 模型服务已启动，能够从压测环境访问。
2. 原项目完整，包含 `aisbench_test.py`、`config.py`、`default_api.py` 和数据生成文件。
3. 已配置原项目 `config.py` 中的 AISBench 路径、模型名、tokenizer 路径、服务地址、鉴权和数据集路径。前缀测试还需配置正确的 `POD_INFO`；留空时原项目使用 `HOST_IP:HOST_PORT` 查询缓存指标。
4. 在实际压测环境中执行工具，使用的 Python 能运行原项目，且 `ais_bench` 命令在该环境的 PATH 中。

原项目运行时会生成配置、数据集和日志，并更新 AISBench 的配置及数据集软链接，因此相应目录需要可写。同一份原项目同时只运行一个搜索器，也不要同时手动执行 `aisbench_test.py`。

以下示例均为 Bash 命令。从 vllm-ascend 仓库根目录执行 `cd benchmarks/kvpool`，然后将 `/path/to/aisbench_auto_tools_prefix` 替换为已配置好的原项目目录，在 `slo_search.py` 所在目录执行。

## 2. 快速开始

### 普通数据集：按 P90 搜索

下面使用 2048 token 输入、1024 token 输出，要求 TTFT P90 < 10s、TPOT P90 < 20ms，最大搜索并发设为 128：

```bash
python slo_search.py \
  --project-dir /path/to/aisbench_auto_tools_prefix \
  --ttft-limit-ms 10000 \
  --tpot-limit-ms 20 \
  --max-concurrency 128 \
  -- --input_len 2048 --output_len 1024
```

默认使用 P90，从并发 8 开始，每轮完整请求数为并发的 4 倍，倍增和二分结束后直接输出结果，不执行 confirm。每轮同时输出平均值和 P90 的 TTFT、TPOT，搜索完成后打印最终汇总及结果目录。

独立的 `--` 分隔两类参数：

- **之前**是搜索参数，名称使用连字符，例如 `--max-concurrency`。
- **之后**是原项目参数，例如 `--input_len`、`--output_len`，会原样传给原项目。

### 前缀测试：64K 输入、1K 输出、90% 前缀

```bash
python slo_search.py \
  --project-dir /path/to/aisbench_auto_tools_prefix \
  --ttft-limit-ms 10000 \
  --tpot-limit-ms 20 \
  --max-concurrency 128 \
  --prefix-num-equals-concurrency \
  -- \
  --input_len 65536 \
  --output_len 1024 \
  --dataset_type prefix_cache \
  --repeat_rate 0.9 \
  --prefix_test
```

该命令在每轮完整压测前先预热前缀，并让前缀种类数随并发一起变化。例如，并发 8 时使用 8 种前缀、32 条完整请求，每种前缀对应 4 条请求。预热请求是额外的，不包含在这 32 条中。

`--repeat_rate 0.9` 表示生成数据中公共前缀约占输入的 90%，不保证实际命中率恰好等于 90%；实际值查看每轮的 `hbm_hit_rate` 和 `external_hit_rate`。

### 仅测试 Prefill 延迟：64K 输入、1 token 输出

以下命令仅约束平均 TTFT < 10 秒，不执行 confirm。每轮请求数默认是并发的 4 倍，前缀种类数与并发一致：

```bash
python slo_search.py \
  --project-dir /path/to/aisbench_auto_tools_prefix \
  --ttft-limit-ms 10000 \
  --percentile mean \
  --max-concurrency 128 \
  --prefix-num-equals-concurrency \
  -- \
  --input_len 65536 \
  --output_len 1 \
  --dataset_type prefix_cache \
  --repeat_rate 0.9 \
  --prefix_test
```

每轮仍同时展示 TTFT mean/P90。单 token 输出没有后续 token 间隔，TPOT 显示为 `N/A`。只有平均 TTFT 参与延迟判定，P90 超过 10 秒不会单独导致该轮不达标。

### 使用平均延迟

在搜索参数中加上：

```bash
--percentile mean
```

此时两个条件改为 **平均 TTFT < 10000ms、平均 TPOT < 20ms**。TTFT 和 TPOT 共用同一统计口径，目前不支持“平均 TTFT + TPOT P90”这样的混合配置。平均值达标不代表尾部请求延迟达标。

无论选择哪种 SLO 口径，每轮的 PASS/FAIL 摘要、终端表格、`metrics.txt`、CSV 和 JSON 都会同时展示或保存平均 TTFT、TTFT P90、平均 TPOT、TPOT P90。摘要通过 `SLO: mean` 或 `SLO: P90` 等标记注明判定口径；使用其他分位数时，还会额外展示该分位数的值。显示这些指标不会同时启用多个 SLO 条件；判定仍只使用 `--percentile` 选中的一组。

### 只约束 TTFT

只传 `--ttft-limit-ms`，不传 `--tpot-limit-ms`，即可只用 TTFT 判断 SLO。例如 `--ttft-limit-ms 10000 --percentile mean` 表示仅约束平均 TTFT < 10s。也可以只传 TPOT 阈值；两种阈值至少指定一种。

当原项目参数为 `--output_len 1` 时，只有首个输出 token，没有后续 token 间隔，因此 TPOT 不适用。工具将 TPOT 显示为 `N/A`，记录为 `null`，并设置 `tpot_status=not_applicable_single_token`；该场景不能指定 TPOT 阈值。TTFT 的平均值和 P90 仍正常记录。

## 3. 达标判定与搜索过程

### 一轮怎样才算达标

一轮必须满足所有已指定的延迟阈值及最低请求成功率：

```text
若指定 TTFT 阈值：选定统计口径的 TTFT < --ttft-limit-ms
若指定 TPOT 阈值：选定统计口径的 TPOT < --tpot-limit-ms
请求成功率 >= --min-success-rate
```

两个延迟阈值的单位均为 **毫秒**，并严格使用 `<`。例如 TPOT 为 20ms 时，不满足 `< 20ms`。默认要求请求成功率为 100%。

`--percentile` 默认是 P90。也可选 `mean` 或日志实际输出的其他分位数，如 `99`；日志没有输出所选分位数时会报错，不会插值或改用其他列。两个分位数各自达标，并不表示同样比例的请求同时满足两个条件。

`--stage total` 默认读取 AISBench 汇总表中 Stage 为 `total` 的完整压测统计，前缀预热指标单独排除。该参数只选择结果阶段，不改变发压方式，也不会启用稳态测试。若已配置稳态汇总器，需按日志实际提供的阶段名称选择。

### 什么时候出现 confirm

终端和历史记录中的 `phase` 表示当前阶段：

| 阶段 | 含义 |
| --- | --- |
| `expand` | 从起始并发倍增，直到首次不达标或达到搜索上限 |
| `bisect` | 在已通过和未通过的并发之间二分，直到边界相邻；起始并发不通过时也会向下搜索 |
| `confirm` | 可选：重新测试候选边界及邻点，确认结果是否可重复 |

默认 `--confirm-runs 0`，不执行 `confirm`，搜索结束后直接返回找到的最高达标并发及该搜索轮次的吞吐。这是单轮搜索结果，尚未经过额外复测。

如需复测，在 `--` 之前添加：

```bash
--confirm-runs 2
```

例如，搜索得到“并发 10 通过、11 不通过”，启用两轮 confirm 后，会按 **11、10、9** 的顺序，每点重新测试两轮。只有两轮都达标的点才确认通过，前面的搜索轮次不计入这两轮。也可设置其他正整数控制轮数。

`--boundary-radius` 仅在启用 confirm 时生效，控制邻点范围，默认 1；设为 0 时只复测候选点。若复测范围内没有确认通过的并发，工具会继续逐点向下复测，直到找到通过点或达到最小并发。

启用 confirm 时，最终输出**已确认通过的最高并发**及其确认轮次的平均吞吐。搜索依赖“并发越高，越容易违反 SLO”的近似单调假设，不是对整个范围逐点穷举。结果仅适用于本次输入输出长度、前缀配置、服务状态和 SLO 口径。

## 4. 参数与默认值

### SLO 和搜索范围

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--ttft-limit-ms` | 不指定 | TTFT 严格上限，单位 ms；两种延迟阈值至少指定一种 |
| `--tpot-limit-ms` | 不指定 | TPOT 严格上限，单位 ms；省略时不约束 TPOT |
| `--percentile` | `90`（P90） | 两种延迟共用的统计口径；平均值用 `mean` |
| `--stage` | `total` | 读取日志中指定 Stage 的延迟、吞吐和请求统计 |
| `--min-success-rate` | `1.0` | 最低请求成功率，例如 `0.99` 表示 99% |
| `--min-concurrency` | `1` | 最低搜索并发 |
| `--start-concurrency` | `8` | 起始并发 |
| `--max-concurrency` | `1024` | 搜索上限，建议按测试范围显式指定 |
| `--confirm-runs` | `0` | 默认不复测；设为正整数时，每点额外测试相应轮数，全部通过才确认 |
| `--boundary-radius` | `1` | 启用 confirm 时，候选边界左右各复测多少个整数邻点 |

并发参数需满足 `1 <= min-concurrency <= start-concurrency <= max-concurrency`。例如最大并发设为 4 时，还需要把起始并发改为不超过 4 的值。

### 请求量和前缀

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--requests-per-concurrency` | `4` | 每轮完整请求数相对于并发的倍数 |
| `--min-data-num` | `0` | 可选的最低完整请求数，默认不设下限 |
| `--data-num` | 不指定 | 固定完整请求数；指定后不再按并发计算，且必须不小于最大搜索并发 |
| `--prefix-num-equals-concurrency` | 关闭 | 每轮设置原项目的 `--prefix_num` 等于该轮并发 |

不指定固定请求数时，生成数据的每轮请求量为：

```text
max(min-data-num, 当前并发 × requests-per-concurrency)
```

启用前缀联动时，必须在 `--` 后指定 `--dataset_type prefix_cache`，不能再传固定的 `--prefix_num` 或外部 `--dataset`。不开启联动时，可在 `--` 后用 `--prefix_num` 指定固定前缀种类数。

是否预热由原项目参数 `--prefix_test` 控制，预热并发由所用原项目版本及 `--dp` 决定。工具不会清空服务缓存；比较各轮结果时，应保持服务配置和缓存测试条件一致。

原项目的 `--dataset` 可用于指定已有数据集，但实际请求量由该数据集决定，搜索器不会自动扩充它。需要自行确保数据量足以测试目标并发。

搜索器统一设置原项目的 `--concurrency`、`--request_rate 0`、`--repeat 1` 和 `--data_num`，这些参数不能放在 `--` 后；不支持透传 `--test_accuracy`。

### 路径和执行控制

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--project-dir` | `slo_search.py` 所在目录 | 原项目目录；工具单独存放时需要指定 |
| `--python` | 启动搜索器的 Python | 执行原项目的 Python，可指定其他环境中的解释器路径 |
| `--output-dir` | `<project-dir>/slo_results/<时间戳>` | 本次结果目录，必须尚不存在 |
| `--timeout` | `3600` 秒 | 每轮子进程的超时阈值，包含该轮数据准备、前缀预热和完整压测 |
| `--latency-unit` | 不指定 | 仅在日志没有时间单位时显式指定 `ms` 或 `s` |

`--timeout` **不是整个搜索的总时限**。任意一轮超时，工具会终止该轮子进程树并中止搜索，保留已有记录。当前没有整体超时参数。

完整帮助：

```bash
python slo_search.py --help
```

## 5. 查看结果和日志

### 结果保存在哪里

不指定 `--output-dir` 时，默认保存到原项目目录下，而不是执行命令时的当前目录：

```text
<project-dir>/slo_results/<时间戳>/
├── settings.json          本次配置和原项目参数
├── summary.json           最终结论或中止原因
├── history.csv            各轮指标，适合用表格软件查看
├── history.jsonl          各轮完整记录及实际命令
└── trial_0001/            后续轮次依次编号
    ├── command.json       本轮执行命令
    ├── console.log        本轮原项目完整输出及指标汇总表
    ├── metrics.txt        本轮带列名的指标汇总表
    └── result.json        本轮完整指标和判定
```

若显式指定相对路径 `--output-dir`，它相对于执行搜索器时的当前目录。重新测试请使用新的结果目录，工具不覆盖已有结果。

### 最高达标并发和吞吐

将 `RESULT_DIR` 替换为终端最后打印的结果目录：

```bash
RESULT_DIR=/path/to/result_directory
python -m json.tool "$RESULT_DIR/summary.json"
```

| 字段 | 含义 |
| --- | --- |
| `status` | `complete` 表示搜索完成；`aborted` 表示异常中止，此时查看 `error` |
| `highest_passing_concurrency` | 最高达标并发；启用 confirm 时必须复测通过；`null` 表示没有找到对应达标点 |
| `result_source` | `search` 为搜索结果，`confirm` 为额外复测结果 |
| `active_slo_metrics` | 实际参与达标判断的延迟指标，例如仅 TTFT 时为 `["ttft"]` |
| `confirmation_performed` | 是否执行了 confirm，默认 `false` |
| `result_trial_ids` | 产生最终结果的轮次编号 |
| `throughput.output_tokens_per_s_mean` | 结果轮次的平均输出吞吐，token/s |
| `throughput.requests_per_s_mean` | 结果轮次的平均请求吞吐，req/s |
| `throughput.prefill_tokens_per_s_mean` | 结果轮次的平均 Prefill 吞吐，token/s |
| `worst_ttft_ms`、`worst_tpot_ms` | 结果轮次中最大的所选延迟统计值，ms |
| `result_cache_metrics` | 结果轮次的缓存命中率及命中/查询计数 |
| `confirmation_trial_ids`、`confirmation_cache_metrics` | 额外复测结果；不执行 confirm 时为空数组 |
| `at_search_limit` | 为 `true` 表示结果达到搜索上限，可提高上限重新测试 |

未执行 confirm 时，吞吐来自最终并发的单个搜索轮次，字段中的 `_mean` 等于该轮实测值。启用 confirm 时，吞吐均值只来自最终选中并发的确认轮次；“最差 P90”表示这些轮次各自 P90 的最大值，不是把所有请求合并后重新计算 P90。

### 历次测试记录

```bash
cat "$RESULT_DIR/history.csv"
cat "$RESULT_DIR/trial_0001/metrics.txt"
```

`history.csv` 中可查看 `concurrency`、`prefix_num`、`data_num`、`phase`、`status`、TTFT/TPOT、吞吐、请求成功率和缓存指标。`status` 为 `pass`、`fail`、`error`，分别表示达标、不达标和测试无效；测试无效会中止搜索，不作为不达标点继续二分。

每轮延迟字段 `ttft_mean_ms`、`ttft_p90_ms`、`tpot_mean_ms`、`tpot_p90_ms` 分别记录平均值和 P90，单位均为 ms。`ttft_ms`、`tpot_ms` 保留为 SLO 所选口径的值，结合 `percentile` 列判断。

### HBM 和 external 命中率

每轮汇总表包含 `hbm_hit_rate` 和 `external_hit_rate`。历史记录对应字段为 `hbm_hit_rate_pct`、`external_hit_rate_pct`，范围为 0～100，并保存各自的 `*_hits`、`*_queries` 和 `*_cache_status`。

- 命中率按完整压测期间的命中/查询计数增量计算，使用有效节点的汇总计数，不平均各节点百分比。
- `external` 保留服务的外部 KV 缓存含义，不直接标为 DRAM。
- `N/A (no_queries)` 表示查询量为 0，命中率无法定义；查询量大于 0 但没有命中时才是 0%。
- `N/A (not_reported)` 表示本轮日志未提供缓存汇总，例如未开启前缀预热。JSON 中对应值为 `null`，CSV 中为空，并保留状态。
- 缓存指标的统计区间是完整压测；即使选择稳态 Stage，也不能把这些缓存指标视为只属于稳态。

Prefill 吞吐使用 AISBench 的 `Prefill Token Throughput`。Prefill 和缓存指标用于观测，当前不参与 SLO 达标判断。

### 保存工具自身的整体日志

搜索器默认将进度和汇总输出到终端，不自动创建整体日志文件。可在 Bash 中使用：

```bash
set -o pipefail
python slo_search.py \
  --project-dir /path/to/aisbench_auto_tools_prefix \
  --ttft-limit-ms 10000 \
  --tpot-limit-ms 20 \
  --max-concurrency 128 \
  -- --input_len 2048 --output_len 1024 \
  2>&1 | tee slo_search.log
```

`slo_search.log` 保存在执行命令时的当前目录。原项目自身的 `aisbench.log`、`aisbench_all.log` 则在 `--project-dir` 目录中；每轮独立日志以结果目录内的 `trial_*/console.log` 为准。

## 6. 常见问题

### 工具必须和原项目放在一起吗？

不需要。通过 `--project-dir` 指定原项目目录即可。若两个工具使用不同 Python 环境，再通过 `--python /path/to/python` 指定执行原项目的解释器；同时确保该子进程环境能找到正确的 `ais_bench` 命令。

### 提示缺少分位数、Prefill 吞吐或请求统计

查看出错轮次的 `console.log`。当前 AISBench 日志需有带平均值、P90 列的 TTFT/TPOT 汇总表，以及 `Prefill Token Throughput`、`Output Token Throughput`、`Request Throughput`、`Total Requests` 和 `Failed Requests`。即使 SLO 使用平均值或其他分位数，平均值和 P90 也必须存在，保证每轮完整记录。时间单位可由日志提供；日志不含单位时需显式指定 `--latency-unit`。

选择日志实际提供的分位数与 Stage。若日志有不同的输出格式，需要适配该版本后再运行；工具不会改用其他分位数或旧 CSV 的值继续搜索。

### 前缀测试提示缺少或无效的缓存汇总

启用 `--prefix_test` 时，原项目必须输出完整压测的 `Prefix cache hit summary`，包含 `ALL_PODS` 汇总以及 HBM、external 的命中/查询计数。检查原项目 `POD_INFO`、服务 `/metrics` 的可访问性和相应指标。节点被跳过、计数异常或汇总缺失时工具会中止。

### 提示目录已存在或另一个搜索器占用

结果目录已存在时，换一个 `--output-dir`，或省略该参数自动生成新目录。

原项目目录下的 `.slo_search.lock` 用于防止多个搜索器同时运行。正常退出会释放锁；若进程被强制结束，先确认相关搜索和压测进程均已停止，再删除遗留锁。

### 中途失败或超时，之前的结果还在吗？

已完成轮次会逐轮保存。先查看 `summary.json` 中的 `error`，再查看对应 `trial_*/console.log`。子进程非零退出、日志出现 ERROR/Traceback、必要指标缺失或超时都会中止搜索，不能把这种情况当作正常的 SLO 不达标。

### 退出码是什么意思？

| 退出码 | 含义 |
| --- | --- |
| `0` | 搜索完成，找到达标并发；启用 confirm 时已复测通过 |
| `1` | 搜索完成，但没有找到符合当前搜索／复测要求的达标点 |
| `2` | 参数、执行或解析错误，搜索未正常完成 |

## 7. 验证工具本身

在 vllm-ascend 仓库根目录运行：

```bash
python -m unittest discover -s tests/ut/benchmarks -p test_kvpool_slo_search.py
```

这些测试使用模拟的原项目命令行输出，覆盖日志解析、搜索边界、可选 confirm、单 token 输出、前缀数量联动及 mean/P90 展示，不需要模型服务、AISBench 或 NPU。
