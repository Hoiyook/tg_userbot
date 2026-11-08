"""pd_dl（pixeldrain daemon 桥接）单元测试。

daemon 交互全部 mock（_daemon_api / urllib），绝不真发请求。覆盖：
- is_pd_command 命令识别（/pd /pdst /pdjobs /pdclean，含带参与误匹配）；
- command_reply：start 成功 / already_running / daemon 不可达提示 /
  /pdst 单任务与全部 / /pdclean / 未知子命令 / 裸 /pd 列任务；
- tag_of：/l/ 与 /file/ 与非法输入；
- api_* 薄封装：透传与 OSError 收口成 ok=False。
跑法：
    .venv/bin/python -m unittest tests.test_pd_dl -v
"""
import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tg_userbot import pd_dl


class IsPdCommandTest(unittest.TestCase):
    def test_matches(self):
        for c in ('/pd', '/pd https://pixeldrain.com/l/abc',
                  '/pdst', '/pdst pd-abc', '/pdjobs', '/pdclean https://x'):
            self.assertTrue(pd_dl.is_pd_command(c), c)

    def test_rejects_lookalikes_and_others(self):
        for c in ('/pdxxx', '/pdst2 x', '/pdz', '/stats', '', None):
            self.assertFalse(pd_dl.is_pd_command(c), repr(c))


class TagOfTest(unittest.TestCase):
    def test_list_and_file(self):
        self.assertEqual(pd_dl.tag_of('https://pixeldrain.com/l/AbCd1234'),
                         'pd-AbCd1234')
        self.assertEqual(pd_dl.tag_of('https://pixeldrain.com/l/abc?download'),
                         'pd-abc')
        self.assertEqual(pd_dl.tag_of('https://pixeldrain.com/file/XyZ12'),
                         'pd-f-XyZ12')

    def test_garbage(self):
        self.assertEqual(pd_dl.tag_of('https://example.com/x'), '')


class CommandReplyTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_success(self):
        with mock.patch.object(pd_dl, '_daemon_api',
                               return_value={'status': 'started',
                                             'tag': 'pd-abc', 'pid': 1,
                                             'dest': '/tmp/x'}):
            out = await pd_dl.command_reply('/pd https://pixeldrain.com/l/abc')
        self.assertIn('已开始下载', out)
        self.assertIn('pd-abc', out)

    async def test_start_with_dest_passes_dest(self):
        with mock.patch.object(pd_dl, '_daemon_api',
                               return_value={'status': 'started',
                                             'tag': 'pd-abc'}) as m:
            await pd_dl.command_reply('/pd https://pixeldrain.com/l/abc /tmp/dest')
        body = m.call_args[0][2]
        self.assertEqual(body['dest'], '/tmp/dest')

    async def test_start_already_running(self):
        with mock.patch.object(pd_dl, '_daemon_api',
                               return_value={'status': 'already_running',
                                             'tag': 'pd-abc', 'dest': '/d'}):
            out = await pd_dl.command_reply('/pd https://pixeldrain.com/l/abc')
        self.assertIn('已在下载中', out)

    async def test_bare_pd_lists_jobs(self):
        with mock.patch.object(pd_dl, '_daemon_api',
                               return_value={'jobs': {'pd-abc': {
                                   'alive': True, 'dest': '/d',
                                   'started': 'x'}}}):
            out = await pd_dl.command_reply('/pd')
        self.assertIn('pixeldrain 任务', out)
        self.assertIn('pd-abc', out)

    async def test_daemon_down_hint(self):
        with mock.patch.object(pd_dl, '_daemon_api',
                               side_effect=OSError('refused')):
            out = await pd_dl.command_reply('/pd https://pixeldrain.com/l/abc')
        self.assertIn('pd_daemon 不可达', out)

    async def test_pdst_single_tag(self):
        with mock.patch.object(pd_dl, '_daemon_api',
                               return_value={'alive': False, 'dest': '/d',
                                             'ok_files': 3,
                                             'log_tail': 'line1\nline2\nline3\nline4'}):
            out = await pd_dl.command_reply('/pdst pd-abc')
        self.assertIn('pd-abc', out)
        self.assertIn('已结束', out)
        self.assertIn('3 个', out)

    async def test_pdclean(self):
        with mock.patch.object(pd_dl, '_daemon_api',
                               return_value={'ok': True, 'cleaned': 'pd-abc'}):
            out = await pd_dl.command_reply(
                '/pdclean https://pixeldrain.com/l/abc')
        self.assertIn('已清理', out)

    async def test_unknown_subcommand(self):
        with mock.patch.object(pd_dl, '_daemon_api',
                               return_value={}):
            out = await pd_dl.command_reply('/pd_nope x')
        self.assertIn('未知子命令', out)


class ApiWrappersTest(unittest.TestCase):
    def test_api_start_passthrough(self):
        with mock.patch.object(pd_dl, '_daemon_api',
                               return_value={'status': 'started'}):
            d = pd_dl.api_start('https://pixeldrain.com/l/abc')
        self.assertTrue(d['ok'])
        self.assertEqual(d['status'], 'started')

    def test_api_status_oserror(self):
        with mock.patch.object(pd_dl, '_daemon_api',
                               side_effect=OSError('down')):
            d = pd_dl.api_status('pd-abc')
        self.assertFalse(d['ok'])
        self.assertIn('pd_daemon 不可达', d['error'])

    def test_api_jobs_and_clean(self):
        with mock.patch.object(pd_dl, '_daemon_api',
                               return_value={'jobs': {}}):
            self.assertTrue(pd_dl.api_jobs()['ok'])
        with mock.patch.object(pd_dl, '_daemon_api',
                               return_value={'ok': True}):
            self.assertTrue(pd_dl.api_clean('https://pixeldrain.com/l/abc')['ok'])


if __name__ == '__main__':
    unittest.main()
