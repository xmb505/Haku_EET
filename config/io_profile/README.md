# IO Profile 说明

三个 profile 文件，由 `config.yaml` 顶层 `io_profile` 字段选择（冷切换，改完需重启）。

## 文件对照

| Profile | 文件 | 特点 | 用途 |
|---------|------|------|------|
| `local_train` | `local_train.yaml` | 10 层 6 部车, 含超载布尔, 无重量 Word | 线下训练（无重量监控） |
| `competition` | `competition.yaml` | 无超载布尔, 自动运行 DBX27.0, 有载重 Word | 2026 CIMC 比赛现场 |
| `local_with_weight` | `local_with_weight.yaml` | 同 local_train + DBW28 载重 Word | 线下测试满载逻辑 |

## 切换方法

1. 修改 `config/config.yaml` 顶层 `io_profile` 字段为 `local_train` / `competition` / `local_with_weight`
2. 重启程序

> **注意**：`/reload` 不会切换 profile（冷切换设计），需重启。

## 重量功能

- `local_train`：无 weight_word 配置，`mapper.addr_word_input('weight', 1)` 抛 KeyError，满载守卫不触发
- `competition`：通过 `POST /word_read` 定期/按需读 PLC DBW28~38
- `local_with_weight`：仅在 TIA 手动输入 DBW28 测试满载逻辑

## competition.yaml 地址说明

本文件是从 `local_train.yaml` 删去各车 `overload:` 行后生成的模板。
**地址未逐位偏移**——若需精确匹配比赛 IO 表，使用 `tools/gen_io_config.py --profile competition` 重新生成。
