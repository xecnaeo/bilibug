# B站视频与评论采集框架

匿名、低频地采集 B站公开视频元数据、互动指标和评论，保存到 SQLite，并导出为 CSV 或 JSONL。

当前版本：`0.2.0`。完整设计和限制见 [项目报告](PROJECT_REPORT.md)。

## 功能

- BV 号或视频 URL 输入，支持顺序处理多个目标；
- 热门或时间顺序的一级评论；
- 可选抓取完整楼中楼；
- 视频基础信息、分P和互动指标快照；
- 评论当前记录与每次抓取观测历史；
- SQLite 分页检查点和中断续抓；
- v0.1 数据库自动迁移到 v2；
- 评论、视频指标和评论指标的 CSV/JSONL 导出；
- 本地数据状态检查；
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
- 页面内部接口不是稳定性受保证的正式开放 API，未来变化可能需要调整 B站适配器。
- 请遵守平台规则和适用法律；数据库及导出数据不应提交到公开仓库。

## 测试

```powershell
pip install -e ".[test]"
python -m pytest
python -m compileall -q src tests
```

默认测试不访问 B站。GitHub Actions 在 Python 3.12 上执行相同检查。
