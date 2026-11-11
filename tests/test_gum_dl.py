"""gum_dl（Gumroad 桥接）单元测试。全 mock，不真跑下载。"""
import sys, os, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tg_userbot import gum_dl


class IsGumCommandTest(unittest.TestCase):
    async def test_matches(self):
        for c in ('/gum', '/gum https://gumroad.com/d/abc',
                  '/gumst /Volumes/V1/x', '/gumjobs'):
            self.assertTrue(gum_dl.is_gum_command(c), c)

    async def test_rejects(self):
        for c in ('/gumx', '/gumstt', '/stats', '', None, '/pd abc'):
            self.assertFalse(gum_dl.is_gum_command(c), repr(c))


class PidOfTest(unittest.TestCase):
    async def test_extract(self):
        self.assertEqual(gum_dl._pid_of('https://gumroad.com/d/abc123'), 'abc123')
        self.assertEqual(gum_dl._pid_of('https://gumroad.com/d/deadbeef?_gl=1*x'), 'deadbeef')

    async def test_garbage(self):
        self.assertEqual(gum_dl._pid_of('https://example.com/x'), '')


class CommandReplyTest(unittest.IsolatedAsyncioTestCase):
    async def test_bare_gum_usage(self):
        out = await gum_dl.command_reply('/gum')
        self.assertIn('用法', out)

    async def test_bad_link(self):
        out = await gum_dl.command_reply('/gum https://example.com/x')
        self.assertIn('不是 gumroad.com/d', out)

    async def test_start_success(self):
        import tempfile, os as _os
        tmp = tempfile.mkdtemp(prefix='gum_test_')
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        with mock.patch.object(gum_dl, '_run_sh') as mr, \
             mock.patch.object(gum_dl, '_load_reg', return_value={}), \
             mock.patch.object(gum_dl, '_save_reg') as ms:
            out = await gum_dl.command_reply(
                f'/gum https://gumroad.com/d/abc123 {tmp}')
        self.assertIn('已开始', out)
        mr.assert_called_once()
        ms.assert_called_once()
        # marker 已真实创建(隔离在 tmp)
        self.assertTrue(os.path.exists(os.path.join(tmp, '.gum_running')))

    async def test_start_already_running(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix='gum_test_')
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        open(os.path.join(tmp, '.gum_running'), 'w').close()
        with mock.patch.object(gum_dl, '_run_sh') as mr:
            out = await gum_dl.command_reply(
                f'/gum https://gumroad.com/d/abc123 {tmp}')
        self.assertIn('已在下载中', out)
        mr.assert_not_called()

    async def test_gumst_with_dir(self):
        import tempfile
        d = tempfile.mkdtemp()
        open(os.path.join(d, 'a.mp4.ok'), 'w').close()
        out = await gum_dl.command_reply(f'/gumst {d}')
        self.assertIn('已完成: 1', out)


class ApiWrappersTest(unittest.TestCase):
    async def test_api_status(self):
        with mock.patch.object(gum_dl, 'status_text',
                               return_value='📦 ok') as m:
            d = gum_dl.api_status('/tmp/x')
        self.assertTrue(d['ok'])
        m.assert_called_once_with('/tmp/x')

    async def test_api_start_error(self):
        with mock.patch.object(gum_dl, 'command_reply',
                               side_effect=Exception('boom')):
            d = gum_dl.api_start('https://gumroad.com/d/x')
        self.assertFalse(d['ok'])


if __name__ == '__main__':
    unittest.main()
