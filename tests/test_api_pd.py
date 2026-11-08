"""pixeldrain /api/pd 端点测试（2026-11-04）。

覆盖纪律要求的两块：
- 鉴权矩阵：token 空 = 全放行；token 配置后缺失/错值一律 401；
- 端点语义：/api/pd/status 与 /jobs 透传 pd_dl 结果；/api/pd/start
  缺 link 400、正常路径打到 pd_dl.api_start；/api/pd/clean 缺 link 400。

daemon 交互全部 mock 在 pd_dl.api_* 层（绝不真发请求）。跑法：
    .venv/bin/python -m unittest tests.test_api_pd -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_api_pd_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config as _cfg  # noqa: E402

_cfg.API_TOKEN = ""
_cfg.API_ENABLED = False

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from tg_userbot import api_server  # noqa: E402
from tg_userbot import config  # noqa: E402
from tg_userbot import pd_dl  # noqa: E402


def make_client() -> TestClient:
    return TestClient(TestServer(api_server.make_app()))


def patch_token(value):
    return mock.patch.object(config, "API_TOKEN", value)


LINK = "https://pixeldrain.com/l/AbCd1234"


class AuthMatrixTest(unittest.IsolatedAsyncioTestCase):
    """pd 端点与全站共用 _auth_middleware——抽样验证语义一致。"""

    async def test_open_when_token_empty(self):
        with patch_token(""), mock.patch.object(
                pd_dl, "api_jobs", return_value={"ok": True, "jobs": {}}):
            client = make_client()
            await client.start_server()
            try:
                resp = await client.get("/api/pd/jobs")
                self.assertEqual(resp.status, 200)
            finally:
                await client.close()

    async def test_missing_token_rejected(self):
        with patch_token("sekret"):
            client = make_client()
            await client.start_server()
            try:
                resp = await client.get("/api/pd/jobs")
                self.assertEqual(resp.status, 401)
            finally:
                await client.close()

    async def test_bearer_accepted(self):
        with patch_token("sekret"), mock.patch.object(
                pd_dl, "api_jobs", return_value={"ok": True, "jobs": {}}):
            client = make_client()
            await client.start_server()
            try:
                resp = await client.get(
                    "/api/pd/jobs", headers={"Authorization": "Bearer sekret"})
                self.assertEqual(resp.status, 200)
            finally:
                await client.close()


class PdEndpointsTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_passthrough(self):
        with mock.patch.object(pd_dl, "api_status",
                               return_value={"ok": True, "ok_files": 3}) as m:
            client = make_client()
            await client.start_server()
            try:
                resp = await client.get("/api/pd/status?tag=pd-abc")
                data = await resp.json()
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertEqual(data["ok_files"], 3)
        m.assert_called_once_with("pd-abc")

    async def test_start_requires_link(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.post("/api/pd/start", json={})
            self.assertEqual(resp.status, 400)
            data = await resp.json()
            self.assertIn("link", data["error"])
        finally:
            await client.close()

    async def test_start_delegates(self):
        with mock.patch.object(
                pd_dl, "api_start",
                return_value={"ok": True, "status": "started",
                              "tag": "pd-abc"}) as m:
            client = make_client()
            await client.start_server()
            try:
                resp = await client.post("/api/pd/start",
                                         json={"link": LINK})
                data = await resp.json()
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertEqual(data["tag"], "pd-abc")
        m.assert_called_once_with(LINK, None)

    async def test_start_delegates_with_dest(self):
        with mock.patch.object(pd_dl, "api_start",
                               return_value={"ok": True}) as m:
            client = make_client()
            await client.start_server()
            try:
                resp = await client.post("/api/pd/start",
                                         json={"link": LINK,
                                               "dest": "/tmp/x"})
                self.assertEqual(resp.status, 200)
            finally:
                await client.close()
        m.assert_called_once_with(LINK, "/tmp/x")

    async def test_clean_requires_link(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.post("/api/pd/clean", json={})
            self.assertEqual(resp.status, 400)
        finally:
            await client.close()

    async def test_clean_delegates(self):
        with mock.patch.object(pd_dl, "api_clean",
                               return_value={"ok": True}) as m:
            client = make_client()
            await client.start_server()
            try:
                resp = await client.post("/api/pd/clean", json={"link": LINK})
                self.assertEqual(resp.status, 200)
            finally:
                await client.close()
        m.assert_called_once_with(LINK)

    async def test_jobs_delegates(self):
        with mock.patch.object(pd_dl, "api_jobs",
                               return_value={"ok": True, "jobs": {}}) as m:
            client = make_client()
            await client.start_server()
            try:
                resp = await client.get("/api/pd/jobs")
                self.assertEqual(resp.status, 200)
            finally:
                await client.close()
        m.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
