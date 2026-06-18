# B站一级评论采集器

匿名、低频地采集公开 B站视频的一级评论，保存到 SQLite，并导出为 CSV 或 JSONL。

完整的设计、测试、限制与发布说明见 [项目报告](PROJECT_REPORT.md)。

## 安装

需要 Python 3.12 或更高版本：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

## 使用

```powershell
# 抓取一个或多个视频；默认数据库为 data/comments.db
bili-comments crawl BV1xx411c7mD https://www.bilibili.com/video/BV1xx411c7mD

# 指定数据库
bili-comments --db result.db crawl BV1xx411c7mD

# 导出当前保存的评论
bili-comments --db result.db export BV1xx411c7mD --format csv --output comments.csv
bili-comments --db result.db export BV1xx411c7mD --format jsonl --output comments.jsonl
```

抓取中断或失败后，再次执行相同命令会从最近保存的分页游标继续。成功完成后再次抓取会创建新运行，刷新评论正文、点赞数、回复数和热门位置。一次抓取中未再次出现的旧评论不会被判定为已删除。

## 范围与限制

- 只采集评论接口匿名返回的一级评论，不请求楼中楼。
- 使用热门排序；该排序会动态变化，因此不能保证严格的历史完整性。
- 遇到登录要求、验证码或风控会停止，不尝试绕过。
- B站页面内部接口并非稳定的正式开放 API，未来变化可能需要调整 `src/bili_comments/client.py`。
- 请遵守平台规则与适用法律，控制采集频率，并避免传播或滥用个人信息。

## 测试

```powershell
pip install -e ".[test]"
pytest
```

默认测试全部离线运行，不访问 B站。
