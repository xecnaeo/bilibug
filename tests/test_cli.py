import json

from bili_comments.cli import main
from bili_comments.database import Database
from bili_comments.models import Video


def test_inspect_outputs_local_status(tmp_path, capsys) -> None:
    path = tmp_path / "db.sqlite"
    with Database(path) as database:
        database.upsert_video(
            Video(1, "BV1xx411c7mD", "标题", "2", "作者"), "BV1xx411c7mD"
        )
    assert main(["--db", str(path), "inspect", "BV1xx411c7mD"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["video"]["title"] == "标题"
    assert payload["schema_version"] == 3
