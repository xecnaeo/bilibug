# B站视频与评论采集框架

匿名、低频地采集 B站公开视频元数据、互动指标和评论，保存到 SQLite，并导出为 CSV 或 JSONL。

当前版本：`0.3.0`，采用 [MIT License](LICENSE)。完整设计和限制见 [项目报告](PROJECT_REPORT.md)。

## 功能

- BV 号或视频 URL 输入，支持顺序处理多个目标；
- 热门或时间顺序的一级评论；
- 可选抓取完整楼中楼；
- 视频基础信息、分P和互动指标快照；
- 评论当前记录与每次抓取观测历史；
- SQLite 分页检查点和中断续抓；
- v0.1 数据库自动迁移到 v2；
- 评论、视频指标和评论指标的 CSV/JSONL/Parquet 导出；
- 本地数据状态检查；
- CSV 目标清单、顺序批处理、失败汇总和批次恢复；
- 可选 Parquet 导出；
- 默认离线的脱敏契约测试。

## 安装

需要 Python 3.12 或更高版本：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

## 使用

```powershell
# 与 v0.1 相同：热门顺序，只抓一级评论
bili-comments crawl BV1xx411c7mD

# 时间顺序并抓取完整楼中楼
bili-comments crawl BV1xx411c7mD --order time --replies all

# 指定数据库和多个目标
bili-comments --db result.db crawl BV1xx411c7mD BV1yy411c7mE

# 导出当前评论
bili-comments --db result.db export BV1xx411c7mD --entity comments --format csv --output comments.csv

# 导出视频或评论的历史观测
bili-comments --db result.db export BV1xx411c7mD --entity video-stats --format jsonl --output video-stats.jsonl
bili-comments --db result.db export BV1xx411c7mD --entity comment-stats --format jsonl --output comment-stats.jsonl

# 查看本地视频、评论数量和最近运行状态
bili-comments --db result.db inspect BV1xx411c7mD
```

## 批量采集

创建 UTF-8 CSV：

```csv
target,order,replies,enabled
BV1xx411c7mD,time,root,true
https://www.bilibili.com/video/BV1yy411c7mE,hot,all,true
```

执行和恢复：

```powershell
bili-comments --db result.db batch run targets.csv
bili-comments --db result.db batch status
bili-comments --db result.db batch status 1
bili-comments --db result.db batch resume 1
```

批次按 CSV 顺序执行并共享全局限速。单项失败不会阻塞后续目标；全部成功返回退出码 `0`，部分失败返回 `1`，清单配置错误返回 `2`。默认摘要写入 `data/batches/<batch_id>.json`。

`batch resume` 使用数据库中固化的任务快照，只重试失败、未开始或中断中的项目，不重新读取原 CSV。

## Parquet

```powershell
pip install -e ".[parquet]"
bili-comments --db result.db export BV1xx411c7mD --entity comments --format parquet --output comments.parquet
```

Parquet 是可选依赖；默认安装仍只包含 HTTP 客户端。

## Windows 任务计划

推荐由任务计划程序定时调用一次批处理，不让采集器常驻运行。示例配置：

```text
程序: powershell.exe
参数: -NoProfile -Command "& 'X:\project\.venv\Scripts\bili-comments.exe' --db 'X:\project\data\comments.db' batch run 'X:\project\targets.csv' *>> 'X:\project\logs\batch.log'; exit $LASTEXITCODE"
起始于: X:\project
```

提前创建日志目录。若任务因关机或进程终止而中断，使用 `batch status` 找到批次编号，再执行 `batch resume <ID>`；底层评论抓取会继续使用已保存检查点。

默认数据库是 `data/comments.db`。首次打开 v0.1 数据库时会执行事务化迁移；建议仍先保留数据库备份。

## 数据与恢复语义

- 评论主记录按 `(bvid, rpid)` 幂等更新；每次抓取另存观测记录。
- 一次运行中未出现的旧评论不会被自动标记为删除。
- 一级评论和楼中楼分别保存分页检查点。
- 未完成的热门游标超过 6 小时后不再续用，而是重新抓取并依靠主键去重。
- 热门排序动态变化，不能保证历史意义上的绝对完整性。

## 范围与合规

- 只使用匿名页面内部接口，不保存账号、Cookie 或访问令牌。
- 只保存作者 ID、昵称和等级等最小公开字段，不保存头像、性别、签名、VIP 或粉丝牌。
- 遇到登录要求、验证码或风控会停止，不尝试绕过。
- 不包含 OAuth、代理池、高并发、弹幕下载、字幕正文、用户画像或 Web UI。
- 不包含常驻调度器；定时执行由 Windows 任务计划或 cron 负责。
- 页面内部接口不是稳定性受保证的正式开放 API，未来变化可能需要调整 B站适配器。
- 请遵守平台规则和适用法律；数据库及导出数据不应提交到公开仓库。

## 测试

```powershell
pip install -e ".[test]"
python -m pytest
python -m compileall -q src tests
```

默认测试不访问 B站。完整 Parquet 测试使用 `pip install -e ".[test,parquet]"`。GitHub Actions 在 Python 3.12 上执行两组检查。
